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


def test_plan_reset_is_paced_and_bounded() -> None:
    from vintage_utm_mcp.mouse import RESET_STEP, plan_reset

    moves = plan_reset(720, 400)
    # No single event may carry a big backlog the PS/2 device can't drain.
    assert all(abs(dx) <= RESET_STEP and abs(dy) <= RESET_STEP for dx, dy in moves)
    total_x = sum(dx for dx, _ in moves)
    total_y = sum(dy for _, dy in moves)
    # Enough to reach the corner from anywhere, without wild overshoot.
    assert -720 * 2 < total_x <= -720
    assert -400 * 4 < total_y <= -400


def test_plan_reset_defaults_without_known_screen() -> None:
    from vintage_utm_mcp.mouse import plan_reset

    assert plan_reset(None, None), "must still plan a reset with no screen info"


def test_image_to_guest_floors_within_the_upscaled_block() -> None:
    # A 2x screenshot turns guest pixel k into image pixels 2k and 2k+1,
    # so both halves of the block must map back to k. Rounding sent the
    # odd ones to k+1: image x=3 lies in guest pixel 1's block, not 2's.
    info = {"width": 1280, "height": 960, "scale": 2}
    assert image_to_guest(0, 0, info) == (0, 0)
    assert image_to_guest(1, 1, info) == (0, 0)
    assert image_to_guest(2, 2, info) == (1, 1)
    assert image_to_guest(3, 3, info) == (1, 1)


def test_image_to_guest_keeps_the_last_pixel_on_screen() -> None:
    # Measured at 640x480: the bottom-right pixel of its 2x screenshot used
    # to map to (640, 480), one row and column past the end of the guest.
    info = {"width": 1280, "height": 960, "scale": 2}
    assert image_to_guest(1279, 959, info) == (639, 479)
