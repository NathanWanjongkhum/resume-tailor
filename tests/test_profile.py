import sqlite3

import pytest

from tailor import db, profile, profile_edit


@pytest.fixture
def conn() -> sqlite3.Connection:
    return db.connect()


def example(kind: str) -> str:
    return (profile.EXAMPLES / f"{kind}.yaml").read_text()


def test_bad_upload_never_replaces_the_stored_copy(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="education"):
        profile.put(conn, "facts", "contact: {}\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        profile.put(conn, "preferences", "gmail: [\n")
    assert profile.get(conn, "facts") == example("facts")
    assert profile.history(conn, "facts") == []


def test_replacing_keeps_the_old_version(conn: sqlite3.Connection) -> None:
    edited = example("facts").replace("Alex Rivera", "Sam Lee")
    profile.put(conn, "facts", edited)
    profile.put(conn, "facts", edited)  # unchanged: no new history entry
    [old] = profile.history(conn, "facts")
    assert old["body"] == example("facts")
    assert profile.get(conn, "facts") == edited


def test_history_is_capped(conn: sqlite3.Connection) -> None:
    for n in range(profile.HISTORY_KEEP + 5):
        profile.put(conn, "preferences", example("preferences") + f"# edit {n}\n")
    assert len(profile.history(conn, "preferences")) == profile.HISTORY_KEEP


def test_changes_describe_facts_by_claim() -> None:
    old = example("facts")
    new = (
        old.replace(
            "      - id: tr-docker", "      - id: tr-tests\n        text: Wrote pytest tests.\n      - id: tr-docker"
        )
        .replace("one-command\n          local setup", "a one-command setup")
        .replace("Sept. 2024", "Aug. 2024")
    )
    new = new.replace(
        """      - id: nw-auth
        text: >-
          Added role-based access control so client and contractor data stay
          isolated from each other.
""",
        "",
    )
    got = profile.changes("facts", old, new)
    assert "Adds claim `tr-tests`: Wrote pytest tests." in got
    assert any(line.startswith("Removes claim `nw-auth`") for line in got)
    assert "Rewords claim `tr-docker`" in got
    assert "Changes dates of `northwind`" in got
    assert profile.changes("facts", old, old + "# note\n") == ["Only comments or formatting change."]


def test_changes_describe_skills_and_order() -> None:
    old = example("facts")
    doc = profile_edit.parse(old)
    profile_edit.move(doc, ("projects", 1), -1)
    profile_edit.move(doc, ("experience", 0, "claims", 2), -2)
    profile_edit.set_value(doc, ("skills", "Tools"), ["Git", "Docker", "AWS", "Firebase", "Linux", "Kubernetes"])
    profile_edit.set_value(doc, ("contact", "phone"), "555-0199")
    assert profile.changes("facts", old, profile_edit.dump(doc)) == [
        "Changes `contact.phone`: 555-0142 → 555-0199",
        "Adds `Kubernetes` to `skills.Tools`",
        "Reorders the claims of `northwind`",
        "Reorders projects",
    ]
    assert profile.changes("facts", None, old) == ["Adds facts (nothing is stored yet)."]


def test_changes_describe_preferences_by_setting() -> None:
    old = example("preferences")
    new = old.replace("max_per_run: 3 ", "max_per_run: 5 ").replace("company_exclude: []", "company_exclude: [Acme]")
    assert profile.changes("preferences", old, new) == [
        "Adds `Acme` to `hard_filters.company_exclude`",
        "Changes `generate.max_per_run`: 3 → 5",
    ]
    assert profile.changes("preferences", old, new.replace("Real engineering", "Hands-on engineering")) == [
        "Adds `Acme` to `hard_filters.company_exclude`",
        "Changes `generate.max_per_run`: 3 → 5",
        "Rewords `wants`",
    ]


def test_preferences_that_would_fail_a_run_are_refused() -> None:
    with pytest.raises(ValueError, match="valid pattern"):
        profile.check("preferences", example("preferences").replace("- remote", "- remote(", 1))
    with pytest.raises(ValueError, match="must be one of"):
        profile.check("preferences", example("preferences").replace("effort: low", "effort: lowest", 1))
