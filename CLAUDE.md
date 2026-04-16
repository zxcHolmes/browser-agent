# Browser Agent Proxy + Browser Agent — AI Context

## Project Overview

A Chrome Extension-based browser automation system with two independent subsystems sharing a single tab registry:

1. **Browser Agent Proxy** (Go) — HTTP/WebSocket hub. Allows external applications to control Chrome via REST API without special launch flags.
2. **Browser Agent** (Chrome Extension) — Self-contained AI agent that runs LLM tool-calling loops directly in the extension's service worker.

**Core value**: Control a user's normal Chrome browser — no `--remote-debugging-port` needed.

---

## Architecture

```
External App (HTTP REST)
        ↓
Go Server (WebSocket hub + in-memory event store)
        ↓
Chrome Extension (tab registry + chrome.debugger bridge)
        ↓
Chrome Browser
```

### Why a Chrome Extension?

Playwright/Puppeteer require Chrome launched with `--remote-debugging-port`. The extension sidesteps this using `chrome.debugger`, which works in any normal Chrome install.

`chrome.debugger.sendCommand(debuggee, method, params)` is a process-local IPC wrapper around CDP JSON. The `id` and serialization are handled by Chrome automatically.

### Tab Registry — single source of truth

`managedTabs: Map<tabId, tabInfo>` in `background.js` is the authoritative state. Only tabs created through the API are managed; user-opened tabs are invisible to the system.

---

## Core Design

### Tab State Fields

```js
{
  tabId: number,
  targetId: string,     // chrome.debugger Target ID
  url: string,
  title: string,
  meta: object,         // caller-supplied arbitrary metadata
  isAttached: boolean,
  lastActivity: number, // Unix ms — refreshed on every cdp.send or debugger event
  createdAt: number,
}
```

### Tab Idle Timeout

- Default: **1 minute** of inactivity → tab is closed (detach + `chrome.tabs.remove`)
- Checked every 5 s via `setInterval`
- Configurable via `tabTimeoutMinutes` in `chrome.storage.local`
- Applies to both Agent-opened tabs and Browser Agent Proxy-opened tabs

### CDP Command Forwarding

```http
POST /api/cdp
{ "tabId": 123, "method": "Page.navigate", "params": { "url": "..." }, "timeout": 30 }
```

Method and params are passed through verbatim to `chrome.debugger.sendCommand`. Auto-attach on first command. Returns 404 if tab not found (caller must recreate).

### In-Memory Event Store (Go server)

All messages pushed by the extension (CDP events + system events) are stored in a time-series ring:

```go
type Event struct {
    TabID       int
    EventMethod string    // e.g. "Page.loadEventFired" or "tab.closed"
    EventParams map[string]interface{}
    Timestamp   int64     // Unix ms from extension
    ReceivedAt  time.Time
}
```

- Retention window: 3 minutes
- GC: every 30 s
- Queryable by `tabId` and `since` timestamp via `GET /api/events`

### Browser Agent Session Model

Sessions live in `activeSessions: Map<sessionId, Session>` in `background.js`.

```
Session {
  sessionId, taskId, taskName, firstPrompt,
  status: 'running' | 'idle' | 'done' | 'error' | 'aborted',
  startedAt, updatedAt, iterations,
  events: ProgressEvent[],   // full UI event log (persisted to storage on completion)
  messages: object[],        // LLM conversation — cleared from memory after each turn
}
```

**Memory management**:
- `messages[]` is cleared from memory immediately after a turn completes; saved first to `agentHistory` in `chrome.storage.local`.
- On `continue_session`, messages are reloaded from storage if the in-memory copy was cleared.
- Completed sessions evict from `activeSessions` after 2 minutes (enough for the chat UI to consume final events).
- `events[]` is persisted to `agentHistory` so the chat UI can replay the full tool-call log from disk after a reload.

### Context Trimming (LLM)

- Budget: 50,000 tokens (`CHARS_PER_TOKEN = 2`, estimated from character count)
- Atomic eviction units: `assistant(tool_calls)` + all following `tool` results are kept together
- `system` + first `user` message are never evicted

---

## WebSocket Protocol (Go ↔ Extension)

### Command (Server → Extension)
```json
{ "id": 1, "method": "tab.create", "params": { "url": "https://example.com", "active": false, "meta": {} } }
```

### Response (Extension → Server)
```json
{ "id": 1, "result": { "tab": { ... } } }
{ "id": 1, "error": "Tab 123 not found" }
```

### Event push (Extension → Server, no `id`)
```json
{
  "method": "event",
  "params": {
    "tabId": 123,
    "eventMethod": "Page.loadEventFired",
    "eventParams": { "timestamp": 1234567890.123 },
    "timestamp": 1700000000000
  }
}
```

System events (non-CDP): `tab.closed` (reason: `"timeout"|"api"|"user"`), `debugger.detached`

### Heartbeat
```
Server → Extension: { "method": "ping" }
Extension → Server: { "method": "pong" }
```

### Extension Commands

| method | params | description |
|--------|--------|-------------|
| `tab.create` | url, active, meta | Create a tab |
| `tab.list` | — | List managed tabs |
| `tab.get` | tabId | Get single tab |
| `tab.close` | tabId | Close a tab |
| `cdp.send` | tabId, method, params | Forward CDP command |

---

## Key File Locations

