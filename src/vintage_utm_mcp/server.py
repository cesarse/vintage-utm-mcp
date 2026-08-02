"""vintage-utm-mcp server.

Increment 1: VM lifecycle via utmctl.
Increment 2: QMP connectivity + check_vm diagnostics.
Next increments: removable media, keyboard/mouse, screenshot, text-mode
screen reading.
"""

import asyncio
import dataclasses
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage

from vintage_utm_mcp import (
    keyboard,
    mouse,
    pointer,
    provenance,
    qmp,
    screen,
    utmctl,
)

mcp = FastMCP("vintage-utm")

_qmp = qmp.QmpManager()


@mcp.tool()
def list_vms() -> list[dict[str, str]]:
    """List all UTM virtual machines with their name, UUID and status.

    Status is one of: stopped, starting, started, pausing, paused,
    resuming, stopping.
    """
    return utmctl.list_vms()


@mcp.tool()
def start_vm(vm: str, disposable: bool = False) -> dict[str, str]:
    """Start a VM (or resume a suspended one).

    Args:
        vm: VM name or UUID.
        disposable: run as a snapshot — changes are NOT saved to disk.
            Great for risky experiments on retro guests.
    """
    utmctl.start(vm, disposable=disposable)
    return utmctl.status(vm)


@mcp.tool()
def stop_vm(vm: str, mode: str = "force") -> dict[str, str]:
    """Stop a VM.

    Args:
        vm: VM name or UUID.
        mode: "force" (hard power-off, default — the reliable choice for
            DOS-era guests without ACPI; make sure disk activity is idle),
            "request" (ask the guest OS to shut down; needs ACPI support),
            or "kill" (kill the QEMU process).
    """
    utmctl.stop(vm, mode=mode)
    return utmctl.status(vm)


@mcp.tool()
def suspend_vm(vm: str, save_state: bool = False) -> dict[str, str]:
    """Suspend a running VM to memory.

    Args:
        vm: VM name or UUID.
        save_state: also persist the suspended state to disk.
    """
    utmctl.suspend(vm, save_state=save_state)
    return utmctl.status(vm)


def _find_vm(vm: str) -> dict[str, str] | None:
    """Locate a VM entry by name or UUID."""
    return next(
        (v for v in utmctl.list_vms() if vm in (v["name"], v["uuid"])), None
    )


def _canonical(vm: str, entry: dict[str, str] | None) -> str:
    """Canonical VM name: the QMP endpoint convention is name-based, so a
    UUID must resolve to the same endpoint as the VM's name."""
    return entry["name"] if entry else vm


def _resolve(vm: str) -> str:
    """Canonical VM name, from utmctl's cached index.

    Every tool except check_vm needs the name and nothing else — the QMP
    endpoint is derived from it — and asking utmctl afresh each time cost
    ~217 ms per call, more than the screendump and the PNG encode
    together. An unknown name passes through unchanged, as it always has,
    and fails a moment later with a QMP error naming the endpoint it
    tried.
    """
    return utmctl.canonical_name(vm) or vm


@mcp.tool()
async def check_vm(vm: str) -> dict[str, Any]:
    """Diagnose a VM: existence, run status and QMP connectivity.

    QMP is what powers in-guest keyboard/mouse, screenshots and removable
    media, so run this first when those tools misbehave. When QMP is not
    reachable, the result includes the exact QEMU argument to add to the
    VM (UTM: VM settings -> QEMU -> Arguments).
    """
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
    result: dict[str, Any] = {
        "exists": entry is not None,
        "status": entry["status"] if entry else None,
        "qmp_address": qmp.describe(qmp.resolve_address(canonical)),
        "qmp_reachable": False,
    }
    if entry is None:
        result["hint"] = "VM not found; check list_vms for the exact name."
        return result
    if entry["status"] != "started":
        result["hint"] = "VM is not running; QMP is only available while started."
        return result
    try:
        version = await _qmp.execute(canonical, "query-version")
        runstate = await _qmp.execute(canonical, "query-status")
    except qmp.QmpError as exc:
        result["error"] = str(exc)
        result["hint"] = (
            "Add this to the VM's extra QEMU arguments and restart it: "
            + qmp.qemu_arg_for(canonical)
        )
        return result
    q = version["qemu"]
    mice = await _qmp.execute(canonical, "query-mice")
    result.update(
        qmp_reachable=True,
        qemu_version=f"{q['major']}.{q['minor']}.{q['micro']}",
        guest_runstate=runstate.get("status"),
        pointer_devices=[
            {"name": m.get("name"), "current": m.get("current"),
             "absolute": m.get("absolute")}
            for m in mice
        ],
    )
    if any(m.get("absolute") for m in mice):
        result["pointer_hint"] = (
            "An absolute pointer (USB tablet) is present; QEMU routes "
            "un-addressed input to the 'current' pointer, which DOS-era "
            "guests can't see when it's the tablet. The mouse tools now "
            "switch to a relative pointer automatically before each "
            "gesture (mouse_set); removing the USB/tablet pointer from "
            "this VM's UTM configuration remains the permanent fix."
        )
    return result


# QEMU runs inside the QEMUHelper XPC service, which has its OWN App
# Sandbox, separate from UTM's. Files UTM knows about are blessed one by
# one via security-scoped bookmarks; the only location QEMU can read
# unconditionally is the helper's own container.
HELPER_DOCUMENTS = (
    Path.home() / "Library/Containers/com.utmapp.QEMUHelper/Data/Documents"
)


def media_library() -> Path | None:
    """Folder inside QEMUHelper's container used as the media image library."""
    if not HELPER_DOCUMENTS.parent.is_dir():
        return None  # helper container does not exist (no QEMU VM ever ran)
    HELPER_DOCUMENTS.mkdir(exist_ok=True)
    library = HELPER_DOCUMENTS / "vintage-utm-mcp"
    try:
        library.mkdir(exist_ok=True)
    except OSError:
        return None
    return library


def _resolve_image(image_path: str) -> Path:
    """Resolve an image argument: explicit path, or filename in the library."""
    path = Path(image_path).expanduser()
    if path.is_file():
        return path.resolve()
    searched = [str(path)]
    library = media_library()
    if library is not None and not path.is_absolute():
        candidate = library / image_path
        if candidate.is_file():
            return candidate.resolve()
        searched.append(str(candidate))
    raise ValueError("image not found on host; searched: " + ", ".join(searched))


