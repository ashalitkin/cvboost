# Worker and Telegram alerts

`cvboost worker` runs a cycle every 10 minutes (`config/worker.yaml`): collect the sources that
are due, triage, one LLM scoring call, then Telegram. Cycles never overlap; a long cycle delays the
next one (minimum pause 30 s). `cvboost worker --once` runs one cycle without the bot thread;
`--health` exits 0 if the last cycle finished less than 3 ticks ago (Docker healthcheck).

## Locks (`<data dir>/`)

- `worker.lock` - one worker per data directory; a second one exits with code 3.
- `pipeline.lock` - held by a worker cycle and by manual `collect` / `triage` / `score`
  (not `score --dry-run`); a manual command refuses with exit 3 while a cycle runs.

Run one worker per bot. A worker on the Mac and one on the VPS at the same time send duplicates
from two databases and fight over `getUpdates` (HTTP 409).

## Telegram

1. Create the bot in @BotFather, send it `/start` once from the owner account.
2. `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (the owner's numeric id). Never in git, never logged.
   Without them the worker still runs and records alerts as `pending`.
3. No webhook may be set on the bot (it blocks `getUpdates`).

Alerts: a scored group with `eligible` and `fit >= alerts.fit_threshold`, first seen after the worker
first started and not older than `max_alert_lag_h`, with at least one live member (not expired, not
older than `max_age_days`, see `core/freshness.py`). More than `max_per_cycle` per cycle: the rest
goes into one summary message. Earlier jobs stay in the panel (`alerts.status = skipped_backlog`).
Buttons: `Interested`, `No` (then a reason), `Prepare CV`, `Undo`. Only the owner's user id in
the owner's chat is served. Decisions land in `decisions`, CV requests in `cv_requests` (task #7
consumes them). `cvboost alerts` prints the latest alerts and decisions.

System alerts (once per episode/day): LLM auth or usage-limit pause, daily LLM cap reached,
credential expiring, a source failing for `source_fail_alert_after` runs, worker started.

Bot API facts this relies on (checked 2026-11-08 at core.telegram.org/bots/api and /bots/faq):
text 1-4096 characters; `callback_data` 1-64 bytes; `link_preview_options.is_disabled`;
`answerCallbackQuery.text` 0-200 characters; `parameters.retry_after` on flood control (HTTP 429);
about one message per second per chat; `getUpdates` does not work while a webhook is set and
`offset` must be last `update_id` + 1.

## Text safety of alerts

Job text and LLM output are untrusted. Messages are plain text (no `parse_mode`). `safe_text` removes
URLs with a scheme, e-mails and `@mentions`, and replaces the dot between any two alphanumeric labels
(`label.label`: any TLD, IDN, `xn--`; also `。` `．` `｡`) and every dot of a 4-octet IPv4 address with U+2024
(one-dot leader). Dots between two digits (`3.5 years`, `€80.000`, `v2.0`) are kept, so Telegram does not turn
it into a link and the text stays readable: `ASP․NET`, `Monday․com`. The only links in a card are
the posting URLs from `jobs.url`, shown only if the host is the source's own (or a subdomain) and the
URL is ASCII https without credentials or a custom port.

Just before sending, each pending alert is re-checked against its own verdict (`alerts.llm_verdict_id`):
eligible, `fit >= fit_threshold`, no `prompt_injection`; otherwise it becomes `skipped_stale`. If the
group's input hash has changed since the alert was queued (the posting text changed), the verdict for the
*current* hash is used: if it still qualifies the alert is rebound to it (`llm_verdict_id`, `fit`) and sent
with the new text; if the text has not been re-scored yet the alert stays `pending` (and ends as
`skipped_backlog` after `max_alert_lag_h`); if the new verdict no longer qualifies, or the rules no longer
keep the group, it becomes `skipped_stale`. A job is therefore not lost because its text was edited.

Send errors: a transport error, 5xx, 429 or 401/403/404 stops sending for this cycle and does not count
as an attempt; only a 4xx for that very message does (`failed` after `max_attempts`). `400 chat not found`
counts as an auth problem (wrong chat id, owner never sent `/start`). If two different messages in one cycle
get the same 4xx description the cause is systemic: the cycle stops and nothing is counted. System alerts
follow the same rules.

## Operations

- Compose: `worker` has `build: .` and the same tag as `app` (`docker compose build` builds one image).
  `stop_grace_period: 240s` (LLM timeout 180 s + 35 s bot-thread join), `cap_drop: [ALL]`,
  `no-new-privileges`. `read_only` is not set yet.
- Lock files are created `0600` with `O_NOFOLLOW`.

## Known limitations

- A single WWR feed that fails on every run is reported as `ok/partial` and its jobs are never
  collected; no alert is sent for it (only a source that fails as a whole raises `source_failing`).
  Watch `cvboost sources` / `source_runs.error_message`.
- Delivery is at-least-once: a crash between `sendMessage` and the `sent` update can repeat a card.
- If one `getUpdates` update is larger than 2 MB even alone, the bot keeps failing on it (backoff);
  with `allowed_updates=["callback_query"]` this should not happen.

## LLM budget

One scoring call per cycle is up to 144 calls a day, plus night backfill (window 02:00-06:00
Lisbon, up to 20 extra calls). `config/llm.yaml` carries the owner defaults of 2026-11-08 (see the
inbox card): `daily_call_caps.haiku: 170`, `daily_cost_cap_usd: 6.0` (notional). When either cap is
reached the scoring step stops, a `llm_daily_cap` / `llm_cost_cap` system alert is sent once a day and
new jobs wait for scoring until the next day.
