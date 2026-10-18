# Browser Agent — Chrome Extension

Runs an AI agent directly inside Chrome. No external server required. Give it a prompt and it will navigate pages, run JavaScript, take screenshots, fetch URLs, and report back — all from within the extension.

Optionally connects to the [Browser Agent Proxy](../API.md) (Go) so external applications can also control the browser via REST API.

## Features

- **In-extension LLM agent** — OpenRouter, OpenAI, or Ollama (any OpenAI-compatible API)
- **Full tool set** — navigate, eval JS, semantic actions (click, fill, upload, wait), screenshot + vision, fetch (no CORS), raw CDP commands, saved skills
- **Chat UI** — real-time conversation view with tool call bubbles and screenshot previews
- **Scheduled tasks** — repeat on a timer via `chrome.alarms`
- **History** — last 24 hours of runs, stored locally; reopen and continue any run
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

Options page → **Add New Task** → set a name, prompt, and interval (0 = manual only). Tasks fire via `chrome.alarms`, which wakes the extension on schedule even with no chat window open. Click **Run** on any task to open a chat window and run it now.

```
Name: Morning Briefing    Interval: 60 min
Prompt: Go to Hacker News, get the top 10 stories with scores, then go to my Notion dashboard and add them as a new entry.

Name: Price Monitor    Interval: 30 min
Prompt: Open amazon.com/dp/B0XXXXX, check the current price, and if it's below $49 send a POST to https://my-webhook.com/alert with the price in the body.
```

### Chat Window

- Live tool call and result bubbles as the agent works
- Screenshots are displayed inline
- **Stop** button aborts the run at any time
- Reopen a chat window for a background run via the **Active Runs** card or **History → View Chat**
- Continue any past conversation: the full message history, tool calls and screenshots are kept (24 hours)

## Settings

| Setting | Description |
|---------|-------------|
| **LLM Provider** | OpenRouter, OpenAI, Ollama, or Custom (any OpenAI-compatible endpoint) |
| **Model** | Any model supported by your provider |
| **API Key** | Stored locally in `chrome.storage.local`, sent only to your LLM endpoint |
| **Max tool calls** | Safety limit on consecutive tool invocations per turn (default 50, 0 = unlimited) |
| **System Prompt** | Fully customizable; leave empty to use the built-in default |
| **Tab idle timeout** | Auto-close agent-opened tabs after N minutes of inactivity (default 30; running operations are protected) |
| **Browser Agent Proxy** | Optional connection to the Go proxy server for external API access |

## Agent tools

| Tool | Description |
|------|-------------|
| `create_tab` / `list_tabs` / `get_tab` / `close_tab` | Manage the agent's own tabs |
| `navigate` | Go to a URL and wait for the page to load |
| `snapshot` | Page text and interactive elements (including open shadow DOM) with refs for the actions below |
| `click` / `fill` / `insert_text` / `press` / `select_option` / `set_checked` | Act on a unique element through real browser input, then verify the result |
| `wait_for` | Wait for an element, text or value; a timeout is an error |
| `upload_files` | Choose local files in an `input[type=file]` |
| `list_frames` / `handle_dialog` / `set_tab_policy` | iframes, JavaScript dialogs, keeping a tab open during long work |
| `eval` | Run JavaScript in the page |
| `screenshot` | Capture the page for visual inspection |
| `fetch` | HTTP requests without CORS restrictions |
| `cdp` | Any raw Chrome DevTools Protocol command |
| `save_skill` / `list_skills` / `get_skill` | Remember how a site's workflow was done and reuse it next time |
| `sleep` | Wait N seconds |

The same semantic actions are available to external programs through the proxy's `POST /api/tabs/{id}/action`; see [API.md](../API.md).

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
| `webNavigation` | Associate tabs a managed tab opens (popups, `target=_blank`) with their opener |
| `activeTab` | Act on the current tab when the user invokes the extension |
| `storage` / `unlimitedStorage` | Save settings, tasks, skills and history (including screenshots) |
| `<all_urls>` | Navigate to any URL |

## Architecture

```
options.html / chat.html
        ↓ chrome.runtime.sendMessage
background.js (service worker) — tab registry, sessions, WebSocket bridge to the Go proxy (optional)
  ├── agent/runner.js          — LLM loop + tool dispatch
  ├── agent/tools.js           — agent tools (tabs, navigate, eval, screenshot, fetch, cdp, skills)
  ├── agent/browser-actions.js — semantic actions shared with the proxy API (snapshot, click, fill, upload…)
  ├── agent/managed-children.js — ownership of tabs opened by managed tabs
  ├── agent/llm.js             — OpenAI-compatible API client + context trimming
  └── agent/scheduler.js       — task CRUD, chrome.alarms, history storage
```

## Debugging

1. Go to `chrome://extensions`.
2. Find **Browser Agent** → click **Service worker**.
3. Watch the Console for `[Agent]`, `[Relay]`, `[Tab]`, `[Debugger]` prefixed logs.
