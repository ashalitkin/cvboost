# LLM scoring (`cvboost score`)

Verdicts for kept groups (after `cvboost triage`) come from `claude -p` on the owner's Claude
subscription. No API key anywhere: `ANTHROPIC_API_KEY` is never passed to the subprocess and
`claude auth status` with `authMethod: api_key` is refused before any call.

```bash
uv run cvboost score --dry-run --limit 5     # batches and token estimate, no call
uv run cvboost score --limit 5 --max-calls 1 # one real Haiku call (PM smoke only)
uv run cvboost usage                         # calls/tokens/cost per day, pause, auth, prompt version
uv run cvboost jobs --why <id>               # includes the LLM verdict
```

- Config: `config/llm.yaml` (`CVBOOST_LLM_CONFIG`). Models by role (`cheap=haiku`, `strong=opus`),
  daily call caps per model, notional daily cost cap, per-run cap, pause lengths, batch size.
- Safety: tools off (`--tools ""`), `--safe-mode`, no session persistence, empty temp cwd,
  minimal env whitelist, job text only via stdin, never `--bare` (it ignores subscription login).
- Prompt-injection handling: each posting is cut to 6000 chars (head 4500 + tail 1500), invisible
  and bidi characters removed, `& < >` escaped (so `</job>` becomes `&lt;/job&gt;`), output validated
  with pydantic. The LLM only writes a verdict (`verdicts.stage='llm'`); a rule reject is never undone.
- Cache key: sha256 of the rendered posting block + model alias + `PROMPT_VERSION` (derived from
  the prompt, task, schema). Editing the prompt re-scores; the same text is never paid for twice.
- Quota: gates before every call (pause, daily calls, daily cost, per-run cap). `usage_limit` and
  auth errors set a pause (30 min doubling to 5 h / 60 min); collection is unaffected.
  `--ignore-pause` overrides a pause (e.g. right after re-login).
- Errors: invalid items get one retry, then a row in `llm_item_errors` (`llm_error`); `score`
  exits 1. Such groups are skipped until `--retry-errors`.
- The verdict text (`summary_ru`, `eligibility_reason`, `red_flags`) is untrusted: escape it when
  rendering in Telegram (#5) or HTML (#6). Contract for them: `Store.llm_verdicts(prompt_version)`.
- Migration 0003 is additive. Back up before first use on a real DB:
  `sqlite3 data/cvboost.sqlite ".backup data/backup-pre-0003.sqlite"`.

## Round 2 notes

- Every call row is written as `started` before `claude` runs and updated afterwards, so a crash or
  Ctrl-C still counts against the daily caps. Any exception from the backend becomes an `error`
  call (retry, then `llm_error`). `score` reads a clock before every gate and every call, so the
  Lisbon day and pause times follow real time during a long run.
- Circuit breaker: two batches in a row that fail entirely with `error`/`timeout` stop the run
  (`circuit open: ...`). No pause is set and the second batch's items stay pending (no `llm_error`).
- Prompt injection: if a verdict has `prompt_injection` in `red_flags`, only that job's verdict is
  kept (forced `eligible=false`); the other items of the call are not cached and are scored again
  immediately, in the same batch run, in a new call without it (this does not use up the retry; the
  per-run and daily caps still apply, leftovers stay pending).
- `--limit N` takes the N newest groups (by first_seen) among those younger than `max_age_days`,
  before the cache check: already-cached groups use up the limit and cost nothing.
- `--dry-run` also works during a pause. A failed preflight caused by a timeout or an unreadable
  `auth status` stops the run without a pause; only real auth errors pause.
- `Store.llm_errors(prompt_version)` gives canonical_id -> reason; consumers must intersect with
  `kept_canonicals` and prefer a newer verdict.
- `CLAUDE_CODE_VERSION` in the Dockerfile is pinned (2.1.311-1). Bumping it requires re-running the
  `classify` fixtures in `tests/llm/test_claude_cli.py` against the new envelope.
