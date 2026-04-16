/**
 * background.js — Browser Agent Service Worker
 *
 * Three independent subsystems:
 *  1. Shared tab registry + debugger helpers
 *  2. Browser Agent Proxy WebSocket bridge (connects to Go proxy server)
 *  3. Browser Agent — scheduled + on-demand AI task execution
 *
 * Run state design:
 *  - activeRuns: Map<runId, RunState>  kept in memory during + after execution
 *  - Each RunState stores the full event log so chat.html can replay it on open
 *  - Completed runs stay in activeRuns for REPLAY_TTL_MS, then evicted
 *  - Long-term storage goes to chrome.storage.local via scheduler.appendHistory
 */

import { setTabRegistry } from './agent/tools.js'
import { runAgent } from './agent/runner.js'
import {
  restoreAlarms, getTaskIdFromAlarm, getTaskById,
  appendHistory, createRunId,
} from './agent/scheduler.js'

// ─────────────────────────────────────────────────────────────────────────────
// SECTION 1: Shared tab registry + debugger helpers
// ─────────────────────────────────────────────────────────────────────────────

const DEFAULT_PORT      = 12345
const CHECK_INTERVAL_MS = 5 * 1000
const REPLAY_TTL_MS     = 2 * 60 * 1000   // keep completed sessions in memory 2 min (just enough for UI to consume done event)

// Tab idle timeout — read from storage, default 1 minute
let TAB_TIMEOUT_MS = 1 * 60 * 1000

async function loadTabTimeout() {
  const data = await chrome.storage.local.get(['tabTimeoutMinutes'])
  const m = Number(data.tabTimeoutMinutes)
  TAB_TIMEOUT_MS = (Number.isFinite(m) && m > 0 ? m : 1) * 60 * 1000
}

chrome.storage.onChanged.addListener((changes) => {
  if (changes.tabTimeoutMinutes) {
    const m = Number(changes.tabTimeoutMinutes.newValue)
    TAB_TIMEOUT_MS = (Number.isFinite(m) && m > 0 ? m : 1) * 60 * 1000
    console.log(`[TabChecker] Timeout updated to ${TAB_TIMEOUT_MS / 1000}s`)
  }
})

/** @type {Map<number, object>} */
const managedTabs = new Map()

let debuggerListenersInstalled = false
let tabCheckInterval = null

async function attachDebugger(tabId) {
  await chrome.debugger.attach({ tabId }, '1.3')
  await chrome.debugger.sendCommand({ tabId }, 'Page.enable').catch(() => {})
  await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable').catch(() => {})
  await chrome.debugger.sendCommand({ tabId }, 'Network.enable').catch(() => {})

  const info = await chrome.debugger.sendCommand({ tabId }, 'Target.getTargetInfo')
  const targetId = String(info?.targetInfo?.targetId || '').trim()
  if (!targetId) throw new Error(`Tab ${tabId}: Target.getTargetInfo returned no targetId`)

  console.log(`[Debugger] Attached tab ${tabId} targetId=${targetId}`)
  return targetId
}

async function detachDebugger(tabId) {
  try {
    await chrome.debugger.detach({ tabId })
    console.log(`[Debugger] Detached tab ${tabId}`)
  } catch (err) {
    console.warn(`[Debugger] Detach tab ${tabId} failed:`, err.message)
  }
}

async function closeTab(tabId, reason) {
  const tab = managedTabs.get(tabId)
  if (!tab) return

  managedTabs.delete(tabId)
  await detachDebugger(tabId)

  try {
    await chrome.tabs.remove(tabId)
  } catch (err) {
    console.warn(`[Tab] Remove tab ${tabId} failed:`, err.message)
  }

  console.log(`[Tab] Closed tab ${tabId} reason=${reason}`)
  pushRelayEvent(tabId, 'tab.closed', { tabId, reason })
}

// Inject shared registry into agent tools
setTabRegistry(managedTabs, attachDebugger, closeTab)

