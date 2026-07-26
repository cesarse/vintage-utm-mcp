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
