# Browser Agent — Chrome Extension

Runs an AI agent directly inside Chrome. No external server required. Give it a prompt and it will navigate pages, run JavaScript, take screenshots, fetch URLs, and report back — all from within the extension.

Optionally connects to the [Browser Agent Proxy](../README.md) (Go) so external applications can also control the browser via REST API.

## Features

- **In-extension LLM agent** — OpenRouter, OpenAI, or Ollama (any OpenAI-compatible API)
- **Full tool set** — navigate, eval JS, screenshot + vision, fetch (no CORS), raw CDP commands
- **Chat UI** — real-time conversation view with tool call bubbles and screenshot previews
- **Scheduled tasks** — repeat on a timer via `chrome.alarms`
- **History** — last 24 hours of runs, stored locally
- **Browser Agent Proxy bridge** — optional WebSocket bridge to the Go relay server (preserved from v1)

## Install (load unpacked)

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select this directory (`chrome-extension/`).
3. Pin the extension icon for easy access.

## Setup

1. Open the extension **Options** page (right-click icon → Options, or click the icon).
2. Go to **Settings** → set your LLM provider, base URL, model, and API key.
3. Click **Test Connection** to verify.

Supported providers:

| Provider | Base URL | Notes |
|----------|----------|-------|
| OpenRouter | `https://openrouter.ai/api/v1` | Requires API key |
| OpenAI | `https://api.openai.com/v1` | Requires API key |
| Ollama | `http://localhost:11434/v1` | No key needed |
| Custom | Any OpenAI-compatible URL | — |

## Usage

### Quick Run

Options page → type a prompt → **Open Chat**. A chat window opens and the agent starts immediately.

### Scheduled Tasks

Options page → **Add New Task** → set a name, prompt, and interval (0 = manual only). Click **Run** on any task to open a chat window and run it now.

### Chat Window

- Live tool call and result bubbles as the agent works
- Screenshots are displayed inline
- **Stop** button aborts the run at any time
- Reopen a chat window for a background run via the **Active Runs** card or **History → View Chat**

## Browser Agent Proxy (optional)

To also allow external apps to control Chrome via REST API, run the Go relay server and set the port in **Settings → Browser Agent Proxy**. The extension will maintain a WebSocket connection to the relay alongside the agent subsystem.

Default relay port: `12345`.

## Permissions

| Permission | Reason |
|------------|--------|
| `tabs` | Create and manage tabs |
| `debugger` | Attach Chrome DevTools Protocol to tabs |
| `alarms` | Drive scheduled task execution |
| `scripting` | Inject scripts when needed |
| `storage` | Save settings, tasks, and history |
| `<all_urls>` | Navigate to any URL |

## Architecture

```
options.html / chat.html
        ↓ chrome.runtime.sendMessage
background.js (service worker)
  ├── agent/runner.js     — LLM loop + tool dispatch
  ├── agent/tools.js      — browser tools (navigate, eval, screenshot, fetch, cdp…)
  ├── agent/llm.js        — OpenAI-compatible API client + context trimming
  ├── agent/scheduler.js  — task CRUD, chrome.alarms, history storage
  └── relay/              — CDP WebSocket bridge to Go relay server (optional)
```

## Debugging

1. Go to `chrome://extensions`.
2. Find **Browser Agent** → click **Service worker**.
3. Watch the Console for `[Agent]`, `[Relay]`, `[Tab]`, `[Debugger]` prefixed logs.