function startTabChecker() {
  if (tabCheckInterval) return
  tabCheckInterval = setInterval(async () => {
    const now = Date.now()
    for (const [tabId, tab] of managedTabs.entries()) {
      if (now - tab.lastActivity >= TAB_TIMEOUT_MS) {
        console.log(`[TabChecker] Tab ${tabId} idle, closing`)
        await closeTab(tabId, 'timeout')
      }
    }
  }, CHECK_INTERVAL_MS)
}

function installDebuggerListeners() {
  if (debuggerListenersInstalled) return
  debuggerListenersInstalled = true

  chrome.debugger.onEvent.addListener((source, method, params) => {
    const tabId = source.tabId
    if (!tabId || !managedTabs.has(tabId)) return
    const tab = managedTabs.get(tabId)
    if (tab) tab.lastActivity = Date.now()
    pushRelayEvent(tabId, method, params)
  })

  chrome.debugger.onDetach.addListener((source, reason) => {
    const tabId = source.tabId
    if (!tabId || !managedTabs.has(tabId)) return
    const tab = managedTabs.get(tabId)
    if (tab) tab.isAttached = false
    pushRelayEvent(tabId, 'debugger.detached', { tabId, reason })
  })
}

chrome.tabs.onRemoved.addListener((tabId) => {
  if (!managedTabs.has(tabId)) return
  managedTabs.delete(tabId)
  pushRelayEvent(tabId, 'tab.closed', { tabId, reason: 'user' })
})

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  const tab = managedTabs.get(tabId)
  if (!tab) return
  if (changeInfo.url)   tab.url   = changeInfo.url
  if (changeInfo.title) tab.title = changeInfo.title
})

function serializeTab(tab) {
  return {
    tabId: tab.tabId, targetId: tab.targetId, url: tab.url, title: tab.title,
    meta: tab.meta, isAttached: tab.isAttached,
    lastActivity: tab.lastActivity, createdAt: tab.createdAt,
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// SECTION 2: Browser Agent Proxy WebSocket bridge
// ─────────────────────────────────────────────────────────────────────────────

const BADGE = {
  agent:      { text: 'AI', color: '#7c3aed' },
  on:         { text: 'ON', color: '#16a34a' },
  off:        { text: '',   color: '#000000' },
  connecting: { text: '…', color: '#F59E0B' },
  error:      { text: '!',  color: '#B91C1C' },
}

/** @type {WebSocket|null} */
let relayWs = null
let relayConnectPromise = null
let reconnectAttempts = 0
let reconnectTimer = null
let keepAliveInterval = null

function setBadge(kind) {
  const cfg = BADGE[kind] || BADGE.off
  void chrome.action.setBadgeText({ text: cfg.text })
  void chrome.action.setBadgeBackgroundColor({ color: cfg.color })
  void chrome.action.setBadgeTextColor({ color: '#FFFFFF' }).catch(() => {})
}

function setTitle(title) {
  void chrome.action.setTitle({ title })
}

async function getRelayPort() {
  const stored = await chrome.storage.local.get(['relayPort'])
  const n = Number.parseInt(String(stored.relayPort || ''), 10)
  if (!Number.isFinite(n) || n <= 0 || n > 65535) return DEFAULT_PORT
  return n
}

// pushRelayEvent: only sends to Go relay server WebSocket (CDP events)
function pushRelayEvent(tabId, eventMethod, eventParams) {
  if (!relayWs || relayWs.readyState !== WebSocket.OPEN) return
  try {
    relayWs.send(JSON.stringify({
      method: 'event',
      params: { tabId, eventMethod, eventParams, timestamp: Date.now() },
    }))
  } catch {}
}

async function ensureRelayConnection() {
  if (relayWs && relayWs.readyState === WebSocket.OPEN) return
  if (relayConnectPromise) return await relayConnectPromise

  relayConnectPromise = (async () => {
    const port = await getRelayPort()
    const wsUrl = `ws://127.0.0.1:${port}/extension`

    try {
      await fetch(`http://127.0.0.1:${port}/`, { method: 'HEAD', signal: AbortSignal.timeout(2000) })
    } catch {
      throw new Error(`Relay server not reachable at port ${port}`)
    }

    const ws = new WebSocket(wsUrl)
    relayWs = ws

    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('WebSocket connect timeout')), 5000)
      ws.onopen  = () => { clearTimeout(t); resolve() }
      ws.onerror = () => { clearTimeout(t); reject(new Error('WebSocket connect failed')) }
      ws.onclose = (ev) => { clearTimeout(t); reject(new Error(`WebSocket closed (${ev.code})`)) }
    })

    ws.onmessage = (event) => void onRelayMessage(String(event.data || ''))
    ws.onclose = () => onRelayClosed('closed')
    ws.onerror = () => onRelayClosed('error')

    installDebuggerListeners()
    startTabChecker()
    startKeepAlive()
    console.log('[Relay] Connected to relay server')
  })()

  try {
    await relayConnectPromise
    reconnectAttempts = 0
  } finally {
    relayConnectPromise = null
  }
}

