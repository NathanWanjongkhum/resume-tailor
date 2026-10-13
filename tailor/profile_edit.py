"""Structured edits to a profile document that keep the user's comments.

The dashboard's profile forms change one value at a time. PyYAML would drop every
comment on the way back to text, and the facts' comments record what each claim was
verified against, so edits go through ruamel's round-trip loader instead.

A comment block directly above a list item (`- id: ...`) is that item's note. While
editing it's carried as a `__note__` key, so it moves and is deleted with its item,
and it's written back as the same comment lines.
"""

import io
import re
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString

from . import llm

NOTE = "__note__"
FOLD_WIDTH = 72  # the folded claims in examples/facts.yaml wrap at about this width
Key = str | int
Path = tuple[Key, ...]

_COMMENT = re.compile(r"^( *)# ?(.*)$")
_ITEM = re.compile(r"^( *)- ([A-Za-z_][\w-]*:(?: .*)?)$")
_NOTE_LINE = re.compile(rf"^( *)- {NOTE}: ['\"]?{NOTE}(\d+)['\"]?$")


def _null(rep: RoundTripRepresenter, _: None) -> Any:  # noqa: ANN401
    return rep.represent_scalar("tag:yaml.org,2002:null", "null")


def _yaml() -> YAML:
    y = YAML()
    y.width = 4096  # never re-wrap: long regexes and queries stay on one line
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(type(None), _null)  # `repo: null`, not `repo:`
    return y


def _without_notes(data: Any) -> Any:  # noqa: ANN401
    if isinstance(data, dict):
        return {k: _without_notes(v) for k, v in data.items() if k != NOTE}
    if isinstance(data, list):
        return [_without_notes(v) for v in data]
    return data


def _lift_notes(text: str) -> str:
    """Turn each comment block directly above a `- key:` item into that item's NOTE key."""
    out: list[str] = []
    block: list[tuple[str, str]] = []  # (indent, comment text)
    for line in text.splitlines():
        if (m := _COMMENT.match(line)) is not None:
            block.append((m.group(1), m.group(2)))
            out.append(line)
            continue
        item = _ITEM.match(line)
        if block and item is not None and all(ind == item.group(1) for ind, _ in block):
            del out[-len(block) :]
            note = "\n".join(t for _, t in block)
            ind = item.group(1)
            out += [f"{ind}- {NOTE}: {_quote(note)}", f"{ind}  {item.group(2)}"]
        else:
            out.append(line)
        block = []
    return "\n".join(out) + "\n"


def _quote(text: str) -> str:
    # JSON strings are valid YAML double-quoted scalars.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def parse(body: str) -> CommentedMap:
    """The document for editing, with item notes lifted into NOTE keys."""
    lifted = _lift_notes(body)
    # A "comment" inside a block scalar isn't one; lifting it would change the data.
    if _without_notes(yaml.safe_load(lifted)) != yaml.safe_load(body):
        lifted = body
    doc = _yaml().load(lifted)
    if not isinstance(doc, CommentedMap):
        msg = "the profile must be a YAML mapping"
        raise TypeError(msg)
    return doc


def dump(doc: CommentedMap) -> str:
    """The document as text, notes back as comments above their items."""
    notes: list[str] = []
    holders: list[CommentedMap] = []
    for m in _mappings(doc):
        if NOTE in m:
            m.move_to_end(NOTE, last=False)  # so it's the line with the dash
            notes.append(str(m[NOTE]))
            holders.append(m)
            m[NOTE] = f"{NOTE}{len(notes) - 1}"
    buf = io.StringIO()
    try:
        _yaml().dump(doc, buf)
    finally:
        for m, note in zip(holders, notes, strict=True):
            m[NOTE] = note
    lines = buf.getvalue().splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _NOTE_LINE.match(lines[i])
        if m is None:
            out.append(lines[i])
            i += 1
            continue
        ind = m.group(1)
        out += [f"{ind}# {t}".rstrip() for t in notes[int(m.group(2))].splitlines()]
        out.append(f"{ind}- {lines[i + 1].removeprefix(ind + '  ')}")
        i += 2
    return "\n".join(out) + "\n"


