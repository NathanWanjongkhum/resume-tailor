# Notes for agents

Job-alert emails → tailored one-page LaTeX resumes, reviewed in a Streamlit dashboard.
README.md covers setup and usage; this file covers what isn't obvious from the code.

## Before you change anything

- **Ask before running anything that calls Claude**: `tailor run`, `triage`, `generate`, `gen`,
  `job`, or dashboard Run/Generate. They spend the user's Claude plan usage, and the user has
  asked for runs to stop when they took too long. Tests and `tailor base` are free.
- **The profile lives in `data/jobs.db`, not the repo** (`profile.py`, table `profile`). The
  user uploads facts and preferences with `tailor profile import` or the dashboard; `examples/`
  holds a fictional profile, which the tests load into a temp database (`tests/conftest.py`).
  Never commit real personal data. Saving archives the replaced copy in `profile_history`, and
  the dashboard and CLI show `profile.changes()` and ask before replacing; keep both paths
  going through review rather than calling `profile.put` on user input directly.
- **The user's preferences are their choices** (generation cap 3 per run, effort `low`, daily
  08:00 timer). Don't change them without being asked.
- **Never copy, move or restore `data/jobs.db` as a file.** It runs in WAL mode, so recent
  writes live in `jobs.db-wal`. Use `sqlite3 data/jobs.db ".backup <path>"`; in tests, use the
  `tmp_db` fixture.
- **The facts hold only claims the user confirmed.** Add a claim only after checking it
  against the source (repo history, the user's own words), and record what was verified in a
  comment next to it. GitHub evidence may add *skills*, never bullets. To edit, export the
  stored copy, change it, and import it.

## Checks (all must pass)

    uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q

Ruff selects ALL rules and ty treats every rule as an error. Fix the code rather than adding
ignores; when a `# noqa` is truly needed, use the bare code. Comments explain *why* only.

## How it fits together

- `pipeline.py`: stages ingest → track → filter → fetch → triage → recheck → generate. Each
  stage only picks up pending work and commits per job, so any stage can be rerun and a
  stopped run resumes where it left off. Per-job failures are caught via `JobError`; anything
  else fails the run, except Gmail failures in ingest and track (`GMAIL_STAGES`).
- `tracking.py`: replies in Gmail → `events` rows → application status, by phrase rules
  (no Claude calls). Statuses only move forward (`RANK`) unless set by hand through
  `tracking.set_status`, which also logs the change. A posting that's up but not taking
  applications is `paused` (with `paused_from`), not `filtered`: `sources.posting_state`
  returns open | paused | closed, and only closed drops a job.
- `progress.py`: the `runs` table, graceful stop (SIGTERM sets a flag checked by
  `Progress.item()` between items), kill (`stop --now`), and `recover()` (dead pid →
  `interrupted`; deletes `out/*.partial`). New loops over jobs must call `p.item()` and
  `p.advance()`.
- `output.py` builds each resume in `out/<name>.partial/` and renames it when complete.
- `validate.py` is the guard against invented claims: numbers and tech terms in a bullet
  must appear in the cited claim or its entry's stack. Capitalized terms match case-sensitively
  so prose like "the rest" doesn't count as REST.
- `company.py` reads the employer's public LinkedIn page at generate time (LinkedIn jobs
  only), cached in `jobs.company_info`. The writer gets it as `<company>` for ranking only.
- `fit.py` measures the compiled PDF (pdftotext, pdfinfo) to trim skill rows and projects.
- `db.py`: new `jobs` columns go in `SCHEMA` and are added to existing databases
  automatically, so they must be nullable or have a default. Dynamic SQL only accepts known
  column names.

## Gotchas

- **Plan backend** (`llm._via_plan`): `ANTHROPIC_API_KEY` is removed from the child's
  environment, or Claude Code would bill the API instead of the plan. Don't add `--bare`,
  which forces API-key auth. The prompt goes on stdin because Linux caps one argv string
  at 128 KiB.
- **systemd**: the units run `.venv/bin/python` directly (not `uv run`), so SIGTERM reaches
  the pipeline. They use `Type=exec` and `KillMode=mixed`, so `systemctl start` returns at
  once and `stop` is graceful. Dashboard runs are transient units (`resume-tailor-manual`)
  started via `systemd-run`.
- **Streamlit**: the pipeline panel is a `st.fragment(run_every=2)` that runs on another
  thread, so use `db_conn()` (one connection per thread), never a module-level connection.
  Buttons that act on a run are keyed by run id. `st.rerun()` drops messages, so use
  `flash()`. The resume preview is the browser's PDF viewer in a plain iframe (`preview.py`):
  `st.iframe` sandboxes its frame and Chrome won't show PDFs in sandboxed frames.
- **Untrusted input**: email and web content flows into prompts and the database. Fetch only
  http(s) (`sources.http_get`), and render only http(s) links. hiring.cafe pages sit behind
  Cloudflare; don't work around it. LinkedIn returns HTTP 429 on bursts, which is what
  `HOST_DELAY` avoids.
- **Template**: Jinja delimiters are `((( )))` / `((* *))`, and every interpolated value goes
  through the `tex` filter. The `\ifPDFTeX` guards keep it compiling on Overleaf too. The
  base resume (`tailor base`) should keep matching the Jake's Resume
  layout.
- **Gmail**: IMAP with an app password from the keyring (`secrets.py`); the mailbox is
  opened read-only and messages are fetched with `BODY.PEEK`.
