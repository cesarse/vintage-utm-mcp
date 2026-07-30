# vintage-utm-mcp

An MCP server for handling vintage operating systems (like MS-DOS,
OS/2, Windows 3.x and classic Mac OS) running in
[UTM](https://mac.getutm.app/) on macOS. Designed for guests that
cannot run a guest agent: everything goes through utmctl (lifecycle)
and QMP (in-guest interaction).

## Requirements

- macOS with UTM installed
- `utmctl` on PATH (symlink from `/Applications/UTM.app/Contents/MacOS/utmctl`)
- [uv](https://docs.astral.sh/uv/)

## Install

```sh
uv sync
```

## Run manually (debug)

```sh
uv run vintage-utm-mcp          # stdio server
uv run mcp dev src/vintage_utm_mcp/server.py   # browser inspector
```

## Register in Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vintage-utm": {
      "command": "/Users/YOUR_USER/.local/bin/uv",
      "args": ["run", "--directory", "/path/to/vintage-utm-mcp", "vintage-utm-mcp"]
    }
  }
}
```

> Use the **absolute** path to `uv` (a curl install lives in
> `~/.local/bin/uv`): apps launched by launchd do not inherit your
> shell's PATH. Confirm with `which uv`.

On first use, macOS will ask for an **Automation** permission
(Claude → UTM). Accept it.

## QMP setup (once per VM)

In-guest tools (media, input, screenshots) talk QMP. For each VM, add
this to VM settings → QEMU → Arguments, then restart the VM:

```
-qmp tcp:127.0.0.1:<port>,server,nowait
```

The `check_vm` tool prints the exact string, port included (ports are
deterministic per VM name). Keep `127.0.0.1`: QMP has no
authentication, and the short form `tcp::port` would listen on every
network interface. Loopback TCP is used instead of a unix socket
because UTM's sandbox denies binding sockets outside its container.

Per-VM overrides (e.g. after renaming a VM) go in `~/.vintage-utm-mcp/qmp.toml`:

```toml
[vms]
"MS-DOS 6.22" = "tcp:127.0.0.1:4501"
```

## Media library

QEMU runs inside the QEMUHelper XPC service, which has its own App
Sandbox separate from UTM's: files UTM knows about are granted one by
one via security-scoped bookmarks, and any other path fails with
"Operation not permitted" — even inside UTM's own container. The one
location QEMU can always read is the helper's container, so that is
where the media library lives (created on demand by the server):

```
~/Library/Containers/com.utmapp.QEMUHelper/Data/Documents/vintage-utm-mcp/
```

You never need to copy anything there manually: `insert_media` accepts
any host path (e.g. `~/Downloads/Disk4.img`) and stages it into the
library automatically — an instant copy-on-write APFS clone. Guest
writes go to the staged copy, not your original. Bare filenames resolve
against the library directly.

Runtime truths worth knowing: `list_drives` (which asks QEMU) is the
source of truth, not UTM's menu — UTM does not observe out-of-band QMP
changes, so its UI will not show media mounted by these tools. QMP
mounts are also ephemeral: a VM restart rebuilds QEMU's command line
from UTM's own config, ejecting whatever these tools inserted. After
`suspend --save-state` + restart, the guest may believe a disk is
inserted when it is not — re-insert it. Avoid mixing GUI mounts and
tool mounts on the same drive in one session. Drive identifiers look like
`drive<UUID>` — that is how UTM names QEMU drives; use them exactly as
returned by `list_drives`.

## Tools

| Tool | Description |
|---|---|
| `list_vms` | List VMs with name, UUID and status |
| `start_vm(vm, disposable=false)` | Start (or resume); `disposable` runs as a snapshot without saving changes |
| `stop_vm(vm, mode=force\|request\|kill)` | Stop the VM |
| `suspend_vm(vm, save_state=false)` | Suspend to memory |
| `check_vm(vm)` | Existence, status and QMP diagnostics (with setup hint) |
| `list_drives(vm)` | Block devices, removable first |
| `insert_media(vm, drive, image_path, image_format=raw)` | Insert an image while running |
| `eject_media(vm, drive, force=false)` | Eject; `force` for guests that lock media (classic Mac OS) |
| `type_text(vm, text, delay_ms=60, layout=us)` | Type text as raw keystrokes (US layout) |
| `send_keys(vm, combo, hold_ms=100)` | Press a chord: "ctrl-alt-delete", "F8", "enter" |
| `screenshot(vm, delay_ms=0, scale=0)` | PNG of the guest framebuffer; retro resolutions auto-upscaled 2× |
| `read_text_screen(vm)` | Exact characters of an x86 text-mode screen, read from video memory |
| `mouse_locate(vm)` | Where the cursor is, read out of the framebuffer (diagnostic) |
| `mouse_click(vm, x, y, button=left, double=false)` | Click at last-screenshot coordinates, closed loop; reports `on_target` and the residual |
| `mouse_drag(vm, x1, y1, x2, y2, button=left)` | Drag: grab verified before the button goes down, drop steered and measured |
| `read_shortcuts(vm)` | Keyboard shortcuts offered by the current text-mode screen: highlighted accelerators + status-bar hints |
| `activate_menu(vm, menu, item=None)` | Open a menu and pick an item by their own accelerator keys |

## Coordinates

All mouse tools take coordinates in the pixel space of the **last
screenshot** you took, not the guest's. Origin `(0,0)` is the top-left
corner, x grows right, y grows down. Retro resolutions come back
upscaled (usually 2×), so a 640×480 guest yields a 1280×960 image and
its center is around `(640, 480)` — the server divides by the recorded
scale for you.

## Notes

- `utmctl` does not work over SSH or before login (macOS AppleEvents
  limitation).
- `stop_vm` defaults to `force` (power-button semantics): DOS-era
  guests do not answer ACPI requests. Stop at an idle `C:\>` prompt and
  mind SMARTDRV's write-behind cache.
- Mouse motion works over a relative pointer. Movement is always paced —
  PS/2 carries at most 127 units per packet, so one huge delta leaves a
  backlog that drains for seconds and swallows everything sent meanwhile
  (every click then lands at 0,0).
- In graphics modes the pointer is **closed loop**: QEMU's `screendump`
  captures the cursor (Windows 3.11's driver draws it into video memory
  as an opaque sprite), so the tools find it, move it, and measure it
  again until it is on target. The sprite is learned once per shape from
  a single probe move and cached per VM; after that one capture locates
  it in well under a millisecond. There is no calibration file to tune —
  the gain is measured from what actually happened. What the guest does,
  as measured on Windows 3.11 at 640x480:
  - relative motion is **piecewise, not a single scale factor**: commands
    of 1 and 2 units move 1 and 2 pixels, 3 moves 6, and everything above
    stays doubled. Modelling only the doubling strands every odd target
    one pixel short, because a 1-pixel error divides to a 0-unit command;
  - the top-left clamp lands at **(0, 1)**, not (0, 0) — the guest cannot
    put the cursor on row 0, and `mouse_click` reports that as a residual
    rather than pretending it arrived;
  - the cursor is drawn **clipped** at the right and bottom edges (it
    extends down and right from its hotspot). Near a corner only a pixel
    or two remain on screen; the tools fall back to partial matching and,
    when too little is left to be sure, say the position is unverified
    instead of guessing.
- Classic Mac OS 9.2.2 (ppc, 800x600) was measured too, and agrees on
  nothing except that the cursor is in the framebuffer:
  - its arrow is 11x16, 97 pixels, coloured the other way round (black
    with a white border), and its image starts a pixel **above and left**
    of the hotspot — so every edge clips it, not just the right and
    bottom;
  - motion is **damped, not amplified**: about 0.6 mid-range, rising past
    1.1 as the move lengthens, with the leftover fractions accumulated.
    Single units often move nothing at all. No one scale factor fits, so
    a gain fitted on short moves badly overshoots a long one — which is
    why corrections are shortened so the cursor can never be driven off
    screen, and why the last few pixels are closed by undershooting;
  - its desktop is a **tiled watermark**, so no part of it is flat. The
    two-frame learner has nothing to reason about there, and a
    three-frame one takes over: across two moves the same way, the
    cursor's position in the middle frame is the only region that changed
    in both steps. That identifies it without knowing what it sits on, at
    the cost of a partial mask — which still matches anywhere, because
    the sprite is opaque.
- Learning anchors at the top-left corner, then steps inward before
  probing: the corner is deterministic but it is also where a cursor gets
  cut short, and a sprite learned a row shy reports every later position
  off by one. Things that legitimately break learning: a blinking text
  caret (the Run dialog's shows up as a third changed region), an
  animating guest, or a cursor the framebuffer does not carry at all.
  Guests in that last category still work; pointing falls back to
  open-loop dead reckoning and says so in the reply's `method`.
- Dragging is verified to move real objects: a Program Manager group icon
  in Windows 3.11, a Finder window in Mac OS 9. The grab point is reached
  closed loop and checked before the button goes down, because that end
  decides what gets picked up. What happens next depends on the guest:
  - Mac OS 9 drags a window as an outline and leaves the arrow alone, so
    the pointer stays recognisable and the tools keep steering while the
    button is held. That matters — an uncorrected drag landed **62 px**
    wide of its drop point, and correcting brought it to 1 px;
  - Windows 3.11 replaces the cursor with a ghost icon, so mid-drag there
    is blind and the paced open-loop travel stands (its exact 2.0 gain
    makes that land on the pixel anyway).
  Either way the drop is measured **after** the button is released, when
  the ordinary cursor is back, and reported as `drop.residual`.
- The cursor's **hotspot is assumed**, not measured: it cannot be read
  out of the framebuffer. (0, 0) is right for the standard arrow, whose
  tip is its top-left corner, but a centred cursor (crosshair, I-beam)
  would need a different value.
- Text-mode tools adapt to the screen geometry rather than assuming
  80x25: every call reads the current mode, column count, row count and
  active page from the BIOS Data Area, so 40-column modes, EGA 80x43,
  VGA 80x50 and the monochrome adapter at 0xB0000 all work. VESA text
  modes are not recognised and are reported as graphics.
- Drive text-mode screens from the keyboard, not the pointer.
  `read_shortcuts` and `activate_menu` are exact: they read the letters
  the application itself highlights in video memory and press them.
  Pointing there was tried and dropped — the DOS driver's 640x200
  virtual screen, PS/2 2:1 scaling and pointer acceleration make
  positioning approximate at best, and applications hide the pointer
  while you type. `mouse_click` and `mouse_drag` remain for graphics
  modes and GUIs (Windows 3.x, classic Mac OS), where no cell grid
  exists.
- QEMU accepts a single QMP client per endpoint; the server keeps one
  persistent connection per VM and reconnects after VM restarts.

## Roadmap

1. ~~Lifecycle (utmctl)~~
2. ~~QMP module + `check_vm`~~
3. ~~Removable media: `list_drives`, `insert_media`, `eject_media`~~
4. ~~Input, keyboard: `type_text`, `send_keys`~~
5. ~~Output: `screenshot` (2× upscale), `read_text_screen` (video memory
   via BIOS Data Area: mode/columns/rows/page-aware, color and mono)~~
6. ~~Mouse: `mouse_click`, `mouse_drag`, `mouse_click_text_screen`
   (relative pointer + per-VM calibration in the `[mouse]` config
   table; DOS usually needs y = 2.0)~~
7. ~~Closed-loop text-mode pointer~~ — built, measured, then dropped:
   keyboard accelerators are exact where pointing is approximate
8. ~~Keyboard-first text mode: `read_shortcuts`, `activate_menu`~~ —
   validated on MS-DOS EDIT and Turbo Pascal 7; accelerators are revealed
   with Alt when an application hides them, and status-bar hints are
   parsed in all three shapes seen in the wild
9. ~~Closed-loop pointer for graphics modes: `mouse_locate`, plus
   `mouse_click`/`mouse_drag` rebuilt on measured feedback~~ — Windows
   3.11 lands exactly, odd pixels included, in one or two iterations. Mac
   OS 9.2.2 is verified working but less precise: it tracks the cursor
   without losing it and lands exactly on some targets, within a pixel or
   two on the rest, because its damped, speed-dependent scaling makes the
   final pixel genuinely hard. Both report the residual rather than
   claiming success
10. Sharpen the Mac OS 9 endgame: a per-regime gain (its ratio for short
   moves is nothing like its ratio for long ones) instead of one estimate
   that swings between 0.5 and 1.5
11. `detect_layout` calibration probe: type ambiguous characters, read
   them back from the screen, deduce the guest's active mapping
   (depends on 5)
12. Known debt: migrate to MCP SDK 2.x (`mcp.server.fastmcp` was removed
   in favour of `mcp.server.mcpserver`); the dependency is pinned `<2`
13. Known debt: media library provenance manifest (name-collision handling
   currently relies on file size, which is blind to fixed-size floppies)
14. Known debt: the `[mouse]` calibration table is now dead weight for
   graphics-mode guests that support the closed loop; it still backs the
   open-loop fallback

## Tests

```sh
uv run pytest
```