function onRelayClosed(reason) {
  console.warn('[Relay] Connection closed:', reason)
  relayWs = null
  stopKeepAlive()
  scheduleReconnect()
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 60000)
  reconnectAttempts++
  reconnectTimer = setTimeout(async () => {
    try {
      await ensureRelayConnection()
      console.log('[Relay] Reconnected to proxy server')
    } catch {
      scheduleReconnect()
    }
  }, delay)
}

function startKeepAlive() {
  if (keepAliveInterval) return
  keepAliveInterval = setInterval(() => {
    chrome.runtime.getPlatformInfo(() => {})
    if (relayWs?.readyState === WebSocket.OPEN) {
      try { relayWs.send(JSON.stringify({ method: 'ping' })) } catch {}
    }
  }, 20000)
}

function stopKeepAlive() {
  if (keepAliveInterval) { clearInterval(keepAliveInterval); keepAliveInterval = null }
}

async function onRelayMessage(text) {
  let msg
  try { msg = JSON.parse(text) } catch { return }

  if (msg?.method === 'ping') {
    try { relayWs.send(JSON.stringify({ method: 'pong' })) } catch {}
    return
  }

  if (typeof msg?.id === 'number' && msg?.method) {
    const { id, method, params } = msg
    try {
      const result = await relayDispatch(method, params || {})
      relayWs.send(JSON.stringify({ id, result }))
    } catch (err) {
      console.error(`[Relay] Error method=${method}:`, err.message)
      relayWs.send(JSON.stringify({ id, error: err.message }))
    }
  }
}

async function relayDispatch(method, params) {
  switch (method) {
    case 'tab.create': {
      const { url = 'about:blank', active = false, meta = {} } = params
      const chromeTab = await chrome.tabs.create({ url, active })
      if (!chromeTab.id) throw new Error('Failed to create tab')
      const tabId = chromeTab.id
      await new Promise(r => setTimeout(r, 500))
      const targetId = await attachDebugger(tabId)
      const tabInfo = {
        tabId, targetId, url, title: chromeTab.title || '', meta,
        isAttached: true, lastActivity: Date.now(), createdAt: Date.now(),
      }
      managedTabs.set(tabId, tabInfo)
      return { tab: serializeTab(tabInfo) }
    }
    case 'tab.list':
      return { tabs: Array.from(managedTabs.values()).map(serializeTab), count: managedTabs.size }
    case 'tab.get': {
      const tab = managedTabs.get(Number(params.tabId))
      if (!tab) throw new Error(`Tab ${params.tabId} not found`)
      return { tab: serializeTab(tab) }
    }
    case 'tab.close': {
      const id = Number(params.tabId)
      if (!managedTabs.has(id)) throw new Error(`Tab ${id} not found`)
      await closeTab(id, 'api')
      return { success: true, tabId: id }
    }
    case 'cdp.send': {
      const { tabId, method, params: cdpParams } = params
      const id = Number(tabId)
      const tab = managedTabs.get(id)
      if (!tab) throw new Error(`Tab ${id} not found`)
      if (!tab.isAttached) { await attachDebugger(id); tab.isAttached = true }
      tab.lastActivity = Date.now()
      const result = await chrome.debugger.sendCommand({ tabId: id }, method, cdpParams || {})
      return { result }
    }
    default:
      throw new Error(`Unknown method: ${method}`)
  }
}

