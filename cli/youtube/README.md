# YouTube upload CLI

`cli/youtube` uploads and publishes videos through YouTube Studio's own web UI, driven by the Browser Agent Proxy (Go) → Chrome extension → your normal Chrome. No YouTube Data API key is needed.

Requirements:

- Python 3.8+, standard library only; `ffmpeg` on `PATH` only when a thumbnail has to be extracted or converted.
- The proxy running (default `http://127.0.0.1:12345`) and the extension at implementation revision ≥ 6 (extension 2.1.2).
- Chrome signed in to an account that can manage the target channel.

Run it from the repository root as a module, or by path from any directory (handy when another tool calls it):

```bash
python3 -m cli.youtube --help
python3 -m cli.youtube upload --help
python3 /path/to/browser-agent/cli/youtube upload --help
```

The examples below use the first form.

## Upload and publish

Upload a local video and keep it as a draft:

```bash
python3 -m cli.youtube upload \
  --video "/path/to/my video.mp4" \
  --title "Video title" \
  --description "Video description" \
  --mode draft
```

Upload and publish immediately (the CLI also clicks the final Publish button):

```bash
python3 -m cli.youtube upload \
  --video /path/to/video.mp4 \
  --title-file /path/to/title.txt \
  --description-file /path/to/description.txt \
  --mode publish \
  --visibility public \
  --audience not-made-for-kids \
  --channel-id UCxxxxxxxxxxxxxxxxxxxxxx
```

- `--audience` (`made-for-kids` / `not-made-for-kids`) is required for `publish`; choose it truthfully for the content.
- `--channel-id` is optional but recommended for accounts with several channels; without it the current Studio channel is used and recorded.
- `--visibility` is `public` (default for `publish`), `unlisted` or `private`. Scheduled publishing is not supported.

A direct video-file URL is downloaded into the job directory first:

```bash
python3 -m cli.youtube upload \
  --video-url https://example.com/media/video.mp4 \
  --title "Title" --mode draft \
  --max-download-mb 4096 --download-timeout 120
```

The URL must point at the file itself — not a YouTube/TikTok player page; DRM, authenticated downloads and page scraping are out of scope. A `--video` path must exist on the machine running the CLI and be readable by Chrome at the same absolute path; a remote proxy does not transfer files to a remote Chrome.

Title and description files are UTF-8 (a BOM is accepted); description line breaks are kept and a one-line title is trimmed. Timing options: `--upload-timeout` and `--processing-timeout` (1800 s each), `--step-timeout` (60 s) and `--poll-interval` (2 s). They bound the CLI's polling; each HTTP request has its own timeout, so a run can take slightly longer. The job's tab is created with `autoClose: false` so it is not idle-closed during an upload.

### Copyright and restriction checks

Studio allows publishing while its checks are still running; they continue after publication. By default the CLI publishes as soon as the upload is complete and the final button is enabled, and stops only if the checks have already reported an issue. Pass `--wait-checks` to wait for the checks to finish first (several extra minutes for a Short).

## Thumbnails

Without one, YouTube picks a frame itself, which can be an unsuitable one such as an end card. Set it while uploading:

```bash
# An image (jpg/png up to 2 MB is uploaded as is; anything else is converted to jpg with ffmpeg)
python3 -m cli.youtube upload ... --thumbnail /path/cover.png
# Or a frame of the video at N seconds (needs ffmpeg)
python3 -m cli.youtube upload ... --thumbnail-at 5
```

The thumbnail is chosen after the title and description, and the run continues only once the form shows a custom thumbnail. To replace the thumbnail of a video that is already up:

```bash
python3 -m cli.youtube thumbnail --video-id VIDEO_ID --thumbnail /path/cover.png
python3 -m cli.youtube thumbnail --video-id VIDEO_ID --video video.mp4 --thumbnail-at 5
```

This opens the video's edit page, uploads the image, saves, reloads and checks that a custom thumbnail is still shown. A channel that may not use custom thumbnails (for example, not yet phone-verified) gets `THUMBNAIL_NOT_ALLOWED` (exit 6) and nothing is changed. Desktop Studio offers custom thumbnails for Shorts too. The public channel page may show the new image only after its cache refreshes.

## Avoiding duplicate uploads

```bash
# Newest videos and Shorts (first page of each list, newest first):
# video_id, title, visibility (public/unlisted/private/draft), date
python3 -m cli.youtube recent --json --quiet

# Is there already a video with this title? (case and extra whitespace ignored, drafts included)
# See `duplicate` and `title_matches` in the result.
python3 -m cli.youtube recent --title "Video title" --json --quiet
```

Options: `--list videos|shorts|all` (default `all`), `--limit` per list (default 30), `--channel-id`. The command only reads, writes no state file and closes its own tab.

`upload` runs the same title check (newest 50 videos and Shorts) before choosing the file. A match fails with `DUPLICATE_TITLE` (exit 7, the existing items in `details.matches`) and nothing is uploaded. Pass `--allow-duplicate` to upload anyway (this also bypasses the local SHA-256 check below). Older videos beyond the first page are not seen, so a batch publisher should keep its own record of published titles or video IDs.

## State and recovery

