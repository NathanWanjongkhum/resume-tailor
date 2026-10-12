from pathlib import Path

import pytest

from tailor import db, profile


@pytest.fixture(autouse=True)
def example_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own database holding the example profile, never the user's."""
    path = tmp_path / "jobs.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.connect()
    for kind in profile.UPLOADABLE:
        profile.put(conn, kind, (profile.EXAMPLES / f"{kind}.yaml").read_text())
    return path
