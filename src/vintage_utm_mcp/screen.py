"""Screen reading: VGA text-mode buffer decoding and screenshot conversion.

Text mode: on x86, the BIOS Data Area (physical 0x400..0x4FF) describes
the current video state — mode byte at 0x449, columns at 0x44A, page
size at 0x44C, active page at 0x462, rows-1 at 0x484 (EGA+). The text
buffer lives at 0xB8000 (color modes 2/3) or 0xB0000 (mono mode 7) as
character+attribute byte pairs, encoded in CP437.

Screenshots: QMP screendump produces PPM; we convert to PNG and upscale
small (retro) resolutions 2x with nearest-neighbor to keep pixels crisp.
"""

from __future__ import annotations

import io
import re

from PIL import Image as PILImage

# CP437 glyphs for byte values 0x00-0x1F, which Python's cp437 codec maps
# to control characters but the VGA text mode renders as symbols.
_LOW_GLYPHS = (
    " ☺☻♥♦♣♠•◘○◙♂♀♪♫☼►◄↕‼¶§▬↨↑↓→←∟↔▲▼"
)

TEXT_MODES = {0x00, 0x01, 0x02, 0x03, 0x07}
MONO_MODE = 0x07


class ScreenError(RuntimeError):
    """The screen cannot be read as requested."""


def parse_bda(raw: bytes) -> dict[str, int]:
    """Parse video state out of a dump of the BIOS Data Area (0x400+).

    `raw` must start at physical address 0x400 and be at least 0x90 long.
    """
    if len(raw) < 0x90:
        raise ScreenError(f"BDA dump too short: {len(raw)} bytes")
    mode = raw[0x49] & 0x7F  # bit 7 = "don't clear screen" flag, not mode
    columns = int.from_bytes(raw[0x4A:0x4C], "little")
    page_size = int.from_bytes(raw[0x4C:0x4E], "little")
    active_page = raw[0x62]
    rows = raw[0x84] + 1 if raw[0x84] else 25
    return {
        "mode": mode,
        "columns": columns or 80,
        "page_size": page_size,
        "active_page": active_page,
        "rows": rows,
    }


def text_buffer_address(bda: dict[str, int]) -> int:
    """Physical address of the active text page."""
    base = 0xB0000 if bda["mode"] == MONO_MODE else 0xB8000
    return base + bda["active_page"] * bda["page_size"]


def decode_text_buffer(raw: bytes, columns: int, rows: int) -> str:
    """Decode a char+attribute VGA buffer into text (attributes dropped).

    Trailing spaces are stripped per line; lines are joined with \\n.
    """
    expected = columns * rows * 2
    if len(raw) < expected:
        raise ScreenError(
            f"text buffer dump too short: {len(raw)} < {expected} bytes"
        )
    lines = []
    for row in range(rows):
        chars = []
        for col in range(columns):
            byte = raw[(row * columns + col) * 2]
            if byte < 0x20:
                chars.append(_LOW_GLYPHS[byte])
            else:
                chars.append(bytes([byte]).decode("cp437"))
        lines.append("".join(chars).rstrip())
    return "\n".join(lines)


def locate_text(
    screen_text: str, needle: str, occurrence: int = 1
) -> tuple[int, int]:
    """Find the Nth occurrence of `needle` on the screen, reading order.

    Returns (row, column-of-center), both zero-based. Raises ScreenError
    when absent or when fewer occurrences exist.
    """
    if not needle:
        raise ScreenError("cannot locate empty text")
    remaining = occurrence
    for row, line in enumerate(screen_text.split("\n")):
        start = 0
        while (idx := line.find(needle, start)) != -1:
            remaining -= 1
            if remaining == 0:
                return row, idx + len(needle) // 2
            start = idx + 1
    raise ScreenError(
        f"text {needle!r} not found on screen"
        + (f" ({occurrence} occurrences requested)" if occurrence > 1 else "")
    )


