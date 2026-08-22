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

Images are identified by content, not by name and size. Staging a file
whose name is already taken compares digests: the same image is reused,
a different one is refused by name. That distinction matters more than
it sounds — every 1.44 MB floppy image is exactly 1474560 bytes, so size
cannot tell two of them apart. A `.provenance.json` in the library
records each image's digest and where it was staged from, and
`insert_media` reports it back.

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
| `type_text(vm, text, delay_ms=60, layout=us)` | Type text as raw keystrokes; `us-intl` for dead-key guests, `mac-option` for Mac accents |
| `send_keys(vm, combo, hold_ms=100)` | Press a chord: "ctrl-alt-delete", "F8", "enter" |
| `screenshot(vm, delay_ms=0, scale=0)` | PNG of the guest framebuffer; retro resolutions auto-upscaled 2× |
| `read_text_screen(vm)` | Exact characters of an x86 text-mode screen, read from video memory |
| `detect_layout(vm)` | Press the keys that move between keyboard layouts and read back what the guest produced |
| `mouse_locate(vm)` | Where the cursor is, read out of the framebuffer (diagnostic) |
| `mouse_click(vm, x, y, button=left, double=false, allow_unverified=false)` | Click at last-screenshot coordinates, closed loop; refuses if it lost the cursor |
| `mouse_drag(vm, x1, y1, x2, y2, button=left)` | Drag: grab verified before the button goes down, drop steered and measured |
| `read_shortcuts(vm)` | Keyboard shortcuts offered by the current text-mode screen: highlighted accelerators + status-bar hints |
| `activate_menu(vm, menu, item=None)` | Open a menu and pick an item by their own accelerator keys |

## Coordinates

All mouse tools take coordinates in the pixel space of the **last
screenshot** you took, not the guest's. Origin `(0,0)` is the top-left
corner, x grows right, y grows down. Retro resolutions come back
upscaled (usually 2×), so a 640×480 guest yields a 1280×960 image and
its center is around `(640, 480)` — the server divides by the recorded
scale for you. The upscale is nearest-neighbour, so guest pixel `k`
becomes the whole image block `k*scale … k*scale+scale-1` and the mapping
back floors rather than rounds: every pixel of a block belongs to the
guest pixel it was drawn from, and the bottom-right pixel of a 2×
screenshot stays at `(639, 479)` instead of naming a `(640, 480)` that
does not exist.

Changing the guest's resolution needs nothing from you: every gesture
re-reads the live framebuffer, so screen bounds, the corner anchor and
the cursor itself all adapt, and a driver that draws a different cursor
is simply learned afresh (Windows 3.11's arrow is 12x20 at 640x480 and
11x20 at 1280x1024). The one thing that does **not** survive a mode
change is an old screenshot: its scale is baked into the coordinates you
aimed on. The mouse tools compare the live guest size against the last
screenshot's and refuse rather than act on a stale mapping — aiming at
(700, 500) on a stale 2× image lands at (350, 250), and the closed loop
would report that as on target, because it is on the target it was given.

## Notes

- `utmctl` does not work over SSH or before login (macOS AppleEvents
  limitation).
- `stop_vm` defaults to `force` (power-button semantics): DOS-era
  guests do not answer ACPI requests. Stop at an idle `C:\>` prompt and
  mind SMARTDRV's write-behind cache.
