# CVBoost

CVBoost is a personal job-search assistant that runs on your own computer. It watches many job
sources for you, throws away the postings that cannot work for you, scores the rest, and tells you
on Telegram when something good appears. When you pick a job it prepares a tailored CV and a short
recruiter message. **You review everything and you send everything yourself.**

It is built for people who look for contract, freelance or remote work and are tired of checking
twenty job boards by hand.

> **Language note.** The panel and the Telegram messages are in English. The short job summaries
> and fit notes the AI writes can be in your language (see [Languages](#languages)). CVs, cover
> letters and recruiter messages are written in English. The rules are tuned for a contractor who
> lives in the EU/EEA: only your **home country** is configurable (see
> [Configuration](#configuration)).

## What it does

```
sources  ->  collect  ->  dedupe  ->  hard rules  ->  cheap score  ->  AI check  ->  alert / panel
 26 job       one          same job     no AI: rate,    no AI:          Claude        Telegram message
 boards,      connector    on 5 boards  location,        keywords        (small        for good jobs,
 ATS pages,   per source   = one job    language,        in the title    model)        the rest wait in
 e-mails                                blocked cos.     and text        reads the     the web panel
                                                                         posting
                                       you press "Prepare CV"  ->  Claude (strong model) writes
                                       a CV and recruiter note from YOUR fact file  ->  you edit and send
```

- **Collect.** 26 small connectors: remote job boards, company career pages (Greenhouse, Lever,
  Ashby and more), and the job-alert e-mails of LinkedIn, Indeed and others. Full list, with what
  each needs and costs: [SOURCES.md](SOURCES.md).
- **Rules, no AI.** Your minimum rate, the countries you can work from, the languages you read,
  companies you never want, roles you do not do. Anything that fails is dropped with a reason
  you can read.
- **AI check.** One small Claude call every 10 minutes reads the new postings that survived the
  rules and answers: can I really take this, how good is the match, what are the red flags.
- **Alerts.** A new job above your threshold arrives on Telegram with buttons
  (interested / no / prepare CV). Everything else is in the panel Inbox.
- **CV kit.** For jobs you pick, Claude writes a CV and a recruiter message using only the facts
  in your own `profile.md`. A checker rejects numbers and tools that are not in your facts.
- **Web panel.** Inbox, job page with the CV editor, applications pipeline, sources health,
  AI usage against your daily limits. It listens on your own computer only.

## What you need

| | |
|---|---|
| A computer | macOS or Linux (Windows through WSL). 4 GB RAM is plenty. It should be on while you want alerts. |
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | uv installs the Python packages for you. |
| [Claude Code](https://code.claude.com/docs/en/setup) with your own **paid Claude plan** (such as Pro or Max; the free plan does not include Claude Code) | This is the AI. There is no API key and no extra account. |
| A Telegram account | For alerts. Optional, but this is the main way to use it. |
| `pango` (for PDF CVs) | macOS: `brew install pango`. Debian/Ubuntu: `sudo apt install libpango-1.0-0 libpangoft2-1.0-0 fonts-liberation2`. Docker has it built in. |

## Quick start (about 20 minutes)

The easy way is to let Claude Code walk you through it.

1. Install [Claude Code](https://code.claude.com/docs/en/setup) and log in with your subscription
   (run `claude` once and follow the browser prompts).
2. Install uv, get this project and create your own config:
   ```bash
   git clone <this repository> cvboost
   cd cvboost
   uv sync
   uv run cvboost init
   ```
   `init` copies the shipped example (`config.example/`, a fictional person) to `config/`, which
   is yours and is ignored by git, and creates `.env` from `.env.example`. It never overwrites
   an existing `config/`.
3. Start Claude Code in the project folder and run the setup wizard:
   ```bash
   claude
   ```
   then type `/setup`.
   Claude interviews you (where you live, what roles you want, your minimum rate, your work
   history), writes your profile and rules, and tells you which Telegram and `.env` steps you do
   yourself. **Secrets (the Telegram token, passwords) are never typed into the chat:** you put
   them into the `.env` file with your own editor.
4. When the wizard finishes it runs `uv run cvboost doctor`, a health check. Fix any `FAIL` line,
   run it again, then start everything:
   ```bash
   scripts/local.sh up
   ```
5. Open <http://127.0.0.1:8080> (the panel). Your first alerts arrive within a few cycles.

## Manual setup

If you prefer to do it by hand:

1. `uv sync`
2. `uv run cvboost init` (creates `config/` from `config.example/` and `.env` from `.env.example`).
   Edit `.env` (see [Telegram](#telegram) for the two Telegram values). Every line in it starts
   with `#`: remove the `#` of a line when you fill it in.
3. Edit the files in `config/` (all are plain text with comments):
   - `config/profile.yaml`: your name, pronouns, time zone, and CV-checker data.
   - `config/resume/profile.md`: **every fact about you** (jobs, dates, projects, numbers, skills
     and a "Known gaps" list of things you must never claim). CVs can only use what is here.
   - `config/resume/gaps.yaml`: one guard per "Known gaps" line.
   - `config/prompts/*.md`: short blocks that describe you to the AI (your rate, location, what
     you want). Do not put instructions about security or data tags there.
   - `config/rules.yaml`: minimum rate, `location.home` (your country), languages, titles and
     stacks you do not want, and the title/skill terms that count as a match.
   - `config/worker.yaml` and `config/llm.yaml`: your time zone, quiet hours, alert threshold,
     daily AI call limits.
4. `init` made a grey placeholder `config/resume/photo.jpg`; replace it with a square photo of
   yours (the PDF layout expects the file).
5. Check: `uv run cvboost profile check` and `uv run cvboost doctor`.

The example describes a fictional person, "Jordan Example". `cvboost doctor` warns you until you
replace it, and fails with a hint to run `init` if `config/` is missing.

## Languages

Three keys in `config/profile.yaml`:

| Key | Values | What it controls |
|---|---|---|
| `ui_language` | `en` | The panel and Telegram texts (English only for now). |
| `explain_language` | `en`, `ru`, `uk`, `pt`, `es`, `de`, `fr`, `it`, `nl`, `pl` | The language of the AI's job summaries and fit notes. |
| `documents_language` | `en` | CVs, cover letters and recruiter messages (English only: the CV checks are written for English). |

Codes: `en` English, `ru` Russian, `uk` Ukrainian, `pt` Portuguese, `es` Spanish, `de` German,
`fr` French, `it` Italian, `nl` Dutch, `pl` Polish. A missing key means `en`. Any other value is a
`FAIL` in `cvboost profile check` and `cvboost doctor`; the AI steps stay off until you fix it, and
collecting goes on. A cover letter for a posting that is not in English follows the posting's
language.

**Changing `explain_language` later is a new scoring prompt version:** recent jobs are scored again
(AI calls against your daily limits) and old summaries stay in the old language. There is no tool
to translate them. Keep the language stated inside `config/prompts/cv_fit_analysis.md` out of that
block: the code adds it. `cvboost doctor` warns if the block names another language.

## Configuring with Claude Code

Open Claude Code in the project folder and ask in plain words, for example "write the AI summaries
in German" or "show my setup". Claude edits `config/profile.yaml` (`explain_language: de`), runs
`uv run cvboost profile check` and `uv run cvboost doctor`, and tells you that recent jobs will be
scored again. The `/setup` wizard asks about languages in the same way.

## Running it

**On your computer (simplest):**

```bash
scripts/local.sh up        # starts the worker and the panel, backs up the database
scripts/local.sh status
scripts/local.sh logs
scripts/local.sh down
```

The worker runs a cycle every 10 minutes. On macOS it also keeps the computer awake. Logs are in
`data/logs/`, the database is `data/cvboost.sqlite`.

**With Docker (for an always-on server):** the image contains Python and Claude Code. Run
`uv run cvboost init` once first: the containers read your `config/` folder.

```bash
docker compose build
docker compose up -d worker web
```

Inside Docker there is no desktop login, so create a long-lived token once on a machine where you
are logged in (`claude setup-token`) and put it in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. The panel
is published on `127.0.0.1:8080` only. Do not expose it to the internet without a login in front.

**Useful commands** (`uv run cvboost ...`): `doctor`, `sources` (health of every source),
`jobs --kept`, `usage` (AI calls against your limits), `profile check`, `telegram chat-id`.

## The panel and Telegram

**Panel** (<http://127.0.0.1:8080>): *Inbox* (scored jobs with the reason for each score and
filters), *Job* (full text, the AI verdict, where else it was seen, the CV kit with an editor
and PDF rebuild), *Applications* (your pipeline; you mark "applied" yourself), *Sources*
(each connector's last success, errors, switch on/off), *Usage* (AI calls per day against caps).

### Telegram

1. In Telegram open `@BotFather`, send `/newbot`, follow the prompts, and copy the token it gives
   you.
2. Put it into `.env` as `TELEGRAM_BOT_TOKEN=...` (edit the file yourself; do not paste it into
   any chat).
3. Open your new bot in Telegram and press **Start**.
4. Run `uv run cvboost telegram chat-id`. It reads the bot's waiting messages once and prints the
   id of your chat. Put it into `.env` as `TELEGRAM_CHAT_ID=...`.
5. Run `uv run cvboost doctor`; restart with `scripts/local.sh down && scripts/local.sh up`.

Run only **one** worker per bot (not on two machines at once).

## Costs and quotas

- The software is free. CVBoost itself needs no paid service.
- The AI uses **your Claude subscription** through Claude Code. A scoring call goes to the small
  model about every 10 minutes (at most 144 a day), CV drafting uses the strong model only for jobs
  you pick. Every call is logged with the cost Claude Code reports.
- Your subscription has its own usage limits, shared with your other Claude use. Depending on your
  plan and on how Anthropic meters `claude -p` (headless) use, such calls may draw on a separate
  credit or limit. Watch `uv run cvboost usage`, and lower `daily_call_caps` and
  `daily_cost_cap_usd` in `config/llm.yaml` if you need to. When a limit is reached the AI steps
  pause and collecting continues.
- CVBoost **never** falls back to a paid API key by itself, and refuses to run if Claude Code is
  logged in with an API key.
- Three optional job sources use free keys with their own quotas (Adzuna, Reed, ITJobs.pt). CVBoost
  counts its calls and pauses a source at the limit (`config/api_budget.yaml`). It never buys
  anything.

## Privacy and safety

- **You send everything.** CVBoost does not apply to jobs, message recruiters or send e-mail for you.
- **Your data stays with you.** The database, your profile and your CVs live in `data/` and
  `config/` on your machine. The only outside traffic is: job sources, Telegram, and Claude (through
  your own Claude Code login). Nothing is sent to the authors of this project.
- **Job text is untrusted.** A posting may contain text that tries to give the AI orders. Postings
  are fed to the AI as quoted data, the AI gets no tools, answers are checked against a schema, and
  suspicious postings are flagged.
- **Truth only.** CVs and letters may only use facts from your `profile.md`. Tools and topics in
  your "Known gaps" are blocked by a checker, and anything it doubts is marked "needs review".
- **Secrets.** Tokens and passwords live only in `.env` (git ignores it). They are never printed,
  and `cvboost doctor` shows only which names are set.
- **Respect for sources.** Official APIs and feeds first, polite delays, a generic User-Agent.
  LinkedIn, Indeed and Glassdoor are never scraped: you forward their job-alert e-mails to a
  dedicated mailbox. Read each source's terms in [SOURCES.md](SOURCES.md).

## Using Claude

CVBoost works with **your own Claude Code subscription, on your own computer, for you**. That is
the ordinary individual use that the plans are designed for. Do not use it differently:

- Run it only for yourself. Do not offer it as a service to other people, and do not share your
  login or token.
- If you want to run CVBoost for other people, or inside a paid product, use the Anthropic API
  under the [Commercial Terms](https://www.anthropic.com/legal/commercial-terms) instead of a
  consumer subscription. (The `LLMClient` interface is built so another backend can be added; none
  is shipped.)
- Claude Code's own rules apply: [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
  for Free, Pro and Max plans, the [Usage Policy](https://www.anthropic.com/legal/aup), and the
  [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) page,
  which also explains how Anthropic expects subscription credentials to be used.
- Check the current text of those documents yourself; they change, and this README is not legal
  advice.

## Configuration

| File | What it controls |
|---|---|
| `config/rules.yaml` (your copy; the shipped example is `config.example/`) | hard rules (rate, location, languages, unwanted roles/stacks, blocked companies), what counts as a match |
| `config/profile.yaml`, `config/prompts/`, `config/resume/` | who you are for the AI and for CVs |
| `config/worker.yaml` | cycle timing, alert threshold, quiet hours, night backfill |
| `config/llm.yaml` | models, time-outs, daily call and cost caps |
| `config/companies.yaml` | companies whose career pages are polled (`uv run cvboost companies discover` adds more) |
| `config/api_budget.yaml` | call budget of the keyed sources |
| `.env` | secrets and machine settings |

Changing `rules.yaml` makes the next cycle re-check every job against the new rules; old results
are kept as history. Changing how you describe yourself to the AI (`prompts/`, the profile names in
`rules.yaml`) makes jobs be scored again, which uses AI calls: do it in one go, not repeatedly.

**Home country.** In `rules.yaml` the block `location.home` names the country you live in. A
posting that names only that country must state a rate, otherwise it is rejected (set
`require_stated_rate: false` to keep such postings). The other region lists (EU, UK, US,
Asia ...) are built in and assume a contractor based in the EU/EEA. Living elsewhere works for
collecting and scoring, but the location rules will not fit you well in v1.

## Development

```bash
uv sync
uv run pytest -q        # no network, no real AI: sources use saved fixtures, the AI is mocked
uv run ruff check .
```

Layout: `src/cvboost/connectors/` (one module per source), `core/` (models, dedupe, rules),
`llm/` (client, prompts, cost ledger), `notify/` (Telegram), `web/` (panel), `cv/` (CV kit).
SOURCES.md is generated: `uv run cvboost sources --markdown` from the connector code and
`docs/sources_meta.yaml`; add an entry there when you add a connector (a test enforces it).

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 Andrey Shalitkin.

Built with [Claude Code](https://code.claude.com) as an AI pair programmer.
