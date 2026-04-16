/**
 * agent/llm.js
 * OpenAI-compatible API client + context window management.
 * Supports OpenRouter, OpenAI, Ollama — all use the same API shape.
 */

// ─── Config ────────────────────────────────────────────────────────────────────

export const PROVIDERS = {
  openrouter: {
    label: 'OpenRouter',
    baseUrl: 'https://openrouter.ai/api/v1',
    defaultModel: 'google/gemini-2.5-flash-preview',
    requiresKey: true,
  },
  openai: {
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    defaultModel: 'gpt-4o-mini',
    requiresKey: true,
  },
  ollama: {
    label: 'Ollama (local)',
    baseUrl: 'http://localhost:11434/v1',
    defaultModel: 'gemma3:12b',
    requiresKey: false,
  },
}

/**
 * Load LLM config from chrome.storage.local.
 * @returns {Promise<{provider, baseUrl, apiKey, model}>}
 */
export async function loadLLMConfig() {
  const data = await chrome.storage.local.get(['llmProvider', 'llmBaseUrl', 'llmApiKey', 'llmModel', 'maxToolCalls'])
  const provider = data.llmProvider || 'openrouter'
  const providerDef = PROVIDERS[provider] || PROVIDERS.openrouter
  const maxToolCalls = data.maxToolCalls !== undefined ? Number(data.maxToolCalls) : 50
  return {
    provider,
    baseUrl: data.llmBaseUrl || providerDef.baseUrl,
    apiKey: data.llmApiKey || '',
    model: data.llmModel || providerDef.defaultModel,
    maxToolCalls: Number.isFinite(maxToolCalls) && maxToolCalls >= 0 ? maxToolCalls : 50,
  }
}

// ─── Context window management ─────────────────────────────────────────────────

const MAX_TOKENS = 50_000
const CHARS_PER_TOKEN = 2
const IMAGE_TOKEN_FIXED = 512
const IMAGE_CHARS_FIXED = IMAGE_TOKEN_FIXED * CHARS_PER_TOKEN

function msgChars(msg) {
  let total = (msg.role || '').length
  const content = msg.content || ''
  if (typeof content === 'string') {
    total += content.length
  } else if (Array.isArray(content)) {
    for (const part of content) {
      if (part?.type === 'image_url') {
        total += IMAGE_CHARS_FIXED
      } else {
        total += JSON.stringify(part).length
      }
    }
  }
  for (const tc of (msg.tool_calls || [])) {
    total += JSON.stringify(tc).length
  }
  return total
}

function totalChars(messages) {
  return messages.reduce((s, m) => s + msgChars(m), 0)
}

export function estimateTokens(messages) {
  return Math.ceil(totalChars(messages) / CHARS_PER_TOKEN)
}

/**
 * Trim messages to stay within MAX_TOKENS.
 * Keeps messages[0] (system) and messages[1] (first user) always.
 * Groups assistant(tool_calls) + their tool results as atomic units.
 */
export function trimMessages(messages, maxTokens = MAX_TOKENS) {
  const maxChars = maxTokens * CHARS_PER_TOKEN

  if (messages.length <= 2) return messages

  const pinned = messages.slice(0, 2)
  const tail = messages.slice(2)

  // Group tail into atomic units
  const units = []
  let i = 0
  while (i < tail.length) {
    const msg = tail[i]
    if (msg.role === 'assistant' && msg.tool_calls?.length) {
      const group = [msg]
      let j = i + 1
      while (j < tail.length && tail[j].role === 'tool') {
        group.push(tail[j])
        j++
      }
      units.push(group)
      i = j
    } else {
      units.push([msg])
      i++
    }
  }

  // Drop oldest units until within budget
  while (units.length > 0) {
    const candidate = [...pinned, ...units.flatMap(u => u)]
    if (totalChars(candidate) <= maxChars) break
    units.shift()
  }

  return [...pinned, ...units.flatMap(u => u)]
}

// ─── API call ──────────────────────────────────────────────────────────────────

/**
 * Call the LLM chat completions endpoint.
 * @param {object[]} messages
 * @param {object[]} tools
 * @param {object} config - { baseUrl, apiKey, model }
 * @returns {Promise<{message, usage}>}
 */
export async function chatCompletion(messages, tools, config) {
  const url = `${config.baseUrl.replace(/\/$/, '')}/chat/completions`

  const headers = { 'Content-Type': 'application/json' }
  if (config.apiKey) {
    headers['Authorization'] = `Bearer ${config.apiKey}`
  }

  const body = {
    model: config.model,
    messages,
    tools,
    temperature: 0.3,
  }

  // OpenRouter supports disabling reasoning tokens
  if (config.provider === 'openrouter') {
    body.reasoning = { effort: 'none', enabled: false, exclude: true }
  }

  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(120_000),
  })

  if (!res.ok) {
    const errText = await res.text().catch(() => '')
    throw new Error(`LLM API error ${res.status}: ${errText.slice(0, 200)}`)
  }

  const data = await res.json()
  const message = data.choices?.[0]?.message
  if (!message) throw new Error('LLM returned no choices')

  return { message, usage: data.usage || {} }
}
