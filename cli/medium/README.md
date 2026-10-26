# Medium CLI

Compose and publish articles through your signed-in Chrome browser and Browser Agent Proxy. No Medium API key, OAuth application, exported cookies, or private publishing endpoint is required. Requires Python 3.9+, the standard library, and extension **2.1.7 / revision 11**. Reload the extension after updating it.

Run from the repository root:

```bash
python -m cli.medium account --json
python -m cli.medium draft --markdown ./article.md
python -m cli.medium post --markdown ./article.md --account YOUR_MEDIUM_HANDLE
python -m cli.medium recent --limit 20 --json
python -m cli.medium status --state-file /path/to/state.json
python -m cli.medium resume --state-file /path/to/state.json
python -m cli.medium resume --state-file /path/to/state.json --publish
```

`draft` saves and reloads the article to verify persistence. `compose` fills and verifies the editor without performing the reload check. `post` verifies the saved draft, opens the site's publication screen, and clicks the final publication button once. It performs a real publication. `resume --publish` explicitly promotes a verified compose job or saved draft; an existing submission cannot be submitted again.

## Article input

```markdown
# Example article

A paragraph before the image.

![Architecture diagram](./images/architecture.png "System architecture")

A paragraph after the image.

## Implementation

More text here.
```

The first level-one heading becomes the title; `--title` overrides it. Without that heading, the filename stem is used. Image paths resolve relative to the Markdown file, including paths wrapped in angle brackets. Images must be local PNG, JPEG, or GIF files, at most 25 MiB each. Remote images must be downloaded before invoking the CLI. The optional quoted caption is applied through the editor. Markdown alt descriptions are retained in task input but are not configured on the image in this version.

Supported Markdown is a strict subset: paragraphs, headings through level three, flat bullet/numbered lists, quotes, fenced code, simple bold/italic/inline-code spans, and HTTP(S) link notation. Markdown links are expanded to visible `label (URL)` text; linked labels are not preserved in this version. Headings map to Medium's header/subheader styles. Code-fence language hints are left to Medium's automatic syntax detection. Nested blocks, nested inline markup, tables, raw HTML, and inline-positioned images are rejected before browser operations. Images are uploaded individually at their positions in the article, preserving interleaving with text.

The workflow uses trusted keyboard input and editor controls. File chooser interception selects a validated local image without requiring an operating-system dialog; it does not invoke a platform API. Draft verification checks title, block order/types, text, image loading, captions, and inline formatting after reloading.

## Options and state

`--api-url` / `--base` overrides the default local proxy at `http://127.0.0.1:12345`. `--account` checks the runtime Medium handle. `--step-timeout` and `--poll-interval` accept positive finite seconds. `--state-dir` and `--state-file` control local task storage. Existing state files are not overwritten. `--allow-duplicate` explicitly bypasses local duplicate protection. `--json` emits single-line JSON; `--quiet` suppresses stderr progress.

State defaults to `$XDG_STATE_HOME/browser-agent/medium`, or `~/.local/state/browser-agent/medium`. State and diagnostics use private permissions and may contain article content, runtime account identity, and local paths. Keep them outside version control. The CLI does not contain account-specific defaults or test fixtures.

Account and recent-list commands close their own temporary tabs. Editing and failed jobs retain their tabs. `recent` is a bounded listing with `complete: false`; it does not claim a full account audit. Publication topics, publications, paywall settings, featured-image settings, and native scheduling are not configured by this CLI; inspect the site's defaults before publishing.

Login expiry, verification challenges, account mismatch, changed files, ambiguous controls, and unsupported editor states stop the job. An uncertain input is not blindly replayed. An uncertain publication is reconciled using the original article ID; a missing success observation remains an error.

## Tests

```bash
python -m unittest cli.medium.tests.test_medium -v
```

Synthetic offline tests cover Markdown parsing, image ordering, invalid inputs, and persistence verification. Live validation covers account detection and saving/reloading a synthetic article with interleaved text, a local image, and a heading. Immediate public publication is not part of that smoke test.