def _mappings(node: Any) -> Iterable[CommentedMap]:  # noqa: ANN401
    if isinstance(node, CommentedMap):
        yield node
        for v in node.values():
            yield from _mappings(v)
    elif isinstance(node, CommentedSeq):
        for v in node:
            yield from _mappings(v)


def get(doc: Any, path: Path, default: Any = None) -> Any:  # noqa: ANN401
    node = doc
    for key in path:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return default
    return node


def _parent(doc: CommentedMap, path: Path) -> Any:  # noqa: ANN401
    node: Any = doc
    for key in path[:-1]:
        if isinstance(node, CommentedMap) and key not in node:
            node[key] = CommentedMap()
        node = node[key]
    return node


def folded(text: str) -> FoldedScalarString:
    """`text` as a `>-` block wrapped like the rest of the facts."""
    text = " ".join(text.split())
    out = FoldedScalarString(text)
    pos, folds = 0, []
    for line in textwrap.wrap(text, FOLD_WIDTH, break_long_words=False, break_on_hyphens=False)[:-1]:
        pos += len(line)
        folds.append(pos)
        pos += 1
    out.fold_pos = folds
    return out


def _styled(old: Any, new: Any) -> Any:  # noqa: ANN401
    """`new` in the same YAML style as the value it replaces."""
    if isinstance(new, str):
        if isinstance(old, FoldedScalarString):
            return folded(new)
        if isinstance(old, LiteralScalarString) or "\n" in new.strip():
            return LiteralScalarString(new.strip() + "\n")
        return new.strip()
    if isinstance(new, list):
        seq = CommentedSeq(new)
        if isinstance(old, CommentedSeq):
            if old.fa.flow_style():
                seq.fa.set_flow_style()
            seq.ca.items.update(old.ca.items)  # keep comments on unchanged positions
        return seq
    return new


def flow_list(items: Iterable[str]) -> CommentedSeq:
    """A list written on one line, like `stack: [React, Flask]`."""
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def set_value(doc: CommentedMap, path: Path, value: Any) -> None:  # noqa: ANN401
    """Set the value at `path`, creating missing mappings; None removes a mapping key."""
    parent, key = _parent(doc, path), path[-1]
    if value is None and isinstance(parent, CommentedMap):
        parent.pop(key, None)
        return
    parent[key] = _styled(get(doc, path), value)


def insert(doc: CommentedMap, path: Path, item: Any, index: int | None = None) -> None:  # noqa: ANN401
    """Add `item` to the list at `path`, at the end unless `index` is given."""
    seq = get(doc, path)
    if not isinstance(seq, list):
        seq = CommentedSeq()
        set_value(doc, path, seq)
        seq = get(doc, path)
    seq.insert(len(seq) if index is None else index, item)


def remove(doc: CommentedMap, path: Path) -> None:
    """Remove a list item or mapping key, with its note."""
    parent = get(doc, path[:-1])
    del parent[path[-1]]


def move(doc: CommentedMap, path: Path, delta: int) -> None:
    """Move a list item `delta` places, with its note."""
    seq, i = get(doc, path[:-1]), int(path[-1])
    j = max(0, min(len(seq) - 1, i + delta))
    seq.insert(j, seq.pop(i))


def rename(doc: CommentedMap, path: Path, new: str) -> None:
    """Rename a mapping key in place."""
    parent, old = get(doc, path[:-1]), path[-1]
    if new == old:
        return
    if new in parent:
        msg = f"{new!r} already exists"
        raise ValueError(msg)
    parent.insert(list(parent).index(old), new, parent.pop(old))


def note(item: Any) -> str:  # noqa: ANN401
    return str(item.get(NOTE, "")) if isinstance(item, dict) else ""


