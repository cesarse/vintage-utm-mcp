"""Closed-loop pointer, exercised on real retro screens.

Every sprite, colour and hazard below was captured from a running guest,
not invented: Windows 3.11 in the MS-DOS 6.22 VM at 640x480, and Mac OS
9.2.2 on ppc at 800x600. The arrows and the I-beam are transcribed pixel
for pixel out of QMP screendumps, and the awkward cases are all things a
guest actually did while this was being built — a blinking caret adding a
third changed region, a cursor changing shape over a text field, a move
too short to separate the two images, a desktop with no flat region
anywhere, and a cursor pinned against an edge coming back as a sliver
that matched two hundred places at once.
"""

import pytest

from vintage_utm_mcp.pointer import (
    ACCEL_THRESHOLD,
    ENDGAME_PIXELS,
    Frame,
    PointerError,
    Sprite,
    bounds,
    changed_points,
    clamp_command,
    cluster,
    correction,
    learn_sprite,
    learn_sprite_sequence,
    locate,
    locate_all,
    locate_near,
    refine_gain,
    verify_at,
    visible_match,
)

# Colours read out of the framebuffer.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
DESKTOP = (192, 199, 200)  # Program Manager desktop grey
LABEL_GREY = (135, 136, 143)  # icon-label shading

# The Windows 3.11 arrow: 12x20, 53 black outline + 73 white fill.
# '#' black, 'O' white, '.' transparent (background shows through).
ARROW = [
    "##..........",
    "#O#.........",
    "#OO#........",
    "#OOO#.......",
    "#OOOO#......",
    "#OOOOO#.....",
    "#OOOOOO#....",
    "#OOOOOOO#...",
    "#OOOOOOOO#..",
    "#OOOOOOOOO#.",
    "#OOOOOO#####",
    "#OOO#OO#....",
    "#OO##OO#....",
    "#O#.##OO#...",
    "##...#OO#...",
    "#.....#OO#..",
    "......#OO#..",
    ".......#OO#.",
    ".......#OO#.",
    "........##..",
]

# The I-beam over the Run dialog's edit field. Only the 27 black pixels
# differ from the white field, so that is all a diff can see there.
IBEAM = [
    "###O###",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "OOO#OOO",
    "###O###",
]

# Tail of the "Accessories" icon label, guest (150,340)-(190,360): the
# busy background the cursor has to stay recognisable against.
LABEL = [
    "........................................",
    "................#....#..................",
    "................##...#........#.........",
    "................##...#........#.........",
    "................#.#..#...###..##.#..#..#",
    "................#.#..#..#...#.#..#..#..#",
    "................#..#.#..#####.#..#.#.#.#",
    "................#...##..#.....#..#.#.#.#",
    "................#...##..#...#.#...#...#.",
    "................#....#...###...#..#...#.",
    "........................................",
]

# The Mac OS 9.2.2 arrow, 11x16, 97 opaque pixels: 54 black and 43 white.
# The inverse of the Windows one — a black arrow with a white border — and
# its image starts a pixel above and left of the hotspot, so every screen
# edge clips it, not just the far ones.
MAC_ARROW = [
    "OO.........",
    "O#O........",
    "O##O.......",
    "O###O......",
    "O####O.....",
    "O#####O....",
    "O######O...",
    "O#######O..",
    "O########O.",
    "O#####OOOOO",
    "O##O##O....",
    "O#O.O##O...",
    "OO..O##O...",
    "O....O##O..",
    ".....O##O..",
    "......OOO..",
]

PALETTE = {"#": BLACK, "O": WHITE, "g": LABEL_GREY}


def canvas(width: int, height: int, colour: tuple[int, int, int]):
    return [[colour] * width for _ in range(height)]


def paint(rows, art: list[str], x: int, y: int) -> None:
    """Draw sprite art onto a canvas, '.' meaning transparent."""
    for dy, line in enumerate(art):
        for dx, glyph in enumerate(line):
            if glyph != ".":
                rows[y + dy][x + dx] = PALETTE[glyph]


def frame(rows) -> Frame:
    return Frame.from_rows(rows)


