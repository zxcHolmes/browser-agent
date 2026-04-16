# Browser Agent Proxy API Documentation

## Overview

The Browser Agent Proxy provides a REST API to control Chrome browser tabs via a Chrome Extension bridge. Only tabs created through this API are managed — tabs opened by the user are not visible or controllable.

**Base URL**: `http://localhost:12345`

**Unified response format**:
```json
{ "success": true, "data": { ... } }
{ "success": false, "error": "...", "code": "ERROR_CODE" }
```

---

## API Endpoints

### 1. Tab Management

#### 1.1 Create Tab

```http
POST /api/tabs
```

**Request Body**:
```json
{
  "url": "https://example.com",
  "active": false,
  "meta": {
    "taskId": "task-123",
    "userId": "user-456"
  }
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | string | No | `"about:blank"` | URL to open |
| `active` | boolean | No | `false` | Whether to bring the tab to foreground |
| `meta` | object | No | `{}` | Arbitrary metadata stored in extension memory |

**Response** (200):
```json
{
  "success": true,
  "data": {
    "tab": {
      "tabId": 123,
      "targetId": "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
      "url": "https://example.com",
      "title": "",
      "meta": { "taskId": "task-123" },
      "isAttached": true,
      "lastActivity": 1700000000000,
      "createdAt": 1700000000000
    }
  }
}
```

**Error responses**:
- `503 EXTENSION_NOT_CONNECTED` — extension not connected
- `500 CREATE_TAB_FAILED` — chrome failed to create the tab

---

#### 1.2 List Tabs

Returns only tabs managed by this extension.

```http
GET /api/tabs
```

**Response** (200):
```json
{
  "success": true,
  "data": {
    "tabs": [ { ...tab... }, { ...tab... } ],
    "count": 2
  }
}
```

**Error responses**:
- `503 EXTENSION_NOT_CONNECTED`
- `500 LIST_TABS_FAILED`

---

#### 1.3 Get Tab

```http
GET /api/tabs/{tabId}
```

**Response** (200):
```json
{
  "success": true,
  "data": {
    "tab": { ...tab... }
  }
}
```

**Error responses**:
- `404 TAB_NOT_FOUND` — tab does not exist or is not managed by this extension
- `500 GET_TAB_FAILED`

---

#### 1.4 Delete Tab

Closes and removes the tab. Only works on managed tabs.

```http
DELETE /api/tabs/{tabId}
```

**Response** (200):
```json
{
  "success": true,
  "data": {
    "success": true,
    "tabId": 123
  }
}
```

**Error responses**:
- `404 TAB_NOT_FOUND`
- `503 EXTENSION_NOT_CONNECTED`
- `500 CLOSE_TAB_FAILED`

---

### 2. High-Level Actions

High-level APIs that combine multiple CDP calls into single convenient operations.

---

#### 2.1 Navigate

Navigate a tab to a URL and wait until the page finishes loading.

```http
POST /api/tabs/{tabId}/navigate
```

**Request Body**:
```json
{
  "url": "https://example.com",
  "waitForLoad": true,
  "timeoutSeconds": 30
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | string | Yes | — | URL to navigate to |
| `waitForLoad` | boolean | No | `true` | Block until `Page.loadEventFired` or timeout |
| `timeoutSeconds` | integer | No | `30` | Overall timeout in seconds |

**Response** (200):
```json
{
  "success": true,
  "data": {
    "frameId": "00EBC7D6D11A7401A71473A1DDF99996",
    "loaderId": "FE0AAD7DDDE274652FB76864EC06D6C5",
    "isDownload": false,
    "loadEventFired": true
  }
}
```

`loadEventFired: false` means the page did not fire a load event within the timeout — navigation still happened, the page was just slow or never completed loading.

**Error responses**:
- `400 URL_REQUIRED` — `url` field missing
- `404 TAB_NOT_FOUND`
- `404 TAB_CLOSED`
- `500 NAVIGATE_FAILED`
- `503 EXTENSION_NOT_CONNECTED`

**vs. `POST /api/cdp`**:

`Page.navigate` via `/api/cdp` only fires the navigation, returning immediately. The caller must separately poll `/api/events?method=Page.loadEventFired` to know when the page is ready. `/api/tabs/{tabId}/navigate` does both in one call.

---

#### 2.2 Screenshot

Capture a screenshot and return it as a binary image.

```http
GET /api/tabs/{tabId}/screenshot
GET /api/tabs/{tabId}/screenshot?format=jpeg&quality=80
```

**Query Parameters**:

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `format` | string | `png` | `png` or `jpeg` |
| `quality` | integer | `80` | JPEG quality 1–100 (ignored for PNG) |

**Response** (200):

Raw image bytes with `Content-Type: image/png` or `Content-Type: image/jpeg`.

**Error responses**:
- `404 TAB_NOT_FOUND`
- `404 TAB_CLOSED`
- `500 SCREENSHOT_FAILED`
- `503 EXTENSION_NOT_CONNECTED`

**vs. `POST /api/cdp`**:

`Page.captureScreenshot` via `/api/cdp` returns `{ data: "<base64>" }`. The caller must base64-decode it. This endpoint decodes it and returns raw bytes directly — useful when feeding the image to a vision model or saving to disk.

---

#### 2.3 Eval

Execute JavaScript in the tab and return the result.

```http
POST /api/tabs/{tabId}/eval
```

**Request Body**:
```json
{
  "expression": "document.title",
  "awaitPromise": false,
  "timeoutSeconds": 30
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `expression` | string | Yes | — | JavaScript expression to evaluate |
| `awaitPromise` | boolean | No | `false` | If `true`, await the returned Promise |
| `timeoutSeconds` | integer | No | `30` | Timeout in seconds |

**Response** (200):
```json
{
  "success": true,
  "data": {
    "type": "string",
    "value": "Example Domain"
  }
}
```

`type` reflects the JavaScript type (`string`, `number`, `boolean`, `object`, `undefined`, etc.).

**JavaScript exception** (422):
```json
{
  "success": false,
  "error": "javascript exception",
  "code": "JS_EXCEPTION",
  "data": {
    "type": "undefined",
    "exception": { ... }
  }
}
```

**Error responses**:
- `400 EXPRESSION_REQUIRED` — `expression` field missing
- `404 TAB_NOT_FOUND`
- `404 TAB_CLOSED`
- `422 JS_EXCEPTION` — expression threw a JavaScript exception
- `500 EVAL_FAILED`
- `503 EXTENSION_NOT_CONNECTED`

**vs. `POST /api/cdp`**:

`Runtime.evaluate` via `/api/cdp` requires remembering to set `returnByValue: true`, and the result is nested as `{ result: { type, value } }`. This endpoint always sets `returnByValue: true` and unwraps the result to `{ type, value }` directly.

---

### 4. CDP Command

Send any Chrome DevTools Protocol command to a tab.

```http
POST /api/cdp
```

**Request Body**:
```json
{
  "tabId": 123,
  "method": "Page.navigate",
  "params": {
    "url": "https://example.com"
  },
  "timeout": 30
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `tabId` | integer | Yes | — | Target tab ID |
| `method` | string | Yes | — | CDP method name |
| `params` | object | No | `{}` | CDP method parameters |
| `timeout` | integer | No | `30` | Timeout in seconds |

**Behavior**:
- If the tab is not attached, it is **automatically attached** before executing
- Each successful command **refreshes the 3-minute inactivity timer**
- If the tab was already closed, returns 404

**Response** (200):
```json
{
  "success": true,
  "data": {
    "result": {
      "frameId": "...",
      "loaderId": "..."
    }
  }
}
```

**Error responses**:
- `400 TAB_ID_REQUIRED` — tabId missing
- `400 METHOD_REQUIRED` — method missing
- `404 TAB_NOT_FOUND` — tab not managed by this extension
- `404 TAB_CLOSED` — tab was closed during or before the command
- `503 EXTENSION_NOT_CONNECTED`
- `500 CDP_COMMAND_FAILED` — chrome rejected the command

---

### 5. Events

Query events pushed from the extension. Events are kept for **3 minutes** and then discarded.

```http
GET /api/events
GET /api/events?tabId=123
GET /api/events?tabId=123&since=1700000000000
GET /api/events?tabId=123&method=Page.loadEventFired
GET /api/events?tabId=123&method=Page.&method=Network.
```

**Query Parameters**:

| Param | Type | Repeatable | Description |
|-------|------|------------|-------------|
| `tabId` | integer | No | Filter by tab ID. Omit for all tabs. |
| `since` | int64 | No | Unix milliseconds. Return only events received after this time. |
| `method` | string | Yes | Filter by event method. Supports exact match and domain prefix. |

**`method` filter rules**:
- **Exact match**: `method=Page.loadEventFired` — only that specific event
- **Domain prefix**: `method=Page.` — all `Page.*` events
- **Multiple**: `method=Page.&method=Network.` — repeatable, OR logic

**Common filter examples**:
```
# Wait for page load
?method=Page.loadEventFired

# All page lifecycle events
?method=Page.

# Network + Page events together
?method=Page.&method=Network.

# Tab closed (system event)
?method=tab.closed

# Incremental polling (only new events since last check)
?tabId=123&method=Page.loadEventFired&since=1700000000000
```

**Response** (200):
```json
{
  "success": true,
  "data": {
    "events": [
      {
        "tabId": 123,
        "eventMethod": "Page.loadEventFired",
        "eventParams": { "timestamp": 1234567890.123 },
        "timestamp": 1700000000123,
        "receivedAt": "2026-04-13T10:00:00.123Z"
      },
      {
        "tabId": 123,
        "eventMethod": "tab.closed",
        "eventParams": { "tabId": 123, "reason": "timeout" },
        "timestamp": 1700000180000,
        "receivedAt": "2026-04-13T10:03:00.001Z"
      }
    ],
    "count": 2
  }
}
```

**Event fields**:

| Field | Description |
|-------|-------------|
| `tabId` | Which tab the event belongs to |
| `eventMethod` | CDP event name (e.g. `Page.loadEventFired`) or system event (see below) |
| `eventParams` | Event payload |
| `timestamp` | Unix ms from the extension (when the event occurred) |
| `receivedAt` | Server time when the event was stored |

**System events** (non-CDP):

| eventMethod | Description |
|-------------|-------------|
| `tab.closed` | Tab was closed. `reason`: `"timeout"` / `"api"` / `"user"` |
| `debugger.detached` | Debugger was forcibly detached (e.g. user opened DevTools) |

**Error responses**:
- `400 INVALID_TAB_ID`
- `400 INVALID_SINCE`

---

## Tab Lifecycle

```
POST /api/tabs  →  Tab created & auto-attached
                        │
                        ▼
              POST /api/cdp  →  Command executed, lastActivity refreshed
                        │
                        ▼
             [3 minutes of no activity]
                        │
                        ▼
              Tab auto-closed by extension
              event: tab.closed (reason=timeout) stored in event store
                        │
                        ▼
              POST /api/cdp  →  404 TAB_CLOSED
              (caller must recreate the tab)
```

---

## Tab Auto-Close

The extension checks all managed tabs **every 5 seconds**. If a tab's last activity is more than **3 minutes** ago, the extension:

1. Calls `chrome.debugger.detach`
2. Calls `chrome.tabs.remove`
3. Pushes `tab.closed` event (reason=`"timeout"`) to the server event store

The `lastActivity` timestamp is updated on:
- Any `POST /api/cdp` command execution
- Any CDP event received from Chrome for that tab

---

## CDP Command Examples

### Navigate

```bash
curl -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "method": "Page.navigate", "params": {"url": "https://example.com"}}'
```

### Execute JavaScript

```bash
curl -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "method": "Runtime.evaluate", "params": {"expression": "document.title", "returnByValue": true}}'
```

### Click element

```bash
curl -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "method": "Runtime.evaluate", "params": {"expression": "document.querySelector(\"button\").click(); true;", "returnByValue": true}}'
```

### Take screenshot

```bash
curl -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "method": "Page.captureScreenshot", "params": {"format": "png"}}'
```

### Get cookies

```bash
curl -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d '{"tabId": 123, "method": "Network.getCookies", "params": {}}'
```

---

## Complete Workflow Example

```bash
# 1. Create a background tab
TAB=$(curl -s -X POST http://localhost:12345/api/tabs \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "meta": {"job": "scrape-001"}}')
TAB_ID=$(echo $TAB | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['tab']['tabId'])")

# 2. Navigate and wait (poll for Page.loadEventFired event)
curl -s -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d "{\"tabId\": $TAB_ID, \"method\": \"Page.navigate\", \"params\": {\"url\": \"https://example.com/page\"}}"

# 3. Poll events until page load
curl -s "http://localhost:12345/api/events?tabId=$TAB_ID"

# 4. Execute script
curl -s -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d "{\"tabId\": $TAB_ID, \"method\": \"Runtime.evaluate\", \"params\": {\"expression\": \"document.querySelector('h1').innerText\", \"returnByValue\": true}}"

# 5. Screenshot
curl -s -X POST http://localhost:12345/api/cdp \
  -H "Content-Type: application/json" \
  -d "{\"tabId\": $TAB_ID, \"method\": \"Page.captureScreenshot\"}"

# 6. Close tab
curl -s -X DELETE http://localhost:12345/api/tabs/$TAB_ID
```

---

## Error Codes Reference

| Code | HTTP | Description |
|------|------|-------------|
| `EXTENSION_NOT_CONNECTED` | 503 | Chrome extension is not connected |
| `METHOD_NOT_ALLOWED` | 405 | HTTP method not allowed |
| `INVALID_REQUEST` | 400 | Malformed request body |
| `TAB_ID_REQUIRED` | 400 | `tabId` is missing |
| `METHOD_REQUIRED` | 400 | CDP `method` is missing |
| `URL_REQUIRED` | 400 | `url` is missing (navigate) |
| `EXPRESSION_REQUIRED` | 400 | `expression` is missing (eval) |
| `INVALID_TAB_ID` | 400 | `tabId` is not a valid integer |
| `INVALID_SINCE` | 400 | `since` is not a valid Unix ms integer |
| `TAB_NOT_FOUND` | 404 | Tab does not exist or is not managed by this extension |
| `TAB_CLOSED` | 404 | Tab was closed (timed out or user closed it) |
| `JS_EXCEPTION` | 422 | JavaScript expression threw an exception |
| `CREATE_TAB_FAILED` | 500 | Failed to create tab in Chrome |
| `CLOSE_TAB_FAILED` | 500 | Failed to close tab |
| `LIST_TABS_FAILED` | 500 | Failed to retrieve tab list |
| `GET_TAB_FAILED` | 500 | Failed to get tab info |
| `CDP_COMMAND_FAILED` | 500 | CDP command was rejected by Chrome |
| `NAVIGATE_FAILED` | 500 | Navigation failed |
| `SCREENSHOT_FAILED` | 500 | Screenshot capture failed |
| `EVAL_FAILED` | 500 | JavaScript evaluation failed |

---

## Notes

1. **Managed tabs only**: This API only manages tabs created through `POST /api/tabs`. Tabs opened manually by the user are not accessible.

2. **Auto-close after 3 minutes**: Tabs with no CDP activity for 3 minutes are automatically closed by the extension. Always handle `404 TAB_CLOSED` and recreate the tab if needed.

3. **Auto-attach**: `POST /api/cdp` automatically attaches the debugger if needed. No manual attach step required.

4. **Event polling**: Use `GET /api/events?tabId=X&since=T` to poll for events like `Page.loadEventFired` to know when a navigation completes.

5. **Page-level CDP only**: Browser-level commands (`Browser.close`, `Target.createTarget` etc.) are not supported. Use the Tab API instead.

6. **CDP reference**: Full list of CDP methods and parameters: https://chromedevtools.github.io/devtools-protocol/
