# TikTok posting CLI

`cli/tiktok` posts a video to TikTok — or saves it as a draft — through TikTok Studio's web uploader in your own signed-in Chrome, driven by the Browser Agent Proxy → Chrome extension. No TikTok API access is needed. It can also list the account's recent posts and drafts so a caller never posts the same thing twice.

Requirements:

- Python 3.8+, standard library only.
- The proxy running (default `http://127.0.0.1:12345`) and the extension at implementation revision ≥ 9 (extension 2.1.5).
- Chrome signed in to tiktok.com with the account you want to post from.

Run it from the repository root as a module, or by path from any directory:

```bash
python3 -m cli.tiktok --help
python3 -m cli.tiktok post --help
python3 /path/to/browser-agent/cli/tiktok post --help
```

## Posting

```bash
# A video with a caption; each --hashtag is appended after a blank line
python3 -m cli.tiktok post --video clip.mp4 \
  --caption "Why prices end in 9" --hashtag MoneyBrain --hashtag Psychology

# Caption from a UTF-8 file (line breaks kept); #hashtags inside it work too
python3 -m cli.tiktok post --video clip.mp4 --caption-file caption.txt

# Save as a draft instead — finish and post it later, e.g. from the TikTok app
python3 -m cli.tiktok post --video clip.mp4 --caption "…" --draft

# Who can watch, the "AI-generated content" label, and an account check
python3 -m cli.tiktok post --video clip.mp4 --caption "…" \
  --visibility friends --ai-generated --account my_handle
```

| Option | Meaning |
| --- | --- |
| `--video PATH` | The video (mp4 recommended; TikTok Studio accepts up to 30 GB / 60 minutes). Required. |
| `--caption TEXT` / `--caption-file PATH` | The caption, up to 4,000 characters. Without one, the caption is left empty. |
| `--hashtag TAG` | Append a hashtag, with or without `#` (letters, digits, `_`); repeatable. |
| `--visibility everyone\|friends\|private` | Who can watch (default `everyone`). |
| `--ai-generated` | Turn on TikTok's "AI-generated content" label. Without it the label is turned **off**, whatever the page started with. |
| `--draft` | Press **Save draft** instead of **Post**. |
| `--account HANDLE` | Refuse to run if Chrome is signed in to another account. |
| `--allow-duplicate` | Skip both duplicate checks (below). |
| `--duplicate-window N` | How many recent posts the duplicate check reads (default 20). |
| `--upload-timeout S` | Seconds to wait for the upload (default 1800). |
| `--step-timeout S`, `--poll-interval S` | Per-step timeout (default 60) and page read interval (default 2). |
| `--state-dir`, `--state-file` | Where the job state lives. |
| `--keep-tab` | Keep the tab open after success (it is always kept on failure). |

What a run does:

1. Opens a tab of its own (your tabs are never touched), loads TikTok Studio's content list and reads the signed-in handle; `--account` must match it.
2. Unless `--allow-duplicate`, reads the newest posts and all drafts and stops with `DUPLICATE_POST` (exit 7) if one has the same caption (links, letter case and spacing ignored). Then records the video's SHA-256 + account in a local index: the same video sent again from a new job fails with `DUPLICATE_UPLOAD`.
3. Opens the uploader, closes any product tour covering the form, selects the file and waits until TikTok has received it.
4. Writes the caption. A `#hashtag` only becomes a real hashtag when it is picked from TikTok's suggestion list, so each one is typed and picked (the list's first entry is always the typed tag, even a brand-new one). A tag that is never offered stays plain text and is reported in `plain_hashtags`.
5. Sets who can watch, turns the AI label on or off, and checks the caption again.
6. Presses **Post** (or **Save draft**) **once**. If TikTok asks whether to post before its content check has finished, the CLI answers *post now*: the check runs on after posting, as it would in the app. It then waits for TikTok to leave the uploader.
7. Finds the result: a post in the content list (created after the attempt, same caption) with its id and URL, or the draft in the draft list.

A result carries `post_id`, `post_url` (for a draft: the draft list), `account`, `state_file` and `evidence`; progress goes to stderr (`--quiet` silences it, `--json` prints the result on one line).

Drafts are stored by TikTok, not by the browser: they are private, available on all your devices (TikTok Studio and the app), up to 30 at a time, and deleted after 60 days.

## Recent posts and duplicate checks

