# Facebook CLI

Automate Facebook's text/image composer through your signed-in Chrome browser, Browser Agent Proxy, and the extension. No Meta API key, OAuth application, cookie export, or direct private publishing API is used. Requires Python 3.9+, the standard library, and extension revision 10 or newer (revision 11 is recommended).

```bash
python -m cli.facebook account --json
python -m cli.facebook compose --text-file ./post.txt --images ./a.png ./b.jpg
python -m cli.facebook draft --text 'Example draft' --image ./a.png
python -m cli.facebook post --text-file ./post.txt --image ./a.png --account YOUR_CURRENT_PUBLISHING_NAME
python -m cli.facebook compose --page-url https://www.facebook.com/YOUR_PAGE --account YOUR_PAGE_NAME --text 'Example Page update'
python -m cli.facebook recent --limit 20 --json
python -m cli.facebook status --state-file /path/to/state.json
python -m cli.facebook resume --state-file /path/to/state.json
python -m cli.facebook resume --state-file /path/to/state.json --publish
```

Run from the repository root, or use `python /path/to/browser-agent/cli/facebook ...`. The workflow uses the browser's active publishing identity. It does not switch profiles or Pages automatically. `--page-url` requires `--account`; a mismatch stops before input. Group posting, videos/Reels, audience changes, and native scheduling are not configured.

`compose` verifies text and image previews and retains the editor. `draft` uses the save-draft control when available, finds the saved work in the professional content library, and reopens it to verify persistence. The content library can lag the save, so read-only refreshes are allowed. If the account has no persistent draft control, use `compose`; it does not claim that the editor was saved. Ambiguous matching drafts stop recovery. `post` performs a real publication using the site's current audience settings. `resume --publish` explicitly promotes a verified editor or saved draft.

Text accepts `--text` or a UTF-8 `--text-file` and is limited to 63,206 characters. Include a caption for draft/publication verification. `--image` is repeatable; `--images` accepts several paths. CLI image limits are 20 files, 20 MiB each, in PNG, JPEG, GIF, or WebP format; the platform may impose further restrictions. All paths must be readable by Chrome and the proxy.

Use `--api-url` / `--base` to override `http://127.0.0.1:12345`. `--step-timeout` and `--poll-interval` accept positive finite seconds (defaults 60 and 1). `--state-dir` or `--state-file` controls local state; existing state files are not overwritten. `--allow-duplicate` explicitly bypasses local and recent-text duplicate checks. `--json` outputs single-line JSON; `--quiet` suppresses stderr progress.

State defaults to `$XDG_STATE_HOME/browser-agent/facebook`, or `~/.local/state/browser-agent/facebook`. Runtime identities, content, local paths, and diagnostics belong in private local state, not the repository. Account/recent commands close only their own tabs; editors and failures retain theirs.

The CLI records input intent and never automatically repeats uncertain submissions. A save/share toast is insufficient evidence. Drafts require reopening; publications require a unique new matching work in the current account's own content list. Recent lists are bounded and report `complete: false`; UI changes or truncated content may prevent confirmation even when the operation succeeded. Resume reconciles existing results rather than publishing again.

```bash
python -m unittest cli.facebook.tests.test_facebook -v
```

Tests use synthetic identities and files. Live checks exercise account detection, text/image composition, and the professional content-library draft flow. Public publication is not required for the smoke test.
