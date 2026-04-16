/**
 * agent/tools.js
 * Browser-native tool implementations + schemas for Browser Agent.
 * All tools run directly in the Chrome Extension background service worker —
 * no Browser Agent Proxy required.
 */

// ─── Tab registry (shared with background.js via import) ───────────────────────
// managedTabs is injected by the background service worker via setTabRegistry()

/** @type {Map<number, object>} */
let _managedTabs = null
/** @type {Function} */
let _attachDebugger = null
/** @type {Function} */
let _closeTab = null

export function setTabRegistry(managedTabs, attachDebugger, closeTab) {
  _managedTabs = managedTabs
  _attachDebugger = attachDebugger
  _closeTab = closeTab
}

// ─── Tool implementations ──────────────────────────────────────────────────────

export async function toolCreateTab({ url = 'about:blank', active = false, meta = {} } = {}) {
  const chromeTab = await chrome.tabs.create({ url, active: !!active })
  if (!chromeTab.id) throw new Error('Failed to create tab: no tabId returned')

  const tabId = chromeTab.id
  await new Promise(r => setTimeout(r, 500))

  const targetId = await _attachDebugger(tabId)

  const tabInfo = {
    tabId,
    targetId,
    url,
    title: chromeTab.title || '',
    meta: meta || {},
    isAttached: true,
    lastActivity: Date.now(),
    createdAt: Date.now(),
  }
  _managedTabs.set(tabId, tabInfo)

  console.log(`[AgentTools] Created tab ${tabId} url=${url}`)
  return { tabId, url, title: tabInfo.title, meta: tabInfo.meta }
}

export async function toolListTabs() {
  return Array.from(_managedTabs.values()).map(t => ({
    tabId: t.tabId, url: t.url, title: t.title, meta: t.meta,
    isAttached: t.isAttached, lastActivity: t.lastActivity,
  }))
}

export async function toolGetTab({ tab_id } = {}) {
  const id = Number(tab_id)
  const tab = _managedTabs.get(id)
  if (!tab) throw new Error(`Tab ${id} not found`)
  return { tabId: tab.tabId, url: tab.url, title: tab.title, meta: tab.meta, isAttached: tab.isAttached }
}

export async function toolCloseTab({ tab_id } = {}) {
  const id = Number(tab_id)
  if (!_managedTabs.has(id)) throw new Error(`Tab ${id} not found`)
  await _closeTab(id, 'agent')
  return { success: true, tabId: id }
}

export async function toolNavigate({ tab_id, url, wait_for_load = true, timeout_seconds = 30 } = {}) {
  const id = Number(tab_id)
  const tab = _managedTabs.get(id)
  if (!tab) throw new Error(`Tab ${id} not found`)

  if (!tab.isAttached) {
    await _attachDebugger(id)
    tab.isAttached = true
  }

  tab.lastActivity = Date.now()

  await chrome.debugger.sendCommand({ tabId: id }, 'Page.navigate', { url })

  if (wait_for_load) {
    await waitForPageLoad(id, timeout_seconds * 1000)
  }

  // Update tab info
  const info = await chrome.tabs.get(id).catch(() => null)
  if (info) { tab.url = info.url || url; tab.title = info.title || '' }
  tab.lastActivity = Date.now()

  return { tabId: id, url: tab.url, title: tab.title }
}

async function waitForPageLoad(tabId, timeoutMs) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const check = async () => {
      if (Date.now() > deadline) { resolve(); return }
      try {
        const result = await chrome.debugger.sendCommand(
          { tabId }, 'Runtime.evaluate',
          { expression: 'document.readyState', returnByValue: true }
        )
        if (result?.result?.value === 'complete') { resolve(); return }
      } catch {}
      setTimeout(check, 300)
    }
    setTimeout(check, 300)
  })
}

export async function toolEval({ tab_id, expression, await_promise = false, timeout_seconds = 30 } = {}) {
  const id = Number(tab_id)
  const tab = _managedTabs.get(id)
  if (!tab) throw new Error(`Tab ${id} not found`)

  if (!tab.isAttached) {
    await _attachDebugger(id)
    tab.isAttached = true
  }

  tab.lastActivity = Date.now()

  const result = await chrome.debugger.sendCommand({ tabId: id }, 'Runtime.evaluate', {
    expression,
    awaitPromise: !!await_promise,
    returnByValue: true,
    timeout: timeout_seconds * 1000,
  })

  if (result?.exceptionDetails) {
    throw new Error(`JS Error: ${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`)
  }

  return result?.result?.value ?? null
}

