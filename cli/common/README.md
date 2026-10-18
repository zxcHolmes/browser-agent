# Shared CLI modules

Platform-neutral building blocks for site workflow CLIs such as [`cli/youtube`](../youtube/README.md). Python 3.8+, standard library only (`ffmpeg` is needed only by the image helpers, and only when a conversion is required).

| Module | What it provides |
|---|---|
| `browser_client.py` | Proxy API client: managed-tab lifecycle, CDP, the semantic actions (`action`, `wait_for`), screenshots; `APIError` (the server answered) vs. `TransportError` (no trustworthy reply — a mutation may already have happened). `load_probe(path)` wraps the DOM helpers and a platform probe into one evaluation. |
| `automation.py` | `AutomationError` (code + exit code + details), `TaskState` (private, atomically written job state with intent/confirmed actions), `FileLock` (cross-process), `claim_upload` (SHA-256 + target de-duplication index). |
| `media.py` | `read_text` (UTF-8 files), `fingerprint` (SHA-256; a file changing while read is rejected), `download_video` (direct-URL download; `reject_hosts` refuses player pages), `extract_frame` (one video frame), `prepare_image` (fit an image to a site's format/size limits). |
| `page_workflow.py` | `PageWorkflow`, the base class of a workflow over one managed tab; `same_text` (ignores the trailing newline rich editors add); `MemoryState` (in-memory state for one-shot commands with nothing to resume). |
| `assets/dom_helpers.js` | `visible`, `clean`, `label`, `first`, `control`, `locate` — available to every probe. |

## How a workflow is put together

A platform supplies two things:

- **A probe** (`cli/<platform>/probe.js`): one read-only JavaScript expression that returns the page state as data — flags such as `loginRequired`, field values, and an `actions` map of controls, each `{target, enabled, stable, name}` built with `control(element)`. It never clicks or types.
- **A workflow class** derived from `PageWorkflow`, which reads the page only through `probe()` / `poll()` and changes it only through `mutate()` / `click()`.

`PageWorkflow` gives every platform the same safety rules:

- Probes are polled and retried freely; a page that is mid-navigation reads as `PAGE_NOT_READY` and is polled again.
- Every mutation is recorded as an intent before it is sent and marked confirmed after. A mutation whose reply is lost raises `ACTION_OUTCOME_UNKNOWN` and is **never replayed** — the caller inspects the site (a draft, a content list) and reconciles instead.
- Subclasses implement `guard(probe)` for states that must stop the run (login wall, captcha, rejection) and `observe(probe)` for facts to record (such as the item ID).
- `diagnostics()` saves a screenshot and the last observation next to the job state on failure.

## Locating controls

`locate(element, scopes)` returns a CSS selector that the extension resolves again right before acting; the extension requires exactly one visible match, so a stale or ambiguous selector fails instead of acting on the wrong element. Selectors are built from what the site itself keeps stable, language-neutral anchors first:

1. the element's own semantic `id` or `name`;
2. the same, or the tag, scoped by a stable ancestor — an id, a semantic class name, or a scope the probe passes in (a dialog tag, a row keyed by a business ID);
3. `aria-label`, which follows the UI language, last.

Generated ids (`goog_1420216748`, anything with long digit runs or hashes) and hashed or state class names (`css-1x2y3z`, `…-active`) are never used. When nothing stable is unique, `locate` falls back to a positional path and reports `stable: false`, so a probe author can see where the page offers no good anchor.

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
