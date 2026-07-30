"""Closed-loop pointing for graphics-mode guests (Windows 3.x, classic Mac OS).

Text mode is driven from the keyboard (see read_shortcuts/activate_menu):
there, a cell grid and an attribute channel make accelerators exact. A
graphics mode has neither, so the pointer is unavoidable — but it does not
have to be blind.

The enabling fact, measured rather than assumed: QEMU's screendump *does*
capture the cursor. Windows 3.11's display driver draws the pointer into
video memory as an ordinary opaque sprite, so it lands in the framebuffer
like any other pixels. That turns pointing into a control problem with
feedback instead of dead reckoning:

    capture -> move -> capture -> the cursor is what moved

From that first long move over a stable background we learn the sprite
(exact pixels, no anti-aliasing to blur it), cache it per VM, and can then
find the cursor in a *single* capture by template matching. Every gesture
reports where the pointer actually ended up, so callers get on_target and
the residual error instead of a promise.

Two guests were measured, and they disagree about almost everything except
the one fact the approach rests on — the cursor is in the framebuffer.

Windows 3.11 (MS-DOS 6.22 VM, 640x480):
- the arrow is 12x20, 126 opaque pixels: 53 black outline + 73 white fill;
- relative motion is piecewise: commands of 1 and 2 units move 1 and 2
  pixels, 3 moves 6, and everything above stays doubled. Modelling only
  the doubling strands every odd target one pixel short;
- the top-left clamp lands at (0, 1): row 0 is unreachable;
- there is flat background to learn over, and over the plain desktop grey
  the full 126 pixels show up.

Mac OS 9.2.2 (ppc, 800x600):
- the arrow is 11x16, 97 opaque pixels — the inverse colouring, black with
  a white border, and its image starts a pixel above and left of the
  hotspot, so every edge clips it rather than just the far two;
- motion is damped, not amplified: roughly 0.6 mid-range, rising past 1.1
  as the move gets longer, with the leftover fractions accumulated. Single
  units often move nothing at all, and no single scale factor fits;
- the desktop is a tiled watermark, so nothing on it is flat and the
  two-frame learner has no plain region to reason about — hence
  learn_sprite_sequence.

What generalises: learn where the cursor is drawn whole, never trust a
gain far from where it was measured, and close in by undershooting.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image as PILImage

#: Per-channel difference below which two pixels count as unchanged. The
#: framebuffer is exact (no anti-aliasing), so this only absorbs the odd
#: palette rounding.
DIFF_THRESHOLD = 8

#: Chebyshev radius used to link changed pixels into one cluster. Cursor
#: outlines are thin and diagonal, so neighbours are often not adjacent.
CLUSTER_BRIDGE = 2

#: A learning move must separate the two sprite images completely; anything
#: shorter risks them touching and merging into a single cluster.
MIN_LEARN_SEPARATION = 24


class PointerError(RuntimeError):
    """The cursor could not be located or learned."""


class Frame:
    """An RGB framebuffer: flat bytes plus dimensions."""

    __slots__ = ("width", "height", "data")

    def __init__(self, width: int, height: int, data: bytes) -> None:
        expected = width * height * 3
        if len(data) != expected:
            raise ValueError(f"expected {expected} bytes of RGB, got {len(data)}")
        self.width = width
        self.height = height
        self.data = data

    @classmethod
    def from_image_bytes(cls, blob: bytes) -> Frame:
        """Build from any format PIL reads — screendump gives us PPM."""
        image = PILImage.open(io.BytesIO(blob)).convert("RGB")
        return cls(image.width, image.height, image.tobytes())

    @classmethod
    def from_rows(cls, rows: list[list[tuple[int, int, int]]]) -> Frame:
        """Build from a grid of RGB tuples (how tests spell a real screen)."""
        height = len(rows)
        width = len(rows[0]) if height else 0
        data = bytearray()
        for row in rows:
            if len(row) != width:
                raise ValueError("rows must all be the same width")
            for pixel in row:
                data.extend(pixel)
        return cls(width, height, bytes(data))

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.data[i], self.data[i + 1], self.data[i + 2]

    def same_size(self, other: Frame) -> bool:
        return self.width == other.width and self.height == other.height


@dataclass(frozen=True)
class Sprite:
    """A learned cursor: opaque pixels relative to the sprite's top-left.

    `hotspot` is the point the guest actually clicks with. It cannot be read
    out of the framebuffer, so it is an assumption: (0, 0) is correct for
    the standard arrow, whose tip *is* its top-left corner. A cursor with a
    centred hotspot (crosshair, I-beam) needs a different value.
    """

    width: int
    height: int
    pixels: tuple[tuple[int, int, tuple[int, int, int]], ...]
    hotspot: tuple[int, int] = (0, 0)

    @property
    def opaque(self) -> int:
        return len(self.pixels)

    def anchor(self) -> tuple[int, int, bytes]:
        """Longest horizontal run of opaque pixels, as (dx, dy, raw bytes).

        Template matching scans for this run with bytes.find (C speed) and
        only then verifies the full mask, so the longer and rarer the run,
        the better. The Windows arrow has a 12-pixel full-width row.
        """
        by_row: dict[int, dict[int, tuple[int, int, int]]] = {}
        for dx, dy, color in self.pixels:
            by_row.setdefault(dy, {})[dx] = color
        best: tuple[int, int, list[tuple[int, int, int]]] = (0, 0, [])
        for dy, row in by_row.items():
            start = None
            run: list[tuple[int, int, int]] = []
            for dx in sorted(row):
                if start is not None and dx == start + len(run):
                    run.append(row[dx])
                else:
                    start, run = dx, [row[dx]]
                if len(run) > len(best[2]):
                    best = (start, dy, list(run))
        if not best[2]:
            raise PointerError("sprite has no opaque pixels")
        raw = bytearray()
        for color in best[2]:
            raw.extend(color)
        return best[0], best[1], bytes(raw)


def changed_points(
    before: Frame, after: Frame, threshold: int = DIFF_THRESHOLD
) -> list[tuple[int, int]]:
    """Pixels that differ between two frames.

    Rows are compared whole first: on a still screen all but the ~20 rows
    the cursor spans are byte-identical, and that comparison runs in C.
    """
    if not before.same_size(after):
        raise PointerError(
            f"frame size changed mid-gesture: {before.width}x{before.height} "
            f"-> {after.width}x{after.height} (guest switched video mode?)"
        )
    points: list[tuple[int, int]] = []
    stride = before.width * 3
    for y in range(before.height):
        lo = y * stride
        hi = lo + stride
        row_a = before.data[lo:hi]
        row_b = after.data[lo:hi]
        if row_a == row_b:
            continue
        for x in range(before.width):
            i = x * 3
            if (
                abs(row_a[i] - row_b[i]) > threshold
                or abs(row_a[i + 1] - row_b[i + 1]) > threshold
                or abs(row_a[i + 2] - row_b[i + 2]) > threshold
            ):
                points.append((x, y))
    return points


def cluster(
    points: list[tuple[int, int]], bridge: int = CLUSTER_BRIDGE
) -> list[list[tuple[int, int]]]:
    """Group points into connected blobs, linking within `bridge` pixels.

    Returned largest first.
    """
    remaining = set(points)
    blobs: list[list[tuple[int, int]]] = []
    offsets = [
        (dx, dy)
        for dx in range(-bridge, bridge + 1)
        for dy in range(-bridge, bridge + 1)
        if (dx, dy) != (0, 0)
    ]
    while remaining:
        seed = remaining.pop()
        blob = [seed]
        queue = [seed]
        while queue:
            cx, cy = queue.pop()
            for dx, dy in offsets:
                neighbour = (cx + dx, cy + dy)
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    blob.append(neighbour)
                    queue.append(neighbour)
        blobs.append(blob)
    blobs.sort(key=len, reverse=True)
    return blobs


def bounds(points: list[tuple[int, int]]) -> tuple[int, int, int, int]:
    """(x0, y0, width, height) covering the points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0 = min(xs), min(ys)
    return x0, y0, max(xs) - x0 + 1, max(ys) - y0 + 1


