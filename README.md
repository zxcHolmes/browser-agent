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

## What's in this repository

| Part | What it is | Docs |
|---|---|---|
| **Chrome extension** (`chrome-extension/`) | The core: tab registry, `chrome.debugger` bridge, and a built-in LLM agent with chat, scheduled tasks and history | [chrome-extension/README.md](chrome-extension/README.md) |
| **Browser Agent Proxy** (`browser-agent-proxy/`, Go, optional) | REST + WebSocket hub so external programs can drive Chrome through the extension | [API.md](API.md) |
| **Python CLIs** (`cli/`) | `agent.py`, an LLM agent over the proxy API; `youtube/`, YouTube Studio upload/publish; `twitter/`, posts on X with text, images or video; both without API keys; `common/`, the shared client and workflow modules for site CLIs | [cli/youtube](cli/youtube/README.md), [cli/twitter](cli/twitter/README.md), [cli/common](cli/common/README.md) |
| **Agent skill** (`skills/`) | Instructions that let an AI coding agent use the proxy | [SKILL.md](skills/browser-agent-skill/SKILL.md) |

## Quick start

**Built-in agent** — no server needed:

1. Open `chrome://extensions/` → enable **Developer mode**
2. **Load unpacked** → select the `chrome-extension/` folder
3. Click the extension icon → Settings → configure your LLM provider (OpenRouter, OpenAI, Ollama, or any OpenAI-compatible API)
4. Click **▶ Open Chat**

**From your own code** — start the proxy, then connect the extension to it:

```bash
cd browser-agent-proxy
make run            # or: go build -o browser-agent-proxy . && ./browser-agent-proxy --port 12345
curl -s http://127.0.0.1:12345/api/capabilities
```

In the extension: Settings → **Browser Agent Proxy** → set the port → **Connect**. Only tabs created through the API are controlled; your own tabs are never touched.

```
Your App (HTTP REST)
      ↓
Go Proxy Server (WebSocket hub + event store)
      ↓
Chrome Extension (tab registry + chrome.debugger)
      ↓
Chrome Browser
```

## Example

```
$ python3 cli/agent.py "Open Hacker News and return the top 5 story titles"

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

## Python CLIs

```bash
# LLM agent over the proxy API (needs `requests` and `python-dotenv`)
cp cli/.env.example cli/.env   # set OPENROUTER_URL, OPENROUTER_API_KEY, OPENROUTER_MODEL
python3 cli/agent.py "Open Hacker News and return the top 5 story titles"

# YouTube upload and publish through Studio's web UI (standard library only)
python3 -m cli.youtube upload --video video.mp4 --title "Title" --mode draft

# A post on X with text, images or a video (standard library only)
python3 -m cli.twitter post --text "Hello" --image photo.jpg
```

For most interactive use the extension's built-in chat is simpler than `agent.py`. Options, recovery model and exit codes are in [cli/youtube/README.md](cli/youtube/README.md) and [cli/twitter/README.md](cli/twitter/README.md); to build a CLI for another site, start from [cli/common/README.md](cli/common/README.md).

## Skills

Browser Agent skill for your AI coding agent:

```bash
npx skills add zxcHolmes/browser-agent
```

## Documentation

- **[chrome-extension/README.md](chrome-extension/README.md)** — extension install, settings, agent tools, permissions
- **[API.md](API.md)** — proxy REST API reference, semantic actions, errors, runnable examples
- **[cli/youtube/README.md](cli/youtube/README.md)** — YouTube upload CLI
- **[cli/twitter/README.md](cli/twitter/README.md)** — X (Twitter) posting CLI
- **[cli/common/README.md](cli/common/README.md)** — shared modules for site CLIs and how to add a platform
- **[TESTING.md](TESTING.md)** — test suites and what they cover
- **[CLAUDE.md](CLAUDE.md)** — architecture and design notes (also linked as `AGENTS.md` for AI coding agents)
