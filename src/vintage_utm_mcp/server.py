"""vintage-utm-mcp server.

Increment 1: VM lifecycle via utmctl.
Increment 2: QMP connectivity + check_vm diagnostics.
Next increments: removable media, keyboard/mouse, screenshot, text-mode
screen reading.
"""

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage

from vintage_utm_mcp import keyboard, qmp, screen, utmctl

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
    result.update(
        qmp_reachable=True,
        qemu_version=f"{q['major']}.{q['minor']}.{q['micro']}",
        guest_runstate=runstate.get("status"),
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
    if target.exists():
        if target.stat().st_size == path.stat().st_size:
            return target  # same name and size: assume it's the same image
        raise ValueError(
            f"a different image named {path.name!r} already exists in the "
            f"library ({target}); rename one of them"
        )
    clone = subprocess.run(
        ["cp", "-c", str(path), str(target)], capture_output=True
    )
    if clone.returncode != 0:  # non-APFS volume or non-macOS: plain copy
        shutil.copy2(path, target)
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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
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
    return await _drive_state(canonical, drive)


@mcp.tool()
async def eject_media(vm: str, drive: str, force: bool = False) -> dict[str, Any]:
    """Eject the medium from a removable drive.

    Args:
        vm: VM name or UUID.
        drive: drive identifier from list_drives.
        force: eject even if the guest locked the medium. DOS never locks
            floppies, but classic Mac OS locks its media — use force there.
    """
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
    await _blockdev(canonical, "eject", drive, {"force": force})
    return await _drive_state(canonical, drive)


@mcp.tool()
async def type_text(
    vm: str, text: str, delay_ms: int = 60, layout: str = "us"
) -> dict[str, Any]:
    """Type text into the VM as raw keystrokes (works in DOS, BIOS, GUIs).

    Args:
        vm: VM name or UUID.
        text: text to type; "\\n" presses Enter. Injected as US-layout
            keystrokes: keep the guest on its default US mapping (in DOS,
            don't load KEYB) or punctuation will come out wrong.
        delay_ms: pause between characters. The default 60 is safe for
            DOS-era guests, which drop keys during disk I/O if flooded;
            raise it if characters go missing, lower it for bulk typing.
        layout: only "us" for now.
    """
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
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
    return {"typed_chars": len(chords)}


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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
    target = await _qmp.execute(canonical, "query-target")
    arch = target.get("arch", "unknown")
    if arch not in ("i386", "x86_64"):
        raise ValueError(
            f"text-screen reading is x86-only and this VM is {arch!r}; "
            "use screenshot instead"
        )
    bda_raw = await _dump_to_bytes(
        canonical, "pmemsave", {"val": 0x400, "size": 0x100}, ".bin"
    )
    bda = screen.parse_bda(bda_raw)
    if bda["mode"] not in screen.TEXT_MODES:
        raise ValueError(
            f"guest is in graphics mode 0x{bda['mode']:02X}; "
            "use screenshot instead"
        )
    size = bda["columns"] * bda["rows"] * 2
    buffer = await _dump_to_bytes(
        canonical,
        "pmemsave",
        {"val": screen.text_buffer_address(bda), "size": size},
        ".bin",
    )
    text = screen.decode_text_buffer(buffer, bda["columns"], bda["rows"])
    return {
        "text": text,
        "columns": bda["columns"],
        "rows": bda["rows"],
        "mode": bda["mode"],
    }


def main() -> None:
    """Entry point: serve over stdio (what Claude Desktop expects)."""
    mcp.run()


if __name__ == "__main__":
    main()
