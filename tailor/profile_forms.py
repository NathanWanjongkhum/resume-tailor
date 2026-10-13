"""The Profile page's forms: common edits to facts and preferences without touching YAML.

Every widget edits the draft through `profile_edit`, so comments and verification
notes survive, and nothing is saved until the user reviews the draft as a whole.
Widget keys carry a form version that the dashboard bumps whenever the draft changes
shape (an entry added, moved or removed, or the YAML edited directly), so widgets
restart from the draft then; plain value edits keep their widgets.

The facts have well over a hundred widgets, so tabs, entry cards and popovers render
their contents only while open, and text boxes get a height up front rather than
sizing themselves after they appear, which shifted the page on every rerun.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import pandas as pd
import streamlit as st
from ruamel.yaml.comments import CommentedMap

from . import profile_edit, triage
from .profile_edit import Field, Path

Edit = Callable[[CommentedMap], None]
Job = dict[str, Any]

CONTACT = (
    ("name", "Name"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("location", "Location"),
    ("linkedin", "LinkedIn URL"),
    ("github", "GitHub URL"),
)
DATES_HINT = "June 2024 -- Sept. 2024"
# Entry lists in facts: section -> (singular, detail fields as (key, label, placeholder)).
ENTRIES = {
    "experience": (
        "experience",
        (
            ("title", "Title", "Software Engineering Intern"),
            ("org", "Organization", "Northwind Studio"),
            ("location", "Location", "Remote"),
            ("dates", "Dates", DATES_HINT),
        ),
    ),
    "projects": (
        "project",
        (("name", "Name", "Trail Conditions App"), ("repo", "Repository URL", "https://github.com/...")),
    ),
}
EDUCATION = (
    ("school", "School", "Example State University"),
    ("degree", "Degree", "Bachelor of Science in Computer Science"),
    ("location", "Location", "Portland, OR"),
    ("dates", "Dates", "Sep. 2021 -- June 2025"),
)
NOTE_HELP = "What you checked this against (a repo, a document, your own words). Kept as a comment in the YAML."
EXAMPLES_SHOWN = 3
LINE_CHARS = 110  # characters per line of a full-width text box, estimated low so text rarely scrolls
LINE_PX, PADDING_PX, MIN_TEXT_PX = 24, 22, 68  # Streamlit's text box can't be shorter than 68px
WORDS_HINT = {"title": "intern, contractor", "company": "Acme, Initech", "location": "Seattle, Tacoma"}


def md(text: object) -> str:
    """User text in markdown: `$` would start LaTeX math."""
    return str(text).replace("$", "\\$")


@dataclass(frozen=True)
class Form:
    kind: str
    version: int
    apply: Callable[[Edit, bool], None]  # (edit, reshape): reshape restarts every widget

    def key(self, *parts: object) -> str:
        return "-".join(["pf", self.kind, str(self.version), *map(str, parts)])

    def edit(self, edit: Edit, *, reshape: bool = False) -> None:
        self.apply(edit, reshape)

    def bind(self, path: Path, convert: Callable[[Any], Any] = lambda v: v) -> dict[str, Any]:
        """Widget arguments that write the widget's value to `path` when it changes."""
        key = self.key(*path)

        def changed() -> None:
            value = convert(st.session_state[key])
            self.edit(lambda doc: profile_edit.set_value(doc, path, value))

        return {"key": key, "on_change": changed}


