# Browser Agent Proxy API

Control normal Chrome through a local Go server and the Browser Agent extension.
No remote-debugging launch flags are required. This reference describes protocol
2 and extension 2.1.5 (implementation revision 9).

Base URL: `http://127.0.0.1:12345`.

The extension implements semantic browser operations shared by the Go API and
its built-in Agent. Go exposes HTTP control, validates upload paths, correlates
requests, and retains recent browser events. Platform-specific publishing
workflows still need to define their fields, processing states and success evidence.

## Start and check the loaded extension

```bash
cd browser-agent-proxy
go run .
curl -sS http://127.0.0.1:12345/api/capabilities
```

The extension connects/reconnects automatically. Reload it after changing extension
files. Version 2.1.0 declares `webNavigation` to associate newly opened windows with
their source tab. Chrome may request acknowledgement of the updated permissions.
Check the **loaded** capabilities instead of inferring them from files on disk:

```json
{
  "success": true,
  "data": {
    "protocolVersion": 2,
    "implementationRevision": 9,
    "navigationTracking": true,
    "managedTabsOnly": true,
    "defaultIdleTimeoutMinutes": 30,
    "actions": ["snapshot", "click", "fill", "insert_text", "press", "select_option", "set_checked", "wait_for", "upload_files", "set_tab_policy", "list_frames", "handle_dialog"]
  }
}
```

`GET /` serves HTML API documentation. `HEAD /` checks Go availability only;
it does not establish that the extension is connected.

## Responses and endpoints

JSON endpoints normally return:

```json
{"success":true,"data":{"filled":true,"value":"New title"}}
```

```json
{"success":false,"error":"WAIT_TIMEOUT: ...","code":"BROWSER_ACTION_FAILED"}
```

Check both HTTP status and `success`. `/screenshot` returns image bytes instead.
Some unsupported-method responses on the tab collection route are plain HTTP errors.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/capabilities` | Loaded extension actions and configuration |
| POST | `/api/tabs` | Create a managed tab |
| GET | `/api/tabs` | List managed tabs |
| GET | `/api/tabs/{id}` | Inspect a managed tab |
| DELETE | `/api/tabs/{id}` | Close a managed tab |
| POST | `/api/tabs/{id}/release` | Stop management and leave the page open |
| POST | `/api/tabs/{id}/action` | Shared semantic browser action |
| POST | `/api/tabs/{id}/navigate` | Navigation with optional load-event wait |
| GET | `/api/tabs/{id}/screenshot` | PNG/JPEG image bytes |
| POST | `/api/tabs/{id}/eval` | JavaScript evaluation in the root page |
| POST | `/api/cdp` | CDP command supported by `chrome.debugger` |
| GET | `/api/events` | Recent CDP and lifecycle events |

## Managed tabs and ownership

The registry contains API-created tabs and new navigation targets explicitly
associated with a managed source page. Existing user tabs are not adopted.
Children are associated through Chrome's `openerTabId` or
`webNavigation.onCreatedNavigationTarget.sourceTabId`; URLs are not used to infer
ownership. The latter also handles cases where tab-strip opener metadata is missing.

### Create

```http
POST /api/tabs
Content-Type: application/json

{"url":"about:blank","active":false,"autoClose":false,"meta":{"job":"video-draft-001"}}
```

| Field | Default | Meaning |
|---|---|---|
| `url` | `about:blank` | URL to open |
| `active` | `false` | Activate the new tab when explicitly needed |
| `autoClose` | `true` | Allow idle cleanup; use false during upload/processing |
| `meta` | `{}` | Caller metadata, for example a task/job identifier |

Response data is `{"tab":{...}}`. A serialized tab has:

```json
{
  "tabId": 123,
  "targetId": "CDP-TARGET-ID",
  "url": "https://example.com/",
  "title": "Example",
  "meta": {"job":"video-draft-001"},
  "isAttached": true,
  "autoClose": false,
  "lastActivity": 1700000000000,
  "createdAt": 1700000000000
}
```

`idleTimeoutMs` is present when a per-tab override is configured. A managed
child's `meta.parentTabId` points to its source tab; other parent metadata and
its lifetime policy are inherited.

`GET /api/tabs` returns `data.tabs` and `data.count`.
`GET /api/tabs/{id}` returns `data.tab`.

### Close or release

`DELETE /api/tabs/{id}` detaches and closes the page.
`POST /api/tabs/{id}/release` detaches and removes management **without closing**
the page, preserving its current form/upload state for the user. Both return
`data.success:true` and `data.tabId`.

Closing or releasing a parent does not automatically close/release its children.
Use `meta.parentTabId` to identify children that belong to the task, and explicitly
finish them when appropriate. After release the API can no longer control that page.

### Lifetime

Default idle close is **30 minutes**, checked every 5 seconds. Other explicit
extension settings are preserved; the old one-minute default migrates to 30 minutes.
The idle limit can be overridden per tab. `autoClose:false` disables idle cleanup.
Pending/running actions protect a tab, and commands/events refresh activity.
Activity is not proof of upload completion: pin a long-running upload/processing
page between commands using `auto_close:false`.

Managed metadata uses `chrome.storage.session` for service-worker recovery.
It does not promise recovery after a full browser restart or extension reload.

## Shared semantic actions

```http
POST /api/tabs/123/action
Content-Type: application/json

