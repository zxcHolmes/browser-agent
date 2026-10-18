import test from 'node:test'
import assert from 'node:assert/strict'
import {createChildAdopter} from './managed-children.js'

test('late opener metadata adopts only a managed descendant, never an unrelated tab',async()=>{
  const tabs=new Map([[1,{tabId:1,meta:{job:'fixture'},autoClose:false,idleTimeoutMs:3600000}]])
  const attached=[],events=[]
  const adopt=createChildAdopter({tabs,attach:async id=>{attached.push(id);return 'target'},changed:()=>{},event:(tab,parent)=>events.push([tab.tabId,parent])})
  assert.equal(await adopt({id:2}),false)
  assert.equal(await adopt({id:3,openerTabId:999}),false)
  assert.equal(await adopt({id:2,openerTabId:1}),true)
  assert.deepEqual(attached,[2])
  assert.deepEqual(events,[[2,1]])
  assert.equal(tabs.has(3),false)
  assert.equal(tabs.get(2).autoClose,false)
  assert.equal(tabs.get(2).meta.parentTabId,1)
  assert.equal(await adopt({id:2,openerTabId:1}),false)
  assert.deepEqual(attached,[2])
})

test('navigation source maps noopener targets without URL guessing',async()=>{
  const tabs=new Map([[1,{tabId:1,meta:{job:'fixture'},autoClose:false}]])
  const attached=[]
  const adopt=createChildAdopter({tabs,attach:async id=>{attached.push(id);return 'target'},changed:()=>{},event:()=>{}})
  assert.equal(await adopt({id:4,url:'https://example.com/'},1),true)
  assert.equal(tabs.get(4).meta.parentTabId,1)
  assert.equal(await adopt({id:5,url:'https://example.com/'},999),false)
  assert.deepEqual(attached,[4])
  assert.equal(tabs.has(5),false)
})