def arrow_on_desktop(x: int, y: int) -> Frame:
    rows = canvas(200, 120, DESKTOP)
    paint(rows, ARROW, x, y)
    return frame(rows)


@pytest.fixture
def arrow() -> Sprite:
    """The sprite as the server learns it: one long move over the desktop."""
    sprite, _, _ = learn_sprite(arrow_on_desktop(10, 10), arrow_on_desktop(90, 60))
    return sprite


def test_transcription_matches_the_capture() -> None:
    # Guards the fixture itself: the real arrow measured 12x20 with 53
    # black and 73 white pixels, 126 opaque in total.
    glyphs = "".join(ARROW)
    assert len(ARROW) == 20 and all(len(row) == 12 for row in ARROW)
    assert glyphs.count("#") == 53
    assert glyphs.count("O") == 73


def test_learns_the_arrow_from_one_move_over_the_desktop(arrow: Sprite) -> None:
    assert (arrow.width, arrow.height) == (12, 20)
    assert arrow.opaque == 126  # outline *and* fill, learnt over grey


def test_learn_reports_both_positions() -> None:
    _, before, after = learn_sprite(arrow_on_desktop(10, 10), arrow_on_desktop(90, 60))
    assert before == (10, 10)
    assert after == (90, 60)


def test_anchor_is_the_full_width_row(arrow: Sprite) -> None:
    # Row 10 of the arrow is opaque across all 12 columns; matching starts
    # from that run because bytes.find makes it nearly free.
    dx, dy, pattern = arrow.anchor()
    assert (dx, dy) == (0, 10)
    assert len(pattern) // 3 == 12


def test_locates_the_cursor_in_a_single_capture(arrow: Sprite) -> None:
    assert locate(arrow_on_desktop(42, 17), arrow) == (42, 17)


def test_locates_over_a_background_it_was_not_learned_on(arrow: Sprite) -> None:
    # Learnt over grey, found over the white Program Manager client area.
    # The sprite is opaque, so its pixels do not depend on what is under it.
    rows = canvas(200, 120, WHITE)
    paint(rows, ARROW, 30, 40)
    assert locate(frame(rows), arrow) == (30, 40)


def test_locates_over_the_icon_label(arrow: Sprite) -> None:
    # The cursor sat on the "Accessories" text: black outline over black
    # glyphs, the worst case for telling sprite from background.
    rows = canvas(60, 40, WHITE)
    for y, line in enumerate(LABEL):
        for x, glyph in enumerate(line[:40]):
            if glyph != ".":
                rows[y + 8][x + 10] = PALETTE[glyph]
    paint(rows, ARROW, 24, 6)
    assert locate(frame(rows), arrow) == (24, 6)


def test_locate_returns_none_when_the_cursor_changes_shape(arrow: Sprite) -> None:
    # Over the Run dialog's edit field Windows swaps the arrow for an
    # I-beam; the cached template stops matching, which is the signal to
    # relearn rather than to report a stale position.
    rows = canvas(200, 120, WHITE)
    paint(rows, IBEAM, 50, 30)
    assert locate_all(frame(rows), arrow) == []
    assert locate(frame(rows), arrow) is None


def test_learn_rejects_a_pointer_that_did_not_move() -> None:
    still = arrow_on_desktop(10, 10)
    with pytest.raises(PointerError, match="may not have moved"):
        learn_sprite(still, still)


def test_learn_rejects_a_move_too_short_to_separate_the_images() -> None:
    # 6 pixels of travel leaves the two 12x20 images overlapping, so the
    # difference is one blob with a bite out of it, not two cursors.
    with pytest.raises(PointerError):
        learn_sprite(arrow_on_desktop(10, 10), arrow_on_desktop(16, 14))


def test_learn_rejects_a_screen_that_changed_elsewhere() -> None:
    # Real case: the Run dialog's text caret blinks. That is a third
    # changed region and there is no way to tell which one is the cursor.
    before = canvas(200, 120, DESKTOP)
    paint(before, ARROW, 10, 10)
    after = canvas(200, 120, DESKTOP)
    paint(after, ARROW, 90, 60)
    for y in range(14):  # the 1x14 caret, exactly as measured
        after[y + 20][150] = BLACK
    with pytest.raises(PointerError, match="changed in 3 places"):
        learn_sprite(frame(before), frame(after))


