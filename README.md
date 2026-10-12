# resume-tailor

Job alert emails in, tailored one-page LaTeX resumes out, with a review
dashboard. Claude only selects and rephrases claims from a verified facts file,
and a deterministic checker rejects anything it can't trace back.

```
Gmail alerts ─► ingest ─► fetch posting ─► triage ─► generate ─► dashboard review
 (IMAP, read-only)  (dedupe)  (LinkedIn/ATS)  (filters + score)  (tailor, check, fit)
                         all state in data/jobs.db (SQLite)
```

## How it works

- **No invented claims.** Claude may only select and rephrase claims from your
  facts. `tailor/validate.py` then checks every bullet in code: each number and
  technology must appear in the claim it cites or in that entry's stack, or the
  resume is rejected and retried with the errors as feedback.
- **Spend calls only where they count.** Hard filters and a free ranking run in
  code first, so Claude scores a capped batch of the most promising postings and
  writes resumes only for the best few each run.
- **One page, measured.** The resume is compiled and measured (`tailor/fit.py`),
  then skill rows and projects are trimmed until it fits.
- **Resumable runs.** Each stage commits per job and only picks up pending work,
  so a stopped, killed or crashed run resumes where it left off.

## Files

| Path | What |
|---|---|
| `examples/facts.yaml` | format of your facts: every claim you're willing to make (fictional example) |
| `examples/preferences.yaml` | format of your preferences: Gmail query, filters, thresholds, wants and avoid |
| `templates/resume.tex.j2` | layout (Jake's Resume template, Jinja delimiters `((( )))` / `((* *))`) |
| `tailor/` | pipeline code; `tailor/pipeline.py` has the stages |
| `deploy/` | systemd user units: the pipeline timer and the dashboard |
| `data/jobs.db` | all state, including your uploaded profile (not committed) |
| `out/` | one folder per generated resume: `resume.pdf`, `report.md`, `posting.txt`, ... (not committed) |

## Setup

    ./setup.sh             # uv deps + Tectonic (LaTeX) into .bin/, no sudo
    deploy/install.sh      # dashboard at http://localhost:8501, starts on login

Claude runs through headless Claude Code (`claude -p`) on your Claude
subscription, with no API bill. Set `backend: api` in your preferences to use
`ANTHROPIC_API_KEY` and per-token Console billing instead.

### Your profile

Your facts and preferences live in the local database, not the repo. Copy the
examples, replace their contents with your own, and upload them on the dashboard's
Profile tab or from the command line:

    uv run python -m tailor profile import facts my-facts.yaml
    uv run python -m tailor profile import preferences my-preferences.yaml
    uv run python -m tailor profile                 # what's uploaded, and when
    uv run python -m tailor profile export facts    # print the stored copy to edit
    uv run python -m tailor github                  # skill evidence from your public repos
    uv run python -m tailor base                    # check the untailored resume

Until both are saved, the other dashboard pages point you to the Profile tab.
Every change (an upload, an edit in the dashboard's editor, or restoring an older
version) is checked first, then shown to you as a summary of added, removed and
reworded claims plus a line diff, and saved only when you confirm. Replaced
versions are kept (the last 20) under History. On the command line, `import`
prints the same summary and asks before replacing (`--yes` skips the question).

### Gmail

Ingest reads Gmail over IMAP, read-only, with an app password:

1. Turn on 2-Step Verification, then create an app password at
   https://myaccount.google.com/apppasswords
2. Store it in your keyring:

       secret-tool store --label='resume-tailor gmail-app-password' service resume-tailor account gmail-app-password

3. Test it with `uv run python -m tailor ingest`, then turn on the schedule:

       deploy/install.sh --timer      # runs daily at 08:00 (catches up if the PC was off)

The timer reads the keyring while you're logged in. For runs while the keyring is
locked, also save an encrypted systemd credential, then rerun `install.sh`:

    mkdir -p ~/.config/resume-tailor
    systemd-creds encrypt --user --name=gmail-app-password - ~/.config/resume-tailor/gmail-app-password.cred

## Use

    uv run python -m tailor run              # all stages (--skip-ingest to leave Gmail alone)
    uv run python -m tailor list [status]    # jobs and scores
    uv run python -m tailor add <url>        # one posting by URL
    uv run python -m tailor gen <job id>     # tailor one job now, whatever its scores
    uv run python -m tailor job posting.txt  # one-off, no database
    uv run python -m tailor dashboard        # if not running as a service

Checks: `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q`

Logs: `journalctl --user -u resume-tailor` (timer runs) and `-u resume-tailor-manual`
(runs started from the dashboard).

## Runs: progress, stopping, recovery

Every run is recorded in the `runs` table with its stage, items done out of the
total, and what it's working on; the dashboard's Pipeline panel shows this live.

    uv run python -m tailor status         # current or last run
    uv run python -m tailor stop           # finish the current item, then stop
    uv run python -m tailor stop --now     # kill the run and its claude call
    uv run python -m tailor run --from triage    # resume from a stage
    uv run python -m tailor retry          # requeue errored jobs

- **Stop** (or `systemctl --user stop resume-tailor`) lets the job in progress
  finish (up to a few minutes for a model call) and records the run as `stopped`.
- **Stop now**, a crash, or a shutdown leaves the run `interrupted`; the next run or
  dashboard refresh notices the dead process and cleans up half-written resumes
  (built in `out/*.partial` and renamed only when complete).
- Nothing is redone either way: every job is saved as it finishes and each stage
  only picks up pending work, so **Resume** continues from the stage that stopped.
- Jobs whose generation fails are retried on later runs, up to 2 attempts; the
  sidebar's *Failed jobs* section can reset that.

A first run over a two-week backlog takes roughly 25 minutes (about 3 s per posting
fetched, 20-30 s per batch of 8 jobs scored, about a minute per resume); daily runs
only handle that day's emails.

## Sources

- **LinkedIn** alerts (digests and single-job emails): the job id comes from the
  link, and the full posting comes from LinkedIn's public job page, with requests
  spaced out to avoid rate limits.
- **hiring.cafe** digests: hiring.cafe job pages sit behind a Cloudflare bot
  check, which is not worked around. These jobs keep the email summary and are
  marked "posting incomplete": triage still scores them (after full postings, when
  `triage.max_per_run` is reached), but they only get a resume when you press
  Generate in the dashboard (`require_full_posting`).
- Add a sender: write a parser in `tailor/sources.py`, register it in `PARSERS`,
  and add the address to the Gmail query.