- **UTM's Retina Mode changes nothing the tools can see.** It is a host
  display setting, so it redraws the window — the guest looks tiny on a
  HiDPI screen — while the framebuffer QEMU hands to `screendump` stays
  as it was. Measured with it on for both VMs: the MS-DOS text screen
  came back **byte-identical** (same SHA-256 as the capture before it),
  Windows 3.11 stayed 640x480 captured at 1280x960 and its `mouse_click`
  repeated the earlier run exactly — 4 iterations, gain 2.0, residual
  (0, 0), the Main group selected — and Mac OS 9 stayed 800x600 and
  relearned the same 97-pixel sprite. The one real effect is that
  toggling it restarts the VM, so the guest loses whatever was on screen.
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
    with a white border), and clamping it into the top-left corner loses
    its top row where Windows 3.11's arrow keeps all of its — so every
    edge can clip it, not just the right and bottom. (An earlier note
    read that as the hotspot sitting a pixel in from the image corner;
    clicking at a known boundary says otherwise, so treat it as a
    clamping quirk rather than a hotspot offset.);
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
- Learning runs in two passes. The first anchors at the top-left corner,
  which is the only position knowable without already being able to see
  the cursor; the second steers to background chosen from the frame and
  learns the sprite properly, because corners are where guests keep their
  menu bars. A mask learned against furniture is a *subset* of the real
  sprite — it still finds the cursor, and its origin still matches (both
  guests reported the same position either way), but it is thinner than
  it needs to be and makes `mouse_locate`'s sprite size say more about
  where it learned than about the cursor. Things that legitimately break
  learning: a blinking text
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
- Pointing costs what the guest's own scaling costs. Windows 3.11 lands
  exactly in one or two passes, about 0.4 s a gesture. Classic Mac OS
  damps motion and banks the remainder, so the last pixel takes a median
  of seven one-unit nudges and a gesture runs nearer 3 s — exact, but
  patient. Driving anything long from the keyboard remains far quicker
  than pointing at it.
- `mouse_click` and `mouse_drag` **refuse to act when the loop lost the
  cursor** — a shape change over the target is the usual cause, and the
  position they would report is the last one actually measured, not where
  the pointer is now. Clicking there is a click somewhere unknown; a drag
  is worse, since the button goes down and takes whatever is under it. A
  verified-but-inexact landing still clicks: being a pixel out hits the
  button you aimed at. Pass `allow_unverified=true` to override, and note
  that the open-loop fallback is unaffected, since it never claimed to
  know where the cursor was.
- The cursor's **hotspot** cannot be read out of the framebuffer, so it
  is (0, 0) by default — confirmed on both guests by clicking either side
  of a boundary whose geometry the framebuffer shows, not by assumption.
  Override it per VM when a cursor points somewhere other than its own
  top-left corner:

  ```toml
  [hotspot."Mac OS 9.2.2 (ppc)"]
  x = 1
  y = 1
  ```

  Beware the tempting shortcut of reading the offset off the screen-edge
  clamp: it assumes the guest pins the *hotspot* to zero, which holds on
  Windows 3.11 and does not on Mac OS 9, where it claims (1, 1) and
  clicking proves otherwise. (0, 0) is right for the standard arrow, whose
  tip is its top-left corner, but a centred cursor (crosshair, I-beam)
  would need a different value.
- **Mac guests reach accents without any dead-key layout**, because Mac
  OS puts them on Option chords that exist on the plain U.S. layout too:
  `type_text(..., layout="mac-option")` sends `option-e` then `a` for á,
  `option-n` then `o` for õ, and `option-c` for ç — measured on Mac OS
  9.2.2, and worth measuring, because `option-e` then `c` gives `´c`
  rather than `ç`. For a Mac you are automating this is the better
  arrangement: leave the guest on plain U.S. so `us` types ASCII exactly,
  and reach for `mac-option` when you need the accents.
- **US-International** guests need `type_text(..., layout="us-intl")`.
  Windows 3.11 uses it whenever `SYSTEM.INI` has
  `keyboard.dll=kbdusx.dll` — as this project's own guest does. There the
  apostrophe, quote, backtick, tilde and caret are dead: they emit
  nothing alone and combine with whatever follows. The failure is narrow
  enough to be treacherous — measured on a dead-key guest, `O'Neil` types
  correctly because acute has no form for N, while `d'agua` comes out
  `dágua`. The `us-intl` layout sends a space after each accent to commit
  it, and composes the accented characters plain US cannot reach at all
  (`ç` is acute then c).
- `send-key` names keys by where they sit on a **US** board; the guest
  decides what character that position emits, and `type_text` implements
  US only. Letters and digits survive any Latin layout, so a national
  driver hides itself until punctuation comes out wrong — run
  `detect_layout` at an idle prompt to find out before it bites. It needs
  an x86 text-mode screen, types onto the current line and clears it
  afterwards, and never presses Enter.
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
- Resolving a VM name is cached for 30 seconds (`utmctl.IDENTITY_TTL`).
  Only the name/UUID mapping is cached, never a VM's status: nothing
  `utmctl` can do changes identity, so `start`/`stop`/`suspend` need no
  invalidation, and `check_vm` always reads status fresh. The one
  staleness window is a VM renamed in UTM's own interface, which keeps
  resolving to its old name until the index expires — the symptom is an
  unreachable QMP endpoint, not a wrong action.
