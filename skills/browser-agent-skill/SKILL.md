---
name: browser-agent-skill
description: Browser automation via the Browser Agent Proxy REST API — tab management, CDP commands, navigation, screenshot, JS eval, and event polling.
---

# Browser Agent Proxy — How to Control Chrome

## What it is

A Go server + Chrome Extension that lets you control a normal Chrome browser via a simple REST API. No `--remote-debugging-port` or Playwright required.

```
Your Code (HTTP REST)
      ↓
Go Server  (localhost:12345)
      ↓
Chrome Extension  (chrome.debugger bridge)
      ↓
Chrome Browser
```

**Important constraint**: Only tabs created through `POST /api/tabs` are managed. Tabs the user opens manually are invisible to the API.

---

## Base URL

```
http://localhost:12345
```

All responses follow this envelope:

```json
{ "success": true, "data": { ... } }
{ "success": false, "error": "...", "code": "ERROR_CODE" }
```

---

## Core Workflow

Every automation follows the same pattern:

1. **Create a tab** → get `tabId`
2. **Navigate** to a URL
3. **Wait for load** (poll events or use the `/navigate` shortcut)
4. **Interact** (JS eval, click, type, screenshot, raw CDP)
5. **Close the tab**

---

## Tab Management

### Create a tab

```http
POST /api/tabs
```

```json
{ "url": "https://example.com", "active": false, "meta": { "job": "scrape-001" } }
```

Returns the tab object with `tabId` — keep this, it's the key to everything else.

### List / get / close tabs

```http
GET    /api/tabs          # list all managed tabs
GET    /api/tabs/{tabId}  # get one tab
DELETE /api/tabs/{tabId}  # close a tab
```

---

## High-Level Actions (recommended)

These combine multiple CDP calls so you don't have to.

### Navigate and wait for load

```http
POST /api/tabs/{tabId}/navigate
```

```json
{ "url": "https://example.com", "waitForLoad": true, "timeoutSeconds": 30 }
```

Blocks until `Page.loadEventFired` fires — no manual event polling needed.

### Screenshot (raw bytes)

```http
GET /api/tabs/{tabId}/screenshot
GET /api/tabs/{tabId}/screenshot?format=jpeg&quality=80
```

Returns raw image bytes (`Content-Type: image/png` or `image/jpeg`) — ready to pass to a vision model or save to disk.

### Eval (JavaScript)

```http
POST /api/tabs/{tabId}/eval
```

```json
{ "expression": "document.title", "awaitPromise": false, "timeoutSeconds": 30 }
```

Returns:
```json
{ "type": "string", "value": "Example Domain" }
```

Always sets `returnByValue: true` and unwraps the result — simpler than raw CDP.

---

## Raw CDP Commands

For anything not covered by the high-level actions:

```http
POST /api/cdp
```

```json
{
  "tabId": 123,
  "method": "Network.getCookies",
  "params": {},
  "timeout": 30
}
```

The debugger is auto-attached on the first command. Every successful command resets the 3-minute idle timer.

**Common CDP methods**:
- `Page.navigate` — fire navigation (no load-wait; use `/navigate` or poll events instead)
- `Page.captureScreenshot` — base64 PNG/JPEG (use `/screenshot` for raw bytes)
- `Runtime.evaluate` — run JS (use `/eval` for simpler interface)
- `Input.dispatchKeyEvent` — send keystrokes
- `Network.getCookies` / `Network.setCookie`
- `DOM.querySelector`, `DOM.getDocument`

---

## Event Polling

CDP events and system events are stored server-side for 3 minutes. Query them to know when things happen:

```http
GET /api/events?tabId=123&method=Page.loadEventFired&since=1700000000000
```

| Param | Description |
|-------|-------------|
| `tabId` | Filter by tab (omit for all tabs) |
| `method` | Exact match or domain prefix (`Page.`, `Network.`). Repeatable (OR logic) |
| `since` | Unix ms — return only events newer than this |

**System events** (non-CDP):

| eventMethod | Description |
|-------------|-------------|
| `tab.closed` | Tab closed. `reason`: `"timeout"` / `"api"` / `"user"` |
| `debugger.detached` | DevTools was opened by user, detaching debugger |

---

## Tab Idle Timeout