chrome.action.onClicked.addListener(async () => {
  chrome.runtime.openOptionsPage()
})

// ─────────────────────────────────────────────────────────────────────────────
// SECTION 3: Browser Agent — session management + execution
//
// Session shape:
// {
//   sessionId,            // unique ID for this chat session
//   taskId, taskName,
//   status: 'running' | 'done' | 'error' | 'aborted' | 'idle',
//   startedAt, updatedAt,
//   iterations: number,
//   result: string,
//   events: Array<ProgressEvent>,  // full ordered event log (all turns)
//   messages: Array,               // full LLM conversation (all turns, persisted)
// }
//
// A session persists across multiple user turns. When the user sends a new
// message in the chat window, we continue the same session (same messages[])
// rather than creating a new one.
// ─────────────────────────────────────────────────────────────────────────────

/** @type {Map<string, object>} sessionId → Session */
const activeSessions = new Map()

/** @type {Map<string, AbortController>} sessionId → AbortController */
const runningSessions = new Map()

function getSession(sessionId) {
  return activeSessions.get(sessionId) || null
}

function listActiveSessions() {
  return Array.from(activeSessions.values()).map(s => ({
    sessionId:  s.sessionId,
    taskId:     s.taskId,
    taskName:   s.taskName,
    prompt:     s.firstPrompt,
    status:     s.status,
    startedAt:  s.startedAt,
    updatedAt:  s.updatedAt,
    iterations: s.iterations,
    result:     s.result,
  }))
}

function evictOldSessions() {
  const cutoff = Date.now() - REPLAY_TTL_MS
  for (const [sessionId, state] of activeSessions.entries()) {
    if (state.status !== 'running' && (state.updatedAt || 0) < cutoff) {
      activeSessions.delete(sessionId)
    }
  }
}

/**
 * Append a progress event to the session event log and broadcast it.
 */
function broadcastProgress(sessionId, event) {
  const session = activeSessions.get(sessionId)
  if (session) {
    session.events.push({ ...event, timestamp: Date.now() })
    if (event.type === 'done') {
      session.status = 'idle'   // idle = ready for next turn
      session.result = event.result || ''
      session.iterations += (event.iterations || 0)
      session.updatedAt = Date.now()
    } else if (event.type === 'error') {
      session.status = 'error'
      session.result = event.error || ''
      session.updatedAt = Date.now()
    } else if (event.type === 'aborted') {
      session.status = 'aborted'
      session.updatedAt = Date.now()
    } else if (event.type === 'running') {
      session.status = 'running'
    }
  }

  // Best-effort broadcast (chat.html polls anyway)
  chrome.runtime.sendMessage({ type: 'agent_progress', sessionId, ...event }).catch(() => {})
}

/**
 * Run one agent turn inside a session. Fire-and-forget.
 * Uses session.messages as context so multi-turn works.
 */
