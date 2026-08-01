"""Mouse support for relative-pointer guests (PS/2, ADB).

Retro guests have no absolute-positioning tablet device, so pointing is
done by dead reckoning: anchor the cursor at the top-left corner with a
huge negative move (the screen edge clamps it deterministically), then
walk to the target with small relative steps.

Two guest-side effects make the walk imperfect and calibratable:
- drivers accelerate large deltas; steps are kept small (<= STEP) to stay
  under the usual thresholds;
- drivers scale mickeys to pixels per axis (DOS's MOUSE.COM default
  vertical ratio is half the horizontal one, i.e. it needs y = 2.0).

Per-VM calibration lives in the [mouse] table of the config file:

    [mouse."MS-DOS 6.22"]
    x = 1.0
    y = 2.0
"""

from __future__ import annotations

import math
import tomllib

from vintage_utm_mcp import qmp

STEP = 24  # max per-axis delta for precise walking
RESET_STEP = 48  # coarser pacing for the corner reset
RESET_MARGIN_X = 1.5  # safety factor over screen width
RESET_MARGIN_Y = 2.5  # vertical mickeys are ~2x pixels on DOS drivers

VALID_BUTTONS = frozenset({"left", "right", "middle"})


def plan_reset(
    guest_width: int | None, guest_height: int | None, step: int = RESET_STEP
) -> list[tuple[int, int]]:
    """Plan a paced walk to the top-left corner.

    PS/2 mice carry at most 127 units per packet, so a single huge delta
    (e.g. -5000) leaves thousands of units queued in the device, draining
    for seconds — any movement or click sent meanwhile is swallowed by
    that backlog and lands at the corner instead of the target. Keep the
    reset just big enough to reach the corner from anywhere on screen,
    and deliver it in paced steps the guest can consume.
    """
    width = guest_width or 1024
    height = guest_height or 768
    return plan_moves(
        -round(width * RESET_MARGIN_X), -round(height * RESET_MARGIN_Y), step
    )


class MouseError(ValueError):
    """Invalid mouse request."""


def image_to_guest(x: int, y: int, screen_info: dict | None) -> tuple[int, int]:
    """Map coordinates in the last screenshot's pixel space to guest pixels.

    Screenshots of retro resolutions are upscaled (usually 2x); clicks are
    aimed on that image, so divide by the recorded scale. Without a prior
    screenshot, assume 1:1.

    Floor, not round: the upscale is nearest-neighbour, so guest pixel k
    becomes the whole image block from k*scale to k*scale+scale-1, and
    every pixel of that block has to map back to k. Rounding sends the
    second half of each block to k+1 — and the very last pixel of a 2x
    screenshot of a 640x480 guest to x=640, a column that does not exist.
    """
    scale = (screen_info or {}).get("scale", 1) or 1
    return int(x // scale), int(y // scale)


def apply_calibration(
    dx: int, dy: int, calibration: tuple[float, float]
) -> tuple[int, int]:
    return round(dx * calibration[0]), round(dy * calibration[1])


def plan_moves(dx: int, dy: int, step: int = STEP) -> list[tuple[int, int]]:
    """Split a movement into steps of at most `step` per axis.

    Uses cumulative rounding so the steps sum exactly to (dx, dy).
    """
    if dx == 0 and dy == 0:
        return []
    n = max(math.ceil(abs(dx) / step), math.ceil(abs(dy) / step), 1)
    moves = []
    prev_x = prev_y = 0
    for i in range(1, n + 1):
        cx = round(dx * i / n)
        cy = round(dy * i / n)
        moves.append((cx - prev_x, cy - prev_y))
        prev_x, prev_y = cx, cy
    return [m for m in moves if m != (0, 0)]


def read_calibration(vm: str) -> tuple[float, float]:
    """Per-VM (x, y) calibration factors from the config file; (1, 1) if unset."""
    if not qmp.CONFIG_FILE.exists():
        return (1.0, 1.0)
    data = tomllib.loads(qmp.CONFIG_FILE.read_text())
    entry = data.get("mouse", {}).get(vm, {})
    return (float(entry.get("x", 1.0)), float(entry.get("y", 1.0)))


def read_hotspot(vm: str) -> tuple[int, int] | None:
    """Per-VM hotspot override from the [hotspot] table, or None to measure.

        [hotspot."Mac OS 9.2.2 (ppc)"]
        x = 1
        y = 1
    """
    if not qmp.CONFIG_FILE.exists():
        return None
    data = tomllib.loads(qmp.CONFIG_FILE.read_text())
    entry = data.get("hotspot", {}).get(vm)
    if not entry:
        return None
    return (int(entry.get("x", 0)), int(entry.get("y", 0)))


def pick_relative_mouse(mice: list[dict]) -> int | None:
    """Given QMP query-mice output, decide whether to switch pointers.

    QEMU routes un-addressed input events to the "current" pointer. When
    a VM has both an absolute tablet and a relative mouse (PS/2, ADB) and
    the tablet is current, retro guests — which have no tablet driver —
    silently lose our events. Returns the index of a relative pointer to
    switch to (via the mouse_set monitor command), or None when the
    current pointer is already relative (or no relative pointer exists).
    """
    current = next((m for m in mice if m.get("current")), None)
    if current is not None and not current.get("absolute"):
        return None  # already routed to a relative pointer
    fallback = next((m for m in mice if not m.get("absolute")), None)
    return fallback["index"] if fallback is not None else None


def validate_button(button: str) -> str:
    if button not in VALID_BUTTONS:
        raise MouseError(
            f"invalid button {button!r}; expected one of {sorted(VALID_BUTTONS)}"
        )
    return button
