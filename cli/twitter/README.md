# X (Twitter) posting CLI

`cli/twitter` publishes posts on x.com — text, up to four images, a GIF, or a video, with or without text — through the web composer in your own signed-in Chrome, driven by the Browser Agent Proxy → Chrome extension. No X API keys are needed. It can also list the account's recent posts so a caller never posts the same thing twice.

Requirements:

- Python 3.8+, standard library only.
- The proxy running (default `http://127.0.0.1:12345`) and the extension at implementation revision ≥ 8 (extension 2.1.4).
- Chrome signed in to x.com with the account you want to post from.

Run it from the repository root as a module, or by path from any directory:

```bash
python3 -m cli.twitter --help
python3 -m cli.twitter post --help
python3 /path/to/browser-agent/cli/twitter post --help
```

## Posting

```bash
# Text only (line breaks, emoji, links and any language are kept as written)
python3 -m cli.twitter post --text "Hello from the command line"
python3 -m cli.twitter post --text-file post.txt

# Text with images (up to 4: jpg, png, webp ≤ 5 MB each) — or images alone
python3 -m cli.twitter post --text "Before and after" --image before.png --image after.png
python3 -m cli.twitter post --image chart.png

# One GIF (≤ 15 MB) or one video (mp4/mov ≤ 512 MB), with or without text
python3 -m cli.twitter post --image reaction.gif
python3 -m cli.twitter post --text "New episode" --video episode.mp4

# Refuse to post if Chrome is signed in to another account
python3 -m cli.twitter post --account my_handle --text "…"
```

Attachment rules are checked locally before the browser is touched: up to 4 images, or 1 GIF, or 1 video, never mixed. The video's length limit depends on the account, so it is left to x.com: a rejected attachment fails with `MEDIA_REJECTED` and x.com's own message.

What a run does:

1. Opens a tab of its own (your tabs are never touched), loads x.com and reads the signed-in handle from the page; `--account` must match it.
2. Unless `--allow-duplicate`, reads the newest posts on the profile and stops with `DUPLICATE_POST` (exit 7) if one has the same text. Links, letter case and spacing are ignored, since x.com shows links shortened; a post cut off with "Show more" matches by prefix. A media-only post has no text to compare, so only the local index applies to it.
3. Records the content (text + SHA-256 of each file + account) in a local index: the same content sent again from a new job fails with `DUPLICATE_UPLOAD` unless `--allow-duplicate`.
4. Opens the composer, fills the text, attaches the files and waits until x.com has finished uploading and processing them — the Post button stays disabled until then, in every UI language (`--media-timeout`, default 900 s).
5. Closes x.com's hashtag/mention suggestion list if the text left it open (its overlay covers the Post button), then presses Post **once**, then waits for x.com's confirmation (the "post sent" notice with its link, or the composer closing), and finally finds the post on the profile by ID — or, without an ID, as the newest own post since the attempt with the same text and attachments.

A result carries `post_id`, `post_url`, `account`, `state_file` and `evidence`; progress goes to stderr (`--quiet` silences it, `--json` prints the result on one line).

## Recent posts and duplicate checks

```bash
python3 -m cli.twitter recent --limit 10 --json --quiet
python3 -m cli.twitter recent --text "Hello from the command line" --json --quiet   # see `duplicate`, `text_matches`
```

Each post has `id`, `url`, `datetime` (ISO 8601, UTC), `text`, `truncated`, `pinned`, `images` and `video`. Only the account's own posts are listed — reposts of others are skipped — newest first, scrolling the profile until `--limit` (default 20, max 200) or the end. The command only reads and closes its tab when done.

## State and recovery

Each `post` is a job with a state file under `$XDG_STATE_HOME/browser-agent/twitter` (default `~/.local/state/browser-agent/twitter`; `--state-dir`, `--state-file` to change). A failure prints a ready-to-run `resume_command`.

```bash
python3 -m cli.twitter status --state-file /path/state.json   # local record only
python3 -m cli.twitter resume --state-file /path/state.json
```

- Before Post is pressed nothing is public, so `resume` simply composes again in a fresh composer.
- `post_intent` is recorded right before the click. If the extension refuses the click before acting (the button covered or gone — `ACTION_NOT_PERFORMED`, exit 4), the intent is withdrawn and `resume` composes and posts normally. Otherwise, after the intent, `resume` **never presses Post again**: it only looks for the post on the profile and either records it or fails with `POST_UNVERIFIED` for a person to check.
- When the API asks to leave or close a page, the extension accepts the page's "leave site?" prompt, so an abandoned draft never blocks a run.

On failure the job's tab is kept and a screenshot plus the last page observation are saved next to the state file (private permissions — they show your account). On success the tab is closed unless `--keep-tab`.

| Exit code | Meaning |
| --- | --- |
| 0 | Posted (or `status` read the local state) |
| 2 | Invalid arguments, text, media or state file |
| 3 | Proxy/extension unavailable or too old, not signed in, captcha/verification, account locked, wrong account |
| 4 | Page control missing, an action refused before it ran (`ACTION_NOT_PERFORMED`, safe to resume), job lock or other execution problem |
| 5 | A step, the media upload or processing timed out |
| 6 | x.com rejected the post or an attachment, or the Post button stayed disabled |
| 7 | Outcome unknown after an action, duplicate content (`DUPLICATE_POST`, `DUPLICATE_UPLOAD`), or the post could not be verified |
| 130 | Interrupted; resume to continue |

## How the page is read

`probe.js` is read-only and keys off what x.com keeps stable in every UI language: URLs (`/compose/post`, `/i/flow/login`, `/account/access`, `/<handle>/status/<id>`), `data-testid` attributes (`tweetTextarea_0`, `fileInput`, `attachments`, `tweetButton`, `tweet`, `tweetText`, `AppTabBar_Profile_Link`, `toast`) and ARIA roles (`dialog`, `alert`). Selectors come from the shared `locate` helper ([cli/common](../common/README.md#locating-controls)). Post text is read with emoji (rendered as images) restored and shortened links dropped. The only UI text the CLI uses is what it reports back: x.com's own error messages, verbatim.

## Tests

```bash
python3 -m unittest cli.twitter.tests.test_twitter -v
```

The offline tests cover the text comparison, attachment rules, argument validation, stop conditions (sign-in, captcha, locked account, wrong account), the single Post click, rejection and unknown outcomes, reconciliation on resume, the duplicate check and timeline reading.

## Known limits

- Replies, quote posts, threads, polls, scheduling, alt text and the audience selector are not supported.
- The duplicate check sees only recent posts (`--duplicate-window`, default 20) and compares text; keep your own record when publishing in bulk.
- The text-length limit depends on the account (premium accounts allow longer posts); the CLI relies on x.com, which disables Post when the text is too long (`POST_BLOCKED`).
- Verified against x.com with a Simplified Chinese UI: text only, text with two images, text with a video (ending in hashtags), the duplicate check and `recent`. GIFs and image-only posts follow the same path but were not posted live.
