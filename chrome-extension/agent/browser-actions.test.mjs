import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { browserAction, configureBrowserActions, withManagedTab } from './browser-actions.js'
import { setTabRegistry } from './tools.js'
import { runAgent } from './runner.js'

function setup(sendCommand = async () => ({}), config = {}) {
  const tabs=new Map([[1,{tabId:1,isAttached:true,lastActivity:Date.now()}],
    [2,{tabId:2,isAttached:true,lastActivity:Date.now()}]])
  globalThis.chrome={debugger:{sendCommand},storage:{local:{get:async()=>({
    llmProvider:'openai',llmBaseUrl:'http://fixture.invalid/v1',llmModel:'fixture',maxToolCalls:50,...config,
  })}}}
  configureBrowserActions(tabs,async()=> 'target')
  setTabRegistry(tabs,async()=> 'target',async()=>{})
  return tabs
}

test('same-tab operations serialize, different tabs can proceed, and leases release after errors',async()=>{
  const tabs=setup(),events=[]
  let unblock,started
  const ready=new Promise(r=>{started=r})
  const gate=new Promise(r=>{unblock=r})
  const first=withManagedTab(1,async()=>{events.push('first');started();await gate;throw new Error('fixture failure')})
  const rejected=assert.rejects(first,/fixture failure/)
  await ready
  const second=withManagedTab(1,async()=>{events.push('second')})
  await withManagedTab(2,async()=>{events.push('other tab')})
  assert.deepEqual(events,['first','other tab'])
  assert.equal(tabs.get(1).pendingOperations,2)
  unblock();await rejected;await second
  assert.deepEqual(events,['first','other tab','second'])
  assert.equal(tabs.get(1).pendingOperations,0)
  await assert.rejects(browserAction('snapshot',{tab_id:999}),/not managed/)
})

test('wait timeout is a failure and releases its tab lease',async()=>{
  const tabs=setup(async()=>({result:{value:{matched:false}}}))
  await assert.rejects(browserAction('wait_for',{tab_id:1,target:'#missing',timeout_seconds:.01}),/WAIT_TIMEOUT/)
  assert.equal(tabs.get(1).pendingOperations,0)
})

test('cancelling a wait releases the queue without leaking a lease',async()=>{
  const tabs=setup(async()=>({result:{value:{matched:false}}}))
  const ctrl=new AbortController()
  const running=browserAction('wait_for',{tab_id:1,target:'#missing'},ctrl.signal)
  const stopped=assert.rejects(running)
  ctrl.abort();await stopped
  assert.equal(tabs.get(1).pendingOperations,0)
})

test('dialog handling bypasses a blocked action queue',async()=>{
  const calls=[],tabs=setup(async(_target,method)=>{calls.push(method);return {}})
  let unblock,entered
  const ready=new Promise(r=>{entered=r}),gate=new Promise(r=>{unblock=r})
  const blocked=withManagedTab(1,async()=>{entered();await gate})
  await ready
  tabs.get(1).dialog={type:'confirm',message:'fixture'}
  assert.deepEqual(await browserAction('handle_dialog',{tab_id:1,accept:false}),{handled:true})
  assert.deepEqual(calls,['Page.handleJavaScriptDialog'])
  assert.equal(tabs.get(1).dialog,null)
  unblock();await blocked
})

test('screenshot images follow all tool results in a batch',async()=>{
  setup(async(_target,method)=>method==='Page.captureScreenshot'?{data:'aGVsbG8='}:{})
  const originalFetch=globalThis.fetch
  let calls=0
  globalThis.fetch=async(_url,request)=>{
    const body=JSON.parse(request.body)
    if(calls++===0) return Response.json({choices:[{message:{role:'assistant',content:null,tool_calls:[
      {id:'screenshot',type:'function',function:{name:'screenshot',arguments:'{"tab_id":1}'}},
      {id:'list',type:'function',function:{name:'list_tabs',arguments:'{}'}},
    ]}}]})
    assert.deepEqual(body.messages.map(m=>m.role),['system','user','assistant','tool','tool','user'])
    return Response.json({choices:[{message:{role:'assistant',content:'done'}}]})
  }
  try {const result=await runAgent('fixture');assert.equal(result.success,true);assert.equal(calls,2)}
  finally {globalThis.fetch=originalFetch}
})