def _stage_into_library(path: Path) -> Path:
    """Stage an image into the library so QEMU's sandbox can read it.

    Uses an APFS copy-on-write clone (cp -c) when available — instant and
    storage-free — falling back to a regular copy elsewhere.
    """
    library = media_library()
    if library is None:
        raise ValueError(
            "QEMUHelper container not found; start any QEMU VM once in UTM "
            "so macOS creates it, then retry"
        )
    target = library / path.name
    entries = provenance.load(library)
    incoming = provenance.digest_file(path)
    if target.exists():
        staged = provenance.digest_of_staged(entries, path.name, target)
        if staged == incoming:
            if path.name not in entries:  # backfill images staged before this
                provenance.record(
                    entries, path.name, digest=staged, staged=target, source=path
                )
                provenance.save(library, entries)
            return target
        raise ValueError(
            f"a different image named {path.name!r} is already in the library "
            f"({target}) — same name, different contents (staged "
            f"{staged[:12]}…, yours {incoming[:12]}…). Rename one of them. "
            "Size alone cannot tell them apart: every 1.44 MB floppy image is "
            "exactly 1474560 bytes."
        )
    clone = subprocess.run(
        ["cp", "-c", str(path), str(target)], capture_output=True
    )
    if clone.returncode != 0:  # non-APFS volume or non-macOS: plain copy
        shutil.copy2(path, target)
    provenance.record(entries, path.name, digest=incoming, staged=target, source=path)
    provenance.save(library, entries)
    return target


async def _blockdev(vm: str, cmd: str, drive: str, extra: dict) -> Any:
    """Run a block command trying the legacy 'device' key, then 'id'."""
    try:
        return await _qmp.execute(vm, cmd, {"device": drive, **extra})
    except qmp.QmpError as first_error:
        try:
            return await _qmp.execute(vm, cmd, {"id": drive, **extra})
        except qmp.QmpError:
            raise first_error


async def _drive_state(vm: str, drive: str) -> dict[str, Any]:
    blocks = await _qmp.execute(vm, "query-block")
    shaped = qmp.shape_drives(blocks)
    return next((d for d in shaped if d["drive"] == drive), {"drive": drive})


@mcp.tool()
async def list_drives(vm: str) -> list[dict[str, Any]]:
    """List the VM's block devices (removable ones listed first).

    Removable drives (floppy, CD-ROM) are the valid targets for
    insert_media/eject_media. The 'drive' field is the identifier to pass
    to those tools.
    """
    canonical = _resolve(vm)
    blocks = await _qmp.execute(canonical, "query-block")
    return qmp.shape_drives(blocks)


@mcp.tool()
async def insert_media(
    vm: str, drive: str, image_path: str, image_format: str = "raw"
) -> dict[str, Any]:
    """Insert a disk image into a removable drive while the VM runs.

    Args:
        vm: VM name or UUID.
        drive: drive identifier from list_drives (UTM names them like
            "drive<UUID>").
        image_path: any host path (e.g. ~/Downloads/Disk4.img), or a bare
            filename already in the media library. Images outside the
            library are staged into it automatically (instant APFS clone),
            because QEMU's sandbox can only read the library. Note: guest
            writes go to the staged copy, not the original file.
        image_format: QEMU image format; "raw" (default) covers .img/.ima
            floppies and .iso images.
    """
    canonical = _resolve(vm)
    path = _resolve_image(image_path)
    library = media_library()
    if library is None or library not in path.parents:
        path = _stage_into_library(path)
    try:
        await _blockdev(
            canonical,
            "blockdev-change-medium",
            drive,
            {"filename": str(path), "format": image_format},
        )
    except qmp.QmpError as exc:
        message = str(exc)
        if "not permitted" in message.lower() or "permission" in message.lower():
            library = media_library()
            where = (
                f"copy the image into the media library ({library}) and pass "
                "just the filename"
                if library
                else "place the image inside UTM's container Documents folder"
            )
            raise qmp.QmpError(
                f"{message} — UTM's sandbox cannot read this location; {where}."
            ) from exc
        raise
    state = await _drive_state(canonical, drive)
    if library is not None:
        known = provenance.describe(provenance.load(library), path.name)
        if known:
            state["provenance"] = known
    return state


@mcp.tool()
async def eject_media(vm: str, drive: str, force: bool = False) -> dict[str, Any]:
    """Eject the medium from a removable drive.

    Args:
        vm: VM name or UUID.
        drive: drive identifier from list_drives.
        force: eject even if the guest locked the medium. DOS never locks
            floppies, but classic Mac OS locks its media — use force there.
    """
    canonical = _resolve(vm)
    await _blockdev(canonical, "eject", drive, {"force": force})
    return await _drive_state(canonical, drive)


