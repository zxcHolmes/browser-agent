/** Shared browser primitives used by both the built-in Agent and the Go relay.
 * No user-opened tabs are discovered or adopted here.
 */
let registry, attach
const queues = new Map()

export function configureBrowserActions(tabs, attachDebugger) {
  registry = tabs
  attach = attachDebugger
}

export const ACTION_NAMES = ['snapshot', 'click', 'fill', 'insert_text', 'press', 'select_option',
  'set_checked', 'wait_for', 'upload_files', 'set_tab_policy', 'list_frames', 'handle_dialog']

// Serializes mutations on one tab, while different tabs remain independent.
export async function withManagedTab(tabId, fn, signal) {
  const id = Number(tabId)
  const tab = registry?.get(id)
  if (!tab) throw new Error(`Tab ${id} not found or not managed`)
  tab.pendingOperations = (tab.pendingOperations || 0) + 1
  const previous = queues.get(id) || Promise.resolve()
  let release
  const next = new Promise(r => { release = r })
  queues.set(id, next)
  try {
    await previous
    signal?.throwIfAborted()
    if (registry.get(id) !== tab) throw new Error(`Tab ${id} closed`)
    if (!tab.isAttached) {
      tab.targetId = await attach(id)
      tab.isAttached = true
    }
    const send = async (method, params = {}, sessionId = '') => {
      signal?.throwIfAborted()
      if (tab.dialog && method!=='Page.handleJavaScriptDialog') throw new Error(`JAVASCRIPT_DIALOG_OPEN: ${JSON.stringify(tab.dialog)}; use handle_dialog`)
      tab.lastActivity = Date.now()
      // The caller asked to leave this page; a "leave site?" prompt is accepted (background.js).
      if (method==='Page.navigate' && !sessionId) tab.leavingAt = Date.now()
      let poll
      const command=chrome.debugger.sendCommand(
        { tabId: id, ...(sessionId ? { sessionId } : {}) }, method, params)
      let value
      try {
        value=await Promise.race([command,new Promise((_resolve,reject)=>{
          poll=setInterval(()=>{
            if(tab.dialog)reject(new Error(`JAVASCRIPT_DIALOG_OPEN: ${JSON.stringify(tab.dialog)}; use handle_dialog. The triggering action may already have executed.`))
          },50)
        })])
      } finally {clearInterval(poll)}
      signal?.throwIfAborted()
      tab.lastActivity = Date.now()
      return value || {}
    }
    return await fn(tab, send)
  } finally {
    tab.pendingOperations--
    tab.lastActivity = Date.now()
    release()
    if (queues.get(id) === next) queues.delete(id)
  }
}