function runSessionTurn(sessionId, prompt) {
  const session = activeSessions.get(sessionId)
  if (!session) return

  if (runningSessions.has(sessionId)) {
    console.warn(`[Agent] Session ${sessionId} already running`)
    return
  }

  const ctrl = new AbortController()
  runningSessions.set(sessionId, ctrl)
  session.status = 'running'
  session.updatedAt = Date.now()

  setBadge('agent')
  setTitle(`Browser Agent — running "${session.taskName}"`)
  console.log(`[Agent] Session ${sessionId} — new turn: "${prompt.slice(0, 60)}"`)

  ;(async () => {
    try {
      // If messages[] were evicted from memory (after prior turn), reload from storage
      if (session.messages.length === 0 && session.startedAt) {
        const { agentHistory } = await chrome.storage.local.get(['agentHistory'])
        const rec = (agentHistory || []).find(r => (r.id || r.sessionId) === sessionId)
        if (rec?.messages?.length) {
          session.messages = rec.messages
          console.log(`[Agent] Restored ${session.messages.length} messages from storage for session ${sessionId}`)
        }
      }

      const { success, result, messages, iterations } = await runAgent(
        prompt,
        (event) => broadcastProgress(sessionId, event),
        ctrl.signal,
        session.messages,   // pass existing messages for context
      )

      // Update session messages with the full conversation after this turn
      session.messages = messages

      // runner.js already emitted 'done' or 'error' via onProgress.
      // Only emit 'aborted' here if signal was aborted (runner throws, not emits aborted).
      if (ctrl.signal.aborted) {
        broadcastProgress(sessionId, { type: 'aborted' })
      }

      await appendHistory({
        id: sessionId,
        sessionId,
        taskId:    session.taskId,
        taskName:  session.taskName,
        prompt:    session.firstPrompt,
        startedAt: session.startedAt,
        finishedAt: Date.now(),
        success, result, iterations,
        messages: session.messages,
        events: session.events,   // persist UI event log for history replay
      })

      // Free messages[] from memory — history is on disk, UI can replay from there.
      // Keep events[] briefly so the polling UI can still read done/error events.
      session.messages = []
    } catch (err) {
      console.error(`[Agent] Session ${sessionId} error:`, err.message)
      // Only emit error if runner didn't already (runner catches internally and returns, so errors here are unexpected)
      broadcastProgress(sessionId, { type: 'error', error: err.message })
      await appendHistory({
        id: sessionId,
        sessionId,
        taskId:    session.taskId,
        taskName:  session.taskName,
        prompt:    session.firstPrompt,
        startedAt: session.startedAt,
        finishedAt: Date.now(),
        success: false, result: err.message, iterations: 0,
        messages: session.messages,
        events: session.events,
      })
      session.messages = []
    } finally {
      runningSessions.delete(sessionId)
      evictOldSessions()
      setBadge('off')
      setTitle('Browser Agent')
    }
  })()
}

/**
 * Create a new session and start first turn.
 * Returns sessionId.
 */
async function startSession(taskId, prompt, taskName, existingMessages = null) {
  const sessionId = createRunId()

  const session = {
    sessionId,
    taskId,
    taskName:   taskName || prompt.slice(0, 40),
    firstPrompt: prompt,
    status:     'running',
    startedAt:  Date.now(),
    updatedAt:  Date.now(),
    iterations: 0,
    result:     '',
    events:     [],
    messages:   existingMessages || [],  // pre-load from history if resuming
  }
  activeSessions.set(sessionId, session)

  installDebuggerListeners()
  startTabChecker()

  runSessionTurn(sessionId, prompt)
  return sessionId
}

/**
 * Continue an existing session with a new user message.
 */
function continueSession(sessionId, prompt) {
  const session = activeSessions.get(sessionId)
  if (!session) return false
  if (runningSessions.has(sessionId)) return false  // already running

  runSessionTurn(sessionId, prompt)
  return true
}

