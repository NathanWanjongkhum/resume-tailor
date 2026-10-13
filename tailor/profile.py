"""The user's profile, stored in the database rather than the repo.

  facts         every claim a resume may make (YAML, see examples/facts.yaml)
  preferences   Gmail query, filters, thresholds, wants and avoid (YAML)
  github        skill evidence from public repos (JSON, written by `tailor github`)

The user uploads facts and preferences with `tailor profile import` or the
dashboard's Profile tab; each upload is checked before it replaces the old one,
and the replaced copy is kept in `profile_history` so it can be restored.
"""

import json
import sqlite3
from typing import Any, cast

import yaml

from . import db
from .paths import ROOT

EXAMPLES = ROOT / "examples"
KINDS = ("facts", "preferences", "github")
UPLOADABLE = ("facts", "preferences")
PREFS_SECTIONS = ("gmail", "hard_filters", "triage", "generate", "wants", "avoid")
HISTORY_KEEP = 20  # replaced versions kept per kind
SHORT = 60  # longer values are summarized in a review


class MissingProfileError(LookupError):
    def __init__(self, kind: str) -> None:
        super().__init__(
            f"no {kind} uploaded yet: run `tailor profile import {kind} FILE` or use the dashboard's "
            f"Profile tab (examples/{kind}.yaml shows the format)"
        )


