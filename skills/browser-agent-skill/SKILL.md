---
name: browser-agent-skill
description: Automate normal Chrome through the local Browser Agent Proxy, including forms, rich text, local file uploads, iframe interactions, screenshots, and managed tab lifetimes. Use when this project's Go server and Chrome extension are the browser control transport.
---

# Browser Agent

Use the local Go REST API at `http://127.0.0.1:12345`. Chrome runs normally; no remote-debugging launch flags are required. Prefer the shared semantic actions over generating JavaScript for every interaction.

Read [API.md](../../API.md) for full request/response schemas. See [cli/demo.py](../../cli/demo.py) for runnable local upload/draft and optional Hacker News examples.

## Connect and identify capabilities

Start the server if needed:

```bash
cd browser-agent-proxy
go run .
```

The extension reconnects automatically. Query `GET /api/capabilities` to verify the loaded implementation. Protocol 2 exposes the high-level actions; implementation revision 4 adds navigation-source tracking (`navigationTracking:true`, extension 2.1.0 with `webNavigation` permission). Revision 5 (extension 2.1.1) verifies selected files through a retained input node even if the site removes the input. Revision 6 (extension 2.1.2) ignores the trailing newline rich editors add when verifying `fill`. If an action is missing, reload the extension instead of assuming the disk version is already running.

API responses use `{success:true,data:{...}}` or `{success:false,error,code}`. `/screenshot` is an exception: it returns image bytes. Check HTTP status and the envelope; preserve error details.

## Ownership and lifetime

- Operate on API-created managed tabs. Reuse an appropriate managed tab when its task metadata and state match the current work.
- Existing user tabs stay outside the registry. New windows can be associated only through Chrome's explicit opener or navigation-source relationship to a managed tab; do not infer ownership from matching URLs.
- Default idle close is **30 minutes**, checked every 5 seconds. Explicit extension settings can override it. Pending/running actions prevent idle cleanup; CDP commands and events refresh activity.
- For uploading, processing, or manual intervention, create with `autoClose:false` or call `set_tab_policy` with `auto_close:false`. This protects the page between actions as well as during a wait.
- `DELETE /api/tabs/{id}` closes the page. `POST /api/tabs/{id}/release` stops management without closing it. Release pages that the user needs to finish manually.
- A missing or closed tab returns 404 (`TAB_NOT_FOUND` or, on some CDP paths, `TAB_CLOSED`). Recreate only after checking whether the task may already have produced a draft or publication.

## Observe → act → verify

Create a tab with `POST /api/tabs`, retain `data.tab.tabId`, then navigate via `POST /api/tabs/{id}/navigate`. A document load event does not prove an SPA or editor is ready. Use `wait_for` on the actual element/state.

All semantic actions use:

```http
POST /api/tabs/123/action
Content-Type: application/json

{"action":"fill","args":{"target":{"role":"textbox","name":"Title"},"value":"New title"},"timeoutSeconds":40}
```

Call `snapshot` to observe text and elements. It returns `ref`, role, accessible name, value, visible/enabled state, and file-input metadata. Targets can be a unique CSS selector, `{"ref":"..."}`, or `{"role":"button","name":"Save"}`. Names match exactly by default; `exact:false` permits substring matching. Open Shadow DOM is searched. Ambiguous matches fail; narrow the target instead of choosing an arbitrary match. Refresh refs after navigation or a stale-element error.

| Action | Key arguments / purpose |
|---|---|
| `snapshot` | `limit` (default 200, max 500); observe current page |
| `click` | `target`; visible/enabled/unobscured browser mouse click |
| `fill` | `target`, string `value`; replace input/textarea/contenteditable through browser input, verify value |
| `press` | `key`, optional `target`; Enter, Tab, Escape, arrows, Meta+a, Control+a |
| `select_option` | `target`, string array `values`; native select |
| `set_checked` | `target`, boolean `checked`; native checkbox/radio |
| `wait_for` | `target`, `state`, optional `value`, `timeout_seconds` |
| `upload_files` | `target`, `files` array of absolute local paths |
| `set_tab_policy` | `auto_close`, `idle_timeout_minutes` (1..1440) |
| `list_frames` | Frame tree, execution contexts and child sessions |
| `handle_dialog` | boolean `accept`, optional `prompt_text` |

For custom rich-text editors, verify application state or a saved/reopened draft, not just the DOM's appearance. Specialized editors and drag/drop-only controls may require additional site-specific actions.

### Waits and dialogs

