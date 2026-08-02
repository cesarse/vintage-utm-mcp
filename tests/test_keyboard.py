from vintage_utm_mcp import keyboard
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


def test_mac_modifier_aliases() -> None:
    assert parse_combo("cmd-q") == ["meta_l", "q"]
    assert parse_combo("option-esc") == ["alt", "esc"]


# --- layout detection, from real probes on MS-DOS 6.22 ----------------

# What the probe keys produced with no national driver loaded.
US_UNSHIFTED = list("qwyzam;'[]\\/,.-=`6")
US_SHIFTED = list('QWYZAM:"{}|?<>_+~^')

# The same probe after `KEYB BR`. Two keys became dead accents and emitted
# nothing on their own; the pending accent then flushed into the next key,
# which is why bracket_left came back as two characters.
BR_UNSHIFTED = ["q", "w", "y", "z", "a", "m", ";", "", "'[", "]",
                "\\", "/", ",", ".", "-", "=", ""]
BR_SHIFTED = ["Q", "W", "Y", "Z", "A", "M", ":", "", '"{', "}",
              "|", "?", "<", ">", "_", "+", ""]


def test_probe_covers_every_signature_position() -> None:
    plain, shift = keyboard.LAYOUT_SIGNATURES["us"]
    assert len(plain) == len(shift) == len(keyboard.PROBE_KEYS)


def test_identifies_an_untouched_dos_guest_as_us() -> None:
    ranked = keyboard.identify_layout(US_UNSHIFTED, US_SHIFTED)
    assert ranked[0] == ("us", 0)


def test_a_national_driver_stops_matching_us() -> None:
    # KEYB BR is not US, and saying so is the whole point: type_text
    # implements US only, so punctuation would come out wrong.
    ranked = keyboard.identify_layout(BR_UNSHIFTED, BR_SHIFTED)
    assert ranked[0][0] == "us"
    assert ranked[0][1] > 0


def test_a_key_that_was_not_probed_is_not_called_dead() -> None:
    # The BR capture predates the caret key joining the probe, so it is a
    # position short. Missing data is not the same as a key that produced
    # nothing, and reporting it as dead would invent a finding.
    assert "6" not in keyboard.dead_keys(BR_UNSHIFTED, BR_SHIFTED)


def test_dead_keys_are_named() -> None:
    # ABNT2's acute and grave accents wait for the next keystroke, so they
    # print nothing on their own and type_text would silently lose them.
    assert keyboard.dead_keys(BR_UNSHIFTED, BR_SHIFTED) == [
        "apostrophe",
        "grave_accent",
    ]
    assert keyboard.dead_keys(US_UNSHIFTED, US_SHIFTED) == []


def test_a_dead_key_costs_one_mismatch_not_a_cascade() -> None:
    # Per-key comparison, not a joined string: a key that emits nothing
    # would otherwise shift every later character and look like a dozen
    # differences instead of one.
    observed = list(US_UNSHIFTED)
    observed[6] = ""  # semicolon goes dead
    assert keyboard.identify_layout(observed, US_SHIFTED)[0] == ("us", 1)


# --- US-International (dead keys) -------------------------------------
#
# Verified on MS-DOS 6.22 with KEYB BR loaded, whose acute and grave
# behave exactly as US-International's do. Windows 3.11 on the same guest
# runs US-International for real: SYSTEM.INI says keyboard.dll=kbdusx.dll.


def test_us_layout_still_emits_one_chord_per_character() -> None:
    assert keyboard.text_to_chords("ab1", "us") == [["a"], ["b"], ["1"]]


def test_literal_accent_needs_a_following_space() -> None:
    # Alone the accent emits nothing and combines with whatever comes
    # next; a space makes the guest commit it on its own.
    assert keyboard.text_to_chords("'", "us-intl") == [["apostrophe"], ["spc"]]
    assert keyboard.text_to_chords('"', "us-intl") == [
        ["shift", "apostrophe"],
        ["spc"],
    ]
    assert keyboard.text_to_chords("^", "us-intl") == [["shift", "6"], ["spc"]]


def test_accented_characters_compose() -> None:
    # Characters the plain US layout cannot type at all.
    assert keyboard.text_to_chords("ç", "us-intl") == [["apostrophe"], ["c"]]
    assert keyboard.text_to_chords("á", "us-intl") == [["apostrophe"], ["a"]]
    assert keyboard.text_to_chords("ñ", "us-intl") == [
        ["shift", "grave_accent"],
        ["n"],
    ]


def test_the_case_that_actually_corrupts() -> None:
    # Measured: on a dead-key guest the plain US layout turns "d'agua"
    # into "dágua", because the apostrophe combines with the a that
    # follows. "O'Neil" survives, since acute has no form for N — which
    # is exactly why the bug hides.
    assert keyboard.text_to_chords("d'a", "us") == [
        ["d"],
        ["apostrophe"],  # bare: the a after it gets absorbed into á
        ["a"],
    ]
    assert keyboard.text_to_chords("d'a", "us-intl") == [
        ["d"],
        ["apostrophe"],
        ["spc"],  # commits the accent, so the a stays an a
        ["a"],
    ]


def test_unknown_layout_names_the_ones_that_work() -> None:
    with pytest.raises(keyboard.KeyboardError, match="us-intl"):
        keyboard.text_to_chords("a", "azerty")


# --- Mac OS Option accents --------------------------------------------
#
# Measured on Mac OS 9.2.2 by typing into Stickies and reading the screen.


def test_mac_composes_accents_from_option_chords() -> None:
    assert keyboard.text_to_chords("á", "mac-option") == [["alt", "e"], ["a"]]
    assert keyboard.text_to_chords("à", "mac-option") == [
        ["alt", "grave_accent"],
        ["a"],
    ]
    assert keyboard.text_to_chords("â", "mac-option") == [["alt", "i"], ["a"]]
    assert keyboard.text_to_chords("õ", "mac-option") == [["alt", "n"], ["o"]]
    assert keyboard.text_to_chords("ü", "mac-option") == [["alt", "u"], ["u"]]


def test_mac_types_cedilla_directly_not_by_composing() -> None:
    # Measured: option-e then c gives "´c", not "ç". Mac puts the cedilla
    # on its own chord, so it has to be checked before the compose table.
    assert keyboard.text_to_chords("ç", "mac-option") == [["alt", "c"]]
    assert keyboard.text_to_chords("Ç", "mac-option") == [["alt", "shift", "c"]]


def test_mac_leaves_the_apostrophe_alone() -> None:
    # The point of the Mac route: accents without dead keys, so ASCII
    # punctuation still types as itself.
    assert keyboard.text_to_chords("d'a", "mac-option") == [
        ["d"],
        ["apostrophe"],
        ["a"],
    ]
