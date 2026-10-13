import yaml

from tailor import profile, profile_edit
from tailor.facts import Facts

NOTED = (
    (profile.EXAMPLES / "facts.yaml")
    .read_text()
    .replace("  - id: trail-app\n", "  # Verified against the trail-app repo.\n  # Second line.\n  - id: trail-app\n")
    .replace("  - id: img-pipeline\n", "  # Verified img.\n  - id: img-pipeline\n")
    .replace("      - id: tr-docker\n", "      # Checked docker-compose.yml.\n      - id: tr-docker\n")
)


def test_round_trip_is_exact() -> None:
    for text in (NOTED, *((profile.EXAMPLES / f"{k}.yaml").read_text() for k in profile.UPLOADABLE)):
        assert profile_edit.dump(profile_edit.parse(text)) == text


def test_notes_move_and_go_with_their_entries() -> None:
    doc = profile_edit.parse(NOTED)
    assert profile_edit.note(doc["projects"][1]) == "Verified against the trail-app repo.\nSecond line."
    profile_edit.move(doc, ("projects", 1), -1)
    text = profile_edit.dump(doc)
    assert "  # Verified against the trail-app repo.\n  # Second line.\n  - id: trail-app\n" in text
    assert "  # Verified img.\n  - id: img-pipeline\n" in text
    assert text.index("trail-app") < text.index("img-pipeline")
    profile_edit.remove(doc, ("projects", 0))
    text = profile_edit.dump(doc)
    assert "trail" not in text
    assert "Checked docker" not in text
    assert "  # Verified img.\n  - id: img-pipeline\n" in text
    Facts(yaml.safe_load(text))


def test_edits_keep_styles_and_comments() -> None:
    doc = profile_edit.parse(NOTED)
    profile_edit.set_value(doc, ("experience", 0, "claims", 0, "text"), "Built  a booking platform " + "word " * 20)
    profile_edit.set_value(doc, ("experience", 0, "stack"), ["React", "Go"])
    profile_edit.set_note(doc, ("experience", 0, "claims", 1), "Seen in the commit log.")
    profile_edit.set_note(doc, ("projects", 0), "")
    text = profile_edit.dump(doc)
    assert "    stack: [React, Go]\n" in text
    assert "        text: >-\n          Built a booking platform word" in text
    assert "      # Checked docker-compose.yml.\n      - id: tr-docker\n" in text
    assert "      # Seen in the commit log.\n      - id: nw-messaging\n" in text
    assert "Verified img" not in text
    assert "# Every claim a generated resume" in text
    assert all(len(line) <= profile_edit.FOLD_WIDTH + 10 for line in text.splitlines())
    data = yaml.safe_load(text)
    assert data["experience"][0]["claims"][0]["text"].startswith("Built a booking platform word word")


def test_settings_are_set_created_and_removed() -> None:
    text = (profile.EXAMPLES / "preferences.yaml").read_text()
    doc = profile_edit.parse(text)
    profile_edit.set_value(doc, ("triage", "min_relevance"), 4)
    profile_edit.set_value(doc, ("hard_filters", "max_years_required"), None)
    profile_edit.set_value(doc, ("hard_filters", "title_exclude"), [r"\bsenior\b"])
    profile_edit.set_value(doc, ("wants",), "Backend roles.\nRemote.")
    profile_edit.set_value(doc, ("extra", "thing"), 1)
    out = profile_edit.dump(doc)
    data = yaml.safe_load(out)
    assert data["triage"]["min_relevance"] == 4
    assert "max_years_required" not in data["hard_filters"]
    assert data["hard_filters"]["title_exclude"] == [r"\bsenior\b"]
    assert data["wants"] == "Backend roles.\nRemote.\n"
    assert data["extra"] == {"thing": 1}
    assert "  min_relevance: 4\n" in out
    assert "# Case-insensitive regexes matched against the job title." in out


def test_a_comment_inside_a_block_scalar_is_left_alone() -> None:
    text = "wants: |\n  # not a comment\n  - id: not an item\nlist:\n  - id: a\n"
    assert profile_edit.dump(profile_edit.parse(text)) == text
    assert profile_edit.parse(text)["wants"] == "# not a comment\n- id: not an item\n"


def test_rename_keeps_position() -> None:
    doc = profile_edit.parse((profile.EXAMPLES / "facts.yaml").read_text())
    profile_edit.rename(doc, ("skills", "Frameworks"), "Libraries")
    assert list(doc["skills"]) == ["Languages", "Libraries", "Tools"]


def test_new_ids_are_unused() -> None:
    assert profile_edit.new_id([], "Northwind Studio") == "northwind-studio"
    assert profile_edit.new_id(["tr-api", "tr-api-2"], "tr", "api") == "tr-api-3"
    assert profile_edit.new_id([], "") == "item"


def test_words_become_one_whole_word_pattern() -> None:
    assert profile_edit.words_pattern(" , ") is None
    assert profile_edit.words_pattern("intern") == r"\bintern\b"
    assert profile_edit.words_pattern("intern, head of") == r"\b(intern|head of)\b"
    pattern = profile_edit.words_pattern("intern, C++")
    assert pattern is not None
    titles = ["Software Intern", "Head of Data", "Internal Tools", "C++ Dev"]
    assert profile_edit.pattern_hits([pattern], titles) == [["Software Intern", "C++ Dev"]]


def test_pattern_hits() -> None:
    hits = profile_edit.pattern_hits([r"\bsenior\b", "(", "intern"], ["Senior Dev", "Intern", "Seniority Analyst"])
    assert hits == [["Senior Dev"], [], ["Intern"]]
