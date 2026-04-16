# Browser Agent

A Chrome Extension-based browser automation system that lets AI agents (and external applications) control your normal Chrome browser — no special launch flags required.

## Overview

```
External App / AI Agent (HTTP REST or natural language)
        ↓
Go Browser Agent Proxy  (WebSocket hub + event store)
        ↓
Chrome Extension     (tab registry + debugger bridge)
        ↓
Chrome Browser       (chrome.debugger API)
```

**Why a Chrome Extension?** Playwright/Puppeteer require Chrome to be launched with `--remote-debugging-port`, which breaks your normal browser session. The extension bridges the gap: it installs into your everyday Chrome and forwards commands from the relay server using `chrome.debugger`, which requires no special flags.

---

## Components

### 1. `browser-agent-proxy/` — Go HTTP / WebSocket Relay

A lightweight Go server that acts as the hub between external callers and the Chrome Extension.

| | |
|---|---|
| Language | Go 1.21 |
| Dependencies | `gorilla/websocket`, `logrus` |
| Default port | `12345` |

**Responsibilities**
- Accepts WebSocket connections from the Chrome Extension (`/extension`)
- Exposes a REST API for tab management, CDP command forwarding, and event queries
- Stores the last 3 minutes of CDP/system events in an in-memory time-series store (GC every 30 s)
- Routes REST requests to the extension and returns responses synchronously

**Key packages**

```
browser-agent-proxy/
├── main.go                  Entry point, CLI flags (--port, --log-level)
├── Makefile                 Build / run targets
└── pkg/
    ├── config/config.go     Server configuration struct
    ├── logger/logger.go     logrus logger factory
    ├── server/server.go     HTTP router, dependency wiring
    ├── relay/hub.go         WebSocket hub, pending-request map, event fanout
    ├── events/store.go      In-memory event store, 3-min TTL, 30-s GC
    └── handler/
        ├── tab.go           POST/GET/DELETE /api/tabs[/:id]
        ├── cdp.go           POST /api/cdp — raw CDP command forwarding
        ├── actions.go       /api/tabs/:id/navigate|screenshot|eval
        ├── events.go        GET /api/events — time-series query
        ├── websocket.go     WebSocket upgrade + read/write pump
        ├── health.go        GET / — health check
        └── response.go      writeSuccess / writeError helpers
```

**Build & run**

```bash
cd browser-agent-proxy
go build -o browser-agent-proxy .
./browser-agent-proxy --port 12345 --log-level info

# or with make
make run
```

**REST API summary** (see [API.md](./API.md) for full docs)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/tabs` | Create a managed tab |
| `GET` | `/api/tabs` | List managed tabs |
| `GET` | `/api/tabs/:id` | Get a single tab |
| `DELETE` | `/api/tabs/:id` | Close a tab |
| `POST` | `/api/tabs/:id/navigate` | Navigate to URL |
| `POST` | `/api/tabs/:id/screenshot` | Capture screenshot |
| `POST` | `/api/tabs/:id/eval` | Execute JavaScript |
| `POST` | `/api/cdp` | Send raw CDP command |
| `GET` | `/api/events` | Query event log |
| `GET` | `/` | Health check |

---

### 2. `chrome-extension/` — Browser Agent Chrome Extension

A Manifest V3 Chrome Extension that serves dual roles:

1. **Browser Agent Proxy bridge** — forwards commands from the Go server to Chrome via `chrome.debugger`
2. **Browser Agent** — a self-contained AI agent that runs LLM tool-calling loops directly inside the extension (no relay server needed)

**Key files**

```
chrome-extension/
├── manifest.json            MV3 manifest, permissions
├── background.js            Service worker: tab registry, relay bridge, session manager
├── chat.html / chat.js      Full-screen agent chat UI (session model, event replay)
├── options.html / options.js Settings page: LLM config, tasks, history, system prompt
└── agent/
    ├── tools.js             Browser tool implementations + schemas + system prompt
    ├── llm.js               OpenAI-compatible API client, context trimming
    ├── runner.js            Agent loop: tool dispatch, vision injection, tool-call limit
    └── scheduler.js         Task CRUD, chrome.alarms scheduling, run history
```

**Installing the extension**

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `chrome-extension/` directory

**Features**
- Supports OpenRouter, OpenAI, Ollama, and any OpenAI-compatible provider
- Configurable system prompt, max consecutive tool calls, tab idle timeout
- Scheduled tasks via `chrome.alarms` with next-run display
- Full conversation history stored in `chrome.storage.local` (24-hour TTL)
- Chat UI with Markdown rendering, screenshot preview, token usage display
- Multi-turn sessions with context preserved across turns (messages reloaded from disk after completion)

**Tools available to the agent**

| Tool | Description |
|------|-------------|
| `create_tab` | Open a new Chrome tab |
| `list_tabs` / `get_tab` / `close_tab` | Tab management |
| `navigate` | Go to a URL, wait for page load |
| `eval` | Execute JavaScript (read DOM, click, fill forms) |
| `screenshot` | Capture page as base64 image for visual inspection |
| `cdp` | Send any raw Chrome DevTools Protocol command |
| `fetch` | HTTP request without CORS restrictions |
| `sleep` | Wait N seconds |

---

### 3. `cli/` — Python CLI Agent (legacy)

A Python CLI that calls the Browser Agent Proxy REST API as tool functions and drives an LLM loop via OpenRouter/Ollama.

```bash
cd cli
cp .env.example .env   # set OPENROUTER_URL, OPENROUTER_API_KEY, OPENROUTER_MODEL
python agent.py "Open Hacker News and return the top 5 story titles"
python agent.py --model qwen/qwen3-235b-a22b "Go to example.com and return the page title"
```

> **Note**: The Chrome Extension's built-in agent (component 2 above) is the recommended approach. The CLI agent requires the Go relay server to be running.

---

## Tab Idle Timeout

Tabs opened by the agent (via either the extension or the relay server) are automatically closed after a configurable idle period (default: **1 minute**). The timeout is configurable in the extension's Settings page and applies to both the AI agent and the Browser Agent Proxy.

---

## Quick Start

1. Start the Go relay server (optional — only needed for external API access):
   ```bash
   cd browser-agent-proxy && make run
   ```
2. Install the Chrome Extension (load unpacked from `chrome-extension/`)
3. Open the extension options page, configure your LLM provider
4. Click **Open Chat** to start chatting with the browser agent

---

## Documentation

- **[API.md](./API.md)** — Full REST API reference
- **[CLAUDE.md](./CLAUDE.md)** — Architecture design notes for AI assistants
- **[chrome-extension/README.md](./chrome-extension/README.md)** — Extension-specific notes