def test_learn_rejects_a_busy_background() -> None:
    # Measured on the "Accessories" label: the two changed regions came
    # back 63 and 54 pixels, so the sprite would be learnt incomplete —
    # yet both still bounded 12x20, so size alone does not catch it. What
    # does: neither position is flat background afterwards.
    def scene(x: int) -> Frame:
        rows = canvas(80, 40, WHITE)
        for y, line in enumerate(LABEL):
            for lx, glyph in enumerate(line[:40]):
                if glyph != ".":
                    rows[y + 10][lx + 20] = PALETTE[glyph]
        paint(rows, ARROW, x, 5)
        return frame(rows)

    # Both positions land on the glyphs (which start at column 36), as the
    # real cursor did when it crossed the label.
    with pytest.raises(PointerError, match="cannot tell which position"):
        learn_sprite(scene(36), scene(52))


def test_learn_rejects_mismatched_region_sizes() -> None:
    # Guards the invariant directly: one sprite that moved must leave two
    # equally sized regions behind.
    before = canvas(200, 120, DESKTOP)
    paint(before, ARROW, 10, 10)
    after = canvas(200, 120, DESKTOP)
    paint(after, IBEAM, 90, 60)  # a different cursor, so a different box
    with pytest.raises(PointerError, match="differ in size"):
        learn_sprite(frame(before), frame(after))


def test_changed_points_rejects_a_video_mode_change() -> None:
    with pytest.raises(PointerError, match="video mode"):
        changed_points(
            frame(canvas(200, 120, DESKTOP)), frame(canvas(320, 200, DESKTOP))
        )


def test_cluster_bridges_a_diagonal_outline() -> None:
    # The arrow's edge is a 1px diagonal: 4-connected labelling would tear
    # it into twenty separate blobs.
    diagonal = [(x, x) for x in range(20)]
    assert len(cluster(diagonal)) == 1


def test_cluster_keeps_distant_blobs_apart() -> None:
    assert len(cluster([(0, 0), (1, 1), (50, 50), (51, 50)])) == 2


def test_bounds_covers_the_points() -> None:
    assert bounds([(5, 7), (9, 7), (5, 20)]) == (5, 7, 5, 14)


def test_verify_rejects_a_sprite_running_off_the_edge(arrow: Sprite) -> None:
    screen = arrow_on_desktop(10, 10)
    assert verify_at(screen, arrow, 10, 10)
    assert not verify_at(screen, arrow, 195, 10)  # would need 12 columns
    assert not verify_at(screen, arrow, 10, 115)
    assert not verify_at(screen, arrow, -1, 10)


def test_correction_divides_by_the_measured_gain() -> None:
    # Windows 3.11 doubles what we send, so 100 pixels of error is a
    # 50-unit command.
    assert correction((150, 100), (50, 40), (2.0, 2.0)) == (50, 30)
    assert correction((150, 100), (50, 40), (1.0, 1.0)) == (100, 60)


def test_correction_reaches_odd_pixels_through_the_threshold() -> None:
    # Measured on Windows 3.11: commands of 1 and 2 units move 1 and 2
    # pixels, 3 moves 6. So a 1-pixel error is commanded as 1, not as
    # round(1/2) = 0 — which silently stranded every odd target one pixel
    # short before the threshold was modelled.
    assert correction((61, 415), (60, 416), (2.0, 2.0)) == (1, -1)
    assert correction((10, 10), (8, 8), (2.0, 2.0)) == (2, 2)


def test_correction_avoids_commands_the_guest_would_not_accelerate() -> None:
    # A 5-pixel error divided by the 2x gain is 2 units — but 2 units is
    # below the threshold, so the guest would move 2 pixels, not 5. Step
    # 1:1 by the threshold instead and close the rest next time round.
    assert correction((5, 0), (0, 0), (2.0, 2.0)) == (ACCEL_THRESHOLD, 0)
    # Far enough out, halving is right and lands in one move.
    assert correction((100, 0), (0, 0), (2.0, 2.0)) == (50, 0)


def test_correction_survives_a_zero_gain() -> None:
    assert correction((10, 10), (0, 0), (0.0, 0.0)) == (10, 10)


