import pytest

from vintage_utm_mcp import mouse, qmp
from vintage_utm_mcp.mouse import (
    MouseError,
    apply_calibration,
    image_to_guest,
    plan_moves,
    read_calibration,
    validate_button,
)


def test_plan_moves_sums_exactly_and_respects_step() -> None:
    for dx, dy in [(100, -30), (-7, 0), (0, 500), (23, 23), (-1000, 999)]:
        moves = plan_moves(dx, dy, step=24)
        assert sum(m[0] for m in moves) == dx
        assert sum(m[1] for m in moves) == dy
        assert all(abs(m[0]) <= 24 and abs(m[1]) <= 24 for m in moves)


def test_plan_moves_zero_is_empty() -> None:
    assert plan_moves(0, 0) == []


def test_image_to_guest_divides_by_scale() -> None:
    info = {"width": 1280, "height": 960, "scale": 2}
    assert image_to_guest(640, 480, info) == (320, 240)
    assert image_to_guest(640, 480, None) == (640, 480)  # no screenshot yet


def test_apply_calibration_rounds() -> None:
    assert apply_calibration(100, 100, (1.0, 2.0)) == (100, 200)
    assert apply_calibration(33, 33, (1.5, 1.0)) == (50, 33)


def test_read_calibration_from_config(tmp_path, monkeypatch) -> None:
    config = tmp_path / "qmp.toml"
    config.write_text('[mouse."MS-DOS 6.22"]\nx = 1.0\ny = 2.0\n')
    monkeypatch.setattr(qmp, "CONFIG_FILE", config)
    assert read_calibration("MS-DOS 6.22") == (1.0, 2.0)
    assert read_calibration("Mac OS 9.2.2 (ppc)") == (1.0, 1.0)


def test_read_calibration_without_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(qmp, "CONFIG_FILE", tmp_path / "absent.toml")
    assert read_calibration("anything") == (1.0, 1.0)


def test_validate_button() -> None:
    assert validate_button("left") == "left"
    with pytest.raises(MouseError, match="wheel"):
        validate_button("wheel")


def test_pick_relative_mouse_switches_from_tablet() -> None:
    mice = [
        {"index": 0, "name": "QEMU PS/2 Mouse", "current": False, "absolute": False},
        {"index": 1, "name": "QEMU USB Tablet", "current": True, "absolute": True},
    ]
    assert mouse.pick_relative_mouse(mice) == 0


def test_pick_relative_mouse_already_relative() -> None:
    mice = [
        {"index": 0, "name": "QEMU PS/2 Mouse", "current": True, "absolute": False},
        {"index": 1, "name": "QEMU USB Tablet", "current": False, "absolute": True},
    ]
    assert mouse.pick_relative_mouse(mice) is None


def test_pick_relative_mouse_no_relative_available() -> None:
    mice = [{"index": 1, "name": "QEMU USB Tablet", "current": True, "absolute": True}]
    assert mouse.pick_relative_mouse(mice) is None
