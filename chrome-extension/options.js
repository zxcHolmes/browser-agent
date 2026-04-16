/**
 * options.js — Browser Agent options page
 * Handles: LLM config, task management, quick-run, history viewer
 */

import { PROVIDERS } from './agent/llm.js'
import { SYSTEM_PROMPT } from './agent/tools.js'
import {
  getTasks, saveTask, deleteTask, createTaskId,
  getHistory, clearHistory as clearHistoryStore, deleteHistory,
} from './agent/scheduler.js'

// ─── Tab navigation ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'))
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'))
    btn.classList.add('active')
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active')

    if (btn.dataset.tab === 'history') renderHistory()
    if (btn.dataset.tab === 'tasks') { renderTasks(); refreshActiveRuns() }
  })
})

// ─── Status helpers ────────────────────────────────────────────────────────────

function setStatus(elId, kind, text) {
  const el = document.getElementById(elId)
  if (!el) return
  const dotClass = { ok: 'dot-ok', error: 'dot-error', warn: 'dot-warn', idle: 'dot-idle' }[kind] || 'dot-idle'
  el.innerHTML = `<span class="dot ${dotClass}"></span><span>${text}</span>`
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function openChat(params = {}) {
  const qs = new URLSearchParams(params).toString()
  chrome.tabs.create({ url: chrome.runtime.getURL(`chat.html${qs ? '?' + qs : ''}`), active: true })
}

// ─── Active runs indicator ─────────────────────────────────────────────────────

async function refreshActiveRuns() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'list_runs' }, (resp) => {
      const runs = resp?.runs || []
      const card = document.getElementById('active-runs-card')
      const list = document.getElementById('active-runs-list')

      const activeRuns = runs.filter(r => r.status === 'running')
      if (activeRuns.length === 0) {
        card.style.display = 'none'
        resolve()
        return
      }

      card.style.display = 'block'
      list.innerHTML = activeRuns.map(r => `
        <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)">
          <div>
            <div style="font-size:13px;font-weight:600">${escHtml(r.taskName || 'Run')}</div>
            <div style="font-size:11px;color:var(--muted)">${escHtml((r.prompt || '').slice(0, 80))}</div>
          </div>
          <button class="btn btn-secondary btn-sm" data-sessionid="${escHtml(r.sessionId || r.runId)}">View</button>
        </div>
      `).join('')

      list.querySelectorAll('[data-sessionid]').forEach(btn => {
        btn.addEventListener('click', () => openChat({ sessionId: btn.dataset.sessionid }))
      })

      resolve()
    })
  })
}

// Poll active runs every 5s while tasks tab is visible
setInterval(() => {
  const tasksPanel = document.getElementById('tab-tasks')
  if (tasksPanel?.classList.contains('active')) refreshActiveRuns()
}, 5000)

// ─── Quick Run ─────────────────────────────────────────────────────────────────

document.getElementById('quick-run-btn').addEventListener('click', () => {
  openChat()
})

// ─── Settings tab ──────────────────────────────────────────────────────────────

const providerSelect       = document.getElementById('llm-provider')
const baseUrlInput         = document.getElementById('llm-base-url')
const modelInput           = document.getElementById('llm-model')
const apiKeyInput          = document.getElementById('llm-api-key')
const maxToolCallsInput    = document.getElementById('llm-max-tool-calls')
const contextLengthInput   = document.getElementById('llm-context-length')

providerSelect.addEventListener('change', () => {
  const p = PROVIDERS[providerSelect.value]
  if (p) {
    baseUrlInput.value = p.baseUrl
    modelInput.value   = modelInput.value || p.defaultModel
    if (!p.requiresKey) apiKeyInput.value = ''
  }
})

async function loadLLMSettings() {
  const data = await chrome.storage.local.get(['llmProvider', 'llmBaseUrl', 'llmApiKey', 'llmModel', 'maxToolCalls', 'llmContextLength'])
  providerSelect.value = data.llmProvider || 'openrouter'
  const p = PROVIDERS[providerSelect.value] || PROVIDERS.openrouter
  baseUrlInput.value = data.llmBaseUrl || p.baseUrl
  modelInput.value   = data.llmModel   || p.defaultModel
  apiKeyInput.value  = data.llmApiKey  || ''
  maxToolCallsInput.value  = data.maxToolCalls !== undefined ? data.maxToolCalls : 50
  contextLengthInput.value = data.llmContextLength !== undefined ? data.llmContextLength : 128000
}

document.getElementById('save-llm-btn').addEventListener('click', async () => {
  await chrome.storage.local.set({
    llmProvider:     providerSelect.value,
    llmBaseUrl:      baseUrlInput.value.trim(),
    llmApiKey:       apiKeyInput.value.trim(),
    llmModel:        modelInput.value.trim(),
    maxToolCalls:    parseInt(maxToolCallsInput.value, 10) || 0,
    llmContextLength: parseInt(contextLengthInput.value, 10) || 128000,
  })
  setStatus('llm-status', 'ok', 'Saved')
})

