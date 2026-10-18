/**
 * agent/tools.js
 * Browser-native tool implementations + schemas for Browser Agent.
 * All tools run directly in the Chrome Extension background service worker —
 * no Browser Agent Proxy required.
 */

import { configureBrowserActions, BROWSER_TOOL_FUNCTIONS, BROWSER_TOOL_SCHEMAS, withManagedTab } from './browser-actions.js'

// ─── Tab registry (shared with background.js via import) ───────────────────────
// managedTabs is injected by the background service worker via setTabRegistry()

/** @type {Map<number, object>} */
let _managedTabs = null
/** @type {Function} */
let _attachDebugger = null
/** @type {Function} */
let _closeTab = null
let _registryChanged = () => {}

export function setTabRegistry(managedTabs, attachDebugger, closeTab, registryChanged = () => {}) {
  _managedTabs = managedTabs
  _attachDebugger = attachDebugger
  _closeTab = closeTab
  _registryChanged = registryChanged
  configureBrowserActions(managedTabs, attachDebugger)
}

// ─── Tool implementations ──────────────────────────────────────────────────────

export async function toolCreateTab({ url = 'about:blank', active = false, auto_close = true, meta = {} } = {}) {
  const chromeTab = await chrome.tabs.create({ url, active: !!active })
  if (!chromeTab.id) throw new Error('Failed to create tab: no tabId returned')

  const tabId = chromeTab.id
  const tabInfo = {
    tabId, url, title: chromeTab.title || '', meta: meta || {}, autoClose: auto_close,
    isAttached: false, pendingOperations: 1, lastActivity: Date.now(), createdAt: Date.now(),
  }
  _managedTabs.set(tabId, tabInfo)
  try {
    tabInfo.targetId = await _attachDebugger(tabId)
    tabInfo.isAttached = true
    const latest = await chrome.tabs.get(tabId)
    tabInfo.url=latest.url || url;tabInfo.title=latest.title || ''
    return { tabId, url:tabInfo.url, title:tabInfo.title, meta:tabInfo.meta }
  } catch (e) {
    await _closeTab(tabId, 'create_failed')
    throw e
  } finally {
    tabInfo.pendingOperations=0
    tabInfo.lastActivity=Date.now()
    _registryChanged()
  }
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

export async function toolNavigate({ tab_id, url, wait_for_load = true, timeout_seconds = 30 } = {}, signal) {
  return withManagedTab(tab_id, async (tab, send) => {
    const nav = await send('Page.navigate', { url })
    if (nav.errorText) throw new Error(`NAVIGATION_FAILED: ${nav.errorText}`)
    let loaded = false
    if (wait_for_load) {
      const deadline = Date.now() + Math.min(300, Math.max(1, timeout_seconds)) * 1000
      while (Date.now() < deadline) {
        signal?.throwIfAborted()
        try {
          const result = await send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true })
          if (result.result?.value === 'complete') { loaded = true; break }
        } catch (e) {
          if (signal?.aborted) throw e
        }
        await new Promise(r => setTimeout(r, 200))
      }
      if (!loaded) throw new Error('NAVIGATION_TIMEOUT: load did not complete')
    }
    const info = await chrome.tabs.get(Number(tab_id))
    tab.url = info.url || url; tab.title = info.title || ''
    return { tabId: tab.tabId, url: tab.url, title: tab.title, loadEventFired: loaded }
  }, signal)
}

export async function toolEval({ tab_id, expression, await_promise = false, timeout_seconds = 30, session_id = '', context_id } = {}, signal) {
  return withManagedTab(tab_id, async (_tab, send) => {
    const result = await send('Runtime.evaluate', { expression, awaitPromise: !!await_promise,
      returnByValue: true, timeout: Math.min(300, Math.max(1, timeout_seconds)) * 1000,
      ...(context_id !== undefined ? {contextId: context_id} : {}) }, session_id)
    if (result.exceptionDetails) throw new Error(`JS Error: ${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`)
    return result.result?.value ?? null
  }, signal)
}

