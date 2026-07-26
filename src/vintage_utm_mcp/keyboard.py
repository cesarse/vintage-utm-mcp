"""Keyboard mapping for QMP send-key.

QMP's send-key takes QEMU key codes ("qcodes"), which name physical keys
on a US layout. Typing text therefore means translating each character
into a chord of qcodes (adding shift where needed).

Layout caveat: the translation below assumes the GUEST interprets
scancodes as US layout — DOS's default. If the guest loaded a national
driver (e.g. KEYB BR for ABNT2), punctuation will come out wrong;
letters and digits are unaffected. An abnt2 table is planned.
"""

from __future__ import annotations

import re

# Characters whose qcode is the character itself (letters handled apart).
_DIGITS = {c: c for c in "0123456789"}

_PUNCT = {
    "-": "minus",
    "=": "equal",
    "[": "bracket_left",
    "]": "bracket_right",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    "`": "grave_accent",
    ",": "comma",
    ".": "dot",
    "/": "slash",
    " ": "spc",
    "\n": "ret",
    "\t": "tab",
}

# Shifted characters -> the unshifted key they live on (US layout).
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "minus", "+": "equal",
    "{": "bracket_left", "}": "bracket_right", "|": "backslash",
    ":": "semicolon", '"': "apostrophe", "~": "grave_accent",
    "<": "comma", ">": "dot", "?": "slash",
}

# Named keys accepted in send_keys combos (a practical subset of QEMU's
# qcode enum, retro-relevant).
VALID_QCODES = frozenset(
    list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
    + [f"f{n}" for n in range(1, 13)]
    + [
        "esc", "ret", "tab", "backspace", "spc",
        "minus", "equal", "bracket_left", "bracket_right", "backslash",
        "semicolon", "apostrophe", "grave_accent", "comma", "dot", "slash",
        "shift", "shift_r", "ctrl", "ctrl_r", "alt", "alt_r",
        "caps_lock", "num_lock", "scroll_lock", "print", "pause",
        "up", "down", "left", "right",
        "home", "end", "pgup", "pgdn", "insert", "delete",
        "kp_0", "kp_1", "kp_2", "kp_3", "kp_4", "kp_5", "kp_6", "kp_7",
        "kp_8", "kp_9", "kp_enter", "kp_add", "kp_subtract",
        "kp_multiply", "kp_divide", "kp_decimal",
    ]
)

KEY_ALIASES = {
    "enter": "ret",
    "return": "ret",
    "escape": "esc",
    "space": "spc",
    "spacebar": "spc",
    "del": "delete",
    "ins": "insert",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "capslock": "caps_lock",
    "numlock": "num_lock",
    "scrolllock": "scroll_lock",
    "printscreen": "print",
    "break": "pause",
    "backtick": "grave_accent",
    "dash": "minus",
    "plus": "equal",  # shift is up to the caller; the key itself is '='
    "control": "ctrl",
    "lshift": "shift",
    "rshift": "shift_r",
    "lctrl": "ctrl",
    "rctrl": "ctrl_r",
    "lalt": "alt",
    "ralt": "alt_r",
    "altgr": "alt_r",
}


class KeyboardError(ValueError):
    """A character or key name cannot be translated."""


def text_to_chords(text: str, layout: str = "us") -> list[list[str]]:
    """Translate text into a list of qcode chords, one per character.

    Only the "us" layout is implemented. Raises KeyboardError on the
    first untranslatable character, naming it.
    """
    if layout != "us":
        raise KeyboardError(
            f"layout {layout!r} is not implemented yet; only 'us' is. "
            "DOS defaults to US — avoid loading KEYB when automating."
        )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chords: list[list[str]] = []
    for char in text:
        lower = char.lower()
        if char.isascii() and char.isalpha():
            chords.append(["shift", lower] if char.isupper() else [lower])
        elif char in _DIGITS:
            chords.append([char])
        elif char in _PUNCT:
            chords.append([_PUNCT[char]])
        elif char in _SHIFTED:
            chords.append(["shift", _SHIFTED[char]])
        else:
            raise KeyboardError(
                f"cannot type character {char!r} on the us layout"
            )
    return chords


def parse_combo(combo: str) -> list[str]:
    """Parse a combo like "ctrl-alt-delete", "F8" or "shift+tab".

    Keys may be separated by '-', '+' or spaces. To press the minus or
    plus keys themselves, use the aliases "minus"/"dash" and "equal".
    """
    tokens = [t for t in re.split(r"[+\-\s]+", combo.strip().lower()) if t]
    if not tokens:
        raise KeyboardError(f"empty key combo: {combo!r}")
    keys = []
    for token in tokens:
        key = KEY_ALIASES.get(token, token)
        if key not in VALID_QCODES:
            raise KeyboardError(
                f"unknown key {token!r} in combo {combo!r}"
            )
        keys.append(key)
    return keys