Wait states: `attached`, `visible`, `hidden`, `absent`, `enabled`, `text`, `value`, `url`. Page-wide `text` and `url` checks can omit the target. Default wait is 30 seconds, maximum 300; the outer `timeoutSeconds` must be longer (default 40, maximum 330). Set the HTTP client's read timeout longer still. A timeout is an error, not completion.

If a click/eval reports `JAVASCRIPT_DIALOG_OPEN`, inspect the dialog metadata (also returned by `snapshot`) and use `handle_dialog` as appropriate to the task. The triggering click may already have executed. Dialog handling bypasses the tab action queue so it can unblock the triggering command.

### Frames

For a same-origin iframe, add `frame_selector` to action arguments. For a cross-origin frame, call `list_frames`, select its default context using `origin`/`frameId`, then pass `context_id:context.id` and `session_id:context.sessionId`. Contexts become invalid after navigation; enumerate again. Top-page JavaScript cannot directly read a cross-origin iframe.

The raw `/api/cdp` request uses camelCase `sessionId`; semantic action arguments use snake_case `session_id`. Raw `/eval` has no frame parameters: use the semantic tools or `/api/cdp` with the context/session instead.

## Upload and publishing

Use `upload_files` for hidden or visible `input[type=file]`. Paths must exist on the **Chrome machine**. Go validates that they are readable regular files, assuming Go and Chrome share the local filesystem. No file bytes pass through the control WebSocket or model context. A remote Go deployment needs separate file staging before this call.

Example sequence (separate requests to `/api/tabs/{id}/action`; selectors/text must come from the actual page):

```jsonl
{"action":"set_tab_policy","args":{"auto_close":false}}
{"action":"upload_files","args":{"target":"input[type=file]","files":["/absolute/path/video.mp4"]}}
{"action":"wait_for","args":{"target":".upload-status","state":"text","value":"Upload complete","timeout_seconds":240},"timeoutSeconds":250}
```

`upload_files` returns `stage:"selected"` and selected file metadata. Distinguish **selected**, **uploaded**, **processed**, **draft saved**, **scheduled**, and **published**. Continue waiting for the state the user requested. Keep unfinished pages open; do not put upload cleanup in an unconditional close-tab `finally` block.

Verify the saved title/body/settings and, where available, a content ID, management-list entry or final URL before reporting success. The primitives do not implement a universal YouTube/TikTok publishing workflow or exactly-once publication.

On timeout/disconnect, inspect the existing page, draft or content list before repeating a submission. The server does not retry mutations. HTTP cancellation is forwarded to stop waits/later commands, but cannot undo input already dispatched.

“Allow access to file URLs” enables `file://` pages (local HTML/offline tools). It is separate from selecting local files on an HTTPS upload form; this project's upload path was tested before that setting was enabled.

## Read-only extraction and troubleshooting

Use `/eval` for page data extraction, `/screenshot` for visual inspection, and `/api/cdp` for commands not covered above. `chrome.debugger` restricts CDP methods; `Target.getTargets` returns `Not allowed` on the tested Chrome. Use managed-tab APIs rather than assuming browser-wide target control.

Events: `GET /api/events?tabId=123&method=Page.&method=Network.&since=...`. Retention is **3 minutes**, with a **10,000-event global cap**; this is independent of the 30-minute tab timeout. `since` filters Go receive time; do not treat events as durable task history. Child events include `sessionId`. `tab.created` includes the managed child's `meta.parentTabId`; `tab.released`, `tab.closed`, and `debugger.detached` report lifecycle changes.

For uncertain interactions, refresh the snapshot and inspect the page before repeating the same action. Preserve a page awaiting login, a user decision or upload processing.

## Run maintained examples

From the repository root (Python 3.8+, standard library only):

```bash
python3 cli/demo.py local
python3 cli/demo.py hn
python3 cli/browser_smoke.py --advanced
```

The default `local` demo exercises a temporary local form, upload with SHA-256 verification, saved draft reload and a screenshot. `hn` reads Hacker News and fills the submit form only when available; it does not submit. Consult `demo.py --help` for the explicit remote upload example and its completion/lifetime options.

For YouTube upload/draft/publish tasks, use `python3 -m cli.youtube` (or `python3 path/to/cli/youtube` from another directory) and read [its workflow documentation](../../cli/youtube/README.md). It reuses the shared API client, persists upload/publish intent, and reconciles uncertain commits. Do not blindly restart an upload after an ambiguous result.
