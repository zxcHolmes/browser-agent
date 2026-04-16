/**
 * chat.js — Browser Agent chat page (session model)
 *
 * URL params:
 *   ?sessionId=xxx        — reconnect to / view an existing in-memory session
 *   ?taskId=xxx&autorun=1 — run a saved task immediately
 *   ?prompt=...&autorun=1 — quick-run with pre-filled prompt
 *   ?historyId=xxx        — resume from a history entry (loads its messages)
 *
 * Flow:
 *   1. On load, check URL params.
 *   2. If sessionId: fetch session from background, replay events, poll for live updates.
 *   3. If taskId + autorun: fetch task, run_task, poll.
 *   4. If prompt + autorun: start_session immediately, poll.
 *   5. If historyId: render history result, on next user message start_session with existingMessages.
 *   6. User sends new message:
 *      - If session exists → continue_session (same LLM context, same sessionId).
 *      - If no session yet → start_session (optionally with existingMessages from history).
 *   7. Polling: every 500ms get_session, render new events via renderedEventCount.
 *      Continues across multiple turns; stops only on error/aborted.
 */

// ─── DOM refs ──────────────────────────────────────────────────────────────────

const msgList     = document.getElementById('messages')
const emptyState  = document.getElementById('empty-state')
const titleEl     = document.getElementById('chat-title')
const metaEl      = document.getElementById('chat-meta')
const pillEl      = document.getElementById('status-pill')
const promptInput = document.getElementById('prompt-input')
const btnSend     = document.getElementById('btn-send')
const btnAbort    = document.getElementById('btn-abort')
const fabBottom   = document.getElementById('fab-bottom')

// ─── State ─────────────────────────────────────────────────────────────────────

let currentSessionId    = null
let currentStatus       = 'idle'  // 'idle' | 'running' | 'done' | 'error' | 'aborted'
let autoScrolling       = true
let historyMessages     = null    // pre-loaded existingMessages from a history entry

// ─── URL params ────────────────────────────────────────────────────────────────

const params        = new URLSearchParams(location.search)
const initSessionId = params.get('sessionId')
const initTaskId    = params.get('taskId')
const initHistoryId = params.get('historyId')
const initPrompt    = params.get('prompt') || ''
const autorun       = params.get('autorun') === '1'

// ─── Scroll helpers ────────────────────────────────────────────────────────────

function scrollToBottom(force = false) {
  if (force || autoScrolling) {
    msgList.scrollTop = msgList.scrollHeight
  }
}

msgList.addEventListener('scroll', () => {
  const atBottom = msgList.scrollHeight - msgList.scrollTop - msgList.clientHeight < 60
  autoScrolling = atBottom
  fabBottom.classList.toggle('visible', !atBottom)
})

fabBottom.addEventListener('click', () => {
  autoScrolling = true
  scrollToBottom(true)
})

// ─── Status pill ───────────────────────────────────────────────────────────────

function setStatusPill(status) {
  currentStatus = status
  pillEl.style.display = ''

  const map = {
    running: { cls: 'pill-running', label: 'Running', pulse: true },
    done:    { cls: 'pill-done',    label: 'Done',    pulse: false },
    idle:    { cls: 'pill-done',    label: 'Done',    pulse: false },
    error:   { cls: 'pill-error',   label: 'Error',   pulse: false },
    aborted: { cls: 'pill-aborted', label: 'Stopped', pulse: false },
  }
  const cfg = map[status] || { cls: '', label: status, pulse: false }

  pillEl.className = `status-pill ${cfg.cls}`
  pillEl.innerHTML = cfg.pulse
    ? `<span class="pulse"></span>${cfg.label}`
    : cfg.label

  // Only disable input while actively running
  const isRunning = status === 'running'
  btnSend.disabled = isRunning
  btnAbort.classList.toggle('visible', isRunning)
}

// ─── Message bubble factory ────────────────────────────────────────────────────

function escHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

