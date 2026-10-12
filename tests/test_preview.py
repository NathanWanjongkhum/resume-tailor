from pathlib import Path

from tailor import preview


def copy_of(url: str, static: Path) -> Path:
    return static / url.removeprefix("app/static/").removesuffix(preview.VIEW)


def test_publish_names_copies_by_content(tmp_path: Path) -> None:
    pdf, static = tmp_path / "resume.pdf", tmp_path / "static"
    pdf.write_bytes(b"%PDF-1 first")
    first = preview.publish(pdf, static)
    assert preview.publish(pdf, static) == first
    pdf.write_bytes(b"%PDF-1 second")
    second = preview.publish(pdf, static)
    assert second != first
    assert copy_of(first, static).read_bytes() == b"%PDF-1 first"
    assert copy_of(second, static).read_bytes() == b"%PDF-1 second"
    assert len(list(static.iterdir())) == 2  # no temporary files left behind


def test_prune_keeps_only_current_resumes(tmp_path: Path) -> None:
    kept, gone, static = tmp_path / "kept.pdf", tmp_path / "gone.pdf", tmp_path / "static"
    kept.write_bytes(b"%PDF-1 kept")
    gone.write_bytes(b"%PDF-1 gone")
    kept_url = preview.publish(kept, static)
    preview.publish(gone, static)
    gone.unlink()
    preview.prune([kept, gone], static)
    assert list(static.iterdir()) == [copy_of(kept_url, static)]