- When a call takes implausibly long (a *single* screenshot blocking for
  minutes), suspect an unanswered macOS permission dialog before
  suspecting the code. QEMU writes screendumps into its own container and
  the server reads them back, so a folder-access prompt stalls exactly
  that path, and an unanswered dialog is indistinguishable from a hang.
  Time a fresh call with a short watchdog to tell the two apart.

## Roadmap

1. ~~Lifecycle (utmctl)~~
2. ~~QMP module + `check_vm`~~
3. ~~Removable media: `list_drives`, `insert_media`, `eject_media`~~
4. ~~Input, keyboard: `type_text`, `send_keys`~~
5. ~~Output: `screenshot` (2× upscale), `read_text_screen` (video memory
   via BIOS Data Area: mode/columns/rows/page-aware, color and mono)~~
6. ~~Mouse: `mouse_click`, `mouse_drag`, `mouse_click_text_screen`
   (relative pointer + per-VM calibration in the `[mouse]` config
   table; DOS usually needs y = 2.0)~~ — the `[mouse]` table was later
   removed in 16; the closed loop measures the scaling instead
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
10. ~~Cache the `utmctl` VM lookup~~ — every tool call used to spawn
   `utmctl list` (217 ms median) just to turn a name into the same name.
   Measured on a clean baseline first, then A/B in one process against
   the same guest: `screenshot` 253 → **31 ms**, `read_text_screen`
   232 → **3.4 ms** (it was 98% subprocess overhead). Only the name/UUID
   index is cached, never status — nothing utmctl does changes identity,
   so the lifecycle tools need no invalidation, and `check_vm` still
   reads status live (verified: it flipped to `stopped` the moment the VM
   was stopped, while the cached name kept resolving)
11. ~~Sharpen the Mac OS 9 endgame~~ — by raising the stall tolerance,
   not by modelling the gain better. Profiling 60 one-unit commands gave
   the constant: classic Mac OS needs a **median of 7 commands per pixel**
   and stalls in runs of 5, 6 (six times) and 7 before its banked
   fraction pays out, so `GOTO_MAX_STALLS = 3` was abandoning moves that
   were still converging. Raised to 10, with `GOTO_MAX_ITERATIONS` 14 → 24
   so the endgame can actually finish. Mac OS 9 went **2/6 → 6/6 targets
   exact, worst error 2 px → 0**, at 2.0 → 2.9 s per gesture; Windows
   3.11 was unchanged to the millisecond (427 → 428 ms, 5/6 exact) because
   it lands in one or two passes and never stalls. The tempting shortcut —
   command 7 units instead of one — does not work: a single 8-unit command
   moves 4 px where eight 1-unit commands move 1, because the guest
   accelerates per event rather than scaling a total
12. ~~Choose the sprite-learning spot by looking at the screen~~ —
   learning now runs twice, because the two requirements pull apart.
   Starting needs a position knowable without seeing the cursor, and only
   a screen corner gives that; corners are also where guests park menu
   bars and window frames, so the mask learned there is whatever happened
   to contrast with the furniture. Once a rough sprite exists the pointer
   can be steered, so a second pass moves to background picked out of the
   frame — scored by distance from *both* black and white, since a sprite
   pixel only registers when it differs from what it covers — and learns
   the cursor properly. Both guests now learn their full sprite:
   Windows 3.11 **78 → 126 px**, Mac OS 9 **85 → 97 px**. Costs one extra
   gesture the first time a VM is touched (~1.4 s and ~2.4 s), and is
   best-effort: with no clear area, the bootstrap mask stands
13. ~~Measure or configure the cursor hotspot~~ — it is now *tested*
   rather than assumed, and (0, 0) survives on both guests. Two methods
   disagreed and that turned out to be the finding: reading the offset
   off the screen-edge clamp says (0, 0) for Windows 3.11 but (1, 1) for
   Mac OS 9, while clicking at a boundary says (0, 0) for both — driving
   the pointer to row 19 opens Mac's 20-row menu bar and row 20 does not,
   exactly as a zero offset predicts. Where they disagree the test that
   measures where clicks *land* wins, so the clamp reading is offered by
   `measure_hotspot` as a diagnostic and never steers anything. A cursor
   that really does point somewhere else — a crosshair, an I-beam — can
   be corrected per VM in a `[hotspot]` config table