export async function toolScreenshot({ tab_id, format = 'png', quality = 80 } = {}) {
  const id = Number(tab_id)
  const tab = _managedTabs.get(id)
  if (!tab) throw new Error(`Tab ${id} not found`)

  if (!tab.isAttached) {
    await _attachDebugger(id)
    tab.isAttached = true
  }

  tab.lastActivity = Date.now()

  const params = { format: format === 'jpeg' ? 'jpeg' : 'png' }
  if (format === 'jpeg') params.quality = quality

  const result = await chrome.debugger.sendCommand({ tabId: id }, 'Page.captureScreenshot', params)
  const dataUrl = `data:image/${params.format};base64,${result.data}`

  return { dataUrl, format: params.format, tabId: id }
}

export async function toolCdp({ tab_id, method, params = {}, timeout = 30 } = {}) {
  const id = Number(tab_id)
  const tab = _managedTabs.get(id)
  if (!tab) throw new Error(`Tab ${id} not found`)

  if (!tab.isAttached) {
    await _attachDebugger(id)
    tab.isAttached = true
  }

  tab.lastActivity = Date.now()

  const result = await chrome.debugger.sendCommand({ tabId: id }, method, params || {})
  return result || {}
}

export async function toolSleep({ seconds = 1 } = {}) {
  const ms = Math.max(0, Math.min(seconds, 300)) * 1000
  await new Promise(r => setTimeout(r, ms))
  return { slept_seconds: ms / 1000 }
}

/**
 * Arbitrary HTTP fetch tool — runs in the extension background,
 * so it is NOT subject to CORS restrictions.
 */
export async function toolFetch({ url, method = 'GET', headers = {}, body = null, timeout_seconds = 30 } = {}) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout_seconds * 1000)

  try {
    const opts = {
      method: method.toUpperCase(),
      headers: headers || {},
      signal: ctrl.signal,
    }
    if (body !== null && body !== undefined) {
      opts.body = typeof body === 'string' ? body : JSON.stringify(body)
    }

    const res = await fetch(url, opts)
    const contentType = res.headers.get('content-type') || ''
    let responseBody
    if (contentType.includes('application/json')) {
      responseBody = await res.json()
    } else {
      responseBody = await res.text()
    }

    return {
      status: res.status,
      statusText: res.statusText,
      headers: Object.fromEntries(res.headers.entries()),
      body: responseBody,
    }
  } finally {
    clearTimeout(timer)
  }
}

// ─── Tool registry ─────────────────────────────────────────────────────────────

export const TOOL_FUNCTIONS = {
  create_tab:  toolCreateTab,
  list_tabs:   toolListTabs,
  get_tab:     toolGetTab,
  close_tab:   toolCloseTab,
  navigate:    toolNavigate,
  eval:        toolEval,
  screenshot:  toolScreenshot,
  cdp:         toolCdp,
  fetch:       toolFetch,
  sleep:       toolSleep,
}

