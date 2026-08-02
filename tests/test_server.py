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


def test_screen_mismatch_accepts_a_matching_screenshot() -> None:
    # 640x480 guest captured at 2x, and the guest is still 640x480.
    info = {"width": 1280, "height": 960, "scale": 2}
    assert server.screen_mismatch(info, 640, 480) is None
    # From 800 px wide up the screenshot is 1:1.
    assert server.screen_mismatch({"width": 1280, "height": 1024, "scale": 1},
                                  1280, 1024) is None


def test_screen_mismatch_catches_a_resolution_change() -> None:
    # Measured: aiming at (700, 500) on a stale 2x image landed the pointer
    # at (350, 250) and still reported on_target, because it was on the
    # target it had been given.
    stale = {"width": 1280, "height": 960, "scale": 2}
    complaint = server.screen_mismatch(stale, 1280, 1024)
    assert complaint is not None
    assert "640x480" in complaint and "1280x1024" in complaint


def test_screen_mismatch_says_nothing_without_a_screenshot() -> None:
    # No screenshot cached yet: coordinates are guest-native, nothing to
    # contradict.
    assert server.screen_mismatch(None, 1280, 1024) is None


def test_resolve_passes_an_unknown_vm_through(monkeypatch) -> None:
    # Unchanged behaviour: an unrecognised name is handed on rather than
    # rejected here, so the failure surfaces as a QMP error naming the
    # endpoint it tried.
    monkeypatch.setattr(server.utmctl, "canonical_name", lambda vm: None)
    assert server._resolve("Nonexistent VM") == "Nonexistent VM"


def test_resolve_maps_a_uuid_to_the_canonical_name(monkeypatch) -> None:
    monkeypatch.setattr(server.utmctl, "canonical_name", lambda vm: "MS-DOS 6.22")
    assert server._resolve("12345678-ABCD-4321-9876-ABCDEF012345") == "MS-DOS 6.22"


def test_unverified_click_allows_a_measured_position() -> None:
    assert server.unverified_click({"verified": True, "on_target": True}) is None


def test_unverified_click_allows_a_near_miss() -> None:
    # Being a pixel out still hits the button aimed at; only a lost
    # cursor means the position is unknown.
    assert (
        server.unverified_click(
            {"verified": True, "on_target": False, "residual": [1, 0]}
        )
        is None
    )


def test_unverified_click_allows_the_open_loop_fallback() -> None:
    # Open loop never claims to know; refusing there would disable
    # pointing on guests whose cursor is not in the framebuffer.
    assert server.unverified_click({"method": "open-loop", "on_target": None}) is None


def test_unverified_click_refuses_a_lost_cursor() -> None:
    problem = server.unverified_click(
        {"verified": False, "guest_coords": [70, 31], "lost_reason": "changed shape"}
    )
    assert problem is not None
    assert "changed shape" in problem and "[70, 31]" in problem