```bash
python3 -m cli.tiktok recent --limit 10 --json --quiet
python3 -m cli.tiktok recent --drafts --json --quiet
python3 -m cli.tiktok recent --caption "Why prices end in 9 #MoneyBrain" --json --quiet   # see `duplicate`
```

Each post has `id`, `url`, `kind` (`video` or `photo`), `created_at` (unix seconds, decoded from the id), `caption` and `privacy` (the label TikTok Studio shows, in its UI language; right after posting it can read "only me" for a few minutes while TikTok processes the video). Drafts have only `caption` and `row`, since TikTok gives them no public id. With `--caption`, `caption_matches`, `draft_matches` and `duplicate` say whether it was already used. The command only reads and closes its tab when done.

## State and recovery

Each `post` is a job with a state file under `$XDG_STATE_HOME/browser-agent/tiktok` (default `~/.local/state/browser-agent/tiktok`). A failure prints a ready-to-run `resume_command`.

```bash
python3 -m cli.tiktok status --state-file /path/state.json   # local record only
python3 -m cli.tiktok resume --state-file /path/state.json
```

- Before Post is pressed nothing is public, so `resume` simply uploads again in a fresh uploader (TikTok does not keep an unfinished upload).
- `post_intent` is recorded right before the click. If the click is refused before it happens (`ACTION_NOT_PERFORMED`), or TikTok answers with a message while staying on the uploader (`POST_REJECTED`), the intent is withdrawn and `resume` tries again — its duplicate check would still catch a post that did go out. Otherwise `resume` **never presses Post again**: it only looks for the post (or draft) and records it, or fails with `POST_UNVERIFIED` / `DRAFT_UNVERIFIED` for a person to check.
- Page loads are retried on a network failure; a page that still cannot load stops with `NAVIGATION_FAILED` (exit 5).

On failure the job's tab is kept and a screenshot plus the last page observation are saved next to the state file (private permissions — they show your account). On success the tab is closed unless `--keep-tab`.

| Exit code | Meaning |
| --- | --- |
| 0 | Posted or saved as a draft (or `status` read the local state) |
| 2 | Invalid arguments, caption, video or state file; the video changed after the job was prepared |
| 3 | Proxy/extension unavailable or too old, not signed in, captcha/verification, wrong account |
| 4 | Page control missing, an action refused before it ran (`ACTION_NOT_PERFORMED`, safe to resume), an unexpected dialog, job lock |
| 5 | A step or the upload timed out, or a page could not be loaded |
| 6 | TikTok rejected the upload or the post, or the Post button stayed disabled |
| 7 | Outcome unknown after an action, duplicate content (`DUPLICATE_POST`, `DUPLICATE_UPLOAD`), or the result could not be verified |
| 130 | Interrupted; resume to continue |

## How the page is read

`probe.js` is read-only and keys off what TikTok Studio keeps stable in every UI language: URLs (`/tiktokstudio/upload`, `/tiktokstudio/content`, `?tab=draft`, `/@<handle>/video/<id>`), `data-e2e` attributes (`caption_container`, `upload_status_container`, `video_visibility_container`, `advanced_settings_container`, `aigc_container`, `post_video_button`, `save_draft_button`), the visibility options' `data-value` (TikTok's privacy levels: 0 everyone, 2 friends, 1 only you), the design system's semantic class names (`TUXModal`, `TUXButton--primary`, `Switch__content`, `.info-status.success`, `.mention-list-popover`) and the content table's `data-tt` component names. Selectors come from the shared `locate` helper ([cli/common](../common/README.md#locating-controls)) or are semantic selectors checked to match exactly one visible element. The only UI text the CLI uses is what it reports back: TikTok's own messages, verbatim.

## Tests

```bash
python3 -m unittest cli.tiktok.tests.test_tiktok -v
```

The offline tests cover caption building and hashtag picking, argument validation, stop conditions, visibility, the AI label (on, and turned off by default), the single Post / Save draft click and the "post anyway?" dialog, rejection and unknown outcomes, reconciliation on resume, and the duplicate checks against posts and drafts.

## Known limits

- One video per post. Photo posts, scheduling, covers, location, music, mentions, comment / duet / stitch permissions and the content disclosure setting are left at TikTok's defaults.
- The duplicate check compares captions; a post with an empty caption is only covered by the local index.
- Verified against TikTok Studio with a Simplified Chinese UI: a draft and a public post (with hashtags, the AI label and the "post anyway?" dialog), the duplicate check against drafts, and `recent`.