### Go Server (`browser-agent-proxy/`)

```
main.go                       Entry point, CLI flags
Makefile                      Build / run targets
pkg/
├── config/config.go          Config struct
├── logger/logger.go          logrus factory
├── server/server.go          Router, dependency wiring
├── relay/hub.go              WebSocket hub, pending-request map, RWMutex
├── events/store.go           In-memory event store, 3-min TTL, 30-s GC
└── handler/
    ├── tab.go                Tab CRUD — proxied to extension
    ├── cdp.go                POST /api/cdp — CDP forwarding
    ├── actions.go            /navigate, /screenshot, /eval shortcuts
    ├── events.go             GET /api/events
    ├── websocket.go          WebSocket upgrade + read/write pump
    ├── health.go             GET /
    └── response.go           writeSuccess / writeError
```

### Chrome Extension (`chrome-extension/`)

```
manifest.json                 MV3, permissions
background.js                 Service worker: tab registry, relay bridge, session manager
chat.html / chat.js           Chat UI — session model, event replay, polling (500 ms)
options.html / options.js     Settings: LLM, tasks, history, system prompt, tab timeout
agent/
├── tools.js                  Tool implementations + TOOL_SCHEMAS + SYSTEM_PROMPT
├── llm.js                    OpenAI-compatible client, trimMessages, loadLLMConfig
├── runner.js                 Agent loop: dispatchTool → {resultStr, dataUrl}, tool-call limit
└── scheduler.js              Task CRUD, chrome.alarms, appendHistory (upsert), deleteHistory
```

---

## Error Codes (Go server)

| Code | HTTP | Meaning |
|------|------|---------|
| `EXTENSION_NOT_CONNECTED` | 503 | Extension not connected |
| `METHOD_NOT_ALLOWED` | 405 | Wrong HTTP method |
| `INVALID_REQUEST` | 400 | Malformed request body |
| `TAB_ID_REQUIRED` | 400 | Missing tabId |
| `METHOD_REQUIRED` | 400 | Missing CDP method |
| `INVALID_TAB_ID` | 400 | tabId is not a number |
| `TAB_NOT_FOUND` | 404 | Tab does not exist or not managed |
| `TAB_CLOSED` | 404 | Tab was closed during CDP execution |
| `CREATE_TAB_FAILED` | 500 | Tab creation error |
| `CLOSE_TAB_FAILED` | 500 | Tab close error |
| `LIST_TABS_FAILED` | 500 | Tab list error |
| `GET_TAB_FAILED` | 500 | Tab get error |
| `CDP_COMMAND_FAILED` | 500 | CDP command error |
| `INVALID_SINCE` | 400 | Bad `since` parameter |

---

## Concurrency (Go)

```go
type Hub struct {
    mu         sync.RWMutex  // guards client + pendingReq
    client     *Client
    pendingReq map[int]*pendingRequest
}
```

Rules:
- Never call external functions while holding the lock
- Acquire the lock to read the client reference, release the lock, then send

The extension service worker is single-threaded — all commands execute serially, no locking needed.

---

## Code Conventions

### Go
- Logger: `logrus`, prefix format `[Module]` (e.g. `[Hub]`, `[Tab]`)
- Errors: `WithError(err)` + `WithField(...)` for context
- API responses: `{success, data}` or `{success, error, code}`
- Locks: minimal granularity, never call externals while holding

### JavaScript
- Style: `async/await`, `try/catch` + `console.error`
- Log prefix: `[Relay]`, `[Tab]`, `[CDP]`, `[Debugger]`, `[TabChecker]`, `[Agent]`, `[Runner]`
- Tab state: `Map<tabId, tabInfo>`
- All extension commands routed through `dispatch()` in background.js

---

## Performance Characteristics

- Tab creation: ~500 ms (includes attach + domain enable)
- CDP command round-trip: 50 ms – several seconds (depends on command)
- Event store latency: < 10 ms (push to storage)
- Timeout check: every 5 s, low overhead
- Event GC: every 30 s

---

## Debugging

### Go server logs
```
[Server] HTTP server listening addr=:12345
[Hub] Chrome extension client registered
[Hub] Command sent id=1 method=tab.create
[Tab] Tab created url=https://example.com
[CDP] Forwarding command tabId=123 method=Page.navigate
```

### Extension logs
1. Open `chrome://extensions/`
2. Find the extension → click **Service Worker**
3. Check the Console

### Common Issues

| Symptom | Likely cause | Check |
|---------|-------------|-------|
| `EXTENSION_NOT_CONNECTED` | Extension not loaded or crashed | Service worker status in `chrome://extensions/` |
| CDP command timeout | Page hung or slow network | Increase `timeout` parameter |
| Tab returns 404 | Tab was idle-closed | Recreate the tab |
| Events query empty | Events older than 3 min | Shorten `since` window |
| History shows only final result | Old record without `events[]` | New sessions persist full event log |

---

## Document Index

- **[README.md](./README.md)** — Project overview and quick start
- **[API.md](./API.md)** — Full REST API reference
- **[CLAUDE.md](./CLAUDE.md)** — This file, architecture notes for AI assistants
- **[cli/agent.py](./cli/agent.py)** — Legacy Python CLI agent

---

**Last Updated**: 2026-04-16
**Version**: 3.2 (full English, events[] persisted, messages[] evicted from memory post-turn)