def set_note(doc: CommentedMap, path: Path, text: str) -> None:
    item = get(doc, path)
    text = "\n".join(line.rstrip() for line in text.strip().splitlines())
    if not text:
        item.pop(NOTE, None)
    elif NOTE in item:
        item[NOTE] = text
    else:
        item.insert(0, NOTE, text)


def new_id(taken: Iterable[str], *words: str) -> str:
    """A short, unused id from the given words, like `northwind` or `nw-platform-2`."""
    base = "-".join(str(w) for w in re.findall(r"[a-z0-9]+", " ".join(words).lower())[:3]) or "item"
    taken = set(taken)
    n, out = 1, base
    while out in taken:
        n += 1
        out = f"{base}-{n}"
    return out


# Preferences forms are built from these specs; a new setting needs one Field here.
Widget = Literal["text", "long_text", "number", "toggle", "choice", "level", "patterns"]


@dataclass(frozen=True)
class Field:
    path: tuple[str, ...]
    label: str
    widget: Widget
    help: str = ""
    default: Any = None  # what the pipeline uses when the setting is left out
    options: tuple[str, ...] = ()
    other_options: bool = False  # a choice that also takes values not in `options`
    optional: bool = False  # may be left out, meaning "no limit"
    min_value: int = 0
    max_value: int | None = None
    matches: str = ""  # for patterns: the job field they're matched against


@dataclass(frozen=True)
class Section:
    title: str
    help: str
    fields: tuple[Field, ...]


BACKENDS = ("plan", "api")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
BACKEND_HELP = "plan bills your Claude subscription; api bills your Anthropic Console account per token."
PREFERENCES = (
    Section(
        "What you want",
        "Claude scores each job that passes the filters against these, in your own words.",
        (
            Field(("wants",), "Looking for", "long_text", "Roles, locations, and what matters to you."),
            Field(("avoid",), "Avoid", "long_text", "What makes a job a poor fit."),
        ),
    ),
    Section(
        "Filters",
        "Checked in code before any scoring, for free, on jobs that arrive from now on. Rows are "
        "case-insensitive regular expressions: `\\b` marks a word's edge and `|` means or; "
        "**Add words** writes one for you.",
        (
            Field(
                ("hard_filters", "title_exclude"),
                "Skip titles matching",
                "patterns",
                "A job is skipped if its title matches any row.",
                default=[],
                matches="title",
            ),
            Field(
                ("hard_filters", "company_exclude"),
                "Skip companies matching",
                "patterns",
                "A job is skipped if its company matches any row.",
                default=[],
                matches="company",
            ),
            Field(
                ("hard_filters", "location_include"),
                "Keep locations matching",
                "patterns",
                "A job is kept only if its location matches a row (or is unknown). Leave empty to keep all.",
                default=[],
                matches="location",
            ),
            Field(
                ("hard_filters", "max_years_required"),
                "Most years of experience a posting may ask for",
                "number",
                "Read from the full posting; 0 keeps only roles that ask for none.",
                optional=True,
                max_value=30,
            ),
        ),
    ),
    Section(
        "Job alerts",
        "Which Gmail messages are read for jobs. The mailbox is opened read-only.",
        (Field(("gmail", "query"), "Gmail search", "long_text", "Gmail search syntax, as in Gmail's search box."),),
    ),
    Section(
        "Scoring",
        "How jobs that pass the filters are scored (1-5 for relevance and desirability).",
        (
            Field(("triage", "min_relevance"), "Least relevance for a resume", "level", min_value=1, max_value=5),
            Field(("triage", "min_desirability"), "Least desirability for a resume", "level", min_value=1, max_value=5),
            Field(
                ("triage", "max_per_run"),
                "Jobs scored per run",
                "number",
                "Ranked in code first; the rest wait for the next run.",
                optional=True,
                min_value=1,
            ),
            Field(("triage", "batch_size"), "Jobs per Claude call", "number", min_value=1, max_value=50),
            Field(
                ("triage", "model"),
                "Model",
                "choice",
                options=(llm.DEFAULT_MODEL, "claude-sonnet-5-5", "claude-haiku-5-5"),
                other_options=True,
            ),
            Field(("triage", "effort"), "Effort", "choice", options=EFFORTS),
            Field(("triage", "backend"), "Billing", "choice", BACKEND_HELP, options=BACKENDS),
        ),
    ),
    Section(
        "Resumes",
        "How tailored resumes are written.",
        (
            Field(
                ("generate", "max_per_run"),
                "Resumes per run",
                "number",
                "Caps plan usage per pipeline run.",
                min_value=0,
            ),
            Field(
                ("generate", "require_full_posting"),
                "Only for jobs with the full posting",
                "toggle",
                "Skip jobs where only the email summary was available.",
                default=True,
            ),
            Field(
                ("generate", "cover_letters"),
                "Write a cover letter when the posting asks for one",
                "toggle",
                "One more, smaller call per such job.",
                default=True,
            ),
            Field(
                ("generate", "effort"),
                "Effort",
                "choice",
                "Raise it if wording gets worse.",
                default="high",
                options=EFFORTS,
            ),
            Field(("generate", "backend"), "Billing", "choice", BACKEND_HELP, options=BACKENDS),
        ),
    ),
    Section(
        "Application tracking",
        "Replies in Gmail move applications along (no Claude calls).",
        (
            Field(("tracking", "query"), "Extra Gmail search terms for replies", "text", default=""),
            Field(
                ("tracking", "pending_days"),
                "Days to keep rechecking a paused posting",
                "number",
                default=30,
                min_value=1,
            ),
        ),
    ),
)
FIELDS = {f.path: f for s in PREFERENCES for f in s.fields}


