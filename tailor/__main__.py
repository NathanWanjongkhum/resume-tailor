"""CLI.

  tailor run [--from STAGE]     pipeline: ingest -> track -> filter -> fetch -> triage -> recheck -> generate
  tailor status                 current or last run, with progress
  tailor stop [--now]           stop after the current item (or kill now)
  tailor retry                  send errored jobs back to the stage they failed in
  tailor ingest | track | fetch | triage | recheck | generate     one stage
  tailor mark <job id> <status> set a status by hand, e.g. interview or not_selected
  tailor replies <job id>       what's happened with an application
  tailor add <url>              add one posting by URL
  tailor ingest-file <file> --sender <addr>     ingest a saved alert email (testing)
  tailor gen <job id>           tailor a resume for one job now (any status)
  tailor letter <job id>        write a cover letter for a job's resume
  tailor list [status]          show jobs
  tailor job <posting.txt>      one-off resume from a text file (no database)
  tailor base                   render the untailored resume
  tailor github                 refresh skill evidence from public GitHub repos
  tailor profile                what's uploaded
  tailor profile import <facts|preferences> <file>    upload (checked first)
  tailor profile export <facts|preferences|github>    print the stored copy
  tailor dashboard              open the review dashboard

Run with `uv run python -m tailor ...`.
"""

import argparse
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import db, github, gmail, letter, llm, pipeline, profile, progress, tracking, triage
from .facts import Facts
from .output import build, produce
from .paths import OUT, ROOT
from .progress import STAGES


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tailor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--skip-ingest", action="store_true", help="don't read Gmail this run")
    run.add_argument("--from", dest="start", choices=STAGES, default="ingest", help="resume from this stage")
    run.add_argument("--trigger", default="cli", help=argparse.SUPPRESS)
    sub.add_parser("stop").add_argument("--now", action="store_true", help="kill instead of finishing the current item")
    sub.add_parser("status")
    sub.add_parser("retry").add_argument("--reset-attempts", action="store_true")
    for name in ("ingest", "track", "fetch", "triage", "recheck", "generate", "base", "github", "dashboard"):
        sub.add_parser(name)
    mark = sub.add_parser("mark")
    mark.add_argument("job_id")
    mark.add_argument("status", choices=db.STATUSES)
    sub.add_parser("replies").add_argument("job_id")
    sub.add_parser("add").add_argument("url")
    f = sub.add_parser("ingest-file")
    f.add_argument("file", type=Path)
    f.add_argument("--sender", required=True, help="e.g. jobalerts-noreply@linkedin.com")
    prof = sub.add_parser("profile").add_subparsers(dest="action")
    imp = prof.add_parser("import")
    imp.add_argument("kind", choices=profile.UPLOADABLE)
    imp.add_argument("file", type=Path)
    imp.add_argument("--yes", action="store_true", help="replace the stored copy without asking")
    prof.add_parser("export").add_argument("kind", choices=profile.KINDS)
    sub.add_parser("gen").add_argument("job_id")
    sub.add_parser("letter").add_argument("job_id")
    sub.add_parser("list").add_argument("status", nargs="?")
    job = sub.add_parser("job")
    job.add_argument("posting", type=Path)
    job.add_argument(
        "--backend", choices=["plan", "api"], default="plan", help="plan: Claude subscription (default); api: per-token"
    )
    return ap


def cmd_dashboard(_args: argparse.Namespace) -> None:
    app = ROOT / "tailor" / "dashboard.py"
    # Streamlit reads .streamlit/config.toml from the working directory.
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)], cwd=ROOT))


def cmd_github(_args: argparse.Namespace) -> None:
    owner = Facts().raw["contact"]["github"].rstrip("/").rsplit("/", 1)[-1]
    for name, repo in github.refresh(owner).items():
        print(f"{name:28} {', '.join(repo['skills'])}")


def cmd_profile(args: argparse.Namespace) -> None:
    conn = db.connect()
    if args.action == "import":
        body, current = args.file.read_text(), profile.get(conn, args.kind)
        try:
            profile.check(args.kind, body)
        except ValueError as e:
            sys.exit(f"not imported: {e}")
        if body == current:
            print(f"{args.kind} unchanged")
            return
        print(*(f"  {line}" for line in profile.changes(args.kind, current, body)), sep="\n")
        if current is not None and not args.yes:
            if not sys.stdin.isatty():
                sys.exit(f"not imported: this replaces the stored {args.kind}; rerun with --yes")
            if input(f"Replace the stored {args.kind}? It's kept in history. [y/N] ").strip().lower() != "y":
                sys.exit("not imported")
        profile.put(conn, args.kind, body)
        print(f"imported {args.kind} from {args.file}")
    elif args.action == "export":
        print(profile.get(conn, args.kind) or "", end="")
    else:
        for kind in profile.KINDS:
            print(f"{kind:12} {profile.updated_at(conn, kind) or 'not uploaded'}")


def cmd_base(_args: argparse.Namespace) -> None:
    _, notes = build(Facts().base_resume(), OUT / "base")
    print(f"wrote {OUT / 'base'}", *notes, sep="\n")