export async function toolScreenshot({ tab_id, format = 'png', quality = 80 } = {}, signal) {
  return withManagedTab(tab_id, async (_tab, send) => {
    const params = { format: format === 'jpeg' ? 'jpeg' : 'png' }
    if (params.format === 'jpeg') params.quality = Math.min(100, Math.max(1, quality))
    const result = await send('Page.captureScreenshot', params)
    return { dataUrl: `data:image/${params.format};base64,${result.data}`, format: params.format, tabId: Number(tab_id) }
  }, signal)
}

export async function toolCdp({ tab_id, method, params = {}, session_id = '' } = {}, signal) {
  return withManagedTab(tab_id, (_tab, send) => send(method, params || {}, session_id), signal)
}

export async function toolSleep({ seconds = 1 } = {}, signal) {
  const ms = Math.max(0, Math.min(seconds, 300)) * 1000
  await new Promise((resolve,reject)=>{
    signal?.throwIfAborted()
    const timer=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve()},ms)
    const abort=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(new Error('Agent aborted'))}
    signal?.addEventListener('abort',abort,{once:true})
  })
  return { slept_seconds: ms / 1000 }
}

// ─── Skill Memory tools ────────────────────────────────────────────────────────

const KEY_SKILLS = 'agentSkills'

async function _getSkills() {
  const data = await chrome.storage.local.get([KEY_SKILLS])
  return data[KEY_SKILLS] || []
}

/**
 * Save (create or update) an automation skill.
 * Pass `id` to update an existing skill; omit to create a new one.
 */
export async function toolSaveSkill({ id, name, site = '', description } = {}) {
  if (!name)        throw new Error('name is required')
  if (!description) throw new Error('description is required')

  const skills = await _getSkills()
  const now = Date.now()
  const idx = id ? skills.findIndex(s => s.id === id) : -1

  let skill
  if (idx >= 0) {
    skill = { ...skills[idx], name, site: site || skills[idx].site, description, updatedAt: now }
    skills[idx] = skill
  } else {
    skill = {
      id: `skill_${now}_${Math.random().toString(36).slice(2, 8)}`,
      name, site, description, createdAt: now, updatedAt: now,
    }
    skills.push(skill)
  }

  await chrome.storage.local.set({ [KEY_SKILLS]: skills })
  console.log(`[AgentTools] Skill saved: ${skill.id} "${skill.name}"`)
  return { id: skill.id, name: skill.name, site: skill.site, action: idx >= 0 ? 'updated' : 'created' }
}

/** List all skills — returns id + name + site only (no full description). */
export async function toolListSkills() {
  const skills = await _getSkills()
  return skills.map(s => ({ id: s.id, name: s.name, site: s.site || '', updatedAt: s.updatedAt }))
}

/** Get full skill details (including description) by id. */
export async function toolGetSkill({ id } = {}) {
  if (!id) throw new Error('id is required')
  const skills = await _getSkills()
  const skill = skills.find(s => s.id === id)
  if (!skill) throw new Error(`Skill "${id}" not found`)
  return skill
}

/**
 * Arbitrary HTTP fetch tool — runs in the extension background,
 * so it is NOT subject to CORS restrictions.
 */
