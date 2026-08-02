"""What each image in the media library actually is.

The library is keyed by filename, and filenames collide. Worse, so do
sizes: every 1.44 MB floppy image is exactly 1474560 bytes, so "same
name, same size" — the old test for "this is the same image" — is true of
*every* pair of floppy images. Staging a different `Disk1.img` therefore
kept the one already there and the guest booted that instead, silently.

A digest answers the question a size cannot. It is kept in a small
manifest beside the images so a large ISO does not have to be re-read on
every insert, and the manifest records where each image came from, which
is the other thing you want to know when the library has been filling up
for weeks.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

#: Sits inside the library, beside the images it describes.
MANIFEST_NAME = ".provenance.json"

#: Read size for hashing. Images run from 1.44 MB floppies to CD-sized
#: ISOs, so they are streamed rather than loaded.
CHUNK = 1 << 20

#: 16 bytes is far more than enough to tell two disk images apart, and
#: blake2b is quick enough that hashing a floppy is unnoticeable.
DIGEST_SIZE = 16


def digest_file(path: Path, chunk: int = CHUNK) -> str:
    digest = hashlib.blake2b(digest_size=DIGEST_SIZE)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_path(library: Path) -> Path:
    return library / MANIFEST_NAME


def load(library: Path) -> dict[str, dict]:
    """Read the manifest, treating anything unreadable as absent.

    A corrupt or hand-edited manifest must not stop media from being
    inserted — the digests can always be recomputed from the images.
    """
    try:
        data = json.loads(manifest_path(library).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(library: Path, entries: dict[str, dict]) -> None:
    try:
        manifest_path(library).write_text(json.dumps(entries, indent=2, sort_keys=True))
    except OSError:
        pass  # the manifest is a cache and a record, never a prerequisite


def stamp(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def record(
    entries: dict[str, dict], name: str, *, digest: str, staged: Path, source: Path
) -> dict[str, dict]:
    size, mtime = stamp(staged)
    entries[name] = {
        "digest": digest,
        "size": size,
        "mtime_ns": mtime,
        "source": str(source),
        "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return entries


def digest_of_staged(entries: dict[str, dict], name: str, staged: Path) -> str:
    """Digest of an image already in the library.

    Uses the manifest only when it still describes the file on disk —
    same size and same modification time — so an image replaced behind
    our back is re-read rather than trusted.
    """
    entry = entries.get(name)
    if entry:
        size, mtime = stamp(staged)
        if entry.get("size") == size and entry.get("mtime_ns") == mtime:
            recorded = entry.get("digest")
            if isinstance(recorded, str):
                return recorded
    return digest_file(staged)


def describe(entries: dict[str, dict], name: str) -> dict | None:
    """What is known about a staged image, for reporting back."""
    entry = entries.get(name)
    if not entry:
        return None
    return {
        "digest": entry.get("digest"),
        "source": entry.get("source"),
        "staged_at": entry.get("staged_at"),
    }
