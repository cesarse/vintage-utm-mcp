import pytest

from vintage_utm_mcp.keyboard import (
    KeyboardError,
    parse_combo,
    text_to_chords,
)


def test_lowercase_and_uppercase_letters() -> None:
    assert text_to_chords("dir") == [["d"], ["i"], ["r"]]
    assert text_to_chords("Dir") == [["shift", "d"], ["i"], ["r"]]


def test_digits_and_shifted_symbols() -> None:
    assert text_to_chords("42") == [["4"], ["2"]]
    assert text_to_chords("!") == [["shift", "1"]]
    assert text_to_chords(":") == [["shift", "semicolon"]]
    assert text_to_chords("?") == [["shift", "slash"]]


def test_dos_command_line() -> None:
    chords = text_to_chords("DIR A:\n")
    assert chords == [
        ["shift", "d"],
        ["shift", "i"],
        ["shift", "r"],
        ["spc"],
        ["shift", "a"],
        ["shift", "semicolon"],
        ["ret"],
    ]


def test_newline_normalization() -> None:
    assert text_to_chords("a\r\nb\r") == [["a"], ["ret"], ["b"], ["ret"]]


def test_untypeable_character_is_named() -> None:
    with pytest.raises(KeyboardError, match="'ç'"):
        text_to_chords("ção")


def test_layout_other_than_us_rejected() -> None:
    with pytest.raises(KeyboardError, match="abnt2"):
        text_to_chords("dir", layout="abnt2")


def test_parse_combo_separators_and_aliases() -> None:
    assert parse_combo("ctrl-alt-delete") == ["ctrl", "alt", "delete"]
    assert parse_combo("shift+tab") == ["shift", "tab"]
    assert parse_combo("F8") == ["f8"]
    assert parse_combo("enter") == ["ret"]
    assert parse_combo("ctrl minus") == ["ctrl", "minus"]


def test_parse_combo_unknown_key() -> None:
    with pytest.raises(KeyboardError, match="'hyperspace'"):
        parse_combo("ctrl-hyperspace")