export const TOOL_SCHEMAS = [
  {
    type: 'function',
    function: {
      name: 'create_tab',
      description: 'Create a new Chrome tab (background, not active). Returns tabId.',
      parameters: {
        type: 'object',
        properties: {
          url:    { type: 'string',  description: 'URL to open (default: about:blank)' },
          active: { type: 'boolean', description: 'Bring tab to foreground (default: false — prefer background)' },
          meta:   { type: 'object',  description: 'Optional metadata' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'list_tabs',
      description: 'List all agent-managed Chrome tabs.',
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_tab',
      description: 'Get info about a specific tab.',
      parameters: {
        type: 'object',
        required: ['tab_id'],
        properties: { tab_id: { type: 'integer', description: 'Tab ID' } },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'close_tab',
      description: 'Close a managed Chrome tab.',
      parameters: {
        type: 'object',
        required: ['tab_id'],
        properties: { tab_id: { type: 'integer', description: 'Tab ID to close' } },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'navigate',
      description: 'Navigate a tab to a URL and wait for page load.',
      parameters: {
        type: 'object',
        required: ['tab_id', 'url'],
        properties: {
          tab_id:          { type: 'integer', description: 'Tab ID' },
          url:             { type: 'string',  description: 'URL to navigate to' },
          wait_for_load:   { type: 'boolean', description: 'Wait for document.readyState=complete (default: true)' },
          timeout_seconds: { type: 'integer', description: 'Timeout in seconds (default: 30)' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'eval',
      description: 'Execute JavaScript in a Chrome tab and return the result. Use to read DOM, click elements, fill forms.',
      parameters: {
        type: 'object',
        required: ['tab_id', 'expression'],
        properties: {
          tab_id:          { type: 'integer', description: 'Tab ID' },
          expression:      { type: 'string',  description: 'JavaScript expression to evaluate' },
          await_promise:   { type: 'boolean', description: 'Await returned Promise (default: false)' },
          timeout_seconds: { type: 'integer', description: 'Timeout in seconds (default: 30)' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'screenshot',
      description: 'Capture a screenshot of a tab. Returns a dataUrl (base64). Use this to visually inspect the page.',
      parameters: {
        type: 'object',
        required: ['tab_id'],
        properties: {
          tab_id:  { type: 'integer', description: 'Tab ID' },
          format:  { type: 'string',  description: 'Image format: png or jpeg (default: png)' },
          quality: { type: 'integer', description: 'JPEG quality 1-100 (default: 80)' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'cdp',
      description: 'Send any Chrome DevTools Protocol command. Use for advanced operations like Input.dispatchKeyEvent, Network.getCookies, etc.',
      parameters: {
        type: 'object',
        required: ['tab_id', 'method'],
        properties: {
          tab_id:  { type: 'integer', description: 'Tab ID' },
          method:  { type: 'string',  description: 'CDP method name, e.g. Input.insertText' },
          params:  { type: 'object',  description: 'CDP method parameters' },
          timeout: { type: 'integer', description: 'Timeout in seconds (default: 30)' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'fetch',
      description: 'Make an arbitrary HTTP request (no CORS restrictions since it runs in the extension background). Use to send data to external APIs, webhooks, etc.',
      parameters: {
        type: 'object',
        required: ['url'],
        properties: {
          url:             { type: 'string', description: 'Full URL to fetch' },
          method:          { type: 'string', description: 'HTTP method: GET, POST, PUT, DELETE, PATCH (default: GET)' },
          headers:         { type: 'object', description: 'HTTP headers as key-value pairs' },
          body:            { description: 'Request body (string or object, will be JSON-stringified if object)' },
          timeout_seconds: { type: 'integer', description: 'Timeout in seconds (default: 30)' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'sleep',
      description: 'Wait for a specified number of seconds before continuing. Use to wait for animations, delayed content, or rate-limit compliance.',
      parameters: {
        type: 'object',
        properties: {
          seconds: { type: 'number', description: 'Number of seconds to wait (max 300, default: 1)' },
        },
      },
    },
  },
]

export const SYSTEM_PROMPT = `You are a Browser Agent — an AI assistant that automates Chrome directly from within the browser extension. You operate in the background; tabs you open should NOT be activated (active: false) unless explicitly required. Always close tabs when you're done.

## CRITICAL: Language Rule
You MUST reply in the exact same language the user wrote in. This is non-negotiable.
- User writes in Chinese → you reply ENTIRELY in Chinese
- User writes in English → you reply in English
- User writes in Japanese → you reply in Japanese
- Never switch languages mid-conversation unless the user does so first

Available tools:
- create_tab: Open a new Chrome tab (use active:false to keep it in the background)
- list_tabs / get_tab / close_tab: Manage tabs
- navigate: Go to a URL and wait for page load
- eval: Run JavaScript in the page (read DOM, click, fill forms, extract data)
- screenshot: Capture the current page as a base64 image — the image will be injected into the conversation so you can visually inspect it
- cdp: Send any raw Chrome DevTools Protocol command (e.g. Input.insertText to type)
- fetch: Make any HTTP request without CORS restrictions (POST to webhooks, APIs, etc.)
- sleep: Wait N seconds (useful for waiting for animations or rate limits)

## Standard operating procedure

1. Always create_tab first (active: false), then navigate.
2. Before interacting with elements, enumerate actual DOM — never guess selectors:
   - Buttons: \`Array.from(document.querySelectorAll('button')).map(b=>({text:b.innerText.trim(),disabled:b.disabled}))\`
   - Inputs: \`Array.from(document.querySelectorAll('input,textarea')).map(e=>({tag:e.tagName,name:e.name,placeholder:e.placeholder,id:e.id}))\`
3. For React/SPA inputs, use cdp Input.insertText (not innerHTML):
   - First focus the element with eval, then cdp Input.insertText.
4. After every navigation, verify with eval('window.location.href').
5. After finishing, close all tabs you opened.
6. If a task requires sending data externally, use the fetch tool.

## Stuck detection — MANDATORY

- If you do the same action 2 times with the same result, STOP and diagnose:
  1. Take a screenshot to visually inspect the page.
  2. Enumerate all interactive elements.
  3. Check window.location.href.
- Never repeat a failing action more than 2 times without diagnosing.`
