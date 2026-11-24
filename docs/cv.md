# CV drafting (`cvboost cv`)

The owner presses "Prepare CV" on a job (Telegram button or panel). That creates a `cv_requests`
row (`queued`). The worker's **cv step** takes it, makes one `claude -p --model opus --tools ""`
call and writes an application kit. Nothing is ever sent or submitted: the owner reads and sends.

```
worker cycle: collect -> triage -> score -> notify -> cv -> notify (only if cv finished something)
```

## What the model gets and returns

- System prompt (static): the tailoring rules (`llm/prompts/cv_v1.py`, adapted from Steps 2, 3 and 5
  of the `resume` skill) + the whole `config/resume/profile.md` without "Applications log".
  The profile is the **only** source of facts.
- stdin: the posting in a `<job>` block (sanitised, `& < >` escaped, head 9000 + tail 3000 chars).
  The posting is data: instructions inside it are ignored and flagged `injection_suspected`.
- Answer (JSON schema, checked again by pydantic, `cv/schema.py`): `resume_md`,
  `recruiter_message {connection_note, inmail}`, `fit_notes_ru`, `claims[]` (each statement with
  the profile section and a verbatim quote), `injection_suspected`.

## Checks before a kit counts as ready (`cv/validate.py`, no LLM)

| check | result |
|---|---|
| a Known-gaps term (`gaps.yaml`, `mode: forbid`): Power BI, Tableau, SFMC, Dataform, MMM, "data scientist" | one retry with feedback, then `needs_review` |
| a number in `resume_md` that is not in the profile; a claim whose quote is not in its section | same |
| missing `# <name>` (the name from profile.yaml), `## Experience`, `## Education`; emoji; images other than `photo.jpg`; non-https links; raw HTML; money in the recruiter message | same |
| a `mode: review` term (HIPAA / healthcare, communities analytics, push / email campaigns) | `needs_review` at once |
| `injection_suspected` | `needs_review` at once |
| PDF could not be built (`pdf_failed`, or `pdf_unavailable` when pango is missing) | `needs_review`, markdown is kept |

Nothing may point anywhere but the owner's contacts: every e-mail, link and domain in `resume_md` and
in the recruiter message must come from the profile's **Contacts** section (`foreign_contact`,
`mode review`, never auto-`done`). Money amounts in the resume or the message are a violation too,
and numbers are checked against the fact sections only (not the Applications log).

A rejected draft is retried in the **next cycle** (at most `cv.max_attempts` = 2 calls per request,
one Opus call per cycle). `usage_limit` / `auth_error` pause **all** LLM work (scoring too) and give
the attempt back. `timeout`, `error`, `invalid_output` use the attempt; after the last one the request
is `failed` (short safe text in `error`). A `needs_review` kit is still written: the owner reads it and
approves it in the panel (7b).

The kit is locked (`kits/{id}.lock`, waits up to 20 s) **before** the Opus call and held until the kit is
written: a busy kit costs nothing (the attempt goes back), and a finished draft is never thrown away
because the panel grabbed the lock. A request whose attempts are used up (also after a worker crash)
becomes `failed` with `finished_at` and a `cv_failed` alert.

Limits: `daily_call_caps.opus`, `daily_cost_cap_usd`, preflight, pause: the same rules as scoring
(`llm/budget.py`). `cv.timeout_s` 300, `cv.max_budget_usd_per_call` 1.5 (notional), see `config/llm.yaml`.

## The kit on disk

`{CVBOOST_KITS_DIR}/{canonical_id}/` (default: `kits/` next to the SQLite file; in Docker
`/data/kits`): `resume.md`, `resume.pdf`, `recruiter_note.md`, `fit_notes.md`, `claims.json`.
Directories 0700, files 0600, written atomically; a symlink in place of the directory is refused.
A new generation moves the old files to `prev/` (one generation). `kits/{id}.lock` is held while
writing (the panel takes the same lock).

## PDF

markdown-it (`html=False`, only `{.class}` attributes) -> HTML -> WeasyPrint. The URL fetcher serves
only `config/resume/photo.jpg`; everything else (files, network, `data:`) is refused. Fonts:
Liberation Sans (metric-compatible with Arial; `fonts-liberation2` in the image). On a Mac:
`brew install pango`, and if Python cannot find the libraries run with
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`. Without them the kit is saved without a PDF
(`pdf_unavailable`).

## Config files (`config/resume/`)

`config/resume` is in `.dockerignore`: the profile (contacts, work history) is never baked into the
image. `app` and `worker` get it from the `./config:/app/config:ro` mount, with
`CVBOOST_RESUME_DIR=/app/config/resume`. Without the mount `cv check-profile` fails with
"cannot read config/resume/profile.md" and the CV step alerts `cv_config`.

| file | |
|---|---|
| `profile.md`, `SKILL.md`, `photo.jpg`, `resume.css` | copied from the resume skill by `scripts/sync_resume.sh` (Mac only) |
| `gaps.yaml` | guards for every bullet of "Known gaps" in `profile.md` (`source` = text before the first `:`), regexes, `forbid` / `review` |

`scripts/sync_resume.sh` copies the files, compares sha256 of `SKILL.md` with `RULES_SOURCE_SHA` in
`cv_v1.py` (changed skill -> review the rules and update the constant), then runs
`cvboost cv check-profile`. A Known-gaps bullet without an entry in `gaps.yaml` is a config error:
the CV step does not run, the request stays `queued`, a `cv_config` alert goes to Telegram.

## Commands

```bash
uv run cvboost cv check-profile                 # profile.md and gaps.yaml agree?
uv run cvboost cv run --dry-run                 # prompt sizes and PROMPT_VERSION, never calls claude
uv run cvboost cv run [--request ID]            # ONE real Opus call (PM smoke only; takes the pipeline lock)
uv run cvboost cv render resume.md out.pdf      # markdown -> PDF with the same renderer
uv run cvboost cv list                          # recent requests: status, attempts, pdf, reasons
```

Env: `CVBOOST_KITS_DIR`, `CVBOOST_PANEL_URL` (optional; the "CV ready" message links to
`{panel}/jobs/{canonical_id}`, otherwise it names the job), `CVBOOST_RESUME_DIR` (default `config/resume`).

Telegram (system alerts): `cv_ready`, `cv_review`, `cv_failed`, `cv_config`, `cv_stopped`.
Rollback: `cv.enabled: false` in `config/llm.yaml` and restart the worker; schema 0006 is compatible
with the old code. Take `sqlite3 data/cvboost.sqlite ".backup data/backup-pre-0006.sqlite"` before the
first run on a real database (0006 rebuilds `llm_calls` and `cv_requests`).

## Rollout (first deploy of 0006)

0006 rebuilds `llm_calls` and `cv_requests`, so do it once, by hand, with everything else stopped:

```bash
docker compose stop worker web                        # nothing may write to the database
sqlite3 data/cvboost.sqlite ".backup data/backup-pre-0006.sqlite"
sqlite3 data/backup-pre-0006.sqlite "PRAGMA integrity_check; PRAGMA foreign_key_check;"   # ok, no rows
docker compose build
docker compose run --rm app cvboost cv list           # migrates once (0006), prints the queue
sqlite3 data/cvboost.sqlite "PRAGMA foreign_key_check;"   # must print nothing
docker compose up -d worker web
```

If `foreign_key_check` on the copy already prints rows, stop and ask: the migration refuses to
commit over violations and rolls back. Rollback of the schema is manual (header of
`0006_cv_kits.sql`); the simplest rollback is the `.backup` file plus the previous code.
