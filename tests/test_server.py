import pytest

from vintage_utm_mcp import server


def test_resolve_image_explicit_path(tmp_path) -> None:
    img = tmp_path / "boot.img"
    img.write_bytes(b"\0")
    assert server._resolve_image(str(img)) == img.resolve()


def test_resolve_image_via_library(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "HELPER_DOCUMENTS", tmp_path / "Documents")
    library = tmp_path / "Documents" / "vintage-utm-mcp"
    library.mkdir(parents=True)
    img = library / "Disk4.img"
    img.write_bytes(b"\0")
    assert server._resolve_image("Disk4.img") == img.resolve()


def test_resolve_image_not_found_reports_searched_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "HELPER_DOCUMENTS", tmp_path / "Documents")
    with pytest.raises(ValueError, match="Disk9.img"):
        server._resolve_image("Disk9.img")


def test_stage_into_library_copies(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "HELPER_DOCUMENTS", tmp_path / "Documents")
    src = tmp_path / "Disk4.img"
    src.write_bytes(b"floppy")
    staged = server._stage_into_library(src)
    assert staged == tmp_path / "Documents" / "vintage-utm-mcp" / "Disk4.img"
    assert staged.read_bytes() == b"floppy"


def test_stage_into_library_reuses_same_size(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "HELPER_DOCUMENTS", tmp_path / "Documents")
    src = tmp_path / "Disk4.img"
    src.write_bytes(b"floppy")
    first = server._stage_into_library(src)
    again = server._stage_into_library(src)
    assert first == again


def test_stage_into_library_rejects_name_collision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "HELPER_DOCUMENTS", tmp_path / "Documents")
    src = tmp_path / "Disk4.img"
    src.write_bytes(b"floppy")
    server._stage_into_library(src)
    other = tmp_path / "elsewhere"
    other.mkdir()
    conflicting = other / "Disk4.img"
    conflicting.write_bytes(b"different content!")
    with pytest.raises(ValueError, match="rename"):
        server._stage_into_library(conflicting)