export async function toolFetch({ url, method = 'GET', headers = {}, body = null, timeout_seconds = 30 } = {}, signal) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout_seconds * 1000)

  try {
    const opts = {
      method: method.toUpperCase(),
      headers: headers || {},
      signal: signal ? AbortSignal.any([signal,ctrl.signal]) : ctrl.signal,
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
  ...BROWSER_TOOL_FUNCTIONS,
  create_tab:   toolCreateTab,
  list_tabs:    toolListTabs,
  get_tab:      toolGetTab,
  close_tab:    toolCloseTab,
  navigate:     toolNavigate,
  eval:         toolEval,
  screenshot:   toolScreenshot,
  cdp:          toolCdp,
  fetch:        toolFetch,
  sleep:        toolSleep,
  save_skill:   toolSaveSkill,
  list_skills:  toolListSkills,
  get_skill:    toolGetSkill,
}

export const TOOL_SCHEMAS = [
  ...BROWSER_TOOL_SCHEMAS,
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
          auto_close: { type: 'boolean', description: 'Close after idle timeout (default true, 30 minutes unless configured); false keeps an uploading tab open.' },
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
      description: 'Send a CDP command supported by chrome.debugger. Some domains/methods are restricted. Prefer the high-level browser tools.',
      parameters: {
        type: 'object',
        required: ['tab_id', 'method'],
        properties: {
          tab_id:  { type: 'integer', description: 'Tab ID' },
          method:  { type: 'string',  description: 'CDP method name, e.g. Input.insertText' },
          params:  { type: 'object',  description: 'CDP method parameters' },
          session_id: { type: 'string', description: 'Optional child session from list_frames' },
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
  {
    type: 'function',
    function: {
      name: 'save_skill',
      description: 'Save an automation skill to persistent memory. Call this after successfully completing a browser automation workflow so the knowledge can be reused in future tasks. Pass the existing skill id to update/fix a skill.',
      parameters: {
        type: 'object',
        required: ['name', 'description'],
        properties: {
          id:          { type: 'string', description: 'Existing skill id to update (omit to create a new skill)' },
          name:        { type: 'string', description: 'Short descriptive name, e.g. "Post a tweet on X" or "Login to GitHub"' },
          site:        { type: 'string', description: 'Domain this skill applies to, e.g. "twitter.com" (optional but recommended)' },
          description: { type: 'string', description: 'Step-by-step natural language description of the automation, including key CSS/data-testid selectors, gotchas, and how to verify success.' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'list_skills',
      description: 'List all saved automation skills (returns id, name, site only — no full description). Call this at the start of a task to find relevant skills, then use get_skill to load the full steps.',
      parameters: {
        type: 'object',
        properties: {},
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_skill',
      description: 'Get the full description of a saved automation skill by its id. Call this after list_skills identifies a relevant skill.',
      parameters: {
        type: 'object',
        required: ['id'],
        properties: {
          id: { type: 'string', description: 'Skill id from list_skills' },
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
- snapshot: Observe actual interactive elements and obtain unique element refs. Prefer this before actions.
- click / fill / insert_text / press / select_option / set_checked: Verified browser interactions (insert_text appends at the end without clearing). Prefer these over ad-hoc eval.
- upload_files: Select files from absolute local paths; returns selected, NOT uploaded. Use wait_for to verify site progress, completion and processing. Never close an uploading/processing tab.
- wait_for: Wait for explicit element, text, value or URL conditions. A timeout is a failure requiring diagnosis.
- set_tab_policy: Set auto_close:false during long uploads or manual intervention, restore it after completion.
- list_frames: Get frame execution contexts and child sessions; pass context_id/session_id to operate inside a frame.
- handle_dialog: Handle an open JavaScript alert, confirm or prompt.
- create_tab: Open a new Chrome tab (use active:false to keep it in the background)
- list_tabs / get_tab / close_tab: Manage tabs
- navigate: Go to a URL and wait for page load
- eval: Run JavaScript in the page (read DOM, click, fill forms, extract data)
- screenshot: Capture the current page as a base64 image — the image will be injected into the conversation so you can visually inspect it
- cdp: Send any raw Chrome DevTools Protocol command (e.g. Input.insertText to type)
- fetch: Make any HTTP request without CORS restrictions (POST to webhooks, APIs, etc.)
- sleep: Wait N seconds (useful for waiting for animations or rate limits)
- list_skills / get_skill / save_skill: Automation skill memory (see below)

## Skill Memory — reuse learned automations

You have a persistent skill memory. Skills are **per-site, per-operation** — one skill = one workflow on one website (e.g. "post a tweet on x.com", "scrape top stories on news.ycombinator.com"). Never bundle multiple sites into one skill.

### BEFORE starting the task:
1. Call \`list_skills\` to see all available skills (returns id + name + site).
2. Identify any skills relevant to the sites you will visit.
3. Call \`get_skill\` for each relevant skill to load its full steps.
4. Follow the skill as a guide — but verify selectors still work; pages change.

### IMMEDIATELY AFTER completing each site's workflow (before moving to the next site):
- Explored a new workflow successfully → \`save_skill\` for that site.
- Used an existing skill and it still worked fine → no action needed.
- Used an existing skill but had to fix/adapt steps → \`save_skill\` with the existing id to overwrite it.
- Pure data read with no UI interaction (e.g. just fetch) → skip.

### Example: task spanning multiple sites
Task: "Read HN, pick a story, post it to X with commentary"
→ At task start: list_skills() — review results, load any relevant skills with get_skill()
→ After scraping HN successfully: save_skill("Scrape top stories from Hacker News", site="news.ycombinator.com", ...)
→ After posting tweet successfully: save_skill("Post a tweet on X", site="x.com", ...)
Result: two independent, reusable skills — not one combined skill.

### What makes a good skill description:
- Numbered step-by-step actions (navigate → find element → interact → verify)
- Stable selectors: prefer data-testid, aria-label, name/id attributes over fragile class names
- Gotchas and workarounds (e.g. "React input requires cdp Input.insertText after focus, not innerHTML")
- How to verify success (e.g. "toast appears with text 'Your post was sent'")

### Example of a well-written skill description:
\`\`\`
Skill name: Post a tweet on X
Site: x.com

Steps:
1. Navigate to https://x.com/home, wait for page load.
2. eval: locate the tweet compose box — document.querySelector('[data-testid="tweetTextarea_0"]')
3. eval: click the compose box to focus it.
4. cdp Input.insertText: type the tweet content (max 280 chars — verify length before posting).
5. eval: locate the Post button — document.querySelector('[data-testid="tweetButtonInline"]')
6. eval: click the Post button.
7. Verify success: wait ~1s then take a screenshot — a toast saying "Your post was sent" should
   appear, or the compose box should be cleared.

Gotchas:
- X is a React SPA — do NOT set text via innerHTML or .value; always use cdp Input.insertText after focusing the element.
- If a tab is already open on x.com, reuse it and skip navigation.
- Tweet character limit is 280. URLs are each counted as 23 characters regardless of actual length.
\`\`\`

## Standard operating procedure

1. Always create_tab first (active: false), then navigate.
2. Before interacting with elements, enumerate actual DOM — never guess selectors:
   - Buttons: \`Array.from(document.querySelectorAll('button')).map(b=>({text:b.innerText.trim(),disabled:b.disabled}))\`
   - Inputs: \`Array.from(document.querySelectorAll('input,textarea')).map(e=>({tag:e.tagName,name:e.name,placeholder:e.placeholder,id:e.id}))\`
3. For React/SPA inputs, use cdp Input.insertText (not innerHTML):
   - First focus the element with eval, then cdp Input.insertText.
4. After every navigation, verify with eval('window.location.href').
5. After finishing, close tabs you opened only after confirming uploads, processing and saves have completed. Keep tabs needed for user intervention open.
6. If a task requires sending data externally, use the fetch tool.

## Publishing and recovery
- File selected, upload finished, processing finished, draft saved, scheduled, and published are distinct states.
- Before reporting success, verify the requested state through the actual UI, content ID or URL. Include evidence in the final result. A successful click alone is not publication evidence.
- If a submit/publish action times out or the connection drops, its outcome is unknown. Inspect the existing draft/content list before retrying; never blindly submit twice.
- If login, a verification challenge, or a user decision is needed, preserve the tab and explain what is needed. Do not report the task complete.

## Stuck detection — MANDATORY

- If you do the same action 2 times with the same result, STOP and diagnose:
  1. Take a screenshot to visually inspect the page.
  2. Enumerate all interactive elements.
  3. Check window.location.href.
- Never repeat a failing action more than 2 times without diagnosing.`
