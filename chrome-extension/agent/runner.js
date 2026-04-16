/**
 * agent/runner.js
 * Core agent loop — runs a single task (prompt) to completion.
 * Handles tool dispatch, screenshot vision injection, context trimming.
 */

import { TOOL_FUNCTIONS, TOOL_SCHEMAS, SYSTEM_PROMPT } from './tools.js'
import { chatCompletion, trimMessages, estimateTokens, loadLLMConfig } from './llm.js'

// ─── Tool dispatch ──────────────────────────────────────────────────────────────

/**
 * Execute a tool call and return { resultStr, dataUrl }.
 * For screenshot: injects the image into the messages array as a vision message,
 * and returns the dataUrl so the UI can display it.
 */
async function dispatchTool(name, args, messages) {
  const fn = TOOL_FUNCTIONS[name]
  if (!fn) {
    return { resultStr: JSON.stringify({ error: `Unknown tool: ${name}` }), dataUrl: null }
  }

  try {
    const result = await fn(args)

    // Screenshot: inject image into conversation so the model can see it
    if (name === 'screenshot' && result?.dataUrl) {
      const { dataUrl, format, tabId } = result
      messages.push({
        role: 'user',
        content: [
          { type: 'text', text: `[Screenshot of tab ${tabId}]` },
          { type: 'image_url', image_url: { url: dataUrl } },
        ],
      })
      return { resultStr: JSON.stringify({ ok: true, tabId, format, injected: true }), dataUrl }
    }

    return { resultStr: JSON.stringify(result, null, 0), dataUrl: null }
  } catch (err) {
    console.error(`[Runner] Tool ${name} error:`, err.message)
    return { resultStr: JSON.stringify({ error: err.message }), dataUrl: null }
  }
}

// ─── Progress callback type ────────────────────────────────────────────────────
// onProgress({ type: 'tool_call'|'tool_result'|'iteration'|'done'|'error', ... })

// ─── Main agent loop ───────────────────────────────────────────────────────────

const MAX_ITERATIONS = 50

/**
 * Run the agent until done.
 *
 * @param {string} prompt - User message to append (new turn)
 * @param {Function} [onProgress] - Optional progress callback
 * @param {AbortSignal} [signal] - Optional abort signal
 * @param {object[]} [existingMessages] - Prior conversation messages to continue from
 * @returns {Promise<{success: boolean, result: string, messages: object[], iterations: number}>}
 */
export async function runAgent(prompt, onProgress = null, signal = null, existingMessages = null) {
  const config = await loadLLMConfig()

  const notify = (data) => { if (onProgress) onProgress(data) }

  // Build messages: reuse existing session or start fresh
  let systemPrompt = SYSTEM_PROMPT
  try {
    const stored = await chrome.storage.local.get(['customSystemPrompt'])
    if (stored.customSystemPrompt && stored.customSystemPrompt.trim()) {
      systemPrompt = stored.customSystemPrompt.trim()
    }
  } catch {}

  const messages = existingMessages && existingMessages.length > 0
    ? [...existingMessages, { role: 'user', content: prompt }]
    : [
        { role: 'system', content: systemPrompt },
        { role: 'user',   content: prompt },
      ]

  console.log(`[Runner] Starting agent | model=${config.model} | prompt=${prompt.slice(0, 80)} | existingMsgs=${existingMessages?.length ?? 0}`)
  notify({ type: 'start', model: config.model, prompt })

  let iteration = 0
  let consecutiveToolCalls = 0
  const maxToolCalls = config.maxToolCalls  // 0 = unlimited

  try {
    while (iteration < MAX_ITERATIONS) {
      if (signal?.aborted) throw new Error('Agent aborted')

      iteration++

      // Trim context
      const before = messages.length
      const trimmed = trimMessages(messages)
      messages.splice(0, messages.length, ...trimmed)
      const dropped = before - messages.length
      const tokenEst = estimateTokens(messages)

      if (dropped > 0) {
        console.warn(`[Runner] Context trimmed: dropped ${dropped} messages, est=${tokenEst} tokens`)
      }

      notify({ type: 'iteration', iteration, tokenEst, dropped, usage: null })
      console.log(`[Runner] Iteration ${iteration} | est=${tokenEst} tokens`)

      const { message, usage } = await chatCompletion(messages, TOOL_SCHEMAS, config)

      console.log(`[Runner] Usage: prompt=${usage.prompt_tokens} completion=${usage.completion_tokens}`)
      notify({ type: 'token_usage', iteration, promptTokens: usage.prompt_tokens, completionTokens: usage.completion_tokens, totalTokens: (usage.prompt_tokens || 0) + (usage.completion_tokens || 0) })

      const toolCalls = message.tool_calls || []
      messages.push(message)

      if (!toolCalls.length) {
        // Model replied without tools — natural end of turn
        consecutiveToolCalls = 0
        const final = message.content || ''
        console.log(`[Runner] Done after ${iteration} iterations`)
        notify({ type: 'done', result: final, iterations: iteration })
        return { success: true, result: final, messages, iterations: iteration }
      }

      // Execute tool calls
      for (const tc of toolCalls) {
        if (signal?.aborted) throw new Error('Agent aborted')

        const tcId   = tc.id || ''
        const fnName = tc.function?.name || ''
        let fnArgs   = tc.function?.arguments || {}
        if (typeof fnArgs === 'string') {
          try { fnArgs = JSON.parse(fnArgs) } catch { fnArgs = {} }
        }

        console.log(`[Runner] Tool call: ${fnName}`, JSON.stringify(fnArgs).slice(0, 200))
        notify({ type: 'tool_call', name: fnName, args: fnArgs })

        const { resultStr, dataUrl } = await dispatchTool(fnName, fnArgs, messages)

        console.log(`[Runner] Tool result: ${fnName} → ${resultStr.slice(0, 200)}`)
        notify({ type: 'tool_result', name: fnName, result: resultStr, dataUrl })

        messages.push({
          role: 'tool',
          tool_call_id: tcId,
          content: resultStr,
        })

        consecutiveToolCalls++

        // Check max consecutive tool calls limit (0 = unlimited)
        if (maxToolCalls > 0 && consecutiveToolCalls >= maxToolCalls) {
          console.warn(`[Runner] Max consecutive tool calls (${maxToolCalls}) reached — pausing for user input`)
          notify({ type: 'tool_limit', count: consecutiveToolCalls, max: maxToolCalls })
          // messages already contain all tool results — safe to pause here
          return { success: true, result: '', messages, iterations: iteration, paused: true }
        }
      }

      // Tool calls were executed — reset counter only when model responds without tools
      // (don't reset here; reset when we get a non-tool response)
    }

    // MAX_ITERATIONS reached
    const lastMsg = messages.findLast(m => m.role === 'assistant')?.content || ''
    notify({ type: 'done', result: lastMsg, iterations: iteration, warning: 'max_iterations' })
    return { success: true, result: lastMsg, messages, iterations: iteration }

  } catch (err) {
    console.error('[Runner] Agent error:', err.message)
    notify({ type: 'error', error: err.message })
    return { success: false, result: err.message, messages, iterations: iteration }
  }
}