def learn_sprite(
    before: Frame, after: Frame, threshold: int = DIFF_THRESHOLD
) -> tuple[Sprite, tuple[int, int], tuple[int, int]]:
    """Learn the cursor from a long move over a still, uniform background.

    Returns (sprite, position_before, position_after), positions being the
    sprite's top-left corner. Raises PointerError when the difference does
    not look like one sprite that moved — a busy background, an animating
    guest, a move too short to separate the two images, or a cursor the
    screendump does not carry at all.
    """
    blobs = cluster(changed_points(before, after, threshold))
    if len(blobs) < 2:
        raise PointerError(
            "expected two cursor images in the difference, found "
            f"{len(blobs)}; the pointer may not have moved, may be clamped "
            "at a screen edge, or may not be drawn into the framebuffer"
        )
    if len(blobs) > 2:
        raise PointerError(
            f"the screen changed in {len(blobs)} places, so the cursor "
            "cannot be told apart; learn over a still, uniform background"
        )
    first, second = blobs
    box_a = bounds(first)
    box_b = bounds(second)
    if (box_a[2], box_a[3]) != (box_b[2], box_b[3]):
        raise PointerError(
            f"the two changed regions differ in size ({box_a[2]}x{box_a[3]} "
            f"vs {box_b[2]}x{box_b[3]}); the background was not uniform or "
            "the cursor changed shape mid-move"
        )
    if _overlaps(box_a, box_b):
        raise PointerError(
            "the two cursor images overlap; learn with a longer move "
            f"(at least {MIN_LEARN_SEPARATION} pixels)"
        )
    # Which blob is where the cursor ENDED? Blob size will not say: over a
    # mixed background the two images expose different pixel counts. But
    # the position the cursor vacated shows flat background in `after`,
    # while the one it moved to shows the sprite. That also checks the
    # uniform-background precondition the complete sprite depends on.
    a_vacated = _is_uniform(after, box_a)
    b_vacated = _is_uniform(after, box_b)
    if a_vacated == b_vacated:
        raise PointerError(
            "cannot tell which position the cursor moved to: expected one "
            "region to be flat background afterwards and the other to hold "
            "the sprite; learn over a still, uniform background"
        )
    start_box, end_box, end_points = (
        (box_a, box_b, second) if a_vacated else (box_b, box_a, first)
    )
    sprite = _sprite_from(after, end_points, end_box)
    return sprite, (start_box[0], start_box[1]), (end_box[0], end_box[1])