{"action":"fill","args":{"target":{"role":"textbox","name":"Title"},"value":"New title"},"timeoutSeconds":40}
```

`action` selects one of the following operations. `args` defaults to `{}`; the tab
ID comes from the URL. The response is unwrapped: `data` is the action result,
without the raw CDP `result` wrapper.

| Action | Arguments | Result/behavior |
|---|---|---|
| `snapshot` | Optional `limit` (default 200, max 500) | URL/title, up to 12,000 chars of body text and interactive elements |
| `click` | `target` | Scroll, check visibility/enabled/occlusion, dispatch browser mouse input; returns `clicked:true`, `ref` |
| `fill` | `target`, string `value` | Replace input/textarea/contenteditable through input events and verify value; rich editors (Draft.js, ProseMirror, Lexical) are cleared with a trusted select-all first, and a trailing editor newline is ignored. Returns `filled:true`, `value` |
| `insert_text` | `target`, non-empty string `value` | Append at the end of an input/textarea/contenteditable without clearing (the caret is moved with a trusted keystroke, so rich editors follow), then verify the field ends with it. Returns `inserted:true`, `value` |
| `press` | `key`, optional `target` | Focus target and dispatch a key, e.g. Enter, Tab, Escape, ArrowDown, Control+a, Meta+a |
| `select_option` | `target`, string array `values` | Native select values; dispatch input/change, return selected `values` |
| `set_checked` | `target`, boolean `checked` | Set/verify native checkbox or radio; a radio cannot be unchecked directly |
| `wait_for` | `target`, `state`, optional `value`, `timeout_seconds` | Wait for a condition; returns `matched:true`, `observation` |
| `upload_files` | `target`, array `files` | Select absolute local paths in a file input; the selection is verified as of the change event, so it holds even when the page removes or resets the input. Returns `stage:"selected"`, `files`, `reset_by_page`, `note` |
| `set_tab_policy` | Optional `auto_close`, `idle_timeout_minutes` | Set tab lifetime; returns `tabId`, `autoClose`, `idleTimeoutMinutes` |
| `list_frames` | Optional `session_id` | Frame tree, execution `contexts`, child `sessions` |
| `handle_dialog` | Boolean `accept`, optional `prompt_text`, `session_id` | Accept/dismiss JavaScript alert/confirm/prompt; returns `handled:true` |

### Targets and snapshots

`target` is one of:

```json
"input[name=title]"
```

```json
{"ref":"snapshot-generation:7"}
```

```json
{"role":"button","name":"Save","exact":true}
```

Object targets can combine `selector`, `role`, `name`, and `text`. Names/text match
exactly by default; `exact:false` permits substring matching. `text` describes
rendered text; `name` uses accessible-label/label/title/placeholder/text fallbacks.
Open Shadow DOM is searched. The match must be unique, including for waits;
multiple matches fail with `AMBIGUOUS_TARGET`.

Snapshot elements expose `ref`, `tag`, `role`, `name`, `value`, `visible`, `enabled`,
and, where relevant, `type`, `checked`, `accept`, `multiple`. Hidden file inputs
are included. Password values are redacted. Element values in snapshots are
truncated to 1,000 characters. Take a fresh snapshot after navigation or
`STALE_ELEMENT`; refs belong to the document where they were observed.

`click` acknowledges dispatch, not a successful save or publication. Wait for and
verify the actual resulting state. The extension emulates page focus for managed
background tabs so mouse events reach the renderer without requiring tab activation.
Normal input, textarea, contenteditable, native select/checkbox/radio and open
Shadow DOM are supported. Specialized editors, canvas controls and drag/drop-only
widgets can require site-specific workflows; DOM text alone may not prove an
editor's saved application state.

### Waits and timeouts

States: `attached`, `visible` (default), `hidden`, `absent`, `enabled`, `text`,
`value`, `url`. `text` matches a substring; `value` matches exactly; `url` matches
a URL substring. Page-wide `text`/`url` checks can omit `target`.
Use selectors/semantic targets when waiting for removal; a removed ref is stale.

There are three deadlines:

1. `args.timeout_seconds`: extension wait, default 30, greater than 0 and at most 300.
2. Outer `timeoutSeconds`: Go request, default 40, range 1..330, must exceed the wait.
3. HTTP client read timeout: set longer than the Go deadline.

```json
{"action":"wait_for","args":{"target":".status","state":"text","value":"Processing complete","timeout_seconds":240},"timeoutSeconds":250}
```

Timeout returns an error containing `WAIT_TIMEOUT`, not a successful match.
Actions on the same tab are serialized; different tabs are independent. Cancelled
HTTP requests send a best-effort cancellation notification to stop extension waits
and later commands. Already-dispatched browser input cannot be undone. The server
does not retry mutations: after timeout/disconnect inspect existing content before
repeating submit/publish. A series of actions is not an atomic transaction.

### Files and publishing

```json
{"action":"set_tab_policy","args":{"auto_close":false}}
```

```json
{"action":"upload_files","args":{"target":"input[type=file]","files":["/absolute/path/video.mp4"]}}
```

These are separate requests to the same tab action endpoint. Go accepts 1..100
readable regular files with absolute paths. The input must allow multiple files
when selecting more than one. Paths must exist on the **Chrome machine**; current
Go validation assumes a shared local filesystem. Remote deployments require a
separate file-staging mechanism. File bytes do not pass through the WebSocket or
LLM context.

Example response:

```json
{"success":true,"data":{"stage":"selected","files":[{"name":"video.mp4","size":123456,"type":"video/mp4","lastModified":1700000000000}],"note":"Wait for the website upload and processing state before closing this tab."}}
```

Selection, transfer, platform processing, draft save, scheduling and publication
are distinct states. Define site-specific waits and verify the requested state,
including title/settings and a content ID or URL when available. Keep unfinished
pages open. Restore `auto_close:true` or close only when no further processing or
user interaction needs that page. Release instead when handing it to the user.
These APIs do not provide platform upload-resume or exactly-once publishing.

Chrome's **Allow access to file URLs** enables `file://` local page automation.
It is separate from local file selection on HTTPS upload forms; the project's
file-input upload path was tested before this setting was enabled.