// Executed in the selected frame. Only primitive arguments cross the CDP boundary.
function pageOperation(op, args) {
  const key = '__browserAgentElements_v1'
  let store = globalThis[key]
  if (!store || store.document !== document) {
    store = globalThis[key] = { document, refs: new Map(), ids: new WeakMap(), seq: 0,
      generation: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}` }
  }
  let root = document
  if (args.frame_selector) {
    const frame = document.querySelector(args.frame_selector)
    if (!frame) throw new Error('FRAME_NOT_FOUND')
    root = frame.contentDocument
    if (!root) throw new Error('CROSS_ORIGIN_FRAME: use list_frames and context_id/session_id')
  }
  const doc = root.ownerDocument || root
  const win = doc.defaultView
  const all = []
  function collect(r) {
    for (const e of r.querySelectorAll('*')) {
      all.push(e)
      if (e.shadowRoot) collect(e.shadowRoot)
    }
  }
  collect(root)
  const text = value => String(value || '').replace(/\s+/g, ' ').trim()
  const role = e => e.getAttribute('role') || ({ BUTTON: 'button', A: 'link',
    TEXTAREA: 'textbox', SELECT: 'combobox', INPUT: ({checkbox:'checkbox',radio:'radio',
      button:'button',submit:'button',file:'file'}[e.type] || 'textbox') }[e.tagName]) || (e.isContentEditable ? 'textbox' : '')
  const name = e => text(e.getAttribute('aria-label') ||
    (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => doc.getElementById(id)?.textContent || '').join(' ') ||
    [...(e.labels || [])].map(l => l.textContent).join(' ') ||
    e.getAttribute('alt') || e.getAttribute('title') || e.getAttribute('placeholder') || e.innerText ||
    (['submit','button'].includes(e.type) ? e.value : ''))
  const visible = e => {
    const r = e.getBoundingClientRect(), s = win.getComputedStyle(e)
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.visibility !== 'collapse'
  }
  const enabled = e => !e.matches(':disabled') && e.getAttribute('aria-disabled') !== 'true' && !e.closest('[inert]')
  const value = e => e.type === 'password' ? '[redacted]' :
    (e.isContentEditable ? e.innerText : e.value ?? null)
  const ref = e => {
    let id = store.ids.get(e)
    if (!id) { id = `${store.generation}:${++store.seq}`; store.ids.set(e, id); store.refs.set(id, e) }
    return id
  }
  function find(optional = false) {
    const target = typeof args.target === 'string' ? {selector: args.target} : args.target
    if (!target || !['selector','ref','role','name','text'].some(k => target[k])) throw new Error('TARGET_REQUIRED')
    let matches
    if (target.ref) {
      const e = store.refs.get(target.ref)
      if (!e?.isConnected) throw new Error('STALE_ELEMENT: take another snapshot')
      matches = [e]
    } else {
      matches = all.filter(e => {
        if (target.selector && !e.matches(target.selector)) return false
        if (target.role && role(e) !== target.role) return false
        const compare = (actual, expected) => target.exact === false ? actual.includes(expected) : actual === expected
        if (target.name !== undefined && !compare(name(e), text(target.name))) return false
        if (target.text !== undefined && !compare(text(e.innerText), text(target.text))) return false
        return true
      })
    }
    if (!args.include_hidden) matches = matches.filter(visible)
    if (matches.length > 1) throw new Error(`AMBIGUOUS_TARGET: ${matches.length} matches; use a ref or a narrower selector`)
    if (!matches.length) { if (optional) return null; throw new Error('ELEMENT_NOT_FOUND') }
    return matches[0]
  }
  if (op === 'snapshot') {
    const elements = all.filter(e => role(e) || e.matches('input,[contenteditable],iframe'))
      .filter(e => visible(e) || e.type === 'file').slice(0, Math.min(args.limit || 200, 500))
      .map(e => ({ref:ref(e),tag:e.tagName.toLowerCase(),role:role(e),name:name(e).slice(0,250),
        type:e.type || undefined,value:value(e)?.slice?.(0,1000) ?? value(e),
        visible:visible(e),enabled:enabled(e),checked:e.checked,
        accept:e.type === 'file' ? e.accept : undefined,multiple:e.type === 'file' ? e.multiple : undefined}))
    // Prune references to removed nodes during long-lived SPA workflows.
    for (const [id,e] of store.refs) if (!e.isConnected) store.refs.delete(id)
    return {url:win.location.href,title:doc.title,text:doc.body?.innerText.slice(0,12000),elements}
  }
  if (op === 'condition') {
    const state = args.state || 'visible'
    if (state === 'url') return {matched:win.location.href.includes(args.value),url:win.location.href}
    if (state === 'text' && !args.target) return {matched:!!doc.body?.innerText.includes(args.value)}
    const e = find(true)
    if (state === 'absent') return {matched:!e}
    if (state === 'hidden') return {matched:!e || !visible(e)}
    if (!e) return {matched:false}
    return {matched: state === 'attached' ? true : state === 'visible' ? visible(e) :
      state === 'enabled' ? visible(e) && enabled(e) : state === 'text' ? text(e.innerText).includes(args.value) :
      state === 'value' ? value(e) === args.value : false,
      value:value(e),text:text(e.innerText).slice(0,1000)}
  }
  const e = find()
  if (op === 'files') return {files:[...(e.files || [])].map(f=>({name:f.name,size:f.size,type:f.type,lastModified:f.lastModified}))}
  if (op === 'value') return {value:value(e)}
  if (op === 'file_node') {
    if (e.tagName !== 'INPUT' || e.type !== 'file') throw new Error('NOT_FILE_INPUT')
    if (args.count > 1 && !e.multiple) throw new Error('INPUT_DOES_NOT_ALLOW_MULTIPLE_FILES')
    return e
  }
  if (!visible(e)) throw new Error('ELEMENT_NOT_VISIBLE')
  if (!enabled(e)) throw new Error('ELEMENT_DISABLED')
  e.scrollIntoView({block:'center',inline:'center',behavior:'instant'})
  if (op === 'select') {
    if (e.tagName !== 'SELECT') throw new Error('NOT_SELECT_ELEMENT')
    const requested = Array.isArray(args.values) ? args.values : [args.value]
    if (requested.some(v => typeof v !== 'string')) throw new Error('OPTION_VALUES_REQUIRED')
    if (requested.length > 1 && !e.multiple) throw new Error('SELECT_DOES_NOT_ALLOW_MULTIPLE')
    const options = [...e.options]
    for (const v of requested) if (!options.some(o=>o.value===v && !o.disabled && !o.parentElement.disabled)) throw new Error(`OPTION_NOT_AVAILABLE: ${v}`)
    for (const o of options) o.selected = requested.includes(o.value)
    e.dispatchEvent(new win.Event('input',{bubbles:true}))
    e.dispatchEvent(new win.Event('change',{bubbles:true}))
    return {values:[...e.selectedOptions].map(o=>o.value)}
  }
  if (op === 'prepare_fill' || op === 'prepare_insert') {
    if ((!e.isContentEditable && !['INPUT','TEXTAREA'].includes(e.tagName)) ||
      ['file','checkbox','radio','button','submit','range','color','date','time','datetime-local'].includes(e.type)) throw new Error('NOT_TEXT_EDITABLE')
    if (e.readOnly) throw new Error('ELEMENT_READONLY')
    e.focus()
    if (op === 'prepare_insert') {
      // The caret of a rich editor is moved by a trusted keystroke afterwards.
      if (!e.isContentEditable) { try { e.setSelectionRange(e.value.length,e.value.length) } catch { throw new Error('INPUT_TYPE_REQUIRES_SPECIALIZED_FILL') } }
      return {ref:ref(e),rich:e.isContentEditable,slate:e.matches('[data-slate-editor="true"]'),before:value(e)}
    }
    if (e.isContentEditable) {
      const range = doc.createRange();range.selectNodeContents(e)
      const selection = win.getSelection();selection.removeAllRanges();selection.addRange(range)
    } else { try { e.select() } catch { throw new Error('INPUT_TYPE_REQUIRES_SPECIALIZED_FILL') } }
    return {ref:ref(e),rich:e.isContentEditable,slate:e.matches('[data-slate-editor="true"]')}
  }
  if (op === 'focus') { e.focus();return {ref:ref(e)} }
  if (op === 'prepare_click') {
    const r = e.getBoundingClientRect()
    const x = Math.max(0,Math.min(win.innerWidth,r.right)) / 2 + Math.max(0,r.left) / 2
    const y = Math.max(0,Math.min(win.innerHeight,r.bottom)) / 2 + Math.max(0,r.top) / 2
    const hit = e.getRootNode().elementFromPoint(x,y)
    if (hit && hit !== e && !e.contains(hit)) throw new Error('ELEMENT_OBSCURED')
    if (args.checked !== undefined) {
      if (!['checkbox','radio'].includes(e.type)) throw new Error('NOT_CHECKABLE_INPUT')
      if (e.type === 'radio' && !args.checked) throw new Error('CANNOT_UNCHECK_RADIO')
    }
    // Coordinates for a same-origin embedded frame must include its viewport offset.
    let offsetX=0, offsetY=0, current=win
    while (current !== window) {
      const f=current.frameElement, b=f.getBoundingClientRect()
      offsetX+=b.left+f.clientLeft;offsetY+=b.top+f.clientTop;current=current.parent
    }
    return {x:x+offsetX,y:y+offsetY,checked:e.checked,ref:ref(e)}
  }
  throw new Error(`Unknown page operation: ${op}`)
}

function boundedSeconds(value, fallback = 30) {
  const n = value === undefined ? fallback : Number(value)
  if (!Number.isFinite(n) || n <= 0 || n > 300) throw new Error('timeout_seconds must be between 0 and 300')
  return n
}

const delay = (ms, signal) => new Promise((resolve,reject) => {
  signal?.throwIfAborted()
  const done=()=>{signal?.removeEventListener('abort',abort);resolve()}
  const timer=setTimeout(done,ms)
  const abort=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(signal.reason || new Error('Aborted'))}
  signal?.addEventListener('abort',abort,{once:true})
})

export async function browserAction(action, args = {}, signal) {
  if (!ACTION_NAMES.includes(action)) throw new Error(`Unknown browser action: ${action}`)
  // Dialog dismissal must bypass the tab queue: its triggering click/eval may
  // still be blocked waiting for the dialog to close.
  if (action==='handle_dialog') {
    const tab=registry?.get(Number(args.tab_id))
    if (!tab) throw new Error(`Tab ${args.tab_id} not found or not managed`)
    if (typeof args.accept!=='boolean') throw new Error('accept must be boolean')
    signal?.throwIfAborted()
    await chrome.debugger.sendCommand({tabId:tab.tabId,...(args.session_id?{sessionId:args.session_id}:{})},
      'Page.handleJavaScriptDialog',{accept:args.accept,promptText:args.prompt_text || ''})
    tab.dialog=null;tab.lastActivity=Date.now()
    return {handled:true}
  }
  return withManagedTab(args.tab_id, async (tab, send) => {
    const sessionId = args.session_id || ''
    const evaluate = async (op, extra = {}, byValue = true) => {
      const result = await send('Runtime.evaluate', {
        expression:`(${pageOperation.toString()})(${JSON.stringify(op)},${JSON.stringify({...args,...extra})})`,
        returnByValue:byValue, awaitPromise:true,
        timeout:boundedSeconds(args.timeout_seconds)*1000,
        ...(args.context_id !== undefined ? {contextId:args.context_id} : {}),
      },sessionId)
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
      return byValue ? result.result?.value : result.result
    }
    if (action === 'set_tab_policy') {
      if (args.auto_close !== undefined) {
        if (typeof args.auto_close !== 'boolean') throw new Error('auto_close must be boolean')
        tab.autoClose = args.auto_close
      }
      if (args.idle_timeout_minutes !== undefined) {
        const minutes = Number(args.idle_timeout_minutes)
        if (!Number.isFinite(minutes) || minutes < 1 || minutes > 1440) throw new Error('idle_timeout_minutes must be 1..1440')
        tab.idleTimeoutMs=minutes*60000
      }
      return {tabId:tab.tabId,autoClose:tab.autoClose!==false,idleTimeoutMinutes:tab.idleTimeoutMs ? tab.idleTimeoutMs/60000 : null}
    }
    if (action === 'list_frames') return {frameTree:await send('Page.getFrameTree',{},sessionId),
      contexts:[...(tab.contexts?.values() || [])],sessions:[...(tab.childSessions?.values() || [])]}
    if (action==='snapshot' && tab.dialog) return {dialog:tab.dialog,elements:[],note:'Handle the open dialog before reading the page.'}
    if (action === 'snapshot') return evaluate('snapshot')
    if (action === 'wait_for') {
      const allowed=['attached','visible','hidden','absent','enabled','text','value','url']
      if (args.state && !allowed.includes(args.state)) throw new Error('Unsupported wait state')
      if (['text','value','url'].includes(args.state) && typeof args.value !== 'string') throw new Error('Wait value must be a string')
      const deadline=Date.now()+boundedSeconds(args.timeout_seconds)*1000
      let observation, lastError
      do {
        try {
          observation=await evaluate('condition',{include_hidden:true})
          lastError=null
          if (observation?.matched) return {matched:true,observation}
        } catch (e) {
          if (!/Execution context was destroyed|Cannot find context|Inspected target navigated/.test(e.message)) throw e
          lastError=e.message
        }
        await delay(200,signal)
      } while (Date.now()<deadline)
      throw new Error(`WAIT_TIMEOUT: ${JSON.stringify({state:args.state || 'visible',observation,lastError})}`)
    }
    if (action === 'upload_files') {
      if (!Array.isArray(args.files) || !args.files.length || args.files.some(f=>typeof f!=='string' || !/^(\/|[A-Za-z]:[\\/])/.test(f))) throw new Error('files must contain absolute paths on the Chrome machine')
      const target=await evaluate('file_node',{include_hidden:true,count:args.files.length},false)
      if (!target?.objectId) throw new Error('File input could not be resolved')
      let result
      try {
        // Upload widgets often remove/replace the input, or reset its value, in
        // their change handler. Record the selection when the event fires — from a
        // window capture listener, which runs before any page handler — and
        // inspect the original retained node rather than resolving it again.
        await send('Runtime.callFunctionOn',{objectId:target.objectId,functionDeclaration:`function(){
          const input=this, win=this.ownerDocument.defaultView
          input.__browserAgentSelection=null
          const record=e=>{ if (e.target!==input || input.__browserAgentSelection) return
            input.__browserAgentSelection=Array.from(input.files || [], f=>({name:f.name,size:f.size,type:f.type,lastModified:f.lastModified})) }
          for (const type of ['input','change']) win.addEventListener(type,record,{capture:true})
          input.__browserAgentForget=()=>{ for (const type of ['input','change']) win.removeEventListener(type,record,{capture:true}) }
        }`},sessionId)
        await send('DOM.setFileInputFiles',{objectId:target.objectId,files:args.files},sessionId)
        const selected=await send('Runtime.callFunctionOn',{objectId:target.objectId,
          functionDeclaration:`function(){
            const now=Array.from(this.files || [], f=>({name:f.name,size:f.size,type:f.type,lastModified:f.lastModified}))
            const atEvent=this.__browserAgentSelection
            try { this.__browserAgentForget?.() } finally { delete this.__browserAgentForget; delete this.__browserAgentSelection }
            return {files: now.length ? now : (atEvent || []), reset_by_page: !now.length && !!atEvent?.length}
          }`,
          returnByValue:true},sessionId)
        if (selected.exceptionDetails) throw new Error('FILE_SELECTION_UNVERIFIED')
        result=selected.result?.value
        if (!Array.isArray(result?.files)) throw new Error('FILE_SELECTION_UNVERIFIED')
      } finally { await send('Runtime.releaseObject',{objectId:target.objectId},sessionId).catch(()=>{}) }
      if (result.files.length !== args.files.length) throw new Error('FILE_SELECTION_MISMATCH')
      return {stage:'selected',...result,note:'Wait for the website upload and processing state before closing this tab.'}
    }
    if (action === 'select_option') return evaluate('select')
    if (action === 'press') {
      if (args.target) await evaluate('focus')
      const keys={Enter:[13,'Enter'],Tab:[9,'Tab'],Escape:[27,'Escape'],Backspace:[8,'Backspace'],
        Delete:[46,'Delete'],ArrowDown:[40,'ArrowDown'],ArrowUp:[38,'ArrowUp'],ArrowLeft:[37,'ArrowLeft'],ArrowRight:[39,'ArrowRight'],
        Home:[36,'Home'],End:[35,'End'],Space:[32,'Space']}
      const parts=String(args.key || '').split('+'), key=parts.pop()
      let modifiers=0
      for (const part of parts) {
        const flag={Alt:1,Control:2,Ctrl:2,Meta:4,Shift:8}[part]
        if (!flag) throw new Error(`Unsupported modifier: ${part}`)
        modifiers|=flag
      }
      const info=keys[key] || (/^[a-zA-Z0-9]$/.test(key) ? [key.toUpperCase().charCodeAt(0),`Key${key.toUpperCase()}`] : null)
      if (!info) throw new Error('Unsupported key; use fill for text')
      const params={key:key==='Space'?' ':key,code:info[1],windowsVirtualKeyCode:info[0],modifiers}
      await send('Input.dispatchKeyEvent',{...params,type:'keyDown',...(!modifiers&&key==='Enter'?{text:'\r'}:{})},sessionId)
      await send('Input.dispatchKeyEvent',{...params,type:'keyUp'},sessionId)
      return {pressed:args.key}
    }
    // Slate serializes browser-created paragraphs correctly only when line breaks
    // arrive as trusted Enter events; inserting a literal LF can display correctly
    // but save a different caption. Other editors retain their existing behavior.
    const insertEditorText=async(text, slate)=>{
      if (!slate) { await send('Input.insertText',{text},sessionId); return }
      const lines=text.replace(/\r\n/g,'\n').split('\n')
      for (let i=0;i<lines.length;i++) {
        if (i) {
          await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r'},sessionId)
          await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13},sessionId)
        }
        if (lines[i]) await send('Input.insertText',{text:lines[i]},sessionId)
      }
    }
    if (action === 'insert_text') {
      if (typeof args.value !== 'string' || !args.value) throw new Error('value must be a non-empty string')
      const prepared=await evaluate('prepare_insert')
      // Slate inserts U+200B caret placeholders, including at the end of non-empty lines.
      // Scope this to Slate; ordinary inputs must preserve intentional zero-width text.
      const normalize=s=>(prepared.slate ? String(s).replace(/\u200b/g,'') : String(s)).replace(/\r\n/g,'\n').replace(/\n+$/,'')
      if (prepared.rich) {
        // Editor frameworks follow trusted caret movement, not a DOM range.
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'End',code:'End',windowsVirtualKeyCode:35,commands:['moveToEndOfDocument']},sessionId)
        await send('Input.dispatchKeyEvent',{type:'keyUp',key:'End',code:'End',windowsVirtualKeyCode:35},sessionId)
      }
      await insertEditorText(args.value,prepared.slate)
      const result=await evaluate('value',{target:{ref:prepared.ref}})
      if (result.value !== '[redacted]' && !normalize(result.value).endsWith(normalize(args.value)))
        throw new Error(`INSERT_VERIFICATION_FAILED: ${JSON.stringify({value:String(result.value).slice(-200)})}`)
      return {inserted:true,...result}
    }
    if (action === 'fill') {
      if (typeof args.value !== 'string') throw new Error('value must be a string')
      const prepared=await evaluate('prepare_fill')
      // Rich editors (YouTube's description box) render multi-line text with a
      // trailing newline of their own; it is not part of the value.
      // Slate inserts U+200B caret placeholders, including at the end of non-empty lines.
      // Scope this to Slate; ordinary inputs must preserve intentional zero-width text.
      const normalize=s=>(prepared.slate ? String(s).replace(/\u200b/g,'') : String(s)).replace(/\r\n/g,'\n').replace(/\n+$/,'')
      const backspace=async()=>{
        await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Backspace',code:'Backspace',windowsVirtualKeyCode:8},sessionId)
        await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Backspace',code:'Backspace',windowsVirtualKeyCode:8},sessionId)
      }
      await backspace()
      if (prepared.rich) {
        // Editor frameworks (Draft.js, ProseMirror, Lexical…) keep their own
        // selection model and ignore a DOM range; a trusted select-all keystroke
        // is handled by the editor itself. Verify it is empty before typing.
        for (let attempt=0; attempt<3; attempt++) {
          if (!normalize((await evaluate('value',{target:{ref:prepared.ref}})).value)) break
          const modifiers=/Mac/i.test(navigator.userAgentData?.platform || navigator.platform || '') ? 4 : 2
          await send('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers,commands:['selectAll']},sessionId)
          await send('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers},sessionId)
          await backspace()
        }
        const left=(await evaluate('value',{target:{ref:prepared.ref}})).value
        if (normalize(left)) throw new Error(`FILL_CLEAR_FAILED: ${JSON.stringify({value:String(left).slice(0,200)})}`)
      }
      if (args.value) await insertEditorText(args.value,prepared.slate)
      const result=await evaluate('value',{target:{ref:prepared.ref}})
      if (result.value !== '[redacted]' && normalize(result.value)!==normalize(args.value)) throw new Error(`FILL_VERIFICATION_FAILED: ${JSON.stringify(result)}`)
      return {filled:true,...result}
    }
    const prepared=await evaluate('prepare_click')
    if (action === 'set_checked' && typeof args.checked !== 'boolean') throw new Error('checked must be boolean')
    if (action !== 'set_checked' || prepared.checked!==args.checked) {
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:prepared.x,y:prepared.y,button:'none'},sessionId)
      for (const type of ['mousePressed','mouseReleased']) await send('Input.dispatchMouseEvent',{
        type,x:prepared.x,y:prepared.y,button:'left',buttons:type==='mousePressed'?1:0,clickCount:1},sessionId)
    }
    if (action === 'set_checked') {
      const result=await evaluate('prepare_click',{target:{ref:prepared.ref}})
      if (result.checked!==args.checked) throw new Error('CHECK_VERIFICATION_FAILED')
      return {checked:result.checked}
    }
    return {clicked:true,ref:prepared.ref}
  },signal)
}

const target = {description:'CSS selector string, or {ref} from snapshot, or {role,name,exact}. Matches must be unique.',
  anyOf:[{type:'string'},{type:'object',properties:{selector:{type:'string'},ref:{type:'string'},role:{type:'string'},name:{type:'string'},text:{type:'string'},exact:{type:'boolean'}}}]}
const common={tab_id:{type:'integer'},target,session_id:{type:'string',description:'Child CDP session from list_frames'},
  context_id:{type:'integer',description:'Execution context from list_frames'},frame_selector:{type:'string',description:'Same-origin iframe CSS selector'}}
const definitions={
  snapshot:['Observe page text and interactive elements, including open Shadow DOM. Returns refs; refresh snapshot after navigation.',[],{limit:{type:'integer'}}],
  click:['Click a unique visible enabled unobscured element through browser mouse input. Verify the resulting page state.',['target'],{}],
  fill:['Replace input, textarea, or contenteditable text through browser input events, then verify the value.',['target','value'],{value:{type:'string'}}],
  insert_text:['Append text at the end of an input, textarea or contenteditable through browser input events (no clearing), then verify it.',['target','value'],{value:{type:'string'}}],
  press:['Send a key such as Enter, Tab, Escape, Control+a, Meta+a. Optional target receives focus.',['key'],{key:{type:'string'}}],
  select_option:['Select native dropdown option values and emit input/change events.',['target','values'],{values:{type:'array',items:{type:'string'}}}],
  set_checked:['Set a native checkbox/radio checked state and verify it.',['target','checked'],{checked:{type:'boolean'}}],
  wait_for:['Wait for a condition; timeout is an error, not success. Use text/value to wait for upload/processing completion.',[],{state:{type:'string',enum:['attached','visible','hidden','absent','enabled','text','value','url']},value:{type:'string'},timeout_seconds:{type:'number'}}],
  upload_files:['Choose local files through a hidden or visible input[type=file]. Paths must exist on the Chrome machine. Returns selected, NOT upload complete. Keep tab open and wait for site completion.',['target','files'],{files:{type:'array',items:{type:'string'}}}],
  set_tab_policy:['Keep a managed tab open during upload, processing or manual intervention. Does not affect user-opened tabs.',[],{auto_close:{type:'boolean'},idle_timeout_minutes:{type:'number'}}],
  list_frames:['List frame tree, execution contexts and child sessions for iframe operations.',[],{}],
  handle_dialog:['Accept or dismiss an open JavaScript alert/confirm/prompt.',['accept'],{accept:{type:'boolean'},prompt_text:{type:'string'}}],
}
export const BROWSER_TOOL_SCHEMAS=Object.entries(definitions).map(([name,[description,required,properties]])=>({
  type:'function',function:{name,description,parameters:{type:'object',properties:{...common,...properties},required:['tab_id',...required]}}
}))
export const BROWSER_TOOL_FUNCTIONS=Object.fromEntries(ACTION_NAMES.map(name=>[name,(args,signal)=>browserAction(name,args,signal)]))