def learn_sprite_sequence(
    first: Frame,
    second: Frame,
    third: Frame,
    threshold: int = DIFF_THRESHOLD,
) -> tuple[Sprite, tuple[int, int]]:
    """Learn a cursor from three frames and two moves in the same direction.

    Assumes nothing about the background, which is what classic Mac OS
    needs: its desktop is a repeating watermark, so no part of it is flat
    and the two regions of a single step cannot be told apart by asking
    which one is plain afterwards.

    The trick is that the cursor's position in `second` is the one region
    that changed in *both* steps — it gained the sprite in the first and
    lost it in the second, while the other two regions each move only
    once. Returns (sprite, its top-left in `second`).

    The mask is whatever the sprite covers that differs from what was
    underneath, so it is partial by nature. That is enough to find it
    again: the sprite is opaque, so the pixels it does contribute look the
    same over any background.
    """
    steps = (
        cluster(changed_points(first, second, threshold)),
        cluster(changed_points(second, third, threshold)),
    )
    for name, blobs in zip(("first", "second"), steps):
        if len(blobs) != 2:
            raise PointerError(
                f"the {name} learning move changed {len(blobs)} regions "
                "instead of two; the screen has to hold still, and each "
                "move must be long enough to separate the two images"
            )
    boxes = [[bounds(blob) for blob in blobs] for blobs in steps]
    shared = [
        (i, j)
        for i, one in enumerate(boxes[0])
        for j, two in enumerate(boxes[1])
        if _overlaps(one, two)
    ]
    if len(shared) != 1:
        raise PointerError(
            f"the two moves share {len(shared)} regions instead of exactly "
            "one; both moves must go the same way, far enough that the "
            "cursor never revisits a position"
        )
    index = shared[0][0]
    box = boxes[0][index]
    sprite = _sprite_from(second, steps[0][index], box)
    return sprite, (box[0], box[1])


def _is_uniform(frame: Frame, box: tuple[int, int, int, int]) -> bool:
    """Is every pixel in this region the same colour?"""
    x0, y0, width, height = box
    first = frame.pixel(x0, y0)
    for y in range(y0, y0 + height):
        for x in range(x0, x0 + width):
            if frame.pixel(x, y) != first:
                return False
    return True


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return (
        a[0] < b[0] + b[2]
        and b[0] < a[0] + a[2]
        and a[1] < b[1] + b[3]
        and b[1] < a[1] + a[3]
    )


#: A cursor pinned against the right or bottom edge is drawn clipped, and
#: what little remains would be learned as the whole sprite — a 5-pixel
#: "cursor" measured at the bottom of a Mac OS 9 screen matched roughly
#: two hundred places at once. Refuse anything too small to be a cursor.
MIN_SPRITE_PIXELS = 20
MIN_SPRITE_SIDE = 5