14. ~~`detect_layout` calibration probe~~ — presses the keys that move
   between layouts and reads back what the guest actually produced, so
   the answer comes from the guest instead of a guess. Verified both
   ways on MS-DOS 6.22: untouched it reports `us` with zero mismatches,
   and after `KEYB BR` it stops matching and names the two keys that went
   dead (ABNT2's acute and grave accents emit nothing until the next
   keystroke). Results are per key, not a joined string — a dead key
   would otherwise shift every character after it and look like a dozen
   differences instead of one. Only `us` has a stored signature, because
   a signature copied out of documentation rather than measured on a
   guest would defeat the point
15. ~~Media library provenance manifest~~ — staging now compares a
   digest instead of a size, which was a wrong-image bug rather than
   debt: **every** 1.44 MB floppy is exactly 1474560 bytes, as both
   images in the real library are, so a different `Disk1.img` was
   silently ignored and the guest booted the one already staged. A
   `.provenance.json` beside the images records each one's digest,
   size, mtime and where it came from, so a large ISO is not re-read on
   every insert — but a file changed behind the tool's back is, since
   the manifest is only trusted while size and mtime still match it.
   Hashing a floppy costs 2–3 ms
16. ~~Retire the `[mouse]` calibration table~~ — gone, along with
   `read_calibration`, `apply_calibration` and the `calibration` field
   the open-loop fallback used to report. The closed loop measures what
   the table used to declare, and across two guests and four resolution
   changes the fallback never once fired. The dead-reckoning walk stays
   for guests whose cursor never reaches the framebuffer, but it no
   longer pretends a config knob can fix an unverifiable position — it
   reports `on_target: null` and says why. `~/.vintage-utm-mcp/qmp.toml`
   now carries only `[vms]` endpoints and the `[hotspot]` override
17. ~~Migrate to MCP SDK 2.x~~ — 2.0.0 shipped 2026-07-28 and the pin
    was `<2`. In `server.py` it was the rename the plan predicted:
    `mcp.server.fastmcp` → `mcp.server.mcpserver`, `FastMCP` →
    `MCPServer`, three lines, with `@mcp.tool()` and `mcp.run()`
    untouched and the constructor's first positional still `name` — the
    argument order worth reading about only bites a caller passing a
    second positional, which 1.x read as `instructions` and 2.x reads as
    `title`. `get_context()` never appeared here at all. The tests were
    the part the plan had wrong: 2.x returns a `CallToolResult` from
    `call_tool` where 1.x returned a content sequence or a
    `(content, structured)` tuple, and the wire models went snake_case —
    `mimeType` → `mime_type`, `inputSchema` → `input_schema` — so the
    image regression test asserted on an attribute that no longer exists
    and had to be rewritten rather than renamed. It now checks
    `is_error` first, because a serialization failure arrives as an error
    result whose content is text, and failing on that reads better than
    failing on a missing image. Verified against a running guest rather
    than by inspection: 164 tests green, 18 tools listed over a real
    stdio handshake (protocol 2025-11-25), `screenshot` returning a
    10 KB PNG of the MS-DOS boot screen, `type_text` + `send_keys`
    running `VER` and `read_text_screen` reading `MS-DOS Version 6.22`
    back out of video memory, `detect_layout` reporting `us` with zero
    mismatches, and a closed-loop `mouse_click` landing on Windows 3.11
    with residual (0, 0) in 4 iterations. Mac OS 9.2.2 was checked as
    well, because ppc exercises different paths: `screenshot` returned
    the Finder at 800x600, `read_text_screen` and `detect_layout` both
    refused with "text-mode reading is x86-only and this VM is 'ppc'"
    rather than guessing, `mouse_locate` learned the same 97-pixel
    sprite item 12 recorded, and two clicks landed with residual (0, 0)
    in 21 and 22 iterations — the second, on empty desktop, visibly
    deselecting the startup disk, which is what shows a click reached
    the guest rather than merely arriving at the right coordinates

## Tests

```sh
uv run pytest
```
