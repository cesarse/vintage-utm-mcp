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

from vintage_utm_mcp import keyboard, mouse, qmp, screen, utmctl

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
    bda, text, _ = await _text_screen_state(canonical)
    return {
        "text": text,
        "columns": bda["columns"],
        "rows": bda["rows"],
        "mode": bda["mode"],
    }


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


@mcp.tool()
async def mouse_click(
    vm: str, x: int, y: int, button: str = "left", double: bool = False
) -> dict[str, Any]:
    """Click at (x, y) in the LAST screenshot's pixel space.

    Take a screenshot first and aim on it; coordinates are automatically
    mapped back to guest pixels (screenshots are usually 2x upscaled).
    The pointer is re-anchored at the top-left corner before every click,
    so driver drift never accumulates. If clicks land offset (DOS's
    default driver halves vertical motion), set per-VM factors in the
    [mouse] table of ~/.vintage-utm-mcp/qmp.toml (y = 2.0 is the usual
    DOS fix) — verify with a follow-up screenshot. Text-mode screens are
    better driven from the keyboard: see read_shortcuts and
    activate_menu, which are exact where pointing is approximate.

    Args:
        vm: VM name or UUID.
        x, y: target in last-screenshot pixels.
        button: "left", "right" or "middle".
        double: double-click.
    """
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
    mouse.validate_button(button)
    gx, gy = await _mouse_goto(canonical, x, y)
    await _click(canonical, button, double)
    return {
        "guest_coords": [gx, gy],
        "button": button,
        "double": double,
        "calibration": list(mouse.read_calibration(canonical)),
        "coordinate_space": (
            f"last screenshot (scale {_LAST_SCREEN[canonical]['scale']}x)"
            if canonical in _LAST_SCREEN
            else "guest-native (no screenshot cached since server start)"
        ),
    }


@mcp.tool()
async def mouse_drag(
    vm: str, x1: int, y1: int, x2: int, y2: int, button: str = "left"
) -> dict[str, Any]:
    """Drag from (x1, y1) to (x2, y2) in the LAST screenshot's pixel space.

    Presses at the start point, walks to the end point in small paced
    steps (old GUIs need to sample the motion — instant jumps break drag
    detection), and releases. Calibration errors offset the DROP point,
    so verify with a screenshot; see mouse_click for the calibration file.
    """
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
    mouse.validate_button(button)
    gx1, gy1 = await _mouse_goto(canonical, x1, y1)
    gx2, gy2 = mouse.image_to_guest(x2, y2, _LAST_SCREEN.get(canonical))
    calibration = mouse.read_calibration(canonical)
    dx, dy = mouse.apply_calibration(gx2 - gx1, gy2 - gy1, calibration)
    await _send_input(canonical, _btn(button, True))
    try:
        await asyncio.sleep(0.15)  # let the guest recognize the grab
        for sx, sy in mouse.plan_moves(dx, dy):
            await _send_input(canonical, _rel(sx, sy))
            await asyncio.sleep(0.018)
        await asyncio.sleep(0.15)  # settle before dropping
    finally:
        await _send_input(canonical, _btn(button, False))
    return {
        "guest_from": [gx1, gy1],
        "guest_to": [gx2, gy2],
        "button": button,
        "calibration": list(calibration),
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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)
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
    entry = _find_vm(vm)
    canonical = _canonical(vm, entry)

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
