"""Accelerator and key-hint detection, exercised on real MS-DOS EDIT screens."""

from vintage_utm_mcp.screen import find_accelerators, parse_key_hints

COLUMNS = 40

# Menu bar as EDIT draws it: every label has one highlighted letter.
MENU_BAR = "   File  Edit  Search  Options     Help"
#            0123456789...


def _attrs(line: str, highlights: list[int], normal: int = 0x70) -> list[int]:
    attrs = [normal] * COLUMNS
    for column in highlights:
        attrs[column] = 0x7F
    return attrs


def test_finds_menu_bar_accelerators() -> None:
    # Highlighted: F of File, E of Edit, S of Search, O of Options, H of Help
    highlights = [3, 9, 15, 23, 35]
    found = find_accelerators(MENU_BAR, _attrs(MENU_BAR, highlights), COLUMNS)
    assert [(a["label"], a["key"]) for a in found] == [
        ("File", "F"),
        ("Edit", "E"),
        ("Search", "S"),
        ("Options", "O"),
        ("Help", "H"),
    ]


def test_accelerator_may_be_any_letter_of_the_word() -> None:
    line = " Exit".ljust(COLUMNS)
    found = find_accelerators(line, _attrs(line, [2]), COLUMNS)
    assert found == [{"label": "Exit", "key": "x", "row": 0, "column": 2}]


def test_uniform_rows_yield_nothing() -> None:
    # A grid of identical characters has no highlighting and no shortcuts.
    line = "x" * 16
    assert find_accelerators(line, [0x07] * COLUMNS, COLUMNS) == []


def test_highlighted_run_is_not_an_accelerator() -> None:
    # A selection bar highlights a whole run; only single cells count.
    line = " Save As...".ljust(COLUMNS)
    attrs = [0x70] * COLUMNS
    for column in range(1, 11):
        attrs[column] = 0x0F
    assert find_accelerators(line, attrs, COLUMNS) == []


def test_multiple_rows_are_scanned_in_reading_order() -> None:
    text = MENU_BAR + "\n" + " New".ljust(COLUMNS)
    attrs = _attrs(MENU_BAR, [3]) + _attrs(" New", [1])
    found = find_accelerators(text, attrs, COLUMNS)
    assert [(a["label"], a["row"]) for a in found] == [("File", 0), ("New", 1)]


# --- status-bar hints -----------------------------------------------------


def test_parses_editor_status_bar() -> None:
    hints = parse_key_hints(
        " F1=Help   Enter=Execute   Esc=Cancel   Tab=Next Field"
    )
    assert hints == [
        {"key": "F1", "action": "Help"},
        {"key": "Enter", "action": "Execute"},
        {"key": "Esc", "action": "Cancel"},
        {"key": "Tab", "action": "Next Field"},
    ]


def test_parses_modifier_combinations() -> None:
    hints = parse_key_hints("Ctrl+Q=Quit    Alt-F4=Close")
    assert {h["key"] for h in hints} == {"Ctrl+Q", "Alt-F4"}


def test_ignores_prose_without_hints() -> None:
    assert parse_key_hints("MS-DOS Editor  Press ALT to activate menus") == []


def test_deduplicates_repeated_hints() -> None:
    hints = parse_key_hints("F1=Help  something  F1=Help")
    assert len(hints) == 1
