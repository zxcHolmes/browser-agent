# WeChat Channels CLI

Control WeChat Channels Assistant through Browser Agent Proxy and the Chrome extension. Requires a signed-in Chrome account, Python 3.9+, and the standard library. The default API is `http://127.0.0.1:12345`. Only CLI-created tabs are managed.

```bash
python -m cli.wechat_channels --help
python -m cli.wechat_channels recent --search 'Example series' --json
python -m cli.wechat_channels post --video /path/to/video.mp4 --title 'Example video' --description-file caption.txt --account 'YOUR_CHANNEL_NAME' --schedule-at '2030-01-20T19:30:00+08:00'
```

Run from the repository root, or use `python /path/to/browser-agent/cli/wechat_channels ...` from another directory. Replace the example schedule with a future, minute-precision time. Times without a timezone use Asia/Shanghai. Scheduling uses the platform's date/time controls and does not require the CLI to remain running. The workflow does not write internal site state or call private publication APIs.

Commands: `post`, `upload` (retain editor), `draft`, `recent`, `status --state-file`, and `resume --state-file`. Publication commands perform real account actions.

| Option | Behavior |
|---|---|
| `--video PATH` | Local MP4/H.264 recommended; at most 20 GiB. |
| `--title TEXT` / `--title-file PATH` | Mutually exclusive; 6–16 characters on one line. Commas become spaces; unsupported symbols are rejected. |
| `--description TEXT` / `--description-file PATH` | Mutually exclusive; UTF-8, at most 2000 characters, preserving newlines. Aliases: `--caption` / `--caption-file`. |
| `--account NAME` | Expected channel name; stop on mismatch. |
| `--upload-timeout` / `--step-timeout` / `--poll-interval` | Positive finite seconds; defaults: 1800 / 90 / 1. |
| `--state-dir` / `--state-file` | Job state; default directory: `~/.local/state/browser-agent/wechat-channels`. |
| `--api-url` / `--base` | Override the proxy URL. |
| `--json` / `--quiet` | Single-line JSON / suppress stderr progress. |
| `--allow-duplicate` | Bypass description and local file-hash duplicate checks. |

The backend content list is the verification source: a unique work must match the description, and scheduled works must also show the requested time and a future-publication label. Intent is saved before submission. Resume verifies uncertain results without submitting again. Login/challenge/account errors, dialogs, rejection, upload timeouts, and lost editors return structured errors and retain the tab and diagnostics. Original-content declarations are not made automatically.

Before submission, fix a rejected short title with `resume --state-file /path/to/job.json --title 'Corrected title'`. This reuses the uploaded video and records the edit. Submitted or completed tasks permit verification only.

The adapter handles the Wujie same-origin iframe and open Shadow DOM using semantic controls rather than random IDs or fixed coordinates. `recent` accumulates all pages and reports completeness. Expired `/login.html` redirects return `LOGIN_REQUIRED`.

```bash
python -m cli.wechat_channels recent --status scheduled --require-complete --json
python -m unittest cli.wechat_channels.tests.test_wechat -v
```

List results include status, ISO timestamps, summary, and completeness; `--require-complete` fails for incomplete lists. Publication-plan integration is documented in [PUBLISHING.md](../PUBLISHING.md). Keep actual plans, account settings, and job state outside version control.

Exit codes: 2 invalid arguments/files; 3 connection/login/account; 4 controls/dialogs/locks; 5 timeout; 6 rejection; 7 duplicate/unverified result; 130 interrupted. Location, collections, links, and declarations retain page defaults. Draft saving is a separate workflow; live validation should cover each intended action using suitable test content.