def decode_attributes(raw: bytes, columns: int, rows: int) -> list[int]:
    """Extract the attribute byte of every cell, row-major.

    The DOS mouse driver draws its text-mode pointer by altering the
    ATTRIBUTE of the cell under it, leaving the character untouched — so
    attributes are how the pointer becomes visible to us.
    """
    expected = columns * rows * 2
    if len(raw) < expected:
        raise ScreenError(
            f"text buffer dump too short: {len(raw)} < {expected} bytes"
        )
    return [raw[i * 2 + 1] for i in range(columns * rows)]


def _attribute_runs(row_attrs: list[int]) -> list[tuple[int, int, int]]:
    """Split a row of attributes into (start, length, attribute) runs."""
    runs: list[tuple[int, int, int]] = []
    start = 0
    for index in range(1, len(row_attrs) + 1):
        if index == len(row_attrs) or row_attrs[index] != row_attrs[start]:
            runs.append((start, index - start, row_attrs[start]))
            start = index
    return runs


def _word_at(line: str, column: int) -> str:
    """The run of non-space characters containing `column`."""
    left = column
    while left > 0 and line[left - 1] != " ":
        left -= 1
    right = column
    while right + 1 < len(line) and line[right + 1] != " ":
        right += 1
    return line[left : right + 1]


def find_accelerators(
    screen_text: str, attributes: list[int], columns: int
) -> list[dict]:
    """Locate keyboard accelerators highlighted on a text-mode screen.

    TUI applications of the era mark the shortcut letter of every label
    by giving that single cell a different attribute — the F of "File",
    the x of "Exit". A one-cell island of attribute inside a row is
    therefore an accelerator, and the surrounding word is its label.

    Returns dicts of label, key, row and column, in reading order.
    """
    lines = screen_text.split("\n")
    found: list[dict] = []
    for row, line in enumerate(lines):
        row_attrs = attributes[row * columns : (row + 1) * columns]
        if not row_attrs:
            continue
        padded = line.ljust(columns)
        for start, length, _ in _attribute_runs(row_attrs):
            if length != 1:
                continue
            char = padded[start]
            if not char.isalnum():
                continue
            label = _word_at(padded, start)
            if len(label) < 2:
                continue
            found.append(
                {
                    "label": label.strip(".<>[]()"),
                    "key": char,
                    "row": row,
                    "column": start,
                }
            )
    return found


#: "F1=Help", "Esc=Cancel", "Ctrl+Q=Quit", "Alt+F4 Close"
_HINT = re.compile(
    r"\b((?:Ctrl|Alt|Shift)[+-])?"
    r"(F\d{1,2}|Esc|Enter|Tab|Del|Ins|Space|PgUp|PgDn|Arrow|[A-Z])"
    r"\s*=\s*"
    r"([A-Za-z][\w ]{0,20}?)"
    r"(?=\s{2,}|\s*[│|]|\s*$)",
    re.MULTILINE,
)


def parse_key_hints(screen_text: str) -> list[dict]:
    """Read the key hints applications print in their status bars.

    Lines like "F1=Help  Esc=Cancel  Tab=Next Field" are a second,
    independent source of shortcuts — one the application states
    outright rather than one we infer from highlighting.
    """
    hints = []
    seen = set()
    for modifier, key, action in _HINT.findall(screen_text):
        combo = f"{modifier}{key}" if modifier else key
        if combo in seen:
            continue
        seen.add(combo)
        hints.append({"key": combo, "action": action.strip()})
    return hints


def ppm_size(ppm: bytes) -> tuple[int, int]:
    """Width and height of a PPM image without full decoding."""
    image = PILImage.open(io.BytesIO(ppm))
    return image.size


def ppm_to_png(ppm: bytes, scale: int = 0) -> tuple[bytes, int, int, int]:
    """Convert a PPM screendump to PNG, upscaling with nearest-neighbor.

    scale=0 means auto: 2x for retro resolutions (width < 800), 1x
    otherwise. Returns (png_bytes, width, height, scale_used).
    """
    image = PILImage.open(io.BytesIO(ppm))
    width, height = image.size
    if scale <= 0:
        scale = 2 if width < 800 else 1
    if scale > 1:
        image = image.resize((width * scale, height * scale), PILImage.NEAREST)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), image.size[0], image.size[1], scale
