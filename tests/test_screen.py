import pytest

from vintage_utm_mcp.screen import (
    ScreenError,
    decode_text_buffer,
    parse_bda,
    ppm_to_png,
    text_buffer_address,
)


def _bda(mode=0x03, columns=80, page_size=0x1000, active_page=0, rows_minus1=24):
    raw = bytearray(0x100)
    raw[0x49] = mode
    raw[0x4A:0x4C] = columns.to_bytes(2, "little")
    raw[0x4C:0x4E] = page_size.to_bytes(2, "little")
    raw[0x62] = active_page
    raw[0x84] = rows_minus1
    return bytes(raw)


def test_parse_bda_standard_dos_screen() -> None:
    bda = parse_bda(_bda())
    assert bda == {
        "mode": 3,
        "columns": 80,
        "page_size": 0x1000,
        "active_page": 0,
        "rows": 25,
    }


def test_parse_bda_masks_no_clear_bit_and_defaults_rows() -> None:
    bda = parse_bda(_bda(mode=0x83, rows_minus1=0))  # bit 7 set, pre-EGA rows
    assert bda["mode"] == 3
    assert bda["rows"] == 25


def test_text_buffer_address_color_mono_and_pages() -> None:
    assert text_buffer_address(parse_bda(_bda())) == 0xB8000
    assert text_buffer_address(parse_bda(_bda(mode=0x07))) == 0xB0000
    assert (
        text_buffer_address(parse_bda(_bda(active_page=2, page_size=0x1000)))
        == 0xB8000 + 0x2000
    )


def _buffer(lines: list[str], columns: int, attr: int = 0x07) -> bytes:
    raw = bytearray()
    for line in lines:
        padded = line.ljust(columns)
        for ch in padded:
            raw += bytes([ch.encode("cp437")[0], attr])
    return bytes(raw)


def test_decode_text_buffer_strips_and_joins() -> None:
    raw = _buffer(["C:\\>DIR", "", " 2 file(s)"], columns=40)
    assert decode_text_buffer(raw, 40, 3) == "C:\\>DIR\n\n 2 file(s)"


def test_decode_text_buffer_cp437_box_drawing() -> None:
    raw = _buffer(["╔═╗", "╚═╝"], columns=3)
    assert decode_text_buffer(raw, 3, 2) == "╔═╗\n╚═╝"


def test_decode_text_buffer_low_glyphs() -> None:
    raw = bytes([0x03, 0x07, 0x0D, 0x07, 0x0E, 0x07])  # ♥ ♪ ♫ in CP437
    assert decode_text_buffer(raw, 3, 1) == "♥♪♫"


def test_decode_text_buffer_too_short() -> None:
    with pytest.raises(ScreenError, match="too short"):
        decode_text_buffer(b"\x41\x07", 80, 25)


def _ppm(width: int, height: int) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode() + b"\x80\x00\x40" * (
        width * height
    )


def test_ppm_to_png_auto_upscales_retro_resolution() -> None:
    png, w, h, scale = ppm_to_png(_ppm(640, 400))
    assert (w, h, scale) == (1280, 800, 2)
    assert png.startswith(b"\x89PNG")


def test_ppm_to_png_leaves_large_screens_alone() -> None:
    _, w, h, scale = ppm_to_png(_ppm(800, 600))
    assert (w, h, scale) == (800, 600, 1)


def test_ppm_to_png_explicit_scale() -> None:
    _, w, h, scale = ppm_to_png(_ppm(100, 50), scale=3)
    assert (w, h, scale) == (300, 150, 3)
