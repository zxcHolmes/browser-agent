# Browser Agent

A Chrome Extension that gives AI agents full control of your everyday Chrome browser — no Puppeteer, no special launch flags, no separate browser instance.

[![Demo Video](https://img.youtube.com/vi/GE09e9LOW1g/maxresdefault.jpg)](https://www.youtube.com/watch?v=GE09e9LOW1g)

---

## Why Browser Agent?

Most browser automation tools (Playwright, Puppeteer) launch a **fresh, sterile browser instance**:

- No cookies → locked out of every site
- No saved sessions → blocked by login walls  
- Detectable bot fingerprints → anti-scraping triggers

**Browser Agent runs inside your everyday Chrome.** The extension reuses your existing cookies, active login sessions, and real browser fingerprint — the agent operates exactly as you would, bypassing most anti-bot measures transparently.

> Scrape sites you're already logged into. Automate internal dashboards. Post to social media. No authentication config needed.

---

## Chrome Extension

The extension is the core of the system. It runs entirely inside Chrome with no external dependencies — just install it, point it at your LLM provider, and start chatting.

### Quick Start

1. Open `chrome://extensions/` → enable **Developer mode**
2. Click **Load unpacked** → select the `chrome-extension/` folder
3. Click the extension icon → opens the Settings page
4. Configure your LLM provider (OpenRouter, OpenAI, Ollama, or any OpenAI-compatible API)
5. Click **▶ Open Chat** to start

### Chat Interface

An interactive chat window where you talk to the agent in natural language. The agent uses a tool-calling loop to autonomously browse, extract data, click, fill forms, and report back.

- Multi-turn conversations — the agent remembers context across messages
- Live tool-call feed — watch every `navigate`, `eval`, `screenshot` as it happens
- Vision fallback — when JavaScript extraction fails, the agent takes a screenshot and uses vision to recover
- Markdown rendering, screenshot previews, token usage display
- Abort at any time with the Stop button

### Scheduled Tasks

Automate recurring jobs without staying at the keyboard.

- Create named tasks with a natural language prompt
- Set an interval (e.g. every 60 minutes) or keep as manual-only
- Tasks fire via `chrome.alarms` — Chrome will wake the extension on schedule even if the tab is closed
- Enable/disable tasks individually; see the next scheduled run time
- Run any task immediately with **▶ Run**

**Example tasks:**
```
Name: Morning Briefing    Interval: 60 min
Prompt: Go to Hacker News, get the top 10 stories with scores, then go to my Notion dashboard and add them as a new entry.

Name: Price Monitor    Interval: 30 min  
Prompt: Open amazon.com/dp/B0XXXXX, check the current price, and if it's below $49 send a POST to https://my-webhook.com/alert with the price in the body.
```

### History

All agent runs are stored locally in `chrome.storage.local` (24-hour retention).

- Browse past runs with result summary, duration, and iteration count
- **View Chat** — reopen any run's full conversation, including all tool calls and screenshots
- Continue any past conversation — the full message history is preserved so the agent has complete context
- Delete individual entries or clear all history

### Settings

| Setting | Description |
|---------|-------------|
| **LLM Provider** | OpenRouter, OpenAI, Ollama, or Custom (any OpenAI-compatible endpoint) |
| **Model** | Any model supported by your provider |
| **API Key** | Stored locally in `chrome.storage.local`, never sent anywhere except your LLM endpoint |
| **Max tool calls** | Safety limit on consecutive tool invocations per turn (default: 50, 0 = unlimited) |
| **System Prompt** | Fully customizable; leave empty to use the built-in default |
| **Tab idle timeout** | Auto-close agent-opened tabs after N minutes of inactivity (default: 1 min) |
| **Browser Agent Proxy** | Optional connection to the Go proxy server for external API access |

### Agent Tools

| Tool | Description |
|------|-------------|
| `create_tab` | Open a new Chrome tab |
| `navigate` | Go to a URL and wait for the page to load |
| `eval` | Run JavaScript in the page — read DOM, click elements, fill forms, extract data |
| `screenshot` | Capture the page as an image for visual inspection |
| `fetch` | Make HTTP requests without CORS restrictions |
| `list_tabs` / `get_tab` / `close_tab` | Manage open tabs |
| `cdp` | Send any raw Chrome DevTools Protocol command |
| `sleep` | Wait N seconds |

---

## Example

```
$ python agent.py "Open Hacker News and return the top 5 story titles"

[agent] model=gemma4:26b
[agent] prompt: Open Hacker News and return the top 5 story titles

[agent] iteration 1 — calling model...
  [tool call] create_tab({"url": "https://news.ycombinator.com/"})
  [tool result] {"tabId": 988465209, "isAttached": true, ...}

[agent] iteration 2 — calling model...
  [tool call] eval({"expression": "Array.from(document.querySelectorAll('.titleline > a')).slice(0, 5).map(a => a.innerText)", "tab_id": 988465209})
  [tool result] []

[agent] iteration 3 — calling model...
  [tool call] screenshot({"tab_id": 988465209})
  [tool result] {"path": "/tmp/tab988465209.png", "size_bytes": 532916}

[agent] iteration 4 — calling model...
  [tool call] read_image({"path": "/tmp/tab988465209.png"})
  [tool result] {"ok": true, "injected": true}

[agent] iteration 5 — calling model...
  [tool call] eval({"expression": "Array.from(document.querySelectorAll('.titleline > a')).slice(0, 5).map(a => a.innerText)", "tab_id": 988465209})
  [tool result] ["Darkbloom – Private inference on idle Macs", "Stop Using Ollama", "IPv6 traffic crosses the 50% mark", ...]

[agent] iteration 6 — calling model...
[agent] finished after 6 iteration(s)

FINAL ANSWER:
1. Darkbloom – Private inference on idle Macs
2. Stop Using Ollama
3. IPv6 traffic crosses the 50% mark
4. FSF trying to contact Google about spammer sending 10k+ mails from Gmail account
5. RedSun: System user access on Win 11/10 and Server with the April 2026 Update
```

The agent took a screenshot when JavaScript returned empty (page not yet rendered), used vision to understand the layout, then retried — all without human intervention.

---

## Browser Agent Proxy (optional)

A lightweight Go server that exposes a REST API so **external applications** can control Chrome through the extension. Only needed if you want programmatic access from outside the browser (e.g. the Python CLI agent, your own scripts).

```
Your App (HTTP REST)
      ↓
Go Proxy Server (WebSocket hub + event store)
      ↓
Chrome Extension (tab registry + chrome.debugger)
      ↓
Chrome Browser
```

**Build & run**

```bash
cd browser-agent-proxy
make run
# or: go build -o browser-agent-proxy . && ./browser-agent-proxy --port 12345
```

Then in the extension Settings → **Browser Agent Proxy** → set the port and click **Connect**.

**REST API summary** (see [API.md](./API.md) for full reference)

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

---

## Python CLI Agent (legacy)

A command-line agent that drives the LLM loop via the Proxy REST API.

```bash
cd cli
cp .env.example .env   # set OPENROUTER_URL, OPENROUTER_API_KEY, OPENROUTER_MODEL
python agent.py "Open Hacker News and return the top 5 story titles"
python agent.py --model qwen/qwen3-235b-a22b "Go to example.com and return the page title"
```

Requires the Browser Agent Proxy to be running. The Chrome Extension's built-in chat is the recommended approach for most use cases.

---

## Documentation

- **[API.md](./API.md)** — Full REST API reference for the Browser Agent Proxy
- **[CLAUDE.md](./CLAUDE.md)** — Architecture and design notes