def test_locate_near_finds_a_cursor_clipped_by_the_screen_edge(
    arrow: Sprite,
) -> None:
    # The cursor extends down and right from its hotspot, so the right and
    # bottom edges cut it off and the whole-template search loses it.
    rows = canvas(200, 120, DESKTOP)
    paint(rows, [line[:6] for line in ARROW], 194, 40)  # 6 of 12 columns left
    screen = frame(rows)
    assert locate(screen, arrow) is None
    assert locate_near(screen, arrow, (194, 40)) == (194, 40)


def test_locate_near_refuses_a_cursor_clipped_past_recognition(
    arrow: Sprite,
) -> None:
    # At the very bottom-right corner exactly one pixel of the arrow stays
    # on screen. Guessing from that would be worse than admitting it.
    rows = canvas(200, 120, DESKTOP)
    paint(rows, [line[:1] for line in ARROW], 199, 40)
    assert locate_near(frame(rows), arrow, (199, 40)) is None


def test_locate_near_stays_within_its_radius(arrow: Sprite) -> None:
    screen = arrow_on_desktop(20, 20)
    assert locate_near(screen, arrow, (20, 20), radius=4) == (20, 20)
    assert locate_near(screen, arrow, (150, 100), radius=4) is None


def test_visible_match_rejects_a_disagreeing_pixel(arrow: Sprite) -> None:
    rows = canvas(200, 120, DESKTOP)
    paint(rows, ARROW, 30, 30)
    rows[30][30] = LABEL_GREY  # corrupt the arrow's tip
    assert visible_match(frame(rows), arrow, 30, 30) is None


def test_visible_match_counts_only_on_screen_pixels(arrow: Sprite) -> None:
    rows = canvas(200, 120, DESKTOP)
    paint(rows, [line[:6] for line in ARROW], 194, 40)
    # 77 of the arrow's 126 pixels live in its first six columns.
    assert visible_match(frame(rows), arrow, 194, 40) == 77


def test_mac_arrow_transcription() -> None:
    glyphs = "".join(MAC_ARROW)
    assert len(MAC_ARROW) == 16 and all(len(row) == 11 for row in MAC_ARROW)
    assert glyphs.count("#") == 54
    assert glyphs.count("O") == 43


def _textured(width: int, height: int):
    """A background with no flat region anywhere, like the Mac OS 9 desktop.

    Uses the real icon-label glyphs, which is the property that matters:
    nothing on classic Mac OS's watermarked desktop is uniform, so the
    two-frame learner cannot ask which region went plain afterwards.
    """
    rows = canvas(width, height, WHITE)
    for band in range(0, height - 10, 11):
        for y, line in enumerate(LABEL):
            for x, glyph in enumerate(line):
                if glyph != "." and x < width:
                    rows[band + y][x] = PALETTE[glyph]
    return rows


def test_learns_over_a_background_with_no_flat_region() -> None:
    # Three frames, two moves the same way: the cursor's position in the
    # middle frame is the only region that changed in both steps.
    def scene(x: int) -> Frame:
        rows = _textured(200, 120)
        paint(rows, ARROW, x, 40)
        return frame(rows)

    sprite, position = learn_sprite_sequence(scene(20), scene(60), scene(100))
    assert position == (60, 40)
    # Partial by nature: only the pixels differing from what they covered.
    assert 0 < sprite.opaque <= 126
    assert locate(scene(60), sprite) == (60, 40)


def test_sequence_learner_locates_over_other_backgrounds() -> None:
    # A partial mask still matches anywhere, because the sprite is opaque.
    def scene(x: int) -> Frame:
        rows = _textured(200, 120)
        paint(rows, ARROW, x, 40)
        return frame(rows)

    sprite, _ = learn_sprite_sequence(scene(20), scene(60), scene(100))
    plain = canvas(200, 120, DESKTOP)
    paint(plain, ARROW, 77, 22)
    assert locate(frame(plain), sprite) == (77, 22)