### Lifetime overrides

```json
{"action":"set_tab_policy","args":{"auto_close":false,"idle_timeout_minutes":60}}
```

`idle_timeout_minutes` range is 1..1440. The returned `idleTimeoutMinutes` is null
when the tab inherits the global limit. Changing a tab policy does not change
extension-wide settings or user-opened tabs.

### Frames and JavaScript dialogs

`list_frames` returns `data.contexts` with `id`, `origin`, `name`, `frameId`,
`isDefault`, `sessionId`; `data.sessions` with child target metadata. The raw
`Page.getFrameTree` result is under `data.frameTree` (its root is therefore
`data.frameTree.frameTree`).

Add `frame_selector` for a same-origin iframe. For another frame, select its default
context by observed origin/frame ID and pass:

```json
{"action":"fill","args":{"target":"#inside","value":"Frame text","context_id":8,"session_id":"CHILD-SESSION-ID"}}
```

Root-target contexts have an empty `sessionId`. Re-enumerate contexts after frame
navigation. Raw `/api/cdp` uses camelCase `sessionId`; semantic arguments use
snake_case `session_id`. `/eval` has no frame parameters.

If an interaction reports `JAVASCRIPT_DIALOG_OPEN`, its triggering click may
already have happened. `snapshot` exposes the dialog while it is open. Call
`handle_dialog` with the appropriate decision; it bypasses the tab action queue
to unblock the triggering command. One exception is handled by the extension
itself: a "leave site?" (`beforeunload`) prompt raised while the API is closing
the tab or navigating it (`/navigate`, `Page.navigate`) is accepted, since the
caller asked to leave the page. This handles JavaScript dialogs, not OS file
pickers, CAPTCHAs or arbitrary browser permission prompts.

