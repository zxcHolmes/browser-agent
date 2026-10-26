# Shared CLI modules

Platform-neutral building blocks for site workflow CLIs such as [`cli/youtube`](../youtube/README.md) and [`cli/twitter`](../twitter/README.md) and [`cli/tiktok`](../tiktok/README.md) and [`cli/douyin`](../douyin/README.md). Python 3.8+ (timezone scheduling requires Python 3.9+), standard library only (`ffmpeg` is needed only by the image helpers, and only when a conversion is required).

| Module | What it provides |
|---|---|
| `browser_client.py` | Proxy API client: managed-tab lifecycle, CDP, the semantic actions (`action`, `wait_for`), screenshots; `events(tab_id, since=…, methods=…)` reads recent proxy events (Go receive time in Unix ms, repeated exact/domain filters; limited by retention); `APIError` (the server answered; `not_performed` is true when the extension refused before any input reached the page, e.g. `ELEMENT_OBSCURED`) vs. `TransportError` (no trustworthy reply — a mutation may already have happened). `load_probe(path)` wraps the DOM helpers and a platform probe into one evaluation. |
| `automation.py` | `AutomationError` (code + exit code + details), `TaskState` (private, atomically written job state with intent/confirmed actions; `TaskState.load(path, platform=…, validate=…)` checks the generic shape and lets the platform check its own fields), `FileLock` (cross-process), `claim_key` (a local index that records which job sent a given content key — e.g. content hash + destination — so the same content is not sent twice). |
| `media.py` | `read_text` (UTF-8 files), `fingerprint` (SHA-256; a file changing while read is rejected), `download_video` (direct-URL download; `reject_hosts` refuses player pages), `extract_frame` (one video frame), `prepare_image` (fit an image to a site's format/size limits). |
| `page_workflow.py` | `PageWorkflow`, the base class of a workflow over one managed tab (`ensure_tab` checks the extension's capabilities and revision, reuses the job's own tab or opens one, and refuses a tab owned by another job); `navigate` (page loads are reads, so a network failure is retried, then `NAVIGATION_FAILED`); `same_text` (ignores the trailing newline rich editors add); `comparable_text` / `same_published_text` (caption/post text for duplicate checks: links dropped, case and spacing ignored, a cut-off text matched by prefix); `MemoryState` (in-memory state for one-shot commands with nothing to resume). |
| `assets/dom_helpers.js` | `visible`, `clean`, `label`, `first`, `control`, `locate`, `openPopup` (the open listbox/menu an input controls through `aria-controls`/`aria-owns`, such as an autocomplete list) — available to every probe. |

## How a workflow is put together

A platform supplies two things:

- **A probe** (`cli/<platform>/probe.js`): one read-only JavaScript expression that returns the page state as data — flags such as `loginRequired`, field values, and an `actions` map of controls, each `{target, enabled, stable, name}` built with `control(element)`. It never clicks or types.
- **A workflow class** derived from `PageWorkflow`, which opens its tab with `ensure_tab()`, reads the page only through `probe()` / `poll()` and changes it only through `mutate()` / `click()`.

`PageWorkflow` gives every platform the same safety rules:

- Probes are polled and retried freely; `poll(..., stable_for=seconds)` can require a condition to hold across successful reads for a quiet interval, resetting on a false or failed read; a page that is mid-navigation reads as `PAGE_NOT_READY` and is polled again.
- Every mutation is recorded as an intent before it is sent and marked confirmed after. A mutation whose reply is lost raises `ACTION_OUTCOME_UNKNOWN` and is **never replayed** — the caller inspects the site (a draft, a content list) and reconciles instead. One the extension refused before acting (target missing, ambiguous, covered, disabled…) raises `ACTION_NOT_PERFORMED` (exit 4): it is known not to have happened, so the workflow may withdraw its intent and a resume may try again. Refusals that usually clear by themselves (`ELEMENT_OBSCURED` during an animation, `ELEMENT_NOT_VISIBLE`, `STALE_ELEMENT`) are retried a few times first — safe for the same reason.
- Subclasses implement `guard(probe)` for states that must stop the run (login wall, captcha, rejection) and `observe(probe)` for facts to record (such as the item ID).
- `diagnostics()` saves a screenshot and the last observation next to the job state on failure.

## Locating controls

`locate(element, scopes)` returns a CSS selector that the extension resolves again right before acting; the extension requires exactly one visible match, so a stale or ambiguous selector fails instead of acting on the wrong element. Selectors are built from what the site itself keeps stable, language-neutral anchors first:

1. the element's own test id (`data-testid`, `data-test`, `data-qa`, `data-cy`, `data-e2e`), semantic `id` or `name`;
2. the same, or the tag, scoped by a stable ancestor — a test id, an id, a semantic class name, or a scope the probe passes in (a dialog, a row keyed by a business ID);
3. `aria-label`, which follows the UI language, last.

Generated values (`goog_1420216748`, `15865878-follow`, anything with long digit runs or hashes) and hashed or state class names (`css-1x2y3z`, `…-active`) are never used. When nothing stable is unique, `locate` falls back to a positional path and reports `stable: false`, so a probe author can see where the page offers no good anchor.

## Adding a platform

```
cli/<platform>/
  __main__.py     # entry point (copy cli/youtube/__main__.py)
  main.py         # argument parsing + a PageWorkflow subclass
  probe.js        # read-only DOM adapter
  README.md       # usage, recovery model, exit codes, known limits
  tests/          # offline unit tests; optional live smoke test against a local fixture
```

Inside the package import the shared modules relatively (`from ..common.page_workflow import PageWorkflow`) and load the probe with `load_probe(Path(__file__).parent / 'probe.js')`. The CLI then runs as `python3 -m cli.<platform>` from the repository root, or `python3 path/to/cli/<platform>` from anywhere.

`PageWorkflow.mutate(..., "cdp.send", method=..., params=...)` records raw CDP input with the same intent/unknown-outcome handling as semantic actions. Controlled date inputs can replace a trusted selection directly, avoiding a transient empty value that some frameworks immediately revert. `scheduling.py` validates timezone-aware, minute-precision future times (Asia/Shanghai by default), including platform lead times and horizons.

`publications.py` normalizes visible cross-platform list evidence: timestamps, publication status, metrics-independent work identity, completeness and truncation flags, status filtering, and scroll-container discovery. It does not depend on a particular series or read site tokens or internal state.

`web_publishing.py` provides the shared browser-only publishing lifecycle for Medium, LinkedIn, and Facebook: argument validation, private job state, account checks, local content fingerprints, duplicate protection, bounded recent lists, and recovery without repeated submissions. Platform adapters own editor controls and persistence/publication evidence. `insert_text(position="caret")` requires extension revision 11 and preserves a cursor placed by a prior browser interaction.