Tabs with no CDP activity for **3 minutes** are automatically closed by the extension. When a tab times out:
- Extension fires `tab.closed` event (reason=`"timeout"`)
- Subsequent CDP calls return `404 TAB_CLOSED`
- Caller must recreate the tab

The timeout is configurable in extension options (`tabTimeoutMinutes`).

---

## Python Helper Pattern

```python
import json, time, requests

BASE = "http://localhost:12345"

def create_tab(url, meta=None):
    r = requests.post(f"{BASE}/api/tabs", json={"url": url, "active": True, "meta": meta or {}})
    r.raise_for_status()
    return r.json()["data"]["tab"]

def close_tab(tab_id):
    requests.delete(f"{BASE}/api/tabs/{tab_id}")

def cdp(tab_id, method, params=None, timeout=30):
    r = requests.post(f"{BASE}/api/cdp", json={"tabId": tab_id, "method": method, "params": params or {}, "timeout": timeout})
    r.raise_for_status()
    return r.json()["data"].get("result", {})

def evaluate(tab_id, expression, timeout=30):
    result = cdp(tab_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, timeout=timeout)
    return result.get("result", {}).get("value")

def navigate(tab_id, url, timeout=30):
    """Navigate and wait for load event."""
    r = requests.post(f"{BASE}/api/tabs/{tab_id}/navigate", json={"url": url, "waitForLoad": True, "timeoutSeconds": timeout})
    r.raise_for_status()
    return r.json()["data"]

def wait_for_load(tab_id, max_wait=15):
    """Poll events until Page.loadEventFired."""
    start_ms = int(time.time() * 1000)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/events", params={"tabId": tab_id, "method": "Page.loadEventFired", "since": start_ms})
        if r.json()["data"]["count"] > 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Tab {tab_id} did not load within {max_wait}s")

def type_into(tab_id, selector, text):
    """Clear a field and type text character by character."""
    evaluate(tab_id, f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            el.focus(); el.value = '';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})()
    """)
    for ch in text:
        cdp(tab_id, "Input.dispatchKeyEvent", {"type": "keyDown", "text": ch})
        cdp(tab_id, "Input.dispatchKeyEvent", {"type": "keyUp",   "text": ch})
    evaluate(tab_id, f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            el.dispatchEvent(new Event('input',  {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})()
    """)
```

### Full example — scrape a page

```python
tab = create_tab("https://news.ycombinator.com", meta={"job": "scrape-hn"})
tab_id = tab["tabId"]
try:
    wait_for_load(tab_id)
    stories = json.loads(evaluate(tab_id, """
        JSON.stringify([...document.querySelectorAll('tr.athing')].map(row => ({
            title: row.querySelector('.titleline > a')?.innerText,
            url:   row.querySelector('.titleline > a')?.href,
        })))
    """))
    print(json.dumps(stories[:5], indent=2))
finally:
    close_tab(tab_id)
```

### Full example — fill a form

```python
tab = create_tab("https://example.com/form")
tab_id = tab["tabId"]
try:
    wait_for_load(tab_id)
    type_into(tab_id, 'input[name="title"]', "My Title")
    type_into(tab_id, 'textarea[name="body"]', "My message body")
    # screenshot to verify before submitting
    import base64
    shot = cdp(tab_id, "Page.captureScreenshot", {"format": "png"})
    open("/tmp/form.png", "wb").write(base64.b64decode(shot["data"]))
    # evaluate(tab_id, "document.querySelector('button[type=submit]').click()")
finally:
    close_tab(tab_id)
```

---

## Error Codes

| Code | HTTP | Meaning |
|------|------|---------|
| `EXTENSION_NOT_CONNECTED` | 503 | Extension not loaded or crashed |
| `TAB_NOT_FOUND` | 404 | Tab doesn't exist or wasn't created via API |
| `TAB_CLOSED` | 404 | Tab timed out or was closed — recreate it |
| `CDP_COMMAND_FAILED` | 500 | Chrome rejected the CDP command |
| `JS_EXCEPTION` | 422 | JavaScript expression threw |
| `INVALID_REQUEST` | 400 | Bad request body |
| `TAB_ID_REQUIRED` | 400 | Missing tabId |
| `METHOD_REQUIRED` | 400 | Missing CDP method |
| `URL_REQUIRED` | 400 | Missing url (navigate) |
| `EXPRESSION_REQUIRED` | 400 | Missing expression (eval) |

---