def text_height(text: str) -> int:
    """A text box height that fits `text`."""
    lines = sum(max(1, -(-len(line) // LINE_CHARS)) for line in text.splitlines() or [""])
    return max(MIN_TEXT_PX, LINE_PX * lines + PADDING_PX)


def is_open(container: Any) -> bool:  # noqa: ANN401
    """Whether a lazy tab, expander or popover is open; None means it isn't tracked, so render it."""
    return container.open is not False


def popover(label: str, key: str, **kwargs: Any) -> Any:  # noqa: ANN401
    """A popover that reruns when opened or closed, so its contents can render only while open."""
    return st.popover(label, key=key, on_change="rerun", **kwargs)


def _text(value: object) -> str | None:
    """A text widget's value for the document: stripped, and None (left out) when empty."""
    text = str(value or "").strip()
    return text or None


def _required_text(value: object) -> str:
    return str(value or "").strip()


def text_input(form: Form, doc: CommentedMap, path: Path, label: str, placeholder: str = "") -> None:
    value = profile_edit.get(doc, path)
    st.text_input(
        label,
        "" if value is None else str(value),
        placeholder=placeholder,
        **form.bind(path, _required_text),
    )


def item_buttons(form: Form, path: Path, count: int, what: str) -> None:
    """Move up, move down and remove buttons for list item `path`."""
    i = int(path[-1])
    st.button(
        "",
        icon=":material/arrow_upward:",
        type="tertiary",
        help=f"Move this {what} up",
        disabled=i == 0,
        key=form.key(*path, "up"),
        on_click=form.edit,
        args=(lambda doc: profile_edit.move(doc, path, -1),),
        kwargs={"reshape": True},
    )
    st.button(
        "",
        icon=":material/arrow_downward:",
        type="tertiary",
        help=f"Move this {what} down",
        disabled=i == count - 1,
        key=form.key(*path, "down"),
        on_click=form.edit,
        args=(lambda doc: profile_edit.move(doc, path, 1),),
        kwargs={"reshape": True},
    )
    st.button(
        "",
        icon=":material/delete:",
        type="tertiary",
        help=f"Remove this {what} (you'll review the change before it's saved)",
        key=form.key(*path, "remove"),
        on_click=form.edit,
        args=(lambda doc: profile_edit.remove(doc, path),),
        kwargs={"reshape": True},
    )


def note_editor(form: Form, doc: CommentedMap, path: Path) -> None:
    item = profile_edit.get(doc, path)
    key = form.key(*path, "note")

    def changed() -> None:
        text = str(st.session_state[key])
        form.edit(lambda d: profile_edit.set_note(d, path, text))

    st.text_area("Verification note", profile_edit.note(item), help=NOTE_HELP, key=key, on_change=changed)


# Facts


def all_skills(doc: CommentedMap) -> list[str]:
    """Every skill named anywhere in the facts, for autocompletion."""
    names = {s for items in (doc.get("skills") or {}).values() for s in items or []}
    names |= {s for section in ENTRIES for e in doc.get(section) or [] for s in e.get("stack") or []}
    return sorted(names, key=str.lower)


def claim_row(form: Form, doc: CommentedMap, path: Path, count: int) -> None:
    claim = profile_edit.get(doc, path)
    note = profile_edit.note(claim)
    text = " ".join(str(claim.get("text") or "").split())
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        st.text_area(
            f"`{md(claim.get('id'))}`" + (" :material/verified:" if note else ""),
            text,
            height=text_height(text),
            help=f"Verified: {md(note)}" if note else None,
            **form.bind((*path, "text"), _required_text),
        )
        with st.container(horizontal=True, vertical_alignment="center", gap="small", width="content"):
            pop = popover(
                "", form.key(*path, "note-pop"), icon=":material/edit_note:", help="Verification note", type="tertiary"
            )
            with pop:
                if is_open(pop):
                    note_editor(form, doc, path)
            item_buttons(form, path, count, "claim")


def add_claim(form: Form, doc: CommentedMap, path: Path) -> None:
    """A form that adds a claim to the entry at `path`."""
    entry = profile_edit.get(doc, path)
    taken = [c["id"] for section in ENTRIES for e in doc.get(section) or [] for c in e.get("claims") or []]
    own = [str(c["id"]) for c in entry.get("claims") or []]
    prefix = own[0].split("-")[0] if own else str(entry["id"])
    text_key, note_key = form.key(*path, "new-claim"), form.key(*path, "new-claim-note")

    def add() -> None:
        text = " ".join(str(st.session_state[text_key]).split())
        note = str(st.session_state[note_key])
        if not text:
            return
        claim = CommentedMap(id=profile_edit.new_id(taken, prefix, *text.split()[:2]), text=profile_edit.folded(text))
        if note.strip():
            claim.insert(0, profile_edit.NOTE, note.strip())
        form.edit(lambda d: profile_edit.insert(d, (*path, "claims"), claim), reshape=True)

    pop = popover("Add a claim", form.key(*path, "claim-pop"), icon=":material/add:")
    with pop:
        if not is_open(pop):
            return
        with st.form(form.key(*path, "claim-form"), border=False):
            st.text_area(
                "Claim",
                key=text_key,
                placeholder="Something you did, in full: what, with which tools, and the result.",
                help="Resumes may rephrase and select claims, never add to them, so put the numbers and tools here.",
            )
            st.text_area("Verification note", key=note_key, help=NOTE_HELP)
            st.form_submit_button("Add claim", type="primary", on_click=add)


def entry_card(form: Form, doc: CommentedMap, section: str, i: int, skills: list[str]) -> None:
    path: Path = (section, i)
    entry = profile_edit.get(doc, path)
    what, details = ENTRIES[section]
    title = " · ".join(str(entry.get(k)) for k, _, _ in details[:2] if entry.get(k) and k != "repo")
    claims = entry.get("claims") or []
    # The label is part of an expander's identity, so a claim count here would close the card
    # whenever a claim is added or removed.
    card = st.expander(
        md(title or entry["id"]),
        key=f"pf-open-{section}-{entry['id']}",
        on_change="rerun",
    )
    with card:
        if not is_open(card):
            return
        cols = st.columns(2)
        for n, (key, label, placeholder) in enumerate(details):
            with cols[n % 2]:
                text_input(form, doc, (*path, key), label, placeholder)
        if note := profile_edit.note(entry):
            st.caption(f":material/verified: {md(note)}")
        stack = [str(s) for s in entry.get("stack") or []]
        st.multiselect(
            "Stack",
            sorted({*skills, *stack}, key=str.lower),
            default=stack,
            accept_new_options=True,
            help="Technologies used here. A bullet may only name a technology that's in its claim or this stack.",
            **form.bind((*path, "stack"), list),
        )
        st.markdown(f"**Claims** ({len(claims)})")
        for c in range(len(claims)):
            claim_row(form, doc, (*path, "claims", c), len(claims))
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            add_claim(form, doc, path)
            pop = popover(
                "Note", form.key(*path, "note-pop"), icon=":material/verified:", help="Verification note for the entry"
            )
            with pop:
                if is_open(pop):
                    note_editor(form, doc, path)
            st.space("stretch")
            item_buttons(form, path, len(doc[section]), what)


def add_entry(form: Form, doc: CommentedMap, section: str) -> None:
    what, details = ENTRIES[section]
    keys = {k: form.key(section, "new", k) for k, _, _ in details}
    taken = [str(e["id"]) for s in ENTRIES for e in doc.get(s) or []]

    def add() -> None:
        values = {k: _text(st.session_state[key]) for k, key in keys.items()}
        named = next((v for k in ("org", "name", "title") if (v := values.get(k)) is not None), None)
        if named is None:
            return
        entry = CommentedMap(id=profile_edit.new_id(taken, named))
        for k, _, _ in details:
            entry[k] = values[k] if k == "repo" else values[k] or ""
        entry["stack"], entry["claims"] = profile_edit.flow_list([]), []
        # Newest first, like the resume.
        form.edit(lambda d: profile_edit.insert(d, (section,), entry, 0), reshape=True)

    pop = popover(f"Add {what}", form.key(section, "new-pop"), icon=":material/add:")
    with pop:
        if not is_open(pop):
            return
        with st.form(form.key(section, "new-form"), border=False):
            for k, label, placeholder in details:
                st.text_input(label, key=keys[k], placeholder=placeholder)
            st.form_submit_button(f"Add {what}", type="primary", on_click=add)


def entries_tab(form: Form, doc: CommentedMap, section: str) -> None:
    skills = all_skills(doc)
    for i in range(len(doc.get(section) or [])):
        entry_card(form, doc, section, i, skills)
    add_entry(form, doc, section)


def skills_tab(form: Form, doc: CommentedMap, github_skills: list[str]) -> None:
    skills = doc.get("skills")
    if not isinstance(skills, CommentedMap):
        skills = CommentedMap()
    known = all_skills(doc)
    st.caption("Skills a resume may list, by category. Each tailored resume picks the ones that fit the posting.")
    for category in list(skills):
        items = [str(s) for s in skills[category] or []]
        name_key = form.key("skills", category, "name")

        def rename(category: str = category, name_key: str = name_key) -> None:
            new = str(st.session_state[name_key]).strip()
            if new:
                form.edit(lambda d: profile_edit.rename(d, ("skills", category), new), reshape=True)

        with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
            st.text_input("Category", category, key=name_key, on_change=rename, width=180)
            st.multiselect(
                "Skills",
                sorted({*known, *items}, key=str.lower),
                default=items,
                accept_new_options=True,
                **form.bind(("skills", category), list),
            )
            st.button(
                "",
                icon=":material/delete:",
                type="tertiary",
                help="Remove this category",
                key=form.key("skills", category, "remove"),
                on_click=form.edit,
                args=(lambda d, c=category: profile_edit.remove(d, ("skills", c)),),
                kwargs={"reshape": True},
            )
    new_key = form.key("skills", "new")

    def add_category() -> None:
        name = str(st.session_state[new_key]).strip()
        if name and name not in skills:
            form.edit(lambda d: profile_edit.set_value(d, ("skills", name), profile_edit.flow_list([])), reshape=True)

    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        st.text_input("Add a category", key=new_key, on_change=add_category, placeholder="Cloud", width=180)
        listed = {str(s).lower() for items in skills.values() for s in items or []}
        unlisted = sorted({s for s in [*known, *github_skills] if s.lower() not in listed}, key=str.lower)
        if unlisted and skills:
            suggested_skills(form, list(skills), unlisted)


def suggested_skills(form: Form, categories: list[str], unlisted: list[str]) -> None:
    """Skills from stacks and GitHub that no category lists yet, to file under one."""
    pick_key, into_key = form.key("skills", "suggested"), form.key("skills", "suggested-into")

    def add() -> None:
        picked, into = list(st.session_state[pick_key]), str(st.session_state[into_key])

        def edit(d: CommentedMap) -> None:
            items = list(profile_edit.get(d, ("skills", into)) or [])
            profile_edit.set_value(d, ("skills", into), [*items, *picked])

        if picked:
            form.edit(edit, reshape=True)

    pop = popover(
        f"Suggested skills ({len(unlisted)})", form.key("skills", "suggested-pop"), icon=":material/lightbulb:"
    )
    with pop:
        if is_open(pop):
            suggested_form(form, categories, unlisted, add)


def suggested_form(form: Form, categories: list[str], unlisted: list[str], add: Callable[[], None]) -> None:
    pick_key, into_key = form.key("skills", "suggested"), form.key("skills", "suggested-into")
    with st.form(form.key("skills", "suggested-form"), border=False):
        st.caption(
            "Named in a stack or shown by your GitHub repos, but not in a category. "
            "Resumes may already list them; filing one here sets its category."
        )
        st.multiselect("Skills", unlisted, key=pick_key)
        st.selectbox("Add to", categories, key=into_key)
        st.form_submit_button("Add", type="primary", on_click=add)


def education_tab(form: Form, doc: CommentedMap) -> None:
    schools = doc.get("education") or []
    for i, school in enumerate(schools):
        path: Path = ("education", i)
        card = st.expander(md(school.get("school") or "School"), key=f"pf-open-education-{i}", on_change="rerun")
        with card:
            if not is_open(card):
                continue
            cols = st.columns(2)
            for n, (key, label, placeholder) in enumerate(EDUCATION):
                with cols[n % 2]:
                    text_input(form, doc, (*path, key), label, placeholder)
            coursework_editor(form, doc, (*path, "coursework"))
            with st.container(horizontal=True):
                st.space("stretch")
                item_buttons(form, path, len(schools), "school")
    st.button(
        "Add school",
        icon=":material/add:",
        key=form.key("education", "add"),
        on_click=form.edit,
        args=(
            lambda d: profile_edit.insert(
                d, ("education",), CommentedMap({k: "" for k, _, _ in EDUCATION} | {"coursework": {}}), 0
            ),
        ),
        kwargs={"reshape": True},
    )


def coursework_editor(form: Form, doc: CommentedMap, path: Path) -> None:
    current = profile_edit.get(doc, path) or {}
    key = form.key(*path)
    df = pd.DataFrame(
        {"Area": list(current), "Courses": [" ".join(str(v).split()) for v in current.values()]},
        dtype="string",
    )

    def changed() -> None:
        rows = table_rows(df, st.session_state[key])
        new = {r["Area"].strip(): r["Courses"].strip() for r in rows if (r.get("Area") or "").strip()}

        def edit(d: CommentedMap) -> None:
            for area in [a for a in profile_edit.get(d, path) or {} if a not in new]:
                profile_edit.remove(d, (*path, area))
            for area, courses in new.items():
                profile_edit.set_value(d, (*path, area), courses)

        form.edit(edit, reshape=True)

    st.markdown("**Coursework**")
    st.data_editor(
        df,
        key=key,
        num_rows="dynamic",
        hide_index=True,
        on_change=changed,
        column_config={
            "Area": st.column_config.TextColumn(width="small"),
            "Courses": st.column_config.TextColumn(width="large"),
        },
    )


def table_rows(df: pd.DataFrame, delta: dict[str, Any]) -> list[dict[str, str]]:
    """The rows of `df` after a data editor's edits (its session-state delta)."""
    rows = [{str(k): "" if pd.isna(v) else str(v) for k, v in row.items()} for row in df.to_dict("records")]
    for i, change in delta.get("edited_rows", {}).items():
        rows[int(i)].update({k: "" if v is None else str(v) for k, v in change.items()})
    gone = set(delta.get("deleted_rows", []))
    rows = [r for i, r in enumerate(rows) if i not in gone]
    rows += [{k: "" if v is None else str(v) for k, v in r.items()} for r in delta.get("added_rows", [])]
    return rows


def contact_tab(form: Form, doc: CommentedMap) -> None:
    contact = doc.get("contact") or {}
    fields = [*CONTACT, *((k, k.capitalize()) for k in contact if k not in dict(CONTACT))]
    cols = st.columns(2)
    for n, (key, label) in enumerate(fields):
        with cols[n % 2]:
            text_input(form, doc, ("contact", key), label)


FACTS_TABS = ("Experience", "Projects", "Skills", "Education", "Contact")


def facts_form(form: Form, doc: CommentedMap, github_skills: list[str]) -> None:
    tabs = st.tabs(FACTS_TABS, key="pf-facts-tabs", on_change="rerun")
    renders = (
        partial(entries_tab, form, doc, "experience"),
        partial(entries_tab, form, doc, "projects"),
        partial(skills_tab, form, doc, github_skills),
        partial(education_tab, form, doc),
        partial(contact_tab, form, doc),
    )
    for tab, render in zip(tabs, renders, strict=True):
        with tab:
            if is_open(tab):
                render()


# Preferences


def number_field(form: Form, f: Field, value: object) -> None:
    st.number_input(
        f.label,
        min_value=f.min_value,
        max_value=f.max_value,
        value=value if isinstance(value, int) else (None if f.optional else f.default),
        step=1,
        help=f.help or None,
        placeholder="No limit" if f.optional else None,
        **form.bind(f.path, lambda v: None if v is None else int(v)),
    )


def choice_field(form: Form, f: Field, value: object) -> None:
    options = [*f.options, *([str(value)] if value is not None and value not in f.options else [])]
    st.selectbox(
        f.label,
        options,
        index=options.index(str(value)) if value is not None else None,
        help=f.help or None,
        accept_new_options=f.other_options,
        **form.bind(f.path),
    )


def field(form: Form, f: Field, prefs: CommentedMap, jobs: list[Job]) -> None:
    value = profile_edit.get(prefs, f.path)
    if value is None and not f.optional:
        value = f.default
    if f.widget == "patterns":
        patterns_field(form, f, [str(p) for p in value or []], jobs)
    elif f.widget == "long_text":
        st.text_area(
            f.label,
            str(value or ""),
            height=text_height(str(value or "")),
            help=f.help or None,
            **form.bind(f.path, _required_text),
        )
    elif f.widget == "text":
        st.text_input(f.label, str(value or ""), help=f.help or None, **form.bind(f.path, _required_text))
    elif f.widget == "number":
        number_field(form, f, value)
    elif f.widget == "level":
        st.slider(
            f.label,
            f.min_value,
            f.max_value,
            int(value or f.min_value),
            help=f.help or None,
            **form.bind(f.path, int),
        )
    elif f.widget == "toggle":
        st.toggle(f.label, bool(value), help=f.help or None, **form.bind(f.path, bool))
    else:
        choice_field(form, f, value)


def patterns_field(form: Form, f: Field, patterns: list[str], jobs: list[Job]) -> None:
    """A pattern list as a table, with how many past jobs each pattern matches."""
    values = sorted({str(j.get(f.matches) or "") for j in jobs} - {""})
    hits = profile_edit.pattern_hits(patterns, values)
    df = pd.DataFrame(
        {
            "Pattern": patterns,
            "Matches": [len(h) for h in hits],
            "For example": [", ".join(h[:EXAMPLES_SHOWN]) for h in hits],
        }
    ).astype({"Pattern": "string", "For example": "string"})
    key = form.key(*f.path)

    words_key = form.key(*f.path, "words")

    def changed() -> None:
        rows = table_rows(df, st.session_state[key])
        new = [r["Pattern"].strip() for r in rows if (r.get("Pattern") or "").strip()]
        form.edit(lambda d: profile_edit.set_value(d, f.path, new), reshape=True)

    def add_words() -> None:
        if (pattern := profile_edit.words_pattern(str(st.session_state[words_key]))) is not None:
            form.edit(lambda d: profile_edit.set_value(d, f.path, [*patterns, pattern]), reshape=True)

    st.markdown(f"**{f.label}**")
    st.caption(f.help)
    st.data_editor(
        df,
        key=key,
        num_rows="dynamic",
        hide_index=True,
        on_change=changed,
        disabled=["Matches", "For example"],
        column_config={
            "Pattern": st.column_config.TextColumn(width=360, required=True),
            "Matches": st.column_config.NumberColumn(help=f"Past jobs whose {f.matches} matches", width=80),
            "For example": st.column_config.TextColumn(),
        },
    )
    for pattern, problem in profile_edit.bad_patterns(patterns):
        st.caption(f":red[:material/error:] `{md(pattern)}` isn't a valid pattern: {md(problem)}")
    st.text_input(
        "Add words",
        key=words_key,
        on_change=add_words,
        placeholder=WORDS_HINT[f.matches],
        help="Comma-separated words or phrases, matched as whole words. Adds one row.",
        width=360,
    )


def filter_tester(prefs: dict[str, Any], stored: dict[str, Any] | None, jobs: list[Job]) -> None:
    """Try the draft's filters on a made-up job, and on the jobs seen so far."""
    st.markdown("**Try the filters**")
    with st.container(horizontal=True, gap="small"):
        title = st.text_input("Title", placeholder="Junior Backend Engineer", key="pf-try-title")
        company = st.text_input("Company", placeholder="Acme", key="pf-try-company")
        location = st.text_input("Location", placeholder="Portland, Oregon, United States", key="pf-try-location")
    if title or company or location:
        reason = triage.hard_filter({"title": title, "company": company, "location": location}, prefs)
        st.markdown(
            f":red[:material/block:] Skipped: {md(reason)}"
            if reason is not None
            else ":green[:material/check_circle:] Kept"
        )
    if stored is None or not jobs:
        return
    now_skipped, now_kept = [], []
    for job in jobs:
        before, after = triage.hard_filter(job, stored), triage.hard_filter(job, prefs)
        if after is not None and before is None:
            now_skipped.append((job, after))
        elif before is not None and after is None:
            now_kept.append((job, before))
    if not now_skipped and not now_kept:
        st.caption(f"Your changes don't change the outcome for any of the {len(jobs)} jobs seen so far.")
        return
    st.caption(
        f"Of the {len(jobs)} jobs seen so far, your changes would skip {len(now_skipped)} more "
        f"and keep {len(now_kept)} more. Jobs already seen aren't filtered again."
    )
    for label, rows in (("Now skipped", now_skipped), ("Now kept", now_kept)):
        if rows:
            with st.expander(f"{label} ({len(rows)})"):
                st.markdown(
                    "\n".join(f"- {md(j.get('title'))} · {md(j.get('company') or '')} ({md(why)})" for j, why in rows)
                )


def preferences_form(form: Form, doc: CommentedMap, stored: dict[str, Any] | None, jobs: list[Job]) -> None:
    for section in profile_edit.PREFERENCES:
        with st.container(border=True):
            st.markdown(f"**{section.title}**")
            st.caption(section.help)
            simple = [f for f in section.fields if f.widget in {"number", "level", "choice"}]
            for f in section.fields:
                if f not in simple:
                    field(form, f, doc, jobs)
            cols = st.columns(3)
            for n, f in enumerate(simple):
                with cols[n % 3]:
                    field(form, f, doc, jobs)
            if section.title == "Filters":
                filter_tester(doc, stored, jobs)
    if other := [".".join(p) for p in leaf_paths(doc) if p not in profile_edit.FIELDS]:
        st.caption(f"Also set, editable under YAML: {md(', '.join(f'`{o}`' for o in other))}.")


def leaf_paths(node: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:  # noqa: ANN401
    """Paths to the settings in a preferences document (lists count as one setting)."""
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in leaf_paths(v, (*path, str(k)))]
    return [path]
