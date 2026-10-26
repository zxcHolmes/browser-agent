# LinkedIn CLI

Post text and images through LinkedIn's website in your signed-in Chrome browser. Uses Browser Agent Proxy and the extension, with no LinkedIn API key, OAuth application, exported cookies, or direct private API requests. Requires Python 3.9+, the standard library, and extension revision 10 or newer (revision 11 is recommended).

```bash
python -m cli.linkedin account --json
python -m cli.linkedin compose --text-file ./post.txt --images ./a.png ./b.jpg
python -m cli.linkedin draft --text-file ./post.txt --image ./a.png
python -m cli.linkedin post --text 'Example product update' --image ./a.png --account YOUR_PROFILE_SLUG
python -m cli.linkedin recent --limit 20 --json
python -m cli.linkedin status --state-file /path/to/state.json
python -m cli.linkedin resume --state-file /path/to/state.json
python -m cli.linkedin resume --state-file /path/to/state.json --publish
```

Run from the repository root, or run `python /path/to/browser-agent/cli/linkedin ...` from another directory. The current adapter targets personal posts. Company Page identity selection, articles, documents, videos, and native scheduling are not configured.

`compose` fills and verifies the editor and retains the tab. `draft` saves through LinkedIn's confirmation dialog, reopens the composer, and checks the text and image count. Existing unrelated drafts are preserved with `EXISTING_DRAFT`. `post` performs a real publication. `resume --publish` explicitly publishes a verified editor or saved draft. Submitted jobs cannot be published again.

| Option | Behavior |
|---|---|
| `--text` / `--text-file` | Mutually exclusive; UTF-8 file input; at most 3000 characters. Include a caption for draft/publication verification. |
| `--image PATH` / `--images PATH...` | Repeat `--image` or pass several paths. CLI limits: 20 images, 20 MiB each; PNG, JPEG, GIF, or WebP. Site limits also apply. |
| `--account` | Expected personal profile slug; read from the browser at runtime and stop on mismatch. |
| `--api-url` / `--base` | Browser Agent Proxy URL; default `http://127.0.0.1:12345`. |
| `--state-dir` / `--state-file` | Local state directory / new state file; existing files are rejected. |
| `--step-timeout` / `--poll-interval` | Positive finite seconds; defaults 60 / 1. |
| `--allow-duplicate` | Explicitly bypass local and recent-text duplicate checks. |
| `--json` / `--quiet` | Single-line JSON / suppress stderr progress. |

State defaults to `$XDG_STATE_HOME/browser-agent/linkedin`, or `~/.local/state/browser-agent/linkedin`. It contains job content, runtime account identity, input fingerprints, and diagnostics. Private permissions are used; keep state outside version control. Account and recent-list commands close their own tabs; editing and failed jobs retain theirs.

Every mutation records intent before browser input. Partial edits are not replayed automatically. Resume verifies the retained editor or saved draft. After a publication intent, recovery only checks for a unique new matching work in the account activity list; it never clicks Post again. A toast alone does not prove publication. Recent lists are bounded, report `complete: false`, and may omit text hidden behind expansion controls; missing evidence returns an unverified outcome.

```bash
python -m unittest cli.linkedin.tests.test_linkedin -v
```

Tests use synthetic accounts and files. Live validation covers account detection, text/image composition, draft saving, and reopening with matching attachments. Immediate public publication is not part of that smoke test.