## Navigation, screenshot and evaluation shortcuts

### Navigate

```http
POST /api/tabs/123/navigate

{"url":"https://example.com/","waitForLoad":true,"timeoutSeconds":30}
```

Returns CDP navigation fields (`frameId`, optional `loaderId`, `errorText`, etc.)
plus `loadEventFired`. Defaults: `waitForLoad:true`, `timeoutSeconds:30`.
Check `errorText`. `loadEventFired:false` means load was not observed within the
wait or waiting was disabled; it does not prove SPA readiness.

The current HTTP shortcut allows one timeout for dispatch and then another for
its load-event wait, so allow approximately `2 * timeoutSeconds + 5` seconds on
the client. Use semantic `wait_for` for the actual form/editor state afterwards.

### Screenshot

```http
GET /api/tabs/123/screenshot
GET /api/tabs/123/screenshot?format=jpeg&quality=80
```

PNG by default. JPEG quality is 1..100, default 80; quality is ignored for PNG.
The response is image bytes with `image/png` or `image/jpeg` content type, not JSON.

### Evaluate

```http
POST /api/tabs/123/eval

{"expression":"document.title","awaitPromise":false,"timeoutSeconds":30}
```

Returns `data.type`, optional `data.value` and `data.subtype`. Always requests
return-by-value. `awaitPromise` defaults to false. A JS exception returns HTTP 422,
`code:"JS_EXCEPTION"`, and details under `data.exception`. The timeout is a relay
request deadline; it does not undo JS that has already executed.
Prefer semantic actions for interaction and use eval for extraction/custom logic.

## Raw CDP

```http
POST /api/cdp

{"tabId":123,"method":"Runtime.evaluate","params":{"expression":"document.title","returnByValue":true},"timeout":30,"sessionId":""}
```

`tabId` and `method` are required. `params` is optional, `timeout` defaults to 30,
and `sessionId` optionally routes to a child session. The debugger is attached
when needed. Response data wraps the CDP result as `{"result":{...}}`; a Runtime
remote value is therefore at `data.result.result.value`.

