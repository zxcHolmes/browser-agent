// Never infer ownership from a URL or the currently active tab. Only Chrome's
// explicit openerTabId or webNavigation sourceTabId establishes ownership.
export function createChildAdopter({tabs,attach,changed,event}) {
  return async function adopt(created, sourceTabId = created?.openerTabId) {
    if (!created?.id || tabs.has(created.id)) return false
    const parent=tabs.get(sourceTabId)
    if (!parent) return false
    const tab={tabId:created.id,url:created.url || 'about:blank',title:created.title || '',
      meta:{...parent.meta,parentTabId:parent.tabId},autoClose:parent.autoClose,
      idleTimeoutMs:parent.idleTimeoutMs,isAttached:false,lastActivity:Date.now(),createdAt:Date.now()}
    tabs.set(tab.tabId,tab)
    changed()
    try {
      tab.targetId=await attach(tab.tabId)
      if(tabs.get(tab.tabId)!==tab)return false
      tab.isAttached=true
      changed()
      event(tab,parent.tabId)
    } catch(e) {console.warn('[Tab] Child attach failed:',e.message)}
    return true
  }
}