@mcp.tool()
async def type_text(
    vm: str, text: str, delay_ms: int = 60, layout: str = "us"
) -> dict[str, Any]:
    """Type text into the VM as raw keystrokes (works in DOS, BIOS, GUIs).

    Args:
        vm: VM name or UUID.
        text: text to type; "\\n" presses Enter. Injected as keystrokes
            for the chosen layout — the guest decides what each key
            position emits, so a mismatch garbles punctuation while
            leaving letters and digits looking fine. Run detect_layout if
            unsure.
        delay_ms: pause between characters. The default 60 is safe for
            DOS-era guests, which drop keys during disk I/O if flooded;
            raise it if characters go missing, lower it for bulk typing.
        layout: "us" (default) or "us-intl". Pick "us-intl" when the guest
            uses US-International — Windows 3.11 does whenever SYSTEM.INI
            says `keyboard.dll=kbdusx.dll`. There the apostrophe, quote,
            backtick, tilde and caret are dead keys: typed plainly they
            emit nothing and swallow the character after them, so an
            apostrophe is sent as accent-then-space. It also unlocks
            accented characters, which the plain US layout cannot type at
            all — "ç" goes out as acute then c.
    """
    canonical = _resolve(vm)
    chords = keyboard.text_to_chords(text, layout=layout)
    hold = max(10, delay_ms // 2)
    for chord in chords:
        await _qmp.execute(
            canonical,
            "send-key",
            {
                "keys": [{"type": "qcode", "data": key} for key in chord],
                "hold-time": hold,
            },
        )
        await asyncio.sleep(delay_ms / 1000)
    return {"characters": len(text), "keystrokes": len(chords), "layout": layout}


@mcp.tool()
async def send_keys(vm: str, combo: str, hold_ms: int = 100) -> dict[str, Any]:
    """Press a key combination (all keys held together, then released).

    Args:
        vm: VM name or UUID.
        combo: keys separated by '-', '+' or spaces: "ctrl-alt-delete",
            "alt-f4", "F8", "enter", "shift+tab". For the minus/plus keys
            themselves use "minus" and "equal".
        hold_ms: how long the chord is held before release.
    """
    canonical = _resolve(vm)
    keys = keyboard.parse_combo(combo)
    await _qmp.execute(
        canonical,
        "send-key",
        {
            "keys": [{"type": "qcode", "data": key} for key in keys],
            "hold-time": hold_ms,
        },
    )
    return {"pressed": keys}


def _exchange_path(suffix: str) -> Path:
    """A unique path QEMU can WRITE to (its own container): the reverse of
    the media-library trick. The server reads the file and deletes it."""
    library = media_library()
    if library is None:
        raise ValueError(
            "QEMUHelper container not found; start any QEMU VM once in UTM "
            "so macOS creates it, then retry"
        )
    exchange = library / ".qemu-out"
    exchange.mkdir(exist_ok=True)
    return exchange / f"{uuid.uuid4().hex}{suffix}"


async def _dump_to_bytes(vm: str, cmd: str, args: dict, suffix: str) -> bytes:
    path = _exchange_path(suffix)
    try:
        await _qmp.execute(vm, cmd, {**args, "filename": str(path)})
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


# Last known screen geometry per VM, fed by screenshot(); the mouse tools
# will need it to map image coordinates back to guest coordinates.
_LAST_SCREEN: dict[str, dict[str, int]] = {}


@mcp.tool()
async def screenshot(vm: str, delay_ms: int = 0, scale: int = 0) -> McpImage:
    """Capture the VM's screen as a PNG image.

    Captures the guest framebuffer at native resolution (host display
    scaling never affects it). Small retro resolutions (width < 800) are
    upscaled 2x with nearest-neighbor for readability — so the image you
    see may be 2x the guest's real coordinate space.

    Args:
        vm: VM name or UUID.
        delay_ms: wait before capturing (let the screen settle after
            typing or a slow redraw).
        scale: 0 = auto (2x when width < 800), or an explicit factor.
    """
    canonical = _resolve(vm)
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    ppm = await _dump_to_bytes(canonical, "screendump", {}, ".ppm")
    png, width, height, used_scale = screen.ppm_to_png(ppm, scale)
    _LAST_SCREEN[canonical] = {
        "width": width,
        "height": height,
        "scale": used_scale,
    }
    return McpImage(data=png, format="png")


@mcp.tool()
async def read_text_screen(vm: str) -> dict[str, Any]:
    """Read the exact characters on an x86 text-mode screen (DOS, OS/2
    console) straight from video memory — deterministic, no vision needed.

    Detects the current video state from the BIOS Data Area (mode,
    columns, rows, active page). Fails with guidance when the guest is in
    a graphics mode or is not x86 (use screenshot there).
    """
    canonical = _resolve(vm)
    bda, text, _ = await _text_screen_state(canonical)
    return {
        "text": text,
        "columns": bda["columns"],
        "rows": bda["rows"],
        "mode": bda["mode"],
    }


async def _press(vm: str, keys: list[str], hold_ms: int = 100) -> None:
    await _qmp.execute(
        vm,
        "send-key",
        {"keys": [{"type": "qcode", "data": k} for k in keys], "hold-time": hold_ms},
    )


async def _current_input_line(vm: str) -> str:
    """Whatever has been typed after the last prompt on a text screen."""
    _, text, _ = await _text_screen_state(vm)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


@mcp.tool()
async def detect_layout(vm: str) -> dict[str, Any]:
    """Work out which keyboard layout the guest is actually using.

    `send-key` names keys by where they sit on a US board, but the guest
    decides what character each position emits — load a national driver
    (DOS's KEYB) and punctuation comes out somewhere else entirely, while
    letters and digits look fine and hide the problem. This presses the
    keys that move between layouts and reads back what appeared, so the
    answer comes from the guest rather than from a guess.

    Needs an x86 text-mode screen with a command prompt ready to type at
    — it types onto the current line and clears it afterwards with Escape,
    never pressing Enter. Run it at an idle `C:\\>`, not inside an editor.

    Reports the character each key produced, the closest known layout and
    how many keys disagreed with it. A poor match everywhere means a
    layout that has not been probed before, which is informative in
    itself: `type_text` only implements US, so anything else will mistype
    punctuation.
    """
    canonical = _resolve(vm)
    await _text_screen_state(canonical)  # fails clearly outside text mode
    observed: dict[str, list[str]] = {}
    for shift in (False, True):
        baseline = await _fresh_prompt(canonical)
        produced: list[str] = []
        for key in keyboard.PROBE_KEYS:
            produced.append(
                await _probe_key(canonical, key, shift, baseline + "".join(produced))
            )
        observed["shifted" if shift else "unshifted"] = produced
    await _fresh_prompt(canonical)
    ranked = keyboard.identify_layout(observed["unshifted"], observed["shifted"])
    best, misses = ranked[0]
    return {
        "keys": list(keyboard.PROBE_KEYS),
        "unshifted": observed["unshifted"],
        "shifted": observed["shifted"],
        "dead_keys": keyboard.dead_keys(observed["unshifted"], observed["shifted"]),
        "layout": best if misses == 0 else None,
        "closest": best,
        "mismatches": misses,
        "ranking": [{"layout": name, "mismatches": n} for name, n in ranked],
        "type_text_safe": misses == 0 and best == "us",
        "note": (
            "type_text implements the US layout only; on any other mapping "
            "letters and digits are still correct but punctuation is not"
        ),
    }


async def _fresh_prompt(vm: str) -> str:
    """Abandon the current line and clear the screen; return the prompt.

    Escape alone is not enough: DOS answers it with a backslash and drops
    to a continuation line without printing a new prompt, so the last
    line on screen is still the old one and every length comparison after
    it is measured against the wrong baseline. `cls` gives a clean screen
    and a real prompt — and c, l and s are letters, which sit in the same
    place on every layout this probe might meet.
    """
    await _press(vm, ["esc"])
    await asyncio.sleep(0.2)
    for key in ("c", "l", "s"):
        await _press(vm, [key])
        await asyncio.sleep(0.12)
    await _press(vm, ["ret"])
    await asyncio.sleep(0.9)
    return await _current_input_line(vm)


async def _probe_key(vm: str, key: str, shift: bool, so_far: str) -> str:
    """Press one key and return the character it added, or '' if none.

    One key at a time, each confirmed against the line it produced. A
    long run of chords quietly loses its tail — sending all seventeen
    shifted keys in a row reliably dropped the last few — and a probe
    that silently reads back less than it typed would invent a layout
    difference that is not there.
    """
    chord = (["shift"] if shift else []) + [key]
    await _press(vm, chord)
    # Poll for the echo; never press again. Retrying the *press* was the
    # first attempt and it typed several keys twice — the guest had taken
    # them, it just had not drawn them yet.
    for _ in range(8):
        await asyncio.sleep(0.12)
        line = await _current_input_line(vm)
        if len(line) > len(so_far):
            return line[len(so_far):]
    return ""  # dead key, or the guest swallowed it


async def _send_input(vm: str, events: list[dict]) -> None:
    await _qmp.execute(vm, "input-send-event", {"events": events})


def _rel(dx: int, dy: int) -> list[dict]:
    events = []
    if dx:
        events.append({"type": "rel", "data": {"axis": "x", "value": dx}})
    if dy:
        events.append({"type": "rel", "data": {"axis": "y", "value": dy}})
    return events


def _btn(button: str, down: bool) -> list[dict]:
    return [{"type": "btn", "data": {"button": button, "down": down}}]


# --- Closed-loop pointing -------------------------------------------------
#
# QEMU's screendump captures the cursor (verified on Windows 3.11: the
# display driver draws it into video memory as an opaque sprite), so the
# pointer can be measured instead of assumed. Learned sprites and the
# measured gain are cached per VM: learning costs one extra probe move,
# after which a single capture locates the cursor in well under a
# millisecond.
_SPRITES: dict[str, list[pointer.Sprite]] = {}
_GAIN: dict[str, tuple[float, float]] = {}

#: Learning probes, tried in order from the top-left corner. Sideways
#: first: the strip along the top of the screen is the likeliest stretch
#: of flat background. 40 units has to clear the cursor's own width on
#: guests that amplify it (Windows 3.11 travels 80 px) and on guests that
#: damp it (classic Mac OS manages about 24), or the two images touch and
#: merge into a single blob.
LEARN_PROBES = ((40, 0), (0, 40), (32, 32))
#: Stepped inward from the corner anchor before probing, so the cursor is
#: drawn whole. Small enough to stay near the flat top strip, big enough
#: to clear the border on guests that damp motion to about 0.6.
LEARN_MARGIN = (30, 30)
DEFAULT_GAIN = (1.0, 1.0)
GOTO_TOLERANCE = 0  # the loop can and does land exactly
#: Enough passes for the coarse approach plus an endgame that may need
#: several commands per pixel. Measured on classic Mac OS: a median of 7
#: one-unit commands buys one pixel, so a two-pixel residual alone can
#: cost ~14 passes and the old cap of 14 cut the loop off mid-approach.
GOTO_MAX_ITERATIONS = 24
#: Consecutive no-movement commands tolerated before giving up. Measured,
#: not guessed: classic Mac OS damps motion and banks the remainder, and
#: profiling 60 one-unit commands gave stall runs of 5, 6 (six times) and
#: 7 before the cursor moved. At 3 the loop abandoned moves that were
#: still converging; this clears the worst run seen with room to spare.
#: Each tolerated stall costs a move and a capture (~156 ms), so this is
#: patience bought at a price, and only damped guests ever spend it —
#: Windows 3.11 lands in one or two passes and never stalls.
GOTO_MAX_STALLS = 10
#: Corrections attempted mid-drag, on guests that keep the cursor visible
#: while the button is down.
DRAG_REFINE_ATTEMPTS = 6


def screen_mismatch(info: dict[str, int] | None, width: int, height: int) -> str | None:
    """Complain if the guest changed resolution since the last screenshot.

    Mouse coordinates are read in the last screenshot's pixel space, and
    that space carries a scale — 2x for retro resolutions, 1x from 800
    pixels wide up. Change the guest's mode and the old scale silently
    misplaces every click: aiming at (700, 500) on a stale 2x image lands
    at (350, 250), and the closed loop faithfully reports it as on target,
    because it is on the target it was given. Better to refuse.
    """
    if not info:
        return None
    scale = info.get("scale", 1) or 1
    implied = (info["width"] // scale, info["height"] // scale)
    if implied == (width, height):
        return None
    return (
        f"the last screenshot was taken of a {implied[0]}x{implied[1]} guest "
        f"but it is now {width}x{height}, so coordinates aimed on that image "
        "would land somewhere else entirely; take a fresh screenshot and aim "
        "on it"
    )


async def _guard_coordinate_space(vm: str) -> None:
    frame = await _capture(vm)
    complaint = screen_mismatch(_LAST_SCREEN.get(vm), frame.width, frame.height)
    if complaint:
        raise ValueError(complaint)


def _measurable(
    position: tuple[int, int], sprite: pointer.Sprite, frame: pointer.Frame
) -> bool:
    """Is the cursor clear of every edge, so its travel was not clamped?"""
    return (
        0 < position[0] < frame.width - sprite.width
        and 0 < position[1] < frame.height - sprite.height
    )


async def _capture(vm: str) -> pointer.Frame:
    return pointer.Frame.from_image_bytes(
        await _dump_to_bytes(vm, "screendump", {}, ".ppm")
    )


async def _walk(vm: str, dx: int, dy: int) -> None:
    """Deliver a relative move in paced steps (PS/2 carries <=127 per packet)."""
    for sx, sy in mouse.plan_moves(dx, dy):
        await _send_input(vm, _rel(sx, sy))
        await asyncio.sleep(0.012)
    await asyncio.sleep(0.12)  # let the guest redraw before we look


def _locate_known(vm: str, frame: pointer.Frame, near: tuple[int, int] | None = None):
    """Try every sprite already learned for this VM.

    Cursors change shape (arrow, hourglass, I-beam) and each shape is
    learned once; keeping a small library per VM means crossing into a text
    field costs a lookup rather than a fresh probe move.
    """
    sprites = _SPRITES.get(vm, [])
    for sprite in sprites:
        position = pointer.locate(frame, sprite)
        if position is not None:
            return sprite, position
    if near is not None:
        # Whole-template matching just failed. Before concluding the cursor
        # changed shape, check whether it is merely clipped against an edge.
        for sprite in sprites:
            position = pointer.locate_near(frame, sprite, near)
            if position is not None:
                return sprite, position
    return None


async def _bootstrap_sprite(vm: str) -> tuple[pointer.Sprite, tuple[int, int]]:
    """Learn *a* cursor from the corner anchor — enough to start steering.

    The corner is deterministic without needing to see anything, which is
    what makes it the right place to start from when no sprite is known
    yet. What it is not is a good place to *look* at a cursor: it is where
    guests keep their menu bars and window furniture, so the mask learned
    here is often a subset of the real sprite. _learn refines it once the
    pointer can be steered.
    """
    frame = await _capture(vm)
    await _corner_reset(vm, frame.width, frame.height)
    # Step clear of the corner before probing. The anchor is deterministic
    # but it is also the one place a cursor can be cut short: classic Mac
    # OS draws its arrow a row above the hotspot, so learning flush against
    # the top edge loses that row and every position it later reports is
    # off by one.
    await _walk(vm, *LEARN_MARGIN)
    errors = []
    for probe in LEARN_PROBES:
        before = await _capture(vm)
        await _walk(vm, *probe)
        after = await _capture(vm)
        try:
            # Preferred: one move over flat background, which exposes the
            # whole sprite including any pixels the same colour as its
            # neighbours.
            sprite, _, _ = pointer.learn_sprite(before, after)
        except pointer.PointerError as flat_error:
            # No flat background to be had — classic Mac OS tiles its
            # desktop with a watermark, so nothing on it is uniform. A
            # second move the same way identifies the cursor as the one
            # region that changed in both steps, whatever it sits on.
            await _walk(vm, *probe)
            third = await _capture(vm)
            try:
                sprite, _ = pointer.learn_sprite_sequence(before, after, third)
            except pointer.PointerError as sequence_error:
                errors.append(f"probe {probe}: {flat_error} / {sequence_error}")
                await _corner_reset(vm, frame.width, frame.height)
                continue
        known = _SPRITES.setdefault(vm, [])
        if not any(s == sprite for s in known):
            known.append(sprite)
        # Both paths learned from frames the cursor has since moved past,
        # so ask where it is now rather than trusting a stale position.
        position = pointer.locate(await _capture(vm), sprite)
        if position is None:
            errors.append(f"probe {probe}: learned a sprite it cannot find again")
            await _corner_reset(vm, frame.width, frame.height)
            continue
        return sprite, position
    raise pointer.PointerError(
        "could not learn the cursor: " + "; then ".join(errors)
    )


#: VMs whose sprite is being refined, so the refinement's own pointer
#: work cannot recurse back into learning.
_REFINING: set[str] = set()


async def _learn(vm: str) -> tuple[pointer.Sprite, tuple[int, int]]:
    """Learn the cursor, then learn it again somewhere it shows properly.

    Two passes because the two requirements pull apart. Starting needs a
    position known without sight of the cursor, which only a screen corner
    gives — and corners are exactly where guests park menu bars and window
    frames, so the mask learned there is whatever happened to contrast
    with that furniture. Once a rough sprite exists the pointer can be
    steered, so the second pass moves somewhere chosen by looking at the
    screen and learns the cursor properly. Measured: the corner gave 78 of
    the Windows arrow's 126 pixels and 85 of Mac OS 9's 97.

    The refinement is best-effort. If no clear area exists, or the cursor
    changes shape on the way, the bootstrap sprite stands — it locates
    correctly either way, being a subset of the real thing.
    """
    sprite, position = await _bootstrap_sprite(vm)
    if vm in _REFINING:
        return sprite, position
    _REFINING.add(vm)
    try:
        refined = await _refine_sprite(vm, sprite)
        if refined is not None:
            sprite, position = refined
        located = await _apply_hotspot(vm, sprite)
        if located is not None:
            sprite, position = located
    except pointer.PointerError:
        pass
    finally:
        _REFINING.discard(vm)
    return sprite, position


async def _apply_hotspot(
    vm: str, sprite: pointer.Sprite
) -> tuple[pointer.Sprite, tuple[int, int]] | None:
    """Apply a *configured* hotspot, if one is set.

    Only configured — never the clamp measurement, which is offered as a
    diagnostic by mouse_locate but is not trustworthy enough to steer by.
    On Mac OS 9 the clamp says (1, 1) while clicking says (0, 0): driving
    the pointer to row 19 opens the menu bar and row 20 does not, exactly
    as a zero offset predicts against its 20-row bar. The clamp's premise
    — that the guest pins the hotspot itself to zero — evidently does not
    hold there, and where the two disagree the one that measures where
    clicks actually land wins.
    """
    offset = mouse.read_hotspot(vm)
    if offset is None or offset == sprite.hotspot:
        return None
    placed = dataclasses.replace(sprite, hotspot=offset)
    known = _SPRITES.setdefault(vm, [])
    _SPRITES[vm] = [placed] + [s for s in known if s.pixels != placed.pixels]
    position = pointer.locate(await _capture(vm), placed)
    return (placed, position) if position else None


async def _refine_sprite(
    vm: str, bootstrap: pointer.Sprite
) -> tuple[pointer.Sprite, tuple[int, int]] | None:
    """Relearn the cursor over background chosen from the frame."""
    frame = await _capture(vm)
    # Room for the cursor at three positions plus the travel between them.
    span_x = bootstrap.width + 3 * abs(LEARN_PROBES[0][0]) * 2
    spot = pointer.find_learning_spot(
        frame, min(span_x, frame.width // 2), bootstrap.height * 3
    )
    if spot is None:
        return None
    await _closed_loop_goto(vm, *spot)
    frames = [await _capture(vm)]
    for _ in range(2):
        await _walk(vm, *LEARN_PROBES[0])
        frames.append(await _capture(vm))
    sprite, _ = pointer.learn_sprite_sequence(*frames)
    if sprite.opaque <= bootstrap.opaque:
        return None  # no better than what we had; keep the cheaper answer
    known = _SPRITES.setdefault(vm, [])
    if not any(s == sprite for s in known):
        known.insert(0, sprite)  # try the fuller mask first from now on
    position = pointer.locate(await _capture(vm), sprite)
    return (sprite, position) if position else None


async def _find_pointer(vm: str) -> tuple[pointer.Sprite, tuple[int, int], bool]:
    """Locate the cursor, learning it first if necessary.

    Returns (sprite, top-left position, learned_now).
    """
    known = _locate_known(vm, await _capture(vm))
    if known is not None:
        return known[0], known[1], False
    sprite, position = await _learn(vm)
    return sprite, position, True


async def _closed_loop_goto(
    vm: str, target_x: int, target_y: int
) -> dict[str, Any]:
    """Drive the cursor's hotspot onto a guest pixel, measuring each step.

    No corner reset and no calibration file: the error is observed after
    every move and the gain is re-estimated from what actually happened.
    """
    sprite, position, learned = await _find_pointer(vm)
    opening = await _capture(vm)
    screen_width, screen_height = opening.width, opening.height
    gain = _GAIN.get(vm, DEFAULT_GAIN)
    target = (target_x, target_y)
    iterations = 0
    lost = False
    stalls = 0
    seen = {position}
    for _ in range(GOTO_MAX_ITERATIONS):
        hotspot = pointer.hotspot_of(position, sprite)
        error = (target[0] - hotspot[0], target[1] - hotspot[1])
        if abs(error[0]) <= GOTO_TOLERANCE and abs(error[1]) <= GOTO_TOLERANCE:
            break
        command = pointer.correction(target, hotspot, gain)
        command = pointer.clamp_command(
            command, position, sprite, screen_width, screen_height, gain
        )
        if command == (0, 0):
            break  # sub-unit error the pointer cannot express
        await _walk(vm, *command)
        iterations += 1
        frame = await _capture(vm)
        # Where the command should have put it, used to recognise a cursor
        # clipped against an edge.
        predicted = (
            position[0] + round(command[0] * gain[0]),
            position[1] + round(command[1] * gain[1]),
        )
        found = _locate_known(vm, frame, near=predicted)
        if found is None:
            # The cursor changed shape mid-gesture — hovering a text field
            # turns the arrow into an I-beam. Deliberately NOT relearned
            # here: learning has to anchor at the corner, which would drag
            # the pointer off the very control that changed the shape, turn
            # it back into an arrow, and lose it again on the way back.
            # Stop and say the position is unverified instead.
            lost = True
            break
        sprite, new_position = found
        observed = (new_position[0] - position[0], new_position[1] - position[1])
        if _measurable(new_position, sprite, frame):
            # A move that ran into a screen edge travelled less than it was
            # told to; believing it would teach the loop a gain far below
            # the truth (1.667 was seen this way).
            gain = pointer.refine_gain(gain, command, observed)
            _GAIN[vm] = gain
        if new_position == position:
            # No movement at all. Not a dead end: guests that scale motion
            # down keep the leftover fraction and pay it out on a later
            # command, so a nudge that seems ignored often lands next time.
            stalls += 1
            if stalls > GOTO_MAX_STALLS:
                break
            continue
        stalls = 0
        if new_position in seen:
            # Back somewhere we have already been, having genuinely moved:
            # the guest is rounding our corrections in a way this loop
            # cannot split, and another pass would only bounce between the
            # same two pixels.
            position = new_position
            break
        position = new_position
        seen.add(position)
    hotspot = pointer.hotspot_of(position, sprite)
    residual = [target[0] - hotspot[0], target[1] - hotspot[1]]
    return {
        "guest_coords": list(hotspot),
        "requested": list(target),
        # A lost cursor means the last move could not be checked, so
        # neither the landing point nor the error is knowable.
        "on_target": None if lost else residual == [0, 0],
        "residual": None if lost else residual,
        "verified": not lost,
        **(
            {
                "lost_reason": "cursor stopped matching every known sprite "
                "(it most likely changed shape over the target); the "
                "position above is the last one actually measured"
            }
            if lost
            else {}
        ),
        "iterations": iterations,
        "gain": [round(gain[0], 3), round(gain[1], 3)],
        "sprite": {
            "width": sprite.width,
            "height": sprite.height,
            "opaque_pixels": sprite.opaque,
            "learned_this_call": learned,
        },
    }


@mcp.tool()
async def mouse_locate(vm: str) -> dict[str, Any]:
    """Find the mouse cursor in the guest's framebuffer (graphics modes).

    Diagnostic for the closed-loop pointer: reports where the cursor
    actually is, in guest pixels, and what sprite it matched. On the first
    call for a VM (or after the cursor changes shape) it moves the pointer
    a little to learn the new sprite — that probe is the only motion it
    causes.

    Fails when the cursor cannot be told apart, which is informative in
    itself: the screen must be still and the background under the pointer
    uniform for learning to work. Text modes should use read_text_screen
    and the keyboard instead.
    """
    canonical = _resolve(vm)
    await _ensure_relative_pointer(canonical)
    sprite, position, learned = await _find_pointer(canonical)
    return {
        "guest_coords": list(pointer.hotspot_of(position, sprite)),
        "sprite_top_left": list(position),
        "sprite": {
            "width": sprite.width,
            "height": sprite.height,
            "opaque_pixels": sprite.opaque,
            # Not readable from the framebuffer. (0, 0) is right for both
            # guests' arrows, confirmed by clicking at a known boundary;
            # override per VM in the [hotspot] config table if a cursor
            # points somewhere else.
            "assumed_hotspot": list(sprite.hotspot),
        },
        "learned_this_call": learned,
        "cached_sprites": len(_SPRITES.get(canonical, [])),
        "gain": [round(g, 3) for g in _GAIN.get(canonical, DEFAULT_GAIN)],
    }


@mcp.tool()
async def mouse_click(
    vm: str,
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    allow_unverified: bool = False,
) -> dict[str, Any]:
    """Click at (x, y) in the LAST screenshot's pixel space.

    Take a screenshot first and aim on it; coordinates are automatically
    mapped back to guest pixels (screenshots are usually 2x upscaled).

    Closed loop in graphics modes: the cursor is found in the framebuffer,
    moved, and re-measured until it is on target, so the reply says where
    the pointer really ended up (`on_target`, `residual`) instead of
    assuming. Guests whose cursor the framebuffer does not carry fall back
    to dead reckoning and say so in `method`. Text-mode screens are better
    driven from the keyboard: see read_shortcuts and activate_menu, which
    are exact where pointing is approximate.

    Args:
        vm: VM name or UUID.
        x, y: target in last-screenshot pixels.
        button: "left", "right" or "middle".
        double: double-click.
    """
    canonical = _resolve(vm)
    mouse.validate_button(button)
    await _guard_coordinate_space(canonical)
    gx, gy = mouse.image_to_guest(x, y, _LAST_SCREEN.get(canonical))
    result = await _goto(canonical, gx, gy)
    problem = unverified_click(result)
    if problem and not allow_unverified:
        raise ValueError(problem)
    await _click(canonical, button, double)
    return {**result, "button": button, "double": double}


def unverified_click(result: dict[str, Any]) -> str | None:
    """Why clicking at this position would be unsafe, or None if it is fine.

    Only the closed loop can lose track of the cursor mid-gesture — a
    shape change over the target is the usual cause — and when it does,
    the position it reports is the last one it actually measured, not
    where the pointer is now. Pressing a button there is a click at an
    unknown place, which is a worse outcome than not clicking at all.

    A verified-but-inexact landing is fine: being a pixel out still hits
    the button you aimed at. So this refuses only on lost, not on
    on_target. The open-loop fallback never claims to know where the
    cursor is, and is left alone — refusing there would disable pointing
    entirely on guests whose cursor the framebuffer does not carry.
    """
    if result.get("verified") is not False:
        return None
    return (
        "refusing to click: the cursor was lost during the move, so its "
        f"position is unknown — {result.get('lost_reason', 'no reason given')}. "
        f"Last measured at {result.get('guest_coords')}. Take a screenshot "
        "to see what the pointer turned into, or pass allow_unverified=True "
        "to click there regardless."
    )


async def _goto(vm: str, gx: int, gy: int) -> dict[str, Any]:
    """Closed-loop move to a guest pixel, falling back to dead reckoning.

    The fallback matters for guests that composite the cursor outside the
    framebuffer (a hardware cursor plane) or hide it altogether: pointing
    still works, it is just open-loop and unverified — and the reply says
    which of the two happened rather than papering over it.
    """
    await _ensure_relative_pointer(vm)
    try:
        return {"method": "closed-loop", **await _closed_loop_goto(vm, gx, gy)}
    except pointer.PointerError as exc:
        gw, gh = _guest_screen(vm)
        await _mouse_goto_guest(vm, gx, gy, gw, gh)
        return {
            "method": "open-loop",
            "guest_coords": [gx, gy],
            "requested": [gx, gy],
            "on_target": None,  # unverifiable without a visible cursor
            "residual": None,
            "calibration": list(mouse.read_calibration(vm)),
            "fallback_reason": str(exc),
        }


@mcp.tool()
async def mouse_drag(
    vm: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    button: str = "left",
    allow_unverified: bool = False,
) -> dict[str, Any]:
    """Drag from (x1, y1) to (x2, y2) in the LAST screenshot's pixel space.

    The grab point is reached closed-loop and verified before the button
    goes down — that is the end that has to be exact, since it decides
    what gets picked up. The travel itself is paced (old GUIs sample the
    motion; instant jumps break drag detection) and the drop point is
    measured afterwards when the cursor is still recognisable: dragging
    often swaps the cursor for an outline or a ghost icon, and `drop` then
    reports what could actually be verified.
    """
    canonical = _resolve(vm)
    mouse.validate_button(button)
    await _guard_coordinate_space(canonical)
    gx1, gy1 = mouse.image_to_guest(x1, y1, _LAST_SCREEN.get(canonical))
    gx2, gy2 = mouse.image_to_guest(x2, y2, _LAST_SCREEN.get(canonical))
    grab = await _goto(canonical, gx1, gy1)
    problem = unverified_click(grab)
    if problem and not allow_unverified:
        # Worse than a stray click: the button goes down and whatever is
        # under it gets dragged somewhere else.
        raise ValueError(problem)
    gain = _GAIN.get(canonical, DEFAULT_GAIN)
    dx, dy = pointer.correction((gx2, gy2), (gx1, gy1), gain)
    await _send_input(canonical, _btn(button, True))
    try:
        await asyncio.sleep(0.15)  # let the guest recognize the grab
        for sx, sy in mouse.plan_moves(dx, dy):
            await _send_input(canonical, _rel(sx, sy))
            await asyncio.sleep(0.018)
        await asyncio.sleep(0.15)  # settle before dropping
        await _refine_drag(canonical, gx2, gy2)
    finally:
        await _send_input(canonical, _btn(button, False))
    # Measured after the release, not before: while the button is down the
    # guest shows a drag outline or ghost icon in place of the cursor and
    # there is nothing recognisable to find. Letting go restores the arrow,
    # and the pointer does not move, so the reading still describes the
    # drop point.
    await asyncio.sleep(0.25)
    drop = await _verify_drop(canonical, gx2, gy2)
    return {
        "grab": grab,
        "drop": drop,
        "guest_from": [gx1, gy1],
        "guest_to": [gx2, gy2],
        "button": button,
    }


async def _refine_drag(vm: str, gx: int, gy: int) -> None:
    """Steer onto the drop point while the button is still held.

    Only some guests allow this. Classic Mac OS drags a window as an
    outline and leaves the arrow alone, so the loop can still see what it
    is steering — worth doing, because its damped scaling made an
    uncorrected drag land 62 px wide of a drop point in one measurement.
    Windows 3.11 swaps the cursor for a ghost icon instead; there is then
    nothing to recognise, and the open-loop travel simply stands.
    """
    gain = _GAIN.get(vm, DEFAULT_GAIN)
    for _ in range(DRAG_REFINE_ATTEMPTS):
        frame = await _capture(vm)
        found = _locate_known(vm, frame)
        if found is None:
            return  # cursor replaced by a drag image: nothing to steer by
        sprite, position = found
        hotspot = pointer.hotspot_of(position, sprite)
        if (hotspot[0], hotspot[1]) == (gx, gy):
            return
        command = pointer.clamp_command(
            pointer.correction((gx, gy), hotspot, gain),
            position,
            sprite,
            frame.width,
            frame.height,
            gain,
        )
        if command == (0, 0):
            return
        for sx, sy in mouse.plan_moves(*command):
            await _send_input(vm, _rel(sx, sy))
            await asyncio.sleep(0.018)
        await asyncio.sleep(0.12)


async def _verify_drop(vm: str, gx: int, gy: int) -> dict[str, Any]:
    """Measure where the pointer ended up, just after the button came up.

    Deliberately does not try to *correct* the position: the drop has
    already happened, so moving now would only misreport it.
    """
    try:
        found = _locate_known(vm, await _capture(vm))
    except Exception as exc:  # a capture failure must not swallow the drop
        return {"verified": False, "reason": str(exc)}
    if found is None:
        return {
            "verified": False,
            "reason": "cursor not recognisable mid-drag (guests often swap "
            "it for a drag outline or ghost icon)",
        }
    sprite, position = found
    hotspot = pointer.hotspot_of(position, sprite)
    return {
        "verified": True,
        "guest_coords": list(hotspot),
        "residual": [gx - hotspot[0], gy - hotspot[1]],
    }


async def _ensure_relative_pointer(vm: str) -> None:
    """Route QEMU input to a relative pointer when an absolute tablet is
    hogging it — retro guests have no tablet driver and lose our events."""
    mice = await _qmp.execute(vm, "query-mice")
    index = mouse.pick_relative_mouse(mice)
    if index is not None:
        await _qmp.execute(
            vm, "human-monitor-command", {"command-line": f"mouse_set {index}"}
        )


async def _corner_reset(vm: str, guest_width: int, guest_height: int) -> None:
    for sx, sy in mouse.plan_reset(guest_width, guest_height):
        await _send_input(vm, _rel(sx, sy))
        await asyncio.sleep(0.010)
    await asyncio.sleep(0.30)


async def _mouse_goto_guest(
    vm: str,
    gx: int,
    gy: int,
    guest_width: int | None = None,
    guest_height: int | None = None,
) -> None:
    """Anchor at the top-left corner, then dead-reckon to guest (gx, gy)."""
    await _ensure_relative_pointer(vm)
    calibration = mouse.read_calibration(vm)
    # The reset drains before the precise walk: anything sent while a
    # backlog remains is simply added to it.
    await _corner_reset(vm, guest_width or 1024, guest_height or 768)
    dx, dy = mouse.apply_calibration(gx, gy, calibration)
    for sx, sy in mouse.plan_moves(dx, dy):
        await _send_input(vm, _rel(sx, sy))
        await asyncio.sleep(0.012)
    await asyncio.sleep(0.10)


def _guest_screen(vm: str) -> tuple[int | None, int | None]:
    """Guest framebuffer size from the last screenshot, if any."""
    info = _LAST_SCREEN.get(vm)
    if not info:
        return None, None
    scale = info.get("scale", 1) or 1
    return info["width"] // scale, info["height"] // scale


async def _mouse_goto(vm: str, x: int, y: int) -> tuple[int, int]:
    """Goto for image-space coordinates (last screenshot's pixel space)."""
    gx, gy = mouse.image_to_guest(x, y, _LAST_SCREEN.get(vm))
    gw, gh = _guest_screen(vm)
    await _mouse_goto_guest(vm, gx, gy, gw, gh)
    return gx, gy


async def _click(vm: str, button: str, double: bool) -> None:
    for i in range(2 if double else 1):
        if i:
            await asyncio.sleep(0.09)  # well inside retro double-click time
        await _send_input(vm, _btn(button, True))
        await asyncio.sleep(0.06)
        await _send_input(vm, _btn(button, False))


async def _text_screen_state(vm: str) -> tuple[dict[str, Any], str, list[int]]:
    """Video state, characters and attributes of an x86 text-mode screen."""
    target = await _qmp.execute(vm, "query-target")
    if target.get("arch") not in ("i386", "x86_64"):
        raise ValueError(
            f"text-mode reading is x86-only and this VM is "
            f"{target.get('arch', 'unknown')!r}; use screenshot instead"
        )
    bda_raw = await _dump_to_bytes(
        vm, "pmemsave", {"val": 0x400, "size": 0x100}, ".bin"
    )
    bda = screen.parse_bda(bda_raw)
    if bda["mode"] not in screen.TEXT_MODES:
        raise ValueError(
            f"guest is in graphics mode 0x{bda['mode']:02X}; use screenshot"
        )
    raw = await _dump_to_bytes(
        vm,
        "pmemsave",
        {
            "val": screen.text_buffer_address(bda),
            "size": bda["columns"] * bda["rows"] * 2,
        },
        ".bin",
    )
    text = screen.decode_text_buffer(raw, bda["columns"], bda["rows"])
    attrs = screen.decode_attributes(raw, bda["columns"], bda["rows"])
    return bda, text, attrs


@mcp.tool()
async def read_shortcuts(vm: str) -> dict[str, Any]:
    """List the keyboard shortcuts currently offered on a text-mode screen.

    Two independent sources, both read straight from video memory:
    - accelerators: the highlighted letter of each visible label (the F
      of "File", the x of "Exit"), detected from the attribute bytes;
    - key hints: what the application spells out in its status bar
      ("F1=Help", "Esc=Cancel").

    Use this to drive DOS/OS2 applications from the keyboard, which is
    exact — unlike pointing, which depends on the guest's mouse driver.
    Call it again after the screen changes: an open menu advertises a
    different set.
    """
    canonical = _resolve(vm)
    bda, text, attrs = await _text_screen_state(canonical)
    return {
        "accelerators": screen.find_accelerators(text, attrs, bda["columns"]),
        "key_hints": screen.parse_key_hints(text),
    }


@mcp.tool()
async def activate_menu(
    vm: str, menu: str, item: str | None = None, settle_ms: int = 250
) -> dict[str, Any]:
    """Open a menu (and optionally choose an item) by keyboard.

    Finds the accelerator letters the application highlights on screen,
    presses Alt+<letter> for the menu, then the item's own letter — the
    deterministic equivalent of clicking, with no pointer involved.

    Some applications only highlight their accelerator letters once Alt
    has been pressed (MS-DOS EDIT does; Turbo Vision shows them always),
    so when none are visible this presses Alt and looks again.

    Args:
        vm: VM name or UUID.
        menu: menu title as shown, e.g. "File" (case-insensitive).
        item: item to choose once the menu is open, e.g. "Save As".
        settle_ms: pause after opening the menu before reading it back.
    """
    canonical = _resolve(vm)

    def _match(accelerators: list[dict], label: str) -> dict | None:
        wanted = label.strip().lower()
        for accelerator in accelerators:
            if accelerator["label"].lower() == wanted:
                return accelerator
        for accelerator in accelerators:
            if accelerator["label"].lower().startswith(wanted):
                return accelerator
        return None

    async def _accelerators() -> list[dict]:
        bda, text, attrs = await _text_screen_state(canonical)
        return screen.find_accelerators(text, attrs, bda["columns"])

    async def _press(keys: list[str]) -> None:
        await _qmp.execute(
            canonical,
            "send-key",
            {
                "keys": [{"type": "qcode", "data": key} for key in keys],
                "hold-time": 100,
            },
        )

    accelerators = await _accelerators()
    found = _match(accelerators, menu)
    revealed = False
    if found is None:
        # The application may be hiding its accelerators until Alt.
        await _press(["alt"])
        await asyncio.sleep(settle_ms / 1000)
        accelerators = await _accelerators()
        found = _match(accelerators, menu)
        revealed = found is not None
    if found is None:
        raise ValueError(
            f"no menu {menu!r} on screen, with or without pressing Alt; "
            "visible labels: "
            + (", ".join(sorted({a["label"] for a in accelerators})[:12]) or "none")
        )
    await _press(["alt", found["key"].lower()])
    result: dict[str, Any] = {
        "menu": found["label"],
        "menu_key": f"alt-{found['key'].lower()}",
    }
    if revealed:
        result["accelerators_revealed_with_alt"] = True
    if item is None:
        return result

    await asyncio.sleep(settle_ms / 1000)
    accelerators = await _accelerators()
    chosen = _match(accelerators, item)
    if chosen is None:
        raise ValueError(
            f"no item {item!r} in the open menu; visible labels: "
            + (", ".join(sorted({a["label"] for a in accelerators})[:12]) or "none")
        )
    await _press([chosen["key"].lower()])
    result.update(item=chosen["label"], item_key=chosen["key"].lower())
    return result


def main() -> None:
    """Entry point: serve over stdio (what Claude Desktop expects)."""
    mcp.run()


if __name__ == "__main__":
    main()