function abortSession(sessionId) {
  const ctrl = runningSessions.get(sessionId)
  if (ctrl) {
    ctrl.abort()
    runningSessions.delete(sessionId)
    broadcastProgress(sessionId, { type: 'aborted' })
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// SECTION 4: Message router (options page + chat page)
// ─────────────────────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg.type) {

    // Start a new session from a task — returns sessionId
    case 'run_task': {
      ;(async () => {
        try {
          const task = await getTaskById(msg.taskId)
          if (!task) { sendResponse({ ok: false, error: 'Task not found' }); return }

          // If already running, return existing sessionId
          for (const [sid, s] of activeSessions.entries()) {
            if (s.taskId === msg.taskId && s.status === 'running') {
              sendResponse({ ok: true, sessionId: sid }); return
            }
          }

          const sessionId = await startSession(msg.taskId, task.prompt, task.name)
          sendResponse({ ok: true, sessionId })
        } catch (err) {
          sendResponse({ ok: false, error: err.message })
        }
      })()
      return true
    }

    // Start a new session from a bare prompt (Quick Run / chat)
    case 'start_session': {
      ;(async () => {
        try {
          const { prompt, taskName, existingMessages } = msg
          // Create a transient task record so session has a taskId
          const taskId = createRunId()
          const sessionId = await startSession(taskId, prompt, taskName || prompt.slice(0, 40), existingMessages || null)
          sendResponse({ ok: true, sessionId })
        } catch (err) {
          sendResponse({ ok: false, error: err.message })
        }
      })()
      return true
    }

    // Continue an existing session with a new user message
    case 'continue_session': {
      const { sessionId, prompt } = msg
      const session = activeSessions.get(sessionId)
      if (!session) {
        sendResponse({ ok: false, error: 'Session not found' })
        return false
      }
      if (runningSessions.has(sessionId)) {
        sendResponse({ ok: false, error: 'Session already running' })
        return false
      }
      runSessionTurn(sessionId, prompt)
      sendResponse({ ok: true })
      return false
    }

    // Abort a running session
    case 'abort_session': {
      abortSession(msg.sessionId)
      sendResponse({ ok: true })
      return false
    }

    // Legacy abort_task support (options page)
    case 'abort_task': {
      // Find session by taskId
      for (const [sid, s] of activeSessions.entries()) {
        if (s.taskId === msg.taskId) abortSession(sid)
      }
      sendResponse({ ok: true })
      return false
    }

    // Get full session state (chat page polling)
    case 'get_session': {
      const session = getSession(msg.sessionId)
      if (!session) {
        sendResponse({ ok: false, error: 'Session not found in memory' })
      } else {
        sendResponse({ ok: true, session })
      }
      return false
    }

    // Legacy get_run_state — map to session
    case 'get_run_state': {
      const session = getSession(msg.runId)
      if (!session) {
        sendResponse({ ok: false, error: 'Session not found in memory' })
      } else {
        sendResponse({ ok: true, state: { ...session, runId: session.sessionId } })
      }
      return false
    }

    // List all sessions (running + recently completed)
    case 'list_runs': {
      sendResponse({ ok: true, runs: listActiveSessions().map(s => ({ ...s, runId: s.sessionId })) })
      return false
    }

    // Connect to proxy relay server (from options page)
    case 'relay_connect': {
      ;(async () => {
        try {
          await ensureRelayConnection()
          sendResponse({ ok: true, connected: true })
        } catch (err) {
          sendResponse({ ok: false, error: err.message })
        }
      })()
      return true
    }

    // Disconnect from proxy relay server (from options page)
    case 'relay_disconnect': {
      if (relayWs) {
        relayWs.close()
        stopKeepAlive()
      }
      sendResponse({ ok: true, connected: false })
      return false
    }

    // Get current relay connection status (from options page)
    case 'relay_status': {
      sendResponse({ ok: true, connected: relayWs?.readyState === WebSocket.OPEN })
      return false
    }

    default:
      return false
  }
})


// ─────────────────────────────────────────────────────────────────────────────
// SECTION 5: Alarms + startup
// ─────────────────────────────────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener(async (alarm) => {
  const taskId = getTaskIdFromAlarm(alarm)
  if (!taskId) return
  console.log(`[Scheduler] Alarm fired: ${alarm.name}`)
  const task = await getTaskById(taskId)
  if (!task) return
  await startSession(taskId, task.prompt, task.name)
})

chrome.runtime.onInstalled.addListener(() => {
  void chrome.runtime.openOptionsPage()
})

;(async () => {
  console.log('[Init] Browser Agent starting')
  await loadTabTimeout()
  await restoreAlarms()

  try {
    await ensureRelayConnection()
    console.log('[Init] Relay connected')
  } catch (err) {
    console.warn('[Init] Relay not available (agent works without relay):', err.message)
  }
  setBadge('off')
  setTitle('Browser Agent')
})()