def get(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute("SELECT body FROM profile WHERE kind = ?", (kind,)).fetchone()
    return None if row is None else str(row["body"])


def updated_at(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute("SELECT updated_at FROM profile WHERE kind = ?", (kind,)).fetchone()
    return None if row is None else str(row["updated_at"])


def load(kind: str, conn: sqlite3.Connection | None = None) -> Any:  # noqa: ANN401
    """The parsed profile document; raises MissingProfileError if it was never uploaded."""
    body = get(conn or db.connect(), kind)
    if body is None:
        if kind == "github":
            return {}
        raise MissingProfileError(kind)
    return json.loads(body) if kind == "github" else yaml.safe_load(body)


def put(conn: sqlite3.Connection, kind: str, body: str) -> None:
    """Check and store a profile document, replacing the previous one."""
    if kind not in KINDS:
        msg = f"unknown profile kind {kind!r}; expected one of {', '.join(KINDS)}"
        raise ValueError(msg)
    if kind != "github":
        check(kind, body)
    old = get(conn, kind)
    if old == body:
        return
    # GitHub evidence is rebuilt by a scan, so only hand-edited kinds keep history.
    if old is not None and kind in UPLOADABLE:
        conn.execute("INSERT INTO profile_history (kind, body, replaced_at) VALUES (?, ?, ?)", (kind, old, db.now()))
        conn.execute(
            "DELETE FROM profile_history WHERE kind = ? AND id NOT IN "
            "(SELECT id FROM profile_history WHERE kind = ? ORDER BY id DESC LIMIT ?)",
            (kind, kind, HISTORY_KEEP),
        )
    conn.execute(
        "INSERT INTO profile (kind, body, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (kind, body, db.now()),
    )
    conn.commit()


def history(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    """Replaced versions, newest first: id, body, replaced_at."""
    cur = conn.execute("SELECT id, body, replaced_at FROM profile_history WHERE kind = ? ORDER BY id DESC", (kind,))
    return cast("list[sqlite3.Row]", cur.fetchall())


def changes(kind: str, old: str | None, new: str) -> list[str]:
    """What replacing `old` with `new` would change, in words, for the reviewer.

    `new` must already pass `check`. Facts are compared claim by claim and
    entry by entry; preferences by top-level section.
    """
    if old is None:
        return [f"Adds {kind} (nothing is stored yet)."]
    before, after = yaml.safe_load(old), yaml.safe_load(new)
    if kind == "preferences":
        return _setting_changes(before, after, ()) or ["Only comments or formatting change."]
    from .facts import Facts  # noqa: PLC0415  # facts imports this module

    a, b = Facts(before), Facts(after)
    out = [f"Adds claim `{c}`: {b.claims[c].text}" for c in b.claims if c not in a.claims]
    out += [f"Removes claim `{c}`: {a.claims[c].text}" for c in a.claims if c not in b.claims]
    out += [f"Rewords claim `{c}`" for c in a.claims if c in b.claims and a.claims[c].text != b.claims[c].text]
    for s in ("contact", "skills"):
        out += _setting_changes(before.get(s), after.get(s), (s,))
    if before.get("education") != after.get("education"):
        out.append("Changes `education`")

    old_entries = {e["id"]: e for e in [*before["experience"], *before["projects"]]}
    new_entries = {e["id"]: e for e in [*after["experience"], *after["projects"]]}
    for i, e in new_entries.items():
        if i not in old_entries:
            out.append(f"Adds entry `{i}`")
            continue
        old_entry = old_entries[i]
        if changed := [k for k in {**old_entry, **e} if k != "claims" and old_entry.get(k) != e.get(k)]:
            out.append(f"Changes {', '.join(changed)} of `{i}`")
        if _reordered([c["id"] for c in old_entry["claims"]], [c["id"] for c in e["claims"]]):
            out.append(f"Reorders the claims of `{i}`")
    out += [f"Removes entry `{i}`" for i in old_entries if i not in new_entries]
    # The base resume lists entries in facts order.
    out += [
        f"Reorders {s}"
        for s in ("experience", "projects")
        if _reordered([e["id"] for e in before[s]], [e["id"] for e in after[s]])
    ]
    return out or ["Only comments or formatting change."]


def _reordered(old: list[str], new: list[str]) -> bool:
    """Whether the ids in both lists come in a different order."""
    both = set(old) & set(new)
    return [i for i in old if i in both] != [i for i in new if i in both]


def _short(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= SHORT else text[: SHORT - 3] + "..."


def _setting_changes(before: object, after: object, path: tuple[str, ...]) -> list[str]:
    """Preference changes setting by setting: `triage.min_relevance`: 3 -> 4."""
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            line
            for k in [*before, *(k for k in after if k not in before)]
            for line in _setting_changes(before.get(k), after.get(k), (*path, str(k)))
        ]
    if before == after:
        return []
    name = ".".join(path)
    if isinstance(before, list) and isinstance(after, list):
        return [f"Adds `{_short(v)}` to `{name}`" for v in after if v not in before] + [
            f"Removes `{_short(v)}` from `{name}`" for v in before if v not in after
        ] or [f"Reorders `{name}`"]
    if before is None:
        change = f"Adds `{name}`: {_short(after)}"
    elif after is None:
        change = f"Removes `{name}`"
    elif isinstance(before, str) and len(before) > SHORT:
        change = f"Rewords `{name}`"
    else:
        change = f"Changes `{name}`: {_short(before)} → {_short(after)}"
    return [change]


def check(kind: str, body: str) -> None:
    """Raise ValueError if the document wouldn't load, so a bad upload never replaces a good one."""
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        msg = f"{kind} is not valid YAML: {e}"
        raise ValueError(msg) from e
    if not isinstance(data, dict):
        msg = f"{kind} must be a YAML mapping"
        raise ValueError(msg)  # noqa: TRY004
    if kind == "preferences":
        if missing := [s for s in PREFS_SECTIONS if s not in data]:
            msg = f"preferences is missing: {', '.join(missing)}"
            raise ValueError(msg)
        from .profile_edit import preference_problems  # noqa: PLC0415  # profile_edit imports llm, which is slow

        if problems := preference_problems(data):
            raise ValueError("; ".join(problems))
        return
    from .facts import Facts  # noqa: PLC0415  # facts imports this module

    try:
        Facts(data)
    except (KeyError, TypeError) as e:
        msg = f"facts is missing a required field: {e}"
        raise ValueError(msg) from e