def preference_problems(prefs: dict[str, Any]) -> list[str]:
    """Settings that would fail a pipeline run, found before they're saved."""
    out: list[str] = []
    for f in FIELDS.values():
        value = get(prefs, f.path)
        name = ".".join(f.path)
        if value is None:
            continue
        if f.widget == "patterns":
            if not isinstance(value, list):
                out.append(f"`{name}` must be a list")
                continue
            out += [f"`{name}`: {pat!r} isn't a valid pattern ({e})" for pat, e in bad_patterns(value)]
        elif f.widget in {"number", "level"} and (not isinstance(value, int) or isinstance(value, bool)):
            out.append(f"`{name}` must be a whole number, not {value!r}")
        elif f.widget == "choice" and not f.other_options and value not in f.options:
            out.append(f"`{name}` must be one of {', '.join(f.options)}, not {value!r}")
    return out


def bad_patterns(patterns: Iterable[object]) -> list[tuple[str, str]]:
    """(pattern, what's wrong) for each pattern that isn't a valid regular expression."""
    out: list[tuple[str, str]] = []
    for pat in patterns:
        try:
            re.compile(str(pat))
        except re.error as e:
            out.append((str(pat), str(e)))
    return out


def _escape(word: str) -> str:
    return re.escape(word).replace("\\ ", " ")  # a space needs no escape, and reads better without


def words_pattern(text: str) -> str | None:
    """A pattern matching any of the comma-separated words or phrases, as whole words."""
    words = [w.strip() for w in text.split(",") if w.strip()]
    if not words:
        return None
    if all(re.match(r"\w", w) and re.search(r"\w$", w) for w in words):
        inner = "|".join(map(_escape, words))
        return rf"\b{inner}\b" if len(words) == 1 else rf"\b({inner})\b"
    # `\b` needs a word character on its side, so `C++` or `.NET` get lookarounds instead.
    return "|".join(rf"(?<!\w){_escape(w)}(?!\w)" for w in words)


def pattern_hits(patterns: Iterable[str], values: Iterable[str]) -> list[list[str]]:
    """For each pattern, the values it matches (case-insensitive); [] for an invalid one."""
    values = list(values)
    out: list[list[str]] = []
    for pat in patterns:
        try:
            rx = re.compile(pat, re.IGNORECASE)
        except re.error:
            out.append([])
            continue
        out.append([v for v in values if rx.search(v)])
    return out
