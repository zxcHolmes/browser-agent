# Publication plans

Run from the repository root with Python 3.9+:

```bash
python -m cli.publish_plan status --plan /path/to/publishing-plan.json
python -m cli.publish_plan sync --plan /path/to/publishing-plan.json
python -m cli.publish_plan run --plan /path/to/publishing-plan.json
python -m cli.publish_plan replan --plan /path/to/publishing-plan.json --daily-times 09:30 12:30 19:30
```

`--plan` is required for every command. `status` reads local state and generates `PUBLISHING_PLAN.md` beside the plan. `sync` reconciles the complete platform content lists without submitting. `run` reconciles first, then schedules the next episode on each platform. Use `--platform douyin` or `--platform wechat_channels` to restrict platforms.

The JSON plan is the source of truth. It contains `schema_version: 1`, `timezone: "Asia/Shanghai"`, `series_name` (display name), `series` (literal marker preceding episode numbers in captions), `platforms` (platform settings including the expected `account`), `cadence`, `episodes`, and `history`. Each episode contains `episode`, `video`, `sha256`, `title`, `description`, `hashtags`, and per-platform state in `platforms`. Initial state uses `status: "planned"` and an ISO `scheduled_at`. An optional `wechat_title` overrides the WeChat short title. Captions must contain the configured series marker followed by the episode number so reconciliation can identify existing works.

`planned` is a local candidate; `in_progress` records an unverified submission; `scheduled` is a platform-confirmed schedule; `published` is observed publication. Resume `uncertain` jobs using their original state files. File locks, duplicate indexes, SHA-256 checks, and immediate atomic saves prevent repeated submissions after partial failures. Existing works, including private works, are skipped.

`cadence.local_times: ["12:30", "19:30"]` defines daily Shanghai time slots; legacy `cadence.local_time` is also supported. Expired candidates or occupied slots roll forward. Accepted schedules retain their original time. `replan` updates only unsubmitted candidates after reconciling both platforms and refuses unresolved jobs.

`run --per-platform 10` adds up to ten submissions per platform (allowed range: 1–20). `run --all --through-episode 40` processes remaining episodes through the specified number. An uncertain result stops further submissions on that platform. Each confirmed result is saved immediately.

Scheduling uses platform-native publication controls. The CLI does not install cron jobs or background services. Candidates outside a platform's scheduling window stop before upload with `schedule_window_limit`; they remain planned for a later run. Douyin uses a 14-day window; WeChat's verified window may be configured with `max_schedule_days`.

```bash
python -m cli.wechat_channels recent --status scheduled --require-complete --json
python -m cli.douyin recent --status published --require-complete --json
```

List results include status, scheduled/publication timestamps, visible evidence, counts, summary, completeness, truncation, and reported totals. `--require-complete` returns a nonzero exit code when pagination or lazy loading is incomplete. Missing previously submitted episodes or ambiguous results require manual review before further publication.

Keep real account settings, plans, job states, screenshots, and private media outside version control. The repository ignores `publishing-plan.json`, its lock, generated `PUBLISHING_PLAN.md`, `publishing-jobs/`, `.local/`, and `private/`.