Only commands allowed by `chrome.debugger` are supported. `Target.getTargets`
returns `Not allowed` on the tested Chrome. Browser-wide commands are not a
substitute for managed-tab APIs. See [the CDP reference](https://chromedevtools.github.io/devtools-protocol/)
for protocol details, subject to extension restrictions.

## Recent events

```http
GET /api/events?tabId=123&method=Page.&method=Network.&since=1700000000000
```

| Query | Meaning |
|---|---|
| `tabId` | Managed tab filter; omit for all retained events |
| `method` | Exact method or domain prefix ending in a dot; repeat for OR matching |
| `since` | Unix ms lower bound on Go **receive time**, not a durable cursor |

Response data is `{"events":[...],"count":1}`. Each event has `tabId`,
`eventMethod`, `eventParams`, extension `timestamp` (Unix ms), and Go `receivedAt`
(RFC3339). Child-session events also have `sessionId`; filter that field on the
client if needed. The query API has no `sessionId` parameter.

Retention is **3 minutes and at most 10,000 events globally**, whichever evicts
an event first. This is separate from the 30-minute tab timeout. Events are not
persistent job history, and Go restarts clear them.

| Lifecycle event | Meaning |
|---|---|
| `tab.created` | Managed child discovered; params include `tab` and `parentTabId` |
| `tab.closed` | Page closed; reason can be timeout/api/agent/user/create_failed |
| `tab.released` | Management ended, page left open |
| `debugger.detached` | Debugger detached; inspect the reported Chrome reason |

Record a polling lower bound **before** triggering a navigation/action. Starting
to poll only after creation can miss an already-fired load event. Prefer the
navigation shortcut and condition waits for ordinary operations.

## Agent recovery and remaining boundaries

The built-in Agent checkpoints conversation/tool intent before and after tools.
On worker recovery it restores an interrupted session and pauses; it does not
replay an uncertain publication automatically. This differs from restoring an
upload connection or implementing a platform-specific durable publishing job.

Local HTTP upload integrity, ordinary contenteditable forms, cross-origin frame
routing, background clicks, dialog handling and local file pages have been tested.
See [TESTING.md](TESTING.md) for the test suites, what they cover and known limits;
these checks do not certify complete YouTube/TikTok publishing or every rich-text editor.

## Errors

| Code | HTTP | Meaning |
|---|---|---|
| `EXTENSION_NOT_CONNECTED` | 503 | Extension unavailable |
| `CAPABILITIES_UNAVAILABLE` | 503 | Capability query failed/disconnected/unsupported by loaded extension |
| `INVALID_REQUEST` | 400 | Invalid body; action bodies are limited to 2 MiB |
| `METHOD_NOT_ALLOWED` | 405 | Unsupported HTTP method |
| `INVALID_ACTION` | 400 | Unknown semantic action |
| `INVALID_TIMEOUT` | 400 | Invalid action timeout or outer timeout shorter than wait |
| `INVALID_UPLOAD_FILES` | 400 | Missing/relative/non-regular/unreadable local files |
| `BROWSER_ACTION_FAILED` | 422 | Action failed, including wait timeout, ambiguity, stale ref or an open dialog |
| `INVALID_EXTENSION_RESPONSE` | 502 | Malformed semantic action result |
| `TAB_ID_REQUIRED` / `INVALID_TAB_ID` | 400 | Missing/invalid tab ID |
| `METHOD_REQUIRED` | 400 | Missing raw CDP method |
| `URL_REQUIRED` / `EXPRESSION_REQUIRED` | 400 | Missing shortcut argument |
| `INVALID_SINCE` | 400 | Bad event timestamp filter |
| `TAB_NOT_FOUND` / `TAB_CLOSED` | 404 | Tab missing, unmanaged or closed, depending on endpoint/error |
| `JS_EXCEPTION` | 422 | Evaluation raised a JS exception |
| `CREATE_TAB_FAILED` / `CLOSE_TAB_FAILED` / `RELEASE_TAB_FAILED` | 500 | Tab lifecycle operation failed |
| `LIST_TABS_FAILED` / `GET_TAB_FAILED` | 500 | Tab lookup failed |
| `CDP_COMMAND_FAILED` / `NAVIGATE_FAILED` | 500 | CDP/navigation forwarding failed |
| `SCREENSHOT_FAILED` / `EVAL_FAILED` | 500 | Shortcut operation failed |

A failure response for submit/publish does not imply that the website rolled back
its effects. Diagnose the existing state before retrying.

## Runnable examples

The examples need Python 3.8+ and only the standard library. Run them from the repository root:

```bash
# Default: local form, real local HTTP upload + SHA-256, draft save/reload, screenshot
python3 cli/demo.py local

# Optional network examples: scrape HN and fill its submit form if logged in; never submit
python3 cli/demo.py hn

# Explicit file selection on your chosen destination; leaves the page open
python3 cli/demo.py upload \
  --url 'https://your-site.example/upload' \
  --file '/absolute/path/video.mp4' \
  --selector 'input[type=file]'

# Integration checks for the real Go -> extension -> Chrome chain
python3 cli/browser_smoke.py --advanced
```

The remote upload example requires a destination with an already-present file
input (or manual setup). It does not perform login or click through a site's upload
wizard. Add `--complete-selector`, `--complete-text`, `--wait-seconds` to wait for
a site-specific state. By default it releases the page without closing it even on
failure; `--keep-tab` keeps the upload tab managed with idle cleanup disabled.
For `local` and `hn`, `--keep-tab` instead releases the demo page and leaves it open.

Screenshot output defaults to `/tmp/browser-agent-demo`; change it with `--output`.
Use `--base` for a different local proxy address. [cli/demo.py](cli/demo.py) also
provides an importable `BrowserClient` helper with correctly separated wait,
Go-request and client-read deadlines.

## Platform workflow CLIs

[cli/tiktok/README.md](cli/tiktok/README.md) documents `cli/tiktok`: TikTok videos posted or saved as drafts through TikTok Studio, with real hashtags, visibility, the AI-generated label, recent posts/drafts and duplicate checks. [cli/twitter/README.md](cli/twitter/README.md) documents `cli/twitter`: posts on X with text, up to four images, a GIF or a video, a recent-posts listing and duplicate checks, and resumable jobs that never press Post twice. [cli/youtube/README.md](cli/youtube/README.md) documents `cli/youtube`: upload from a local path or a direct URL, title/description as strings or text files, draft or immediate publish, thumbnails, duplicate checks, and resumable job state. It talks to this API on port 12345 by default. The shared Python client and workflow modules are in [cli/common](cli/common/README.md).
