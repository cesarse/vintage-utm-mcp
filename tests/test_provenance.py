"""The media library's record of what each staged image actually is.

Sizes were the old test for sameness and they do not work: both floppy
images in the real library are exactly 1474560 bytes, as every 1.44 MB
floppy image is.
"""

import os

from vintage_utm_mcp import provenance

FLOPPY = 1474560


def test_same_size_different_content_is_a_different_digest(tmp_path) -> None:
    one = tmp_path / "Disk1.img"
    two = tmp_path / "other.img"
    one.write_bytes(b"A" * FLOPPY)
    two.write_bytes(b"B" * FLOPPY)
    assert one.stat().st_size == two.stat().st_size
    assert provenance.digest_file(one) != provenance.digest_file(two)


def test_digest_is_stable_across_chunk_boundaries(tmp_path) -> None:
    # Images are streamed, so the answer must not depend on the read size.
    image = tmp_path / "Disk1.img"
    image.write_bytes(bytes(range(256)) * 900)
    assert provenance.digest_file(image, chunk=64) == provenance.digest_file(
        image, chunk=1 << 20
    )


def test_manifest_round_trips(tmp_path) -> None:
    src = tmp_path / "src.img"
    src.write_bytes(b"floppy")
    entries = provenance.record(
        {}, "Disk1.img", digest="abc", staged=src, source=src
    )
    provenance.save(tmp_path, entries)
    assert provenance.load(tmp_path)["Disk1.img"]["digest"] == "abc"


def test_a_corrupt_manifest_is_treated_as_absent(tmp_path) -> None:
    # It is a cache and a record, never a prerequisite: a hand-edited or
    # truncated manifest must not stop media from being inserted.
    provenance.manifest_path(tmp_path).write_text("{ this is not json")
    assert provenance.load(tmp_path) == {}


def test_missing_manifest_is_treated_as_absent(tmp_path) -> None:
    assert provenance.load(tmp_path) == {}


def test_recorded_digest_is_reused_while_the_file_matches(tmp_path) -> None:
    image = tmp_path / "Disk1.img"
    image.write_bytes(b"A" * 4096)
    entries = provenance.record(
        {}, "Disk1.img", digest="pretend", staged=image, source=image
    )
    # Trusted without re-reading, which is what keeps a 700 MB ISO cheap.
    assert provenance.digest_of_staged(entries, "Disk1.img", image) == "pretend"


def test_a_file_changed_behind_our_back_is_rehashed(tmp_path) -> None:
    image = tmp_path / "Disk1.img"
    image.write_bytes(b"A" * 4096)
    entries = provenance.record(
        {}, "Disk1.img", digest="pretend", staged=image, source=image
    )
    image.write_bytes(b"B" * 4096)  # same size, different contents
    os.utime(image, ns=(0, 0))  # and a different mtime
    assert provenance.digest_of_staged(
        entries, "Disk1.img", image
    ) == provenance.digest_file(image)


def test_describe_returns_nothing_for_an_unknown_image(tmp_path) -> None:
    assert provenance.describe({}, "Disk9.img") is None