// Minimal markdown → HTML (no external deps)
function renderMarkdown(text) {
  let s = String(text || '')
  // Escape HTML first
  s = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  // Fenced code blocks
  s = s.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, code) =>
    `<pre><code>${code.trimEnd()}</code></pre>`)
  // Inline code
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>')
  // Bold
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  // Italic
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>')
  // HR
  s = s.replace(/^---$/gm, '<hr>')
  // Headers
  s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>')
  s = s.replace(/^## (.+)$/gm, '<h2>$1</h2>')
  s = s.replace(/^# (.+)$/gm, '<h1>$1</h1>')
  // Blockquote
  s = s.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
  // Unordered list items
  s = s.replace(/^[\*\-] (.+)$/gm, '<li>$1</li>')
  s = s.replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`)
  // Ordered list items
  s = s.replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
  // Links
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')
  // Paragraphs: double newlines
  s = s.replace(/\n{2,}/g, '</p><p>')
  // Single newlines (not inside blocks)
  s = s.replace(/\n/g, '<br>')
  return `<p>${s}</p>`
}

function appendMeta(text) {
  const el = document.createElement('div')
  el.className = 'msg-meta'
  el.textContent = text
  msgList.appendChild(el)
  scrollToBottom()
  return el
}

function appendUserMsg(text) {
  emptyState.style.display = 'none'
  const el = document.createElement('div')
  el.className = 'msg msg-user'
  el.innerHTML = `
    <div class="msg-role">You</div>
    <div class="msg-bubble">${escHtml(text)}</div>
  `
  msgList.appendChild(el)
  scrollToBottom(true)
}

function appendToolCall(name, args) {
  const argsStr = typeof args === 'string' ? args : JSON.stringify(args, null, 2)
  const preview = argsStr.length > 300 ? argsStr.slice(0, 300) + '…' : argsStr

  const el = document.createElement('div')
  el.className = 'msg msg-tool_call'
  el.innerHTML = `
    <div class="msg-role">Tool call</div>
    <div class="msg-bubble">
      <span class="tool-name">${escHtml(name)}</span>
      <span class="tool-toggle" data-open="false">▸ args</span>
      <div class="tool-detail">
        <pre class="tool-args">${escHtml(preview)}</pre>
      </div>
    </div>
  `

  el.querySelector('.tool-toggle').addEventListener('click', function() {
    const open = this.dataset.open === 'true'
    this.dataset.open = String(!open)
    this.textContent = open ? '▸ args' : '▾ args'
    el.querySelector('.tool-detail').classList.toggle('open', !open)
  })

  msgList.appendChild(el)
  scrollToBottom()
  return el
}

function appendToolResult(name, result) {
  const resultStr = String(result || '')

  let isScreenshot = false
  try {
    const parsed = JSON.parse(resultStr)
    if (parsed?.injected && parsed?.ok) isScreenshot = true
  } catch {}

  const el = document.createElement('div')

  if (isScreenshot) {
    el.className = 'msg msg-screenshot'
    el.innerHTML = `
      <div class="msg-role">Screenshot</div>
      <div class="msg-bubble"><em style="color:var(--muted);font-size:12px">Screenshot captured and sent to model</em></div>
    `
  } else {
    // Show first 300 chars, with toggle to expand full content
    const short = resultStr.length > 300 ? resultStr.slice(0, 300) + '…' : resultStr
    const hasFull = resultStr.length > 300
    el.className = 'msg msg-tool_result'
    el.innerHTML = `
      <div class="msg-role">Result <span style="color:var(--muted)">← ${escHtml(name)}</span></div>
      <div class="msg-bubble">
        <span class="result-label">←</span> <span class="result-short">${escHtml(short)}</span>
        ${hasFull ? `<span class="tool-toggle" data-open="false" style="display:block;margin-top:4px">▸ show full (${resultStr.length} chars)</span><div class="tool-detail"><pre class="tool-args">${escHtml(resultStr)}</pre></div>` : ''}
      </div>
    `
    if (hasFull) {
      el.querySelector('.tool-toggle').addEventListener('click', function() {
        const open = this.dataset.open === 'true'
        this.dataset.open = String(!open)
        this.textContent = open ? `▸ show full (${resultStr.length} chars)` : '▾ hide'
        el.querySelector('.tool-detail').classList.toggle('open', !open)
      })
    }
  }

  msgList.appendChild(el)
  scrollToBottom()
  return el
}

function appendScreenshotImage(dataUrl) {
  const el = document.createElement('div')
  el.className = 'msg msg-screenshot'
  el.innerHTML = `
    <div class="msg-role">Screenshot</div>
    <div class="msg-bubble"><img src="${dataUrl}" alt="Screenshot" /></div>
  `
  msgList.appendChild(el)
  scrollToBottom()
}

function appendDone(result, iterations) {
  const el = document.createElement('div')
  el.className = 'msg msg-done'
  const display = String(result || '(no output)').trim() || '(no output)'
  el.innerHTML = `
    <div class="msg-role">Result</div>
    <div class="msg-bubble md">
      <div class="done-header">✓ Task complete${iterations ? ` — ${iterations} iterations` : ''}</div>
      ${renderMarkdown(display)}
    </div>
  `
  msgList.appendChild(el)
  scrollToBottom(true)
}

function appendTokenUsage(promptTokens, completionTokens, totalTokens) {
  const el = document.createElement('div')
  el.className = 'token-bar'
  el.textContent = `prompt ${promptTokens} + completion ${completionTokens} = ${totalTokens} tokens`
  msgList.appendChild(el)
  scrollToBottom()
  return el
}

function appendError(errorMsg) {
  const el = document.createElement('div')
  el.className = 'msg msg-error'
  el.innerHTML = `
    <div class="msg-role">Error</div>
    <div class="msg-bubble">✗ ${escHtml(errorMsg)}</div>
  `
  msgList.appendChild(el)
  scrollToBottom()
}

// ─── Event → bubble dispatcher ─────────────────────────────────────────────────

function applyEvent(event) {
  switch (event.type) {
    case 'start':
      setStatusPill('running')
      if (event.prompt) titleEl.textContent = event.prompt.slice(0, 60)
      appendMeta(`Started · model: ${event.model || '?'}`)
      break

    case 'iteration':
      metaEl.textContent = `iteration ${event.iteration} · ~${event.tokenEst} tokens`
      break

    case 'tool_call':
      appendToolCall(event.name, event.args)
      break

    case 'tool_result':
      if (event.dataUrl) {
        appendScreenshotImage(event.dataUrl)
      } else {
        appendToolResult(event.name, event.result)
      }
      break

    case 'token_usage':
      appendTokenUsage(event.promptTokens, event.completionTokens, event.totalTokens)
      metaEl.textContent = `iter ${event.iteration} · ${event.totalTokens} tokens`
      break

    case 'confirm':
      appendMeta(`Checking completion… (attempt ${event.attempt})`)
      break

    case 'done':
      // Use 'idle' so the user can continue chatting in the same session
      setStatusPill('idle')
      appendDone(event.result, event.iterations)
      break

    case 'error':
      setStatusPill('error')
      appendError(event.error)
      titleEl.textContent = 'Error'
      break

    case 'aborted':
      setStatusPill('aborted')
      appendMeta('Stopped by user')
      titleEl.textContent = 'Stopped'
      break

    case 'tool_limit':
      setStatusPill('idle')
      appendMeta(`⚠ Reached limit of ${event.max} consecutive tool calls — type a message to continue.`)
      break
  }
}

// ─── Polling ───────────────────────────────────────────────────────────────────
// Poll get_session every 500ms. Use renderedEventCount to only render new events.
// We continue polling across turns (status returns to 'running' on continue_session).
// Stop only on terminal states (error / aborted).

let pollTimer          = null
let renderedEventCount = 0

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function startPolling(sessionId) {
  stopPolling()
  pollTimer = setInterval(() => {
    chrome.runtime.sendMessage({ type: 'get_session', sessionId }, (resp) => {
      if (chrome.runtime.lastError || !resp?.ok || !resp.session) return
      const session = resp.session
      const events  = session.events || []

      for (let i = renderedEventCount; i < events.length; i++) {
        applyEvent(events[i])
      }
      renderedEventCount = events.length

      // Only stop polling on terminal states; 'idle' means we can still continue
      if (session.status === 'error' || session.status === 'aborted') {
        stopPolling()
      }
    })
  }, 500)

  window.addEventListener('pagehide', stopPolling, { once: true })
}

// ─── Reconnect to existing in-memory session ───────────────────────────────────

async function reconnectToSession(sessionId) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'get_session', sessionId }, (resp) => {
      if (!resp?.ok || !resp.session) { resolve(false); return }

      const session = resp.session
      currentSessionId = sessionId

      titleEl.textContent = session.taskName || session.firstPrompt?.slice(0, 60) || 'Agent Session'

      if (session.firstPrompt) appendUserMsg(session.firstPrompt)

      const events = session.events || []
      for (const event of events) applyEvent(event)
      renderedEventCount = events.length

      if (session.status === 'running') {
        startPolling(sessionId)
      } else {
        setStatusPill(session.status)
      }

      resolve(true)
    })
  })
}

// ─── Start a brand-new session ─────────────────────────────────────────────────

async function startSession(prompt, taskName = null, existing = null) {
  renderedEventCount = 0
  setStatusPill('running')
  titleEl.textContent = prompt.slice(0, 60) || 'Running…'

  return new Promise((resolve) => {
    chrome.runtime.sendMessage(
      {
        type: 'start_session',
        prompt,
        taskName: taskName || prompt.slice(0, 40),
        existingMessages: existing || null,
      },
      (resp) => {
        if (chrome.runtime.lastError || !resp?.ok) {
          appendError(chrome.runtime.lastError?.message || resp?.error || 'Failed to start')
          setStatusPill('error')
          resolve(null)
          return
        }
        currentSessionId = resp.sessionId
        startPolling(currentSessionId)
        resolve(currentSessionId)
      }
    )
  })
}

// ─── Run a saved task ──────────────────────────────────────────────────────────

async function runTask(taskId) {
  renderedEventCount = 0
  setStatusPill('running')

  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'run_task', taskId }, (resp) => {
      if (chrome.runtime.lastError || !resp?.ok) {
        appendError(chrome.runtime.lastError?.message || resp?.error || 'Failed to start task')
        setStatusPill('error')
        resolve(null)
        return
      }
      currentSessionId = resp.sessionId
      startPolling(currentSessionId)
      resolve(currentSessionId)
    })
  })
}

// ─── Input handling ────────────────────────────────────────────────────────────

function autoResizeTextarea() {
  promptInput.style.height = 'auto'
  promptInput.style.height = Math.min(promptInput.scrollHeight, 160) + 'px'
}

promptInput.addEventListener('input', autoResizeTextarea)

promptInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    handleSend()
  }
})

btnSend.addEventListener('click', handleSend)

async function handleSend() {
  const prompt = promptInput.value.trim()
  if (!prompt || currentStatus === 'running') return

  promptInput.value = ''
  autoResizeTextarea()

  // Show user bubble immediately
  appendUserMsg(prompt)

  if (currentSessionId) {
    // Continue existing session — LLM sees full prior context
    setStatusPill('running')
    chrome.runtime.sendMessage(
      { type: 'continue_session', sessionId: currentSessionId, prompt },
      (resp) => {
        if (chrome.runtime.lastError || !resp?.ok) {
          appendError(chrome.runtime.lastError?.message || resp?.error || 'Failed to continue session')
          setStatusPill('error')
          return
        }
        // Resume polling (may have stopped if previous turn ended)
        startPolling(currentSessionId)
      }
    )
  } else {
    // First message in this window — consume historyMessages if we loaded one
    const existing = historyMessages
    historyMessages = null
    await startSession(prompt, null, existing)
  }
}

btnAbort.addEventListener('click', () => {
  if (!currentSessionId) return
  chrome.runtime.sendMessage({ type: 'abort_session', sessionId: currentSessionId })
  setStatusPill('aborted')
  stopPolling()
  appendMeta('Stopping…')
})

// ─── Init ──────────────────────────────────────────────────────────────────────

;(async () => {
  if (initSessionId) {
    // Reconnect to an active or recently completed session
    titleEl.textContent = 'Loading…'
    const found = await reconnectToSession(initSessionId)
    if (!found) {
      titleEl.textContent = 'Browser Agent'
      appendError('Session not found — it may have expired from memory.')
    }

  } else if (initHistoryId) {
    // Load from history and replay full event log
    titleEl.textContent = 'Loading…'
    const { agentHistory } = await chrome.storage.local.get(['agentHistory'])
    const rec = (agentHistory || []).find(e => (e.id || e.sessionId) === initHistoryId)

    if (!rec) {
      titleEl.textContent = 'Browser Agent'
      appendError('History entry not found.')
    } else {
      titleEl.textContent = rec.taskName || 'Previous Session'
      if (rec.prompt) appendUserMsg(rec.prompt)
      appendMeta(`History · ${new Date(rec.startedAt).toLocaleString()} · ${rec.iterations || 0} iterations`)

      // Replay the full UI event log if available
      if (rec.events?.length) {
        for (const event of rec.events) applyEvent(event)
      } else if (rec.result) {
        // Fallback for older records without events[]
        appendDone(rec.result, rec.iterations)
      }

      if (rec.messages?.length) {
        // Store for use when user sends the next message
        historyMessages = rec.messages
        appendMeta('✎ Type a message to continue this conversation.')
      }

      setStatusPill('idle')
      pillEl.style.display = 'none'  // no pill until user starts chatting
    }

  } else if (initTaskId && autorun) {
    // Run a saved task immediately
    const { agentTasks } = await chrome.storage.local.get(['agentTasks'])
    const task = (agentTasks || []).find(t => t.id === initTaskId)
    if (task) {
      titleEl.textContent = task.name
      appendUserMsg(task.prompt)
      await runTask(initTaskId)
    } else {
      titleEl.textContent = 'Browser Agent'
      appendError('Task not found.')
    }

  } else if (initPrompt && autorun) {
    // Quick-run with a given prompt
    appendUserMsg(initPrompt)
    await startSession(initPrompt)

  } else {
    // Blank chat — optionally pre-fill textarea
    titleEl.textContent = 'Browser Agent'
    if (initPrompt) {
      promptInput.value = initPrompt
      autoResizeTextarea()
    }
  }
})()