test('tool limit completes the protocol batch but does not execute remaining actions',async()=>{
  setup(async()=>({}),{maxToolCalls:1})
  const originalFetch=globalThis.fetch
  globalThis.fetch=async()=>Response.json({choices:[{message:{role:'assistant',content:null,tool_calls:[
    {id:'first',type:'function',function:{name:'list_tabs',arguments:'{}'}},
    {id:'second',type:'function',function:{name:'list_tabs',arguments:'{}'}},
  ]}}]})
  try {
    const result=await runAgent('fixture')
    assert.equal(result.paused,true);assert.equal(result.success,false)
    const results=result.messages.filter(m=>m.role==='tool')
    assert.equal(results.length,2)
    assert.match(results[1].content,/NOT_EXECUTED/)
  } finally {globalThis.fetch=originalFetch}
})

test('LLM request receives cancellation',async()=>{
  setup()
  const originalFetch=globalThis.fetch,ctrl=new AbortController()
  let entered
  const ready=new Promise(r=>{entered=r})
  globalThis.fetch=async(_url,{signal})=>new Promise((_resolve,reject)=>{
    signal.addEventListener('abort',()=>reject(new Error('cancelled fixture')),{once:true});entered()
  })
  try {
    const running=runAgent('fixture',null,ctrl.signal)
    await ready;ctrl.abort()
    const result=await running
    assert.equal(result.success,false);assert.match(result.result,/cancelled/)
  } finally {globalThis.fetch=originalFetch}
})

test('upload verifies retained input even when change handler removes it',async()=>{
  const calls=[]
  setup(async(_target,method,params)=>{
    calls.push(method)
    if(method==='Runtime.evaluate')return {result:{objectId:'retained-input'}}
    if(method==='Runtime.callFunctionOn'){
      assert.equal(params.objectId,'retained-input')
      return {result:{value:{files:[{name:'video.mp4',size:123}]}}}
    }
    return {}
  })
  const result=await browserAction('upload_files',{tab_id:1,target:'#removed-after-change',files:['/tmp/video.mp4']})
  assert.equal(result.stage,'selected')
  assert.deepEqual(calls,['Runtime.evaluate','Runtime.callFunctionOn','DOM.setFileInputFiles','Runtime.callFunctionOn','Runtime.releaseObject'])
})

test('upload uses the selection recorded at the change event when the page resets the input',async()=>{
  let verify=0
  setup(async(_target,method)=>{
    if(method==='Runtime.evaluate')return {result:{objectId:'reset-input'}}
    if(method==='Runtime.callFunctionOn'&&++verify===2)
      return {result:{value:{files:[{name:'a.jpg',size:1},{name:'b.jpg',size:2}],reset_by_page:true}}}
    return {}
  })
  const result=await browserAction('upload_files',{tab_id:1,target:'input[type=file]',files:['/tmp/a.jpg','/tmp/b.jpg']})
  assert.equal(result.files.length,2)
  assert.equal(result.reset_by_page,true)
})

test('insert_text moves a rich editor caret to the end, appends, and never clears',async()=>{
  const calls=[]
  let evaluated=0
  setup(async(_target,method,params)=>{
    calls.push(method==='Input.dispatchKeyEvent'?`${params.type}:${(params.commands||[]).join()}`:method)
    if(method==='Runtime.evaluate')return {result:{value:++evaluated===1?{ref:'r1',rich:true,before:'Hi '}:{value:'Hi #tag'}}}
    return {}
  })
  const result=await browserAction('insert_text',{tab_id:1,target:'[contenteditable]',value:'#tag'})
  assert.equal(result.inserted,true)
  assert.deepEqual(calls,['Runtime.evaluate','keyDown:moveToEndOfDocument','keyUp:','Input.insertText','Runtime.evaluate'])
})

test('insert_text reports text that did not land',async()=>{
  let evaluated=0
  setup(async(_target,method)=>method==='Runtime.evaluate'
    ?{result:{value:++evaluated===1?{ref:'r1',rich:false,before:''}:{value:'something else'}}}:{})
  await assert.rejects(browserAction('insert_text',{tab_id:1,target:'textarea',value:'#tag'}),/INSERT_VERIFICATION_FAILED/)
})