def cmd_job(args: argparse.Namespace) -> None:
    posting, facts, via = args.posting.read_text(), Facts(), llm.backend(args.backend)
    outdir, rep, _ = produce(posting, args.posting.stem, facts, via=via)
    print(f"wrote {outdir}")
    print("checks:", "PASS" if rep.ok else f"{len(rep.errors)} errors, see report.md")
    if letter.asks_for_letter(posting):
        rep = letter.write(outdir, facts, employer=None, via=via)
        print("cover letter:", "PASS" if rep.ok else f"{len(rep.errors)} errors, see cover_letter.json")


def cmd_run(args: argparse.Namespace) -> None:
    print(pipeline.run(start=args.start, skip_ingest=args.skip_ingest, trigger=args.trigger))


def cmd_stop(args: argparse.Namespace) -> None:
    print(progress.stop(db.connect(), now=args.now, source="cli"))


def cmd_status(_args: argparse.Namespace) -> None:
    conn = db.connect()
    progress.recover(conn, clean_partials=False)
    rows = progress.runs(conn, limit=1)
    if not rows:
        print("no runs yet")
        return
    r = rows[0]
    total = "?" if r["stage_total"] is None else r["stage_total"]
    print(f"run {r['id']} ({r['trigger']}): {r['status']}, stage {r['stage']} {r['stage_done']}/{total}")
    if r["current"]:
        print(f"  working on: {r['current']}")
    if r["error"]:
        print(f"  error: {r['error']}")
    if (stage := progress.resume_stage(conn)) is not None:
        print(f"  resume with: tailor run --from {stage}")


def cmd_retry(args: argparse.Namespace) -> None:
    print("jobs requeued:", pipeline.retry_errors(db.connect(), reset_attempts=args.reset_attempts))


def cmd_ingest(_args: argparse.Namespace) -> None:
    print("new jobs:", pipeline.ingest(db.connect(), triage.load_prefs(), Facts()))


def cmd_ingest_file(args: argparse.Namespace) -> None:
    msg = gmail.Message(f"file:{args.file.resolve()}", args.sender, args.file.name, "", args.file.read_text())
    print("new jobs:", pipeline.ingest_text(db.connect(), msg))


def cmd_track(_args: argparse.Namespace) -> None:
    print("applications moved on:", pipeline.track(db.connect(), triage.load_prefs(), Facts()))


def cmd_recheck(_args: argparse.Namespace) -> None:
    print("reopened:", pipeline.recheck(db.connect(), triage.load_prefs()))


def cmd_mark(args: argparse.Namespace) -> None:
    tracking.set_status(db.connect(), args.job_id, args.status)
    print(f"{args.job_id}: {args.status}")


def cmd_replies(args: argparse.Namespace) -> None:
    conn = db.connect()
    print(f"{args.job_id}: {db.get_required(conn, args.job_id)['status']}")
    for e in db.events(conn, args.job_id):
        print(f"  {e['at'][:10]} {e['kind']:12} {e['source']:6} {e['subject'] or ''}")


def cmd_add(args: argparse.Namespace) -> None:
    print("added:", pipeline.add_url(db.connect(), args.url))


def cmd_fetch(_args: argparse.Namespace) -> None:
    conn = db.connect()
    print("filtered:", pipeline.prefilter(conn, triage.load_prefs()))
    print("fetched:", pipeline.fetch(conn))


def cmd_triage(_args: argparse.Namespace) -> None:
    print("scored:", pipeline.triage_new(db.connect(), triage.load_prefs(), Facts()))


def cmd_generate(_args: argparse.Namespace) -> None:
    print("generated:", pipeline.generate(db.connect(), triage.load_prefs(), Facts()))


def cmd_gen(args: argparse.Namespace) -> None:
    g = triage.load_prefs()["generate"]
    via, effort = llm.backend(g["backend"]), llm.effort(g.get("effort", "high"))
    print("wrote", pipeline.generate_one(db.connect(), args.job_id, Facts(), via, effort))


def cmd_letter(args: argparse.Namespace) -> None:
    g = triage.load_prefs()["generate"]
    via, effort = llm.backend(g["backend"]), llm.effort(g.get("effort", "high"))
    print("wrote", pipeline.write_letter(db.connect(), args.job_id, Facts(), via, effort) / f"{letter.NAME}.txt")


def cmd_list(args: argparse.Namespace) -> None:
    for r in db.jobs(db.connect(), args.status):
        score = f"{r['relevance']}/{r['desirability']}" if r["relevance"] is not None else "  -  "
        print(
            f"{r['status']:12} {score:5} {'full' if r['posting_ok'] else 'part'} "
            f"{r['id']:28} {(r['company'] or '?')[:22]:22} {(r['title'] or '?')[:50]}"
        )


COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "dashboard": cmd_dashboard,
    "github": cmd_github,
    "profile": cmd_profile,
    "base": cmd_base,
    "job": cmd_job,
    "run": cmd_run,
    "stop": cmd_stop,
    "status": cmd_status,
    "retry": cmd_retry,
    "ingest": cmd_ingest,
    "ingest-file": cmd_ingest_file,
    "track": cmd_track,
    "recheck": cmd_recheck,
    "mark": cmd_mark,
    "replies": cmd_replies,
    "add": cmd_add,
    "fetch": cmd_fetch,
    "triage": cmd_triage,
    "generate": cmd_generate,
    "gen": cmd_gen,
    "letter": cmd_letter,
    "list": cmd_list,
}


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        COMMANDS[args.cmd](args)
    except profile.MissingProfileError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
