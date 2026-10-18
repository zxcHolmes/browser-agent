# Testing

Run everything from the repository root. The Python suites need Python 3.8+ and only the standard library.

## Without a browser

```bash
# Go proxy: hub, connection replacement, request cancellation, upload-file checks,
# action routing, event ring buffer and GC, docs page rendering
cd browser-agent-proxy && go test -race ./... && go vet ./... && cd ..

# Extension modules: per-tab action queue, wait timeouts and cancellation, dialog
# handling, screenshot ordering, tool-call limits, managed child-tab ownership
node --test chrome-extension/agent/*.test.mjs

# YouTube CLI: error handling, de-duplication, locks, unknown-publish recovery,
# content-list reading, title check, thumbnails
python3 -m unittest cli.youtube.tests.test_youtube -v

# X CLI: text comparison, attachment rules, stop conditions, single Post click,
# rejection/unknown outcomes, resume reconciliation, duplicate check, timeline reading
python3 -m unittest cli.twitter.tests.test_twitter -v
python3 -m unittest cli.tiktok.tests.test_tiktok -v
```

## Against the real proxy, extension and Chrome

These need the proxy running (`cd browser-agent-proxy && go run .`) and the extension loaded and connected. They use only local fixtures served from `localhost`, upload only to that local server, sign in nowhere, and close only the tabs they created.

```bash
# Bridge checks: tab lifecycle, navigation, DOM and Unicode input, open shadow DOM,
# same- and cross-origin iframes, delayed elements, file upload with SHA-256 check,
# draft save/reload, dialogs, background clicks, child-window ownership, screenshots.
# Writes a JSON report under /tmp/browser-agent-smoke/.
python3 cli/browser_smoke.py --advanced

# YouTube CLI flows on a local mock of Studio: draft, draft resumed into publish,
# immediate publish, lost publish reply reconciled on resume, login wall, rejected upload
python3 -m cli.youtube.tests.youtube_smoke

# Runnable demo: local form, upload integrity, draft reload and screenshot
python3 cli/demo.py local
```

After changing the extension, reload it in `chrome://extensions` and check `GET /api/capabilities` reports the expected `implementationRevision` before running these.

## Known limits

- The local fixtures use plain DOM and `contenteditable`; they do not prove every React/ProseMirror-style editor works with `fill`.
- Upload tests verify file selection and the real HTTP transfer, not large-video processing, scheduled publishing or captchas.
- Mutating actions are never retried automatically: a timeout or dropped connection can leave the outcome unknown, so inspect the site's state (draft or content list) before retrying.
- `chrome.storage.session` does not survive a browser restart or an extension reload, so managed tabs are not recovered across either; workflows such as the YouTube CLI recover through their own persisted state instead.
- Live-site coverage of the site CLIs is described in [cli/youtube/README.md](cli/youtube/README.md#compatibility-and-known-limits) [cli/twitter/README.md](cli/twitter/README.md#known-limits) and [cli/tiktok/README.md](cli/tiktok/README.md#known-limits).