def _sprite_from(
    frame: Frame, points: list[tuple[int, int]], box: tuple[int, int, int, int]
) -> Sprite:
    x0, y0, width, height = box
    pixels = tuple(
        sorted((x - x0, y - y0, frame.pixel(x, y)) for x, y in points)
    )
    if (
        len(pixels) < MIN_SPRITE_PIXELS
        or width < MIN_SPRITE_SIDE
        or height < MIN_SPRITE_SIDE
    ):
        raise PointerError(
            f"the cursor came out as {width}x{height} with {len(pixels)} "
            "pixels, too little to identify — it is most likely clipped "
            "against a screen edge; learn somewhere it is drawn whole"
        )
    return Sprite(width=width, height=height, pixels=pixels)


def verify_at(frame: Frame, sprite: Sprite, x: int, y: int) -> bool:
    """Do all of the sprite's opaque pixels match the frame at (x, y)?"""
    if x < 0 or y < 0 or x + sprite.width > frame.width:
        return False
    if y + sprite.height > frame.height:
        return False
    for dx, dy, color in sprite.pixels:
        if frame.pixel(x + dx, y + dy) != color:
            return False
    return True


def locate_all(frame: Frame, sprite: Sprite) -> list[tuple[int, int]]:
    """Every position whose pixels match the sprite, top-left coordinates."""
    anchor_dx, anchor_dy, pattern = sprite.anchor()
    run = len(pattern) // 3
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        found = frame.data.find(pattern, start)
        if found < 0:
            break
        start = found + 3
        if found % 3:
            continue  # not aligned to a pixel boundary
        index = found // 3
        px, py = index % frame.width, index // frame.width
        if px + run > frame.width:
            continue  # the run would wrap onto the next row
        x, y = px - anchor_dx, py - anchor_dy
        if verify_at(frame, sprite, x, y):
            matches.append((x, y))
    return matches


#: Fewest visible pixels that still identify a cursor hanging off an edge.
#: Below this it genuinely cannot be told from background — at the very
#: bottom-right corner exactly one pixel of the arrow remains on screen.
MIN_EDGE_PIXELS = 12


def visible_match(
    frame: Frame, sprite: Sprite, x: int, y: int
) -> int | None:
    """How many of the sprite's pixels are on screen and match, or None
    if any on-screen pixel disagrees.

    Unlike verify_at this tolerates the sprite hanging off the frame: the
    cursor is drawn clipped at the right and bottom edges (it extends down
    and right from its hotspot), so demanding the whole template there
    would lose the pointer exactly when it is pinned against a border.
    """
    matched = 0
    for dx, dy, colour in sprite.pixels:
        px, py = x + dx, y + dy
        if 0 <= px < frame.width and 0 <= py < frame.height:
            if frame.pixel(px, py) != colour:
                return None
            matched += 1
    return matched