document.getElementById('test-llm-btn').addEventListener('click', async () => {
  setStatus('llm-status', 'idle', 'Testing…')
  const baseUrl = baseUrlInput.value.trim().replace(/\/$/, '')
  const apiKey  = apiKeyInput.value.trim()
  const model   = modelInput.value.trim()

  try {
    const headers = { 'Content-Type': 'application/json' }
    if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`

    const res = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: 'Reply with: ok' }],
      }),
      signal: AbortSignal.timeout(15_000),
    })

    if (!res.ok) {
      const txt = await res.text().catch(() => '')
      throw new Error(`HTTP ${res.status}: ${txt.slice(0, 120)}`)
    }
    const data = await res.json()
    const reply = data.choices?.[0]?.message?.content || '(no content)'
    setStatus('llm-status', 'ok', `Connected — model replied: "${reply.slice(0, 60)}"`)
  } catch (err) {
    setStatus('llm-status', 'error', `Failed: ${err.message.slice(0, 120)}`)
  }
})

// Proxy relay settings
async function loadRelaySettings() {
  const data = await chrome.storage.local.get(['relayPort'])
  document.getElementById('relay-port').value = data.relayPort || 12345
  refreshRelayStatus()
}

async function refreshRelayStatus() {
  const btn = document.getElementById('relay-connect-btn')
  chrome.runtime.sendMessage({ type: 'relay_status' }, (resp) => {
    if (resp?.connected) {
      const port = document.getElementById('relay-port').value || 12345
      setStatus('relay-status', 'ok', `Connected to proxy on port ${port}`)
      if (btn) { btn.textContent = 'Disconnect'; btn.classList.replace('btn-primary', 'btn-secondary') }
    } else {
      setStatus('relay-status', 'idle', 'Not connected (optional)')
      if (btn) { btn.textContent = 'Connect'; btn.classList.replace('btn-secondary', 'btn-primary') }
    }
  })
}

document.getElementById('save-relay-btn').addEventListener('click', async () => {
  const port = parseInt(document.getElementById('relay-port').value, 10) || 12345
  await chrome.storage.local.set({ relayPort: port })
  setStatus('relay-status', 'idle', `Port saved — click Connect to connect`)
})

document.getElementById('relay-connect-btn').addEventListener('click', async () => {
  const btn = document.getElementById('relay-connect-btn')

  // Check current state
  chrome.runtime.sendMessage({ type: 'relay_status' }, async (resp) => {
    if (resp?.connected) {
      // Disconnect
      chrome.runtime.sendMessage({ type: 'relay_disconnect' }, () => {
        setStatus('relay-status', 'idle', 'Disconnected')
        btn.textContent = 'Connect'
        btn.classList.replace('btn-secondary', 'btn-primary')
      })
    } else {
      // Connect
      btn.disabled = true
      btn.textContent = 'Connecting…'
      setStatus('relay-status', 'idle', 'Connecting…')
      chrome.runtime.sendMessage({ type: 'relay_connect' }, (resp) => {
        btn.disabled = false
        if (resp?.ok) {
          const port = document.getElementById('relay-port').value || 12345
          setStatus('relay-status', 'ok', `Connected to proxy on port ${port}`)
          btn.textContent = 'Disconnect'
          btn.classList.replace('btn-primary', 'btn-secondary')
        } else {
          setStatus('relay-status', 'error', `Failed: ${resp?.error || 'Unknown error'}`)
          btn.textContent = 'Connect'
          btn.classList.replace('btn-secondary', 'btn-primary')
        }
      })
    }
  })
})

// Tab timeout settings
async function loadTabTimeoutSettings() {
  const data = await chrome.storage.local.get(['tabTimeoutMinutes'])
  document.getElementById('tab-timeout').value = data.tabTimeoutMinutes || 1
}

document.getElementById('save-tab-timeout-btn').addEventListener('click', async () => {
  const minutes = Math.max(1, parseInt(document.getElementById('tab-timeout').value, 10) || 1)
  await chrome.storage.local.set({ tabTimeoutMinutes: minutes })
  setStatus('tab-timeout-status', 'ok', `Saved — tabs close after ${minutes} min idle`)
})

// System prompt settings
async function loadSystemPromptSettings() {
  const data = await chrome.storage.local.get(['customSystemPrompt'])
  const textarea = document.getElementById('system-prompt')
  const custom = data.customSystemPrompt || ''
  if (custom) {
    textarea.value = custom
    setStatus('system-prompt-status', 'ok', 'Using custom prompt')
  } else {
    // Show built-in default in the textarea so user can read and edit it
    textarea.value = SYSTEM_PROMPT
    setStatus('system-prompt-status', 'idle', 'Showing built-in default (not saved as custom)')
  }
}

document.getElementById('save-system-prompt-btn').addEventListener('click', async () => {
  const val = document.getElementById('system-prompt').value.trim()
  // If user typed exactly the built-in default, treat as "no custom"
  const isDefault = val === SYSTEM_PROMPT.trim()
  await chrome.storage.local.set({ customSystemPrompt: isDefault ? '' : val })
  setStatus('system-prompt-status', 'ok', isDefault ? 'Using built-in default' : 'Custom prompt saved')
})

document.getElementById('reset-system-prompt-btn').addEventListener('click', async () => {
  document.getElementById('system-prompt').value = SYSTEM_PROMPT
  await chrome.storage.local.set({ customSystemPrompt: '' })
  setStatus('system-prompt-status', 'idle', 'Reset to built-in default')
})

// ─── Scheduled Tasks tab ───────────────────────────────────────────────────────

async function renderTasks() {
  const tasks = await getTasks()
  const container = document.getElementById('task-list')
  const empty = document.getElementById('tasks-empty')

  // Remove existing task items (not the empty placeholder)
  container.querySelectorAll('.task-item').forEach(el => el.remove())

  if (tasks.length === 0) {
    empty.style.display = ''
    return
  }
  empty.style.display = 'none'

  // Pre-fetch all alarm info in parallel
  const alarmMap = {}
  await Promise.all(tasks.map(async (task) => {
    const alarm = await chrome.alarms.get(`agent_task_${task.id}`).catch(() => null)
    if (alarm) alarmMap[task.id] = alarm
  }))

  for (const task of tasks) {
    const div = document.createElement('div')
    div.className = 'task-item'
    div.dataset.taskId = task.id

    const intervalText = task.intervalMinutes > 0
      ? `every ${task.intervalMinutes} min`
      : 'manual only'

    // Next run time from alarm
    let nextRunText = ''
    const alarm = alarmMap[task.id]
    if (alarm?.scheduledTime) {
      const diff = alarm.scheduledTime - Date.now()
      if (diff > 0) {
        const mins = Math.ceil(diff / 60000)
        nextRunText = mins < 2
          ? ` · next run in &lt;1 min`
          : ` · next run in ${mins} min`
      } else {
        nextRunText = ' · next run imminent'
      }
    }

    div.innerHTML = `
      <div class="task-header">
        <div>
          <div class="task-name">${escHtml(task.name)}</div>
          <div class="task-meta">${intervalText}${nextRunText} · ${task.enabled ? '<span class="badge badge-enabled">enabled</span>' : '<span class="badge badge-disabled">disabled</span>'}</div>
          <div class="task-prompt">${escHtml(task.prompt)}</div>
        </div>
        <div class="task-actions">
          <button class="btn btn-success btn-sm run-btn" title="Run now in chat">▶ Run</button>
          <button class="btn btn-secondary btn-sm toggle-btn">${task.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-danger btn-sm delete-btn">Delete</button>
        </div>
      </div>
    `

    div.querySelector('.run-btn').addEventListener('click', () => {
      openChat({ taskId: task.id, autorun: '1' })
    })
    div.querySelector('.toggle-btn').addEventListener('click', () => toggleTask(task))
    div.querySelector('.delete-btn').addEventListener('click', () => deleteTaskItem(task.id))

    container.appendChild(div)
  }
}

async function toggleTask(task) {
  task.enabled = !task.enabled
  await saveTask(task)
  renderTasks()
}

async function deleteTaskItem(taskId) {
  if (!confirm('Delete this task?')) return
  await deleteTask(taskId)
  renderTasks()
}

document.getElementById('add-task-btn').addEventListener('click', async () => {
  const name     = document.getElementById('new-task-name').value.trim()
  const interval = parseInt(document.getElementById('new-task-interval').value, 10) || 0
  const prompt   = document.getElementById('new-task-prompt').value.trim()

  if (!name)   return setStatus('add-task-status', 'error', 'Name is required')
  if (!prompt) return setStatus('add-task-status', 'error', 'Prompt is required')

  const task = {
    id: createTaskId(),
    name,
    prompt,
    intervalMinutes: interval,
    enabled: true,
    createdAt: Date.now(),
  }

  await saveTask(task)
  document.getElementById('new-task-name').value = ''
  document.getElementById('new-task-prompt').value = ''
  setStatus('add-task-status', 'ok', 'Task added')
  renderTasks()
})

// ─── History tab ───────────────────────────────────────────────────────────────

async function renderHistory() {
  const records = await getHistory()
  const container = document.getElementById('history-list')
  const empty = document.getElementById('history-empty')

  container.querySelectorAll('.history-item').forEach(el => el.remove())

  if (records.length === 0) {
    empty.style.display = ''
    return
  }
  empty.style.display = 'none'

  // Most recent first
  const sorted = [...records].sort((a, b) => b.startedAt - a.startedAt)

  for (const rec of sorted) {
    const div = document.createElement('div')
    div.className = 'history-item'

    const duration = rec.finishedAt
      ? `${Math.round((rec.finishedAt - rec.startedAt) / 1000)}s`
      : '?'
    const timeStr = new Date(rec.startedAt).toLocaleString()
    const successIcon = rec.success ? '✓' : '✗'
    const successColor = rec.success ? 'var(--success)' : 'var(--error)'

    div.innerHTML = `
      <div class="history-header">
        <div style="flex:1;min-width:0">
          <div class="history-title">
            <span style="color:${successColor}">${successIcon}</span>
            ${escHtml(rec.taskName || 'Quick Run')}
            <span style="font-size:11px;color:var(--muted);font-weight:400"> · ${rec.iterations || 0} iterations · ${duration}</span>
          </div>
          <div class="history-time">${timeStr}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0;align-items:flex-start">
          <button class="btn btn-secondary btn-sm view-chat-btn" data-historyid="${escHtml(rec.id || rec.sessionId || '')}">View Chat</button>
          <button class="btn btn-danger btn-sm delete-history-btn" data-historyid="${escHtml(rec.id || rec.sessionId || '')}" title="Delete this entry">✕</button>
        </div>
      </div>
      <div class="history-result">${escHtml((rec.result || '').slice(0, 400))}</div>
      <div class="history-expand" data-open="false">Show full conversation (${(rec.messages || []).length} messages)</div>
      <div class="history-messages">
        ${renderMessages(rec.messages || [])}
      </div>
    `

    // "View Chat" — open chat.html with historyId so it can load from storage
    // and optionally continue the conversation.
    const viewBtn = div.querySelector('.view-chat-btn')
    if (viewBtn) {
      viewBtn.addEventListener('click', () => {
        const historyId = viewBtn.dataset.historyid
        if (!historyId) return

        // Try to reconnect to in-memory session first (if still within 30-min TTL)
        // fall back to historyId so chat.js loads from storage
        const sessionId = rec.sessionId || rec.id
        chrome.runtime.sendMessage({ type: 'get_session', sessionId }, (resp) => {
          if (resp?.ok) {
            openChat({ sessionId })
          } else {
            openChat({ historyId })
          }
        })
      })
    }

    const deleteBtn = div.querySelector('.delete-history-btn')
    if (deleteBtn) {
      deleteBtn.addEventListener('click', async () => {
        const id = deleteBtn.dataset.historyid
        if (!id) return
        await deleteHistory(id)
        div.remove()
        // Show empty state if no items left
        if (!container.querySelector('.history-item')) {
          document.getElementById('history-empty').style.display = ''
        }
      })
    }

    div.querySelector('.history-expand').addEventListener('click', function() {
      const msgDiv = div.querySelector('.history-messages')
      const open = this.dataset.open === 'true'
      this.dataset.open = String(!open)
      msgDiv.classList.toggle('open', !open)
      this.textContent = open
        ? `Show full conversation (${(rec.messages || []).length} messages)`
        : 'Hide conversation'
    })

    container.appendChild(div)
  }
}

function renderMessages(messages) {
  return messages.map(m => {
    const roleClass = `msg-${m.role}`
    let content = ''

    if (typeof m.content === 'string') {
      content = escHtml(m.content.slice(0, 2000))
    } else if (Array.isArray(m.content)) {
      content = m.content.map(part => {
        if (part.type === 'text') return escHtml(part.text.slice(0, 1000))
        if (part.type === 'image_url') return '[screenshot image]'
        return ''
      }).join('\n')
    }

    if (m.role === 'assistant' && m.tool_calls?.length) {
      content += m.tool_calls.map(tc =>
        `\n→ ${tc.function?.name}(${JSON.stringify(tc.function?.arguments || {}).slice(0, 200)})`
      ).join('')
    }

    return `<div class="msg-bubble ${roleClass}"><strong>${m.role}</strong>: ${content}</div>`
  }).join('')
}

document.getElementById('clear-history-btn').addEventListener('click', async () => {
  if (!confirm('Clear all history?')) return
  await clearHistoryStore()
  renderHistory()
})

// ─── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  await loadLLMSettings()
  await loadRelaySettings()
  await loadTabTimeoutSettings()
  await loadSystemPromptSettings()
  await renderTasks()
  await refreshActiveRuns()
}

init()