test('Slate fill tolerates its empty and end-of-line caret placeholders',async()=>{
  let reads=0
  const typed=[], enters=[]
  setup(async(_target,method,args)=>{
    if(method==='Runtime.evaluate') return {result:{value:++reads===1
      ? {ref:'slate',rich:true,slate:true}
      : {value: reads<=3 ? '\u200b' : '第一行\u200b\n第二行\u200b'}}}
    if(method==='Input.insertText')typed.push(args.text)
    if(method==='Input.dispatchKeyEvent' && args.key==='Enter')enters.push(args.type)
    return {}
  })
  assert.equal((await browserAction('fill',{tab_id:1,target:'[data-slate-editor]',value:'第一行\n第二行'})).filled,true)
  assert.deepEqual(typed,['第一行','第二行'])
  assert.deepEqual(enters,['keyDown','keyUp'])
})

test('Slate append tolerates placeholders but plain fields retain zero-width semantics',async()=>{
  for(const slate of [true,false]) {
    let reads=0
    setup(async(_target,method)=>method==='Runtime.evaluate'?{result:{value:++reads===1
      ? {ref:'text',rich:true,slate,before:'Hello\u200b'} : {value:'Hello\u200b world\u200b'}}}:{})
    const action=browserAction('insert_text',{tab_id:1,target:'[contenteditable]',value:' world'})
    if(slate) assert.equal((await action).inserted,true)
    else await assert.rejects(action,/INSERT_VERIFICATION_FAILED/)
  }
})

test('caret insertion verifies text before the cursor without moving to the end',async()=>{
  let reads=0
  const keys=[]
  setup(async(_target,method,args)=>{
    if(method==='Runtime.evaluate')return {result:{value:++reads===1
      ?{ref:'paragraph',rich:true,before:'before after'}
      :{value:'before inserted after',caretText:'before inserted'}}}
    if(method==='Input.dispatchKeyEvent')keys.push(args.key)
    return {}
  })
  assert.equal((await browserAction('insert_text',{tab_id:1,target:'[contenteditable]',value:'inserted',position:'caret'})).inserted,true)
  assert.deepEqual(keys,[])
})

test('caret insertion rejects missing caret evidence and invalid positions',async()=>{
  let reads=0,insertions=0
  setup(async(_target,method)=>{
    if(method==='Runtime.evaluate')return {result:{value:++reads===1?{ref:'paragraph',rich:true}:{value:'text elsewhere'}}}
    if(method==='Input.insertText')insertions++
    return {}
  })
  await assert.rejects(browserAction('insert_text',{tab_id:1,target:'[contenteditable]',value:'text',position:'elsewhere'}),/position must/)
  assert.equal(insertions,0)
  await assert.rejects(browserAction('insert_text',{tab_id:1,target:'[contenteditable]',value:'text',position:'caret'}),/INSERT_VERIFICATION_FAILED/)
})


test('caret verification preserves password redaction in actual page operations',async()=>{
  const input={tagName:'INPUT',type:'password',value:'fixture-secret',selectionStart:7,selectionEnd:7,
    isContentEditable:false,isConnected:true,readOnly:false,
    getAttribute:()=>null,closest:()=>null,scrollIntoView:()=>{},focus:()=>{},
    getBoundingClientRect:()=>({width:100,height:20}),
    matches:s=>s==='#fixture-password',
    setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end}}
  const document={querySelectorAll:()=>[input],defaultView:{getComputedStyle:()=>({display:'block',visibility:'visible'})}}
  const context=vm.createContext({document})
  setup(async(_target,method,args)=>{
    if(method==='Runtime.evaluate')return {result:{value:vm.runInContext(args.expression,context)}}
    if(method==='Input.insertText'){
      input.value=input.value.slice(0,input.selectionStart)+args.text+input.value.slice(input.selectionEnd)
      input.selectionStart+=args.text.length;input.selectionEnd=input.selectionStart
    }
    return {}
  })
  const result=await browserAction('insert_text',{tab_id:1,target:'#fixture-password',value:'new',position:'caret'})
  assert.equal(result.value,'[redacted]')
  assert.equal(result.caretText,undefined)
  assert.equal(JSON.stringify(result).includes('fixture-secret'),false)
  assert.equal(input.value,'fixturenew-secret')
})