def locate_near(
    frame: Frame,
    sprite: Sprite,
    expected: tuple[int, int],
    radius: int = 48,
    min_visible: int | None = None,
) -> tuple[int, int] | None:
    """Find a possibly-clipped cursor near where it was expected.

    Bounded on purpose: this is the fallback for the screen edges, and a
    whole-frame partial search would be both slow and prone to matching a
    handful of pixels anywhere.
    """
    if min_visible is None:
        min_visible = max(MIN_EDGE_PIXELS, sprite.opaque // 4)
    best: tuple[tuple[int, int], tuple[int, int]] | None = None
    for y in range(expected[1] - radius, expected[1] + radius + 1):
        for x in range(expected[0] - radius, expected[0] + radius + 1):
            matched = visible_match(frame, sprite, x, y)
            if matched is None or matched < min_visible:
                continue
            # Prefer the most complete match, then the closest one.
            score = (matched, -(abs(x - expected[0]) + abs(y - expected[1])))
            if best is None or score > best[0]:
                best = (score, (x, y))
    return best[1] if best else None


def locate(frame: Frame, sprite: Sprite) -> tuple[int, int] | None:
    """Where the cursor is, or None when it is not recognisable.

    None is the signal to relearn: the cursor changes shape (arrow,
    hourglass, I-beam) and a stale template simply stops matching.
    """
    matches = locate_all(frame, sprite)
    return matches[0] if len(matches) == 1 else None


def hotspot_of(position: tuple[int, int], sprite: Sprite) -> tuple[int, int]:
    return position[0] + sprite.hotspot[0], position[1] + sprite.hotspot[1]


#: Largest command the guest does NOT accelerate. Measured on Windows
#: 3.11: commands of 1 and 2 units move 1 and 2 pixels, while 3 moves 6
#: and everything above stays doubled. That threshold is the only reason
#: odd pixels are reachable at all — dividing a 1-pixel error by a gain of
#: 2 rounds to zero, so the endgame has to be walked 1:1.
ACCEL_THRESHOLD = 2


#: Above this gain the guest is amplifying our motion, and the only way to
#: land on a specific pixel is its unaccelerated slow regime. Guests that
#: scale motion *down* (classic Mac OS runs about 0.6 and accumulates the
#: fractions, so single units often move nothing at all) have no such
#: regime, and asking them for a 1-pixel step just stalls.
AMPLIFYING_GAIN = 1.5

#: Within this many pixels of the target the loop stops trusting the gain
#: and closes in by undershooting instead.
ENDGAME_PIXELS = 8


def _axis_command(error: int, gain: float, threshold: int) -> int:
    if error == 0:
        return 0
    scale = gain or 1.0
    if scale >= AMPLIFYING_GAIN and abs(error) <= threshold:
        return error  # below the threshold an amplifying guest moves 1:1
    if abs(error) <= ENDGAME_PIXELS:
        # Close in, deliberately undershoot. Dividing by a gain estimate
        # that swings with movement speed — classic Mac OS ranges from 0.5
        # to 1.5 depending on how far it is asked to go — overshoots the
        # last pixel and bounces between two positions until the iteration
        # budget runs out. Never commanding more than the error itself
        # cannot overshoot, so the approach is monotone: slower in steps,
        # but it actually arrives.
        step = max(1, int(abs(error) // max(scale, 1.0)))
        return step if error > 0 else -step
    command = round(error / scale)
    if command == 0:
        # Never round a real error down to "do nothing" — that is what
        # stranded every odd pixel when the guest doubled our motion.
        return 1 if error > 0 else -1
    if (
        scale >= AMPLIFYING_GAIN
        and abs(command) <= threshold
        and abs(error) > threshold
    ):
        # A command this small would not be accelerated, so it would move
        # `command` pixels instead of `error`. Step 1:1 by the threshold
        # and let the next iteration close the rest.
        return threshold if error > 0 else -threshold
    return command


def correction(
    target: tuple[int, int],
    current: tuple[int, int],
    gain: tuple[float, float],
    threshold: int = ACCEL_THRESHOLD,
) -> tuple[int, int]:
    """Relative units to command so the cursor lands on target.

    The guest's response is piecewise, not a single scale factor: small
    moves pass through untouched and larger ones are accelerated. Ignoring
    that costs the last pixel or two of every gesture.
    """
    return (
        _axis_command(target[0] - current[0], gain[0], threshold),
        _axis_command(target[1] - current[1], gain[1], threshold),
    )


#: The largest amplification these guests have shown (Windows 3.11 doubles
#: relative motion). Corrections are shortened so that even at this ratio
#: the cursor cannot be driven off screen — pinned against an edge it is
#: drawn clipped and stops being recognisable, which costs the loop its
#: feedback exactly when it is furthest from the target. Classic Mac OS
#: makes this essential: its gain rises with speed, so a factor fitted on
#: small moves badly underestimates a long one and overshoots the border.
WORST_CASE_GAIN = 2.0


def clamp_command(
    command: tuple[int, int],
    position: tuple[int, int],
    sprite: Sprite,
    width: int,
    height: int,
    gain: tuple[float, float],
) -> tuple[int, int]:
    """Shorten a command so the cursor stays on screen whatever the gain.

    Costs an extra iteration or two on a long move, which is cheap next to
    losing sight of the cursor altogether.
    """

    def axis(cmd: int, pos: int, extent: int, limit: int, scale: float) -> int:
        if cmd == 0:
            return 0
        # Every edge has to be respected, not just the far ones: classic
        # Mac OS draws its arrow a pixel above and left of the hotspot, so
        # the top and left borders cut it just as the others do.
        headroom = max((limit - extent - pos) if cmd > 0 else pos, 0)
        allowed = max(int(headroom / max(scale, WORST_CASE_GAIN)), 1)
        return max(-allowed, min(allowed, cmd))

    return (
        axis(command[0], position[0], sprite.width, width, gain[0]),
        axis(command[1], position[1], sprite.height, height, gain[1]),
    )


def refine_gain(
    gain: tuple[float, float],
    commanded: tuple[int, int],
    observed: tuple[int, int],
    minimum: int = 8,
) -> tuple[float, float]:
    """Update the gain estimate from what a move actually achieved.

    Only axes moved far enough to measure are updated: a one-unit command
    rounds to noise, and an axis clamped at a screen edge would otherwise
    teach the loop a gain of zero.
    """
    gain_x, gain_y = gain
    if abs(commanded[0]) >= minimum and observed[0]:
        gain_x = observed[0] / commanded[0]
    if abs(commanded[1]) >= minimum and observed[1]:
        gain_y = observed[1] / commanded[1]
    return gain_x, gain_y