def test_sequence_learner_rejects_a_screen_that_moved_elsewhere() -> None:
    def scene(x: int, caret: bool) -> Frame:
        rows = canvas(200, 120, DESKTOP)
        paint(rows, ARROW, x, 40)
        if caret:
            for y in range(14):
                rows[y + 80][150] = BLACK
        return frame(rows)

    with pytest.raises(PointerError, match="changed 3 regions"):
        learn_sprite_sequence(scene(20, False), scene(60, True), scene(100, False))


def test_sequence_learner_rejects_a_cursor_that_doubles_back() -> None:
    # Both moves must go the same way; returning to the start leaves two
    # shared regions and no way to say which frame held the cursor where.
    def scene(x: int) -> Frame:
        rows = canvas(200, 120, DESKTOP)
        paint(rows, ARROW, x, 40)
        return frame(rows)

    with pytest.raises(PointerError, match="share 2 regions"):
        learn_sprite_sequence(scene(20), scene(60), scene(20))


def test_rejects_a_sprite_too_small_to_identify() -> None:
    # Measured against the bottom of a Mac OS 9 screen: a cursor pinned
    # there is drawn as a 3x2 sliver, and that "sprite" matched about two
    # hundred places at once.
    def scene(x: int) -> Frame:
        rows = canvas(200, 120, DESKTOP)
        paint(rows, ["OO.", "O#O"], x, 40)
        return frame(rows)

    with pytest.raises(PointerError, match="too little to identify"):
        learn_sprite_sequence(scene(20), scene(60), scene(100))


def test_clamp_leaves_a_move_in_open_space_alone(arrow: Sprite) -> None:
    assert clamp_command((10, 10), (60, 40), arrow, 640, 480, (2.0, 2.0)) == (10, 10)


def test_clamp_shortens_a_move_that_would_leave_the_screen(
    arrow: Sprite,
) -> None:
    # 40 px of headroom at the worst amplification seen (2x) is 20 units.
    command = clamp_command((500, 0), (588, 40), arrow, 640, 480, (0.6, 0.6))
    assert command == (20, 0)


def test_clamp_uses_the_worst_case_not_the_measured_gain(arrow: Sprite) -> None:
    # A gain fitted on small moves badly underestimates a long one on
    # classic Mac OS, so the guard must not trust it.
    optimistic = clamp_command((500, 0), (588, 40), arrow, 640, 480, (0.5, 0.5))
    pessimistic = clamp_command((500, 0), (588, 40), arrow, 640, 480, (2.0, 2.0))
    assert optimistic == pessimistic


def test_clamp_always_allows_a_nudge(arrow: Sprite) -> None:
    # Hard against the edge, still let the loop try: a guest that damps
    # motion pays out accumulated fractions on a later command.
    assert clamp_command((5, 5), (628, 460), arrow, 640, 480, (2.0, 2.0)) == (1, 1)


def test_clamp_respects_the_near_edges_too(arrow: Sprite) -> None:
    # The Mac arrow starts above and left of its hotspot, so overshooting
    # into the top-left clips it exactly like the far edges do.
    assert clamp_command((-500, -500), (40, 40), arrow, 640, 480, (2.0, 2.0)) == (
        -20,
        -20,
    )


def test_endgame_never_overshoots() -> None:
    # Whatever the guest does with our units, closing in must not sail past
    # the target — overshoot turns into a bounce between two pixels that
    # burns the whole iteration budget.
    for gain in (0.5, 0.6, 1.0, 1.4, 2.0):
        for error in range(1, ENDGAME_PIXELS + 1):
            command, _ = correction((error, 0), (0, 0), (gain, gain))
            assert command >= 1, (gain, error)
            assert command * min(gain, 1.0) <= error, (gain, error, command)


def test_refine_gain_learns_the_doubling() -> None:
    assert refine_gain((1.0, 1.0), (40, 10), (80, 20)) == (2.0, 2.0)


def test_refine_gain_ignores_moves_too_small_to_measure() -> None:
    assert refine_gain((2.0, 2.0), (1, 1), (2, 2)) == (2.0, 2.0)


def test_refine_gain_ignores_an_axis_clamped_at_the_edge() -> None:
    # The pointer was already against the top edge: it went nowhere
    # vertically, which does not mean the gain is zero.
    assert refine_gain((2.0, 2.0), (40, -40), (80, 0)) == (2.0, 2.0)
