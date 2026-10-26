# Douyin (China) CLI

Control the signed-in account at `creator.douyin.com` through Browser Agent Proxy and the Chrome extension. Requires Python 3.9+ for scheduling, the standard library, and extension **2.1.6 / implementationRevision ≥ 10**. The default API is `http://127.0.0.1:12345`. Reload updated extension files in `chrome://extensions`.

Run from the repository root:

```bash
python -m cli.douyin --help
python -m cli.douyin upload --video /path/to/video.mp4 --title 'Example video' --description 'Example caption' --visibility private
python -m cli.douyin draft --video /path/to/video.mp4 --title-file title.txt --description-file description.txt --account YOUR_DOUYIN_ID
python -m cli.douyin post --video /path/to/video.mp4 --title 'Example video' --visibility private
python -m cli.douyin post --video /path/to/video.mp4 --title 'Example video' --draft
```

`upload` fills the editor and retains the tab without saving or publishing. `draft` saves a browser-local draft and reloads to verify it. `post` submits immediately unless `--schedule-at` or `--draft` is supplied. Publication is public by default; use `--visibility private` for private works. These commands perform real account actions.

| Option | Behavior |
|---|---|
| `--video PATH` | Nonempty local video, at most 16 GiB; MP4 recommended. Chrome and the proxy must access the same file path. Site duration/codec limits also apply. |
| `--title TEXT` / `--title-file PATH` | Mutually exclusive; 1–30 characters on one line. Defaults to the filename stem; oversized titles are rejected. |
| `--description TEXT` / `--description-file PATH` | Mutually exclusive; at most 1000 characters. UTF-8 files preserve newlines. Aliases: `--caption` / `--caption-file`. |
| `--visibility public\|friends\|private` | Checked again before submission; default: public. |
| `--account ID` | Expected profile Douyin ID; stop on mismatch. |
| `--reuse-draft` | Explicitly reuse a draft only when filename, size, and modification time match. Applies the requested metadata and visibility. |
| `--allow-duplicate` | Bypass the local file SHA-256/account duplicate check. |
| `--upload-timeout SECONDS` | Positive finite timeout; default: 1800. |
| `--step-timeout SECONDS` | Step timeout; default: 90. |
| `--poll-interval SECONDS` | Read interval; default: 2. |
| `--api-url URL` / `--base URL` | Override the proxy URL; place after the subcommand. |
| `--state-dir PATH` / `--state-file PATH` | State directory or new state file; existing files are not overwritten. |
| `--json` / `--quiet` | Single-line JSON / suppress stderr progress. |
| `--keep-tab` | Keep the tab after successful publication; uploads, drafts, and failures always retain it. |

## Draft persistence

The site's save-and-leave control stores one temporary video in localStorage for the same Chrome profile and account. This is not a cloud or mobile draft; clearing site data may remove it.

Unrelated drafts are preserved. `EXISTING_DRAFT` requires explicit `--reuse-draft` with matching file metadata, or recovery of the same recorded video ID. The probe reads only whitelisted draft fields, never account tokens, and does not write site storage. After saving, the CLI reloads the upload page and checks persisted video metadata, title, description, and visibility. Manually continuing a draft consumes the temporary slot; save again to retain it.

## Recovery and limits

```bash
python -m cli.douyin status --state-file /path/to/state.json
python -m cli.douyin resume --state-file /path/to/state.json --json
```

Results are JSON on stdout; progress goes to stderr. Argument errors use stderr and exit 2. Results include stage, error code/message/details, state path, resume command, and success evidence. State defaults to `$XDG_STATE_HOME/browser-agent/douyin`, or `~/.local/state/browser-agent/douyin`. Job files and diagnostic screenshots use private permissions. Process locks protect CLI jobs, but cannot prevent manual changes to the account or editor.

Login failures, challenges, account mismatches, dialogs, and platform rejection stop the workflow and retain diagnostics. Semantic controls are resolved from the current page; ambiguous controls stop the run. The adapter targets the Simplified Chinese interface, with some English button aliases. Other language variants are untested.

Lost upload tabs are recovered only through matching drafts; uploads are not blindly repeated. Changed video files are rejected. Submission intent is saved before input. Uncertain submissions are verified on resume without clicking submit again.

Immediate publication requires a browser response from Douyin's `create` / `create_v2` request with HTTP 200, `status_code: 0`, and a work ID. Redirects or notifications alone are insufficient. Proxy events expire after three minutes and response bodies may be evicted; missing evidence returns `POST_UNVERIFIED` for manual review.

Covers, music, collections, location, and declarations retain site defaults. Additional required fields stop publication. Hashtags are entered as text; suggestion-list registration is not guaranteed. Slate editor input uses trusted line breaks and waits for stable form values before saving, then verifies persisted content.

| Exit code | Meaning |
|---|---|
| 0 | Operation succeeded; inspect local `status` output for the recorded job outcome. |
| 2 | Invalid arguments, files, state, or local IO. |
| 3 | API, extension, login, account, or verification issue. |
| 4 | Ambiguous/refused control, dialog, or lock. |
| 5 | Page, control, upload, or processing timeout. |
| 6 | Platform rejection or editor mismatch. |
| 7 | Existing draft, duplicate, lost editor, or unverified outcome. |
| 130 | Interrupted. |

## Scheduling and content lists

```bash
python -m cli.douyin post --video /path/to/video.mp4 --title 'Example series 002' --description-file caption.txt --account YOUR_DOUYIN_ID --schedule-at '2030-01-20T19:30:00+08:00'
python -m cli.douyin recent --search 'Example series' --json
python -m cli.douyin recent --status scheduled --require-complete --json
```

Replace the example timestamp with a future time at least two hours ahead and within 14 days, at minute precision. Times without a timezone use Asia/Shanghai. `--schedule-at` cannot be combined with `--draft`. The CLI verifies the editor schedule and the resulting content-list row. Success returns `stage: scheduled` and `scheduled_at`.

`recent` reads lazy-loaded works and reports statuses, ISO timestamps, summary, and completeness. Truncation never reports `complete: true`; `--require-complete` fails when the list is incomplete. Publication-plan integration is documented in [PUBLISHING.md](../PUBLISHING.md).

## Verification

```bash
python -m unittest cli.douyin.tests.test_douyin -v
node --test chrome-extension/agent/browser-actions.test.mjs
```

Offline tests cover validation, persistence, visibility, existing-draft protection, account/login/challenge errors, refused input, uncertain transport outcomes, single submission, and response-ID verification. Live checks require a signed-in account and suitable test content.
