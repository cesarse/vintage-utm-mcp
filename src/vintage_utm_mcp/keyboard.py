"""Keyboard mapping for QMP send-key.

QMP's send-key takes QEMU key codes ("qcodes"), which name physical keys
on a US layout. Typing text therefore means translating each character
into a chord of qcodes (adding shift where needed).

Layout caveat: the translation below assumes the GUEST interprets
scancodes as US layout — DOS's default. If the guest loaded a national
driver (e.g. KEYB BR for ABNT2), punctuation comes out wrong while
letters and digits stay right, which is what makes the problem so easy
to miss. `detect_layout` presses the keys that move between layouts and
reads back what the guest produced, so the mismatch shows up before it
corrupts anything; typing on a non-US mapping is still unimplemented.
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
    + ["meta_l", "meta_r"]
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
    # Mac guests (e.g. Mac OS 9): QEMU maps these qcodes to ADB modifiers.
    "cmd": "meta_l",
    "command": "meta_l",
    "meta": "meta_l",
    "option": "alt",
}


class KeyboardError(ValueError):
    """A character or key name cannot be translated."""


# --- dead-key layouts ---------------------------------------------------
#
# US-International keeps every US key where it is and turns five of them
# into dead accents: they emit nothing on their own and combine with the
# next keystroke instead. Two consequences, both verified on the guest
# (with DOS's KEYB BR, whose acute and grave behave identically):
#   - a literal accent needs a following space: ' then space gives '
#   - a letter after one composes: ' then c gives ç
# So typing plain ASCII on this layout is not a no-op — every apostrophe
# and quote silently eats the character after it.

#: Dead characters -> the key that carries them.
_DEAD = {
    "'": ("apostrophe", False),
    '"': ("apostrophe", True),
    "`": ("grave_accent", False),
    "~": ("grave_accent", True),
    "^": ("6", True),
}

def _build_compose() -> dict[str, tuple[str, str]]:
    """Accented character -> (dead character, base letter).

    Built from unicodedata so the pairs come from the character database
    rather than from a hand-typed table that could quietly disagree with
    it.
    """
    import unicodedata

    marks = {
        "'": "ACUTE",
        "`": "GRAVE",
        "~": "TILDE",
        "^": "CIRCUMFLEX",
        '"': "DIAERESIS",
    }
    table: dict[str, tuple[str, str]] = {}
    for dead, mark in marks.items():
        for base in "aeiouyn c":
            if base == " ":
                continue
            for letter in (base, base.upper()):
                name = unicodedata.name(letter, "")
                if not name:
                    continue
                target = f"{name} WITH {mark}"
                if dead == "'" and letter.lower() == "c":
                    target = f"{name} WITH CEDILLA"  # ' + c is ç, not ć
                try:
                    table[unicodedata.lookup(target)] = (dead, letter)
                except KeyError:
                    continue
    return table


COMPOSE = _build_compose()

# --- Mac OS Option accents ---------------------------------------------
#
# Mac OS puts its accents on Option chords, and they are there on the
# plain U.S. layout too — so a Mac guest reaches accented characters
# without any dead-key layout, and its apostrophe stays an apostrophe.
# For a Mac you are automating that is the better arrangement: "us" types
# ASCII exactly, "mac-option" adds the accents.
#
# Measured on Mac OS 9.2.2, typed into Stickies and read off the screen.
_MAC_ACCENT = {
    "'": ["alt", "e"],             # acute:      option-e then a -> á
    "`": ["alt", "grave_accent"],  # grave:      option-` then a -> à
    "^": ["alt", "i"],             # circumflex: option-i then a -> â
    "~": ["alt", "n"],             # tilde:      option-n then o -> õ
    '"': ["alt", "u"],             # diaeresis:  option-u then u -> ü
}

#: Characters Mac gives their own Option chord rather than composing.
#: Measured, and worth measuring: option-e then c produces "´c", not "ç".
_MAC_DIRECT = {
    "ç": ["alt", "c"],
    "Ç": ["alt", "shift", "c"],
}

#: Layouts type_text can actually produce. "us" is the plain mapping,
#: "us-intl" adds dead-key handling, "mac-option" adds Mac's Option
#: accents on top of a plain US board.
SUPPORTED_LAYOUTS = ("us", "us-intl", "mac-option")


def text_to_chords(text: str, layout: str = "us") -> list[list[str]]:
    """Translate text into qcode chords.

    Usually one chord per character, but a dead-key layout needs two for
    anything involving an accent — the accent key, then whatever it
    combines with (a space to make the accent literal).

    Raises KeyboardError on the first untranslatable character, naming it.
    """
    if layout not in SUPPORTED_LAYOUTS:
        raise KeyboardError(
            f"layout {layout!r} is not implemented; known layouts are "
            f"{', '.join(SUPPORTED_LAYOUTS)}. Run detect_layout to see what "
            "the guest is actually using."
        )
    dead_layout = layout == "us-intl"
    mac = layout == "mac-option"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chords: list[list[str]] = []
    for char in text:
        if mac and char in _MAC_DIRECT:
            # Checked before COMPOSE: Mac reaches ç with its own chord,
            # and the acute accent refuses to combine with c.
            chords.append(list(_MAC_DIRECT[char]))
            continue
        if mac and char in COMPOSE:
            accent, base = COMPOSE[char]
            chords.append(list(_MAC_ACCENT[accent]))
            chords.extend(text_to_chords(base, "us"))
            continue
        if dead_layout and char in COMPOSE:
            accent, base = COMPOSE[char]
            chords.append(_dead_chord(accent))
            chords.extend(text_to_chords(base, "us"))
            continue
        if dead_layout and char in _DEAD:
            # The accent alone would swallow the next character; a space
            # after it makes the guest commit the accent on its own.
            chords.append(_dead_chord(char))
            chords.append(["spc"])
            continue
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
                f"cannot type character {char!r} on the {layout} layout"
            )
    return chords


def _dead_chord(char: str) -> list[str]:
    key, needs_shift = _DEAD[char]
    return (["shift", key] if needs_shift else [key])


# --- layout detection --------------------------------------------------
#
# send-key names keys by their position on a US board; the guest decides
# what character that position emits. Only the guest can say which, so
# detect_layout presses these and reads the result back off the screen.
# Letters come first because QWERTZ swaps y/z and AZERTY swaps a/q and
# z/w; the rest is the punctuation that national layouts rearrange.
PROBE_KEYS = (
    "q", "w", "y", "z", "a", "m",
    "semicolon", "apostrophe", "bracket_left", "bracket_right",
    "backslash", "slash", "comma", "dot", "minus", "equal",
    "grave_accent",
    # Digit 6 for its shifted caret: US-International makes that a dead
    # key too, alongside the apostrophe and grave, and a probe that never
    # presses it would call the layout US.
    "6",
)

#: What those keys actually produced, measured on a running guest. Each
#: entry is (unshifted, shifted), one character per probe key, with None
#: for a key that emitted nothing. Only layouts that have been probed on
#: a real guest belong here — a signature copied out of documentation
#: would defeat the point of asking the guest.
LAYOUT_SIGNATURES: dict[str, tuple[str, str]] = {
    "us": ("qwyzam;'[]\\/,.-=`6", 'QWYZAM:"{}|?<>_+~^'),
}


def identify_layout(
    unshifted: list[str], shifted: list[str]
) -> list[tuple[str, int]]:
    """Rank known layouts by how many probe keys disagree with them.

    Takes one entry per probe key, empty for a key that produced nothing,
    rather than a joined string: a dead key would otherwise shorten the
    string and shift every comparison after it, turning one difference
    into a dozen. Returns (name, mismatches) best first — a zero means
    every probed key matched, and anything else is reported rather than
    rounded away, so an unprobed national layout looks like a poor match
    everywhere instead of a confident wrong answer.
    """
    ranked = []
    for name, (plain, shift) in LAYOUT_SIGNATURES.items():
        misses = sum(a != b for a, b in zip(unshifted, plain))
        misses += sum(a != b for a, b in zip(shifted, shift))
        ranked.append((name, misses))
    return sorted(ranked, key=lambda item: item[1])


def dead_keys(unshifted: list[str], shifted: list[str]) -> list[str]:
    """Probe keys that emitted nothing at all.

    Real and worth naming: loading DOS's KEYB BR turns the grave key into
    an accent that waits for the next keystroke, so it prints nothing on
    its own. type_text would hang a character on it silently.
    """
    probed = min(len(unshifted), len(shifted), len(PROBE_KEYS))
    return [
        PROBE_KEYS[i]
        for i in range(probed)
        if not unshifted[i] and not shifted[i]
    ]


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