Progress goes to stderr; one JSON result goes to stdout (`--quiet` silences progress, `--json` prints the result on one line). A result carries `stage`, `video_id`, `video_url`, `state_file` and `evidence`; a failure adds `code`, `message`, `details` and a ready-to-run `resume_command`.

State lives in `$XDG_STATE_HOME/browser-agent/youtube` (default `~/.local/state/browser-agent/youtube`). `--state-dir` moves the state and the de-duplication index; `--state-file` fixes one job's JSON path. A resumed job reuses the original API URL unless `--api-url` overrides it.

```bash
# Print the local record only; does not contact the browser or re-check Studio
python3 -m cli.youtube status --state-file /path/state.json

# Continue after handling a login or verification prompt in the kept tab
python3 -m cli.youtube resume --state-file /path/state.json

# Publish a draft the CLI saved and verified
python3 -m cli.youtube resume \
  --state-file /path/state.json --mode publish \
  --audience not-made-for-kids --visibility public
```

State is written atomically under a per-job process lock. The upload intent is recorded before the file is chosen and de-duplicated by SHA-256 + channel: a repeated file returns the existing job's path unless `--allow-duplicate` is given. This index covers only jobs run with the same `--state-dir`; it is not a scan of the channel's history. Do not delete state or switch state directories to get around an unknown outcome.

File selection, upload completion, publish readiness, draft saving and the final publish are each verified:

- A draft is saved once the upload completes, by closing the wizard; the CLI then finds the draft by video ID in the content list and reopens it to compare title and description. It never presses the final button.
- An immediate publish proceeds once Studio reports processing done or enables the final button; HD processing is not awaited. `processing_complete` in the state is true only when an explicit "processing complete" message was read.
- Publication is confirmed by Studio's confirmation dialog and by the content-list row with the same video ID, title and visibility. See YouTube's [upload help](https://support.google.com/youtube/answer/57407) for how closing the wizard and visibility behave.

`publish_intent` is recorded before the final click, which is sent exactly once. If its reply is lost, `resume` only reconciles against the content list and never clicks again. When the original video ID cannot be found, the original tab is gone with no ID recorded, the UI is not recognised or the outcome is otherwise uncertain, the CLI stops with an error for a person to check.

On failure the job's own tab is always kept, and a screenshot plus the last page observation are saved in the job directory (with private file permissions — they can contain account and video details). On success the tab is closed unless `--keep-tab` is given. The CLI never touches tabs it did not create; do not switch the job's tab to another upload while it runs.

| Exit code | Meaning |
| --- | --- |
| 0 | Done (also: `status` read the local state) |
| 2 | Invalid arguments, media, download or state file |
| 3 | Proxy/extension unavailable, login, permission or channel problem |
| 4 | Page control, validation, job lock or other execution problem |
| 5 | Upload, processing or step timed out |
| 6 | YouTube rejected the upload, or a check needs manual review |
| 7 | Outcome of a performed action unknown, duplicate upload (incl. `DUPLICATE_TITLE`), video identity or publication not verified |
| 130 | Interrupted by the user; the job can be resumed |

## How controls are located

`probe.js` is read-only and reports each control with a selector built by the shared `locate` helper (rules in [cli/common](../common/README.md#locating-controls)); the extension resolves it again right before acting and requires exactly one visible match. On Studio this yields selectors such as `#next-button`, `#done-button`, `input[name="Filedata"]`, `[name=PUBLIC]`, `.ytcpAppHeaderCreateIcon button`, and content-list rows scoped by video ID (`ytcp-video-row:has(img[src*="/vi/<id>/"])`) — no generated ids, and `aria-label` only as a last resort.

## Files

- `main.py` — arguments, the `YouTubeWorkflow` (a [`PageWorkflow`](../common/README.md)), checkpoints and recovery.
- `probe.js` — the read-only DOM adapter for Studio, including the UI strings it recognises.
- `__main__.py` — entry point for `python3 -m cli.youtube` and `python3 path/to/cli/youtube`.
- `tests/` — offline unit tests, and a live smoke test against `tests/fixtures/youtube_studio.html`.

## Tests

```bash
# No browser: errors, de-duplication, locks, unknown-publish recovery, thumbnails
python3 -m unittest cli.youtube.tests.test_youtube -v

# Real proxy + extension + Chrome against a temporary localhost mock of Studio;
# uploads only test bytes to localhost, never to YouTube
python3 -m cli.youtube.tests.youtube_smoke
```

## Compatibility and known limits

Verified against real YouTube Studio (Simplified Chinese UI): uploading and publishing a Short, recovering a draft by video ID after the job's tab was lost, `recent` and the title check, and replacing a published video's thumbnail. The full draft / resume / publish flow is also covered by `youtube_smoke` on the local mock.

Text matching for the English and Traditional Chinese UIs (button names, status messages, confirmation dialogs) is written from the UI strings but not yet verified on live pages; an unrecognised state stops the run with the page kept, rather than guessing. Custom channel setup, extra agreements, monetisation questionnaires, copyright disputes, captchas and Studio redesigns may need a person or a probe update; the CLI does not try to bypass them. When the content list does not show the video, the CLI tries the visible Shorts tab, still limited to the loaded rows, so an old draft beyond the first page may be reported as not found.
