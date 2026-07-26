# vintage-utm-mcp

An MCP server for handling vintage operating systems (like MS-DOS, OS/2,
Windows 3.x and classic Mac OS) running in [UTM](https://mac.getutm.app/)
on macOS. Designed for guests that cannot run a guest agent: everything
goes through utmctl (lifecycle) and QMP (in-guest interaction).

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

## Notes

- `utmctl` does not work over SSH or before login (macOS AppleEvents
  limitation).
- `stop_vm` defaults to `force` (power-button semantics): DOS-era
  guests do not answer ACPI requests. Stop at an idle `C:\>` prompt and
  mind SMARTDRV's write-behind cache.
- QEMU accepts a single QMP client per endpoint; the server keeps one
  persistent connection per VM and reconnects after VM restarts.

## Roadmap

1. ~~Lifecycle (utmctl)~~
2. ~~QMP module + `check_vm`~~
3. ~~Removable media: `list_drives`, `insert_media`, `eject_media`~~
4. ~~Input, keyboard: `type_text`, `send_keys` (mouse pending)~~
5. Output: `screenshot` (2× upscale), `read_text_screen` (B8000 text-mode
   video memory)
6. Mouse: `mouse_click`, `mouse_drag`
7. Known debt: media library provenance manifest (name-collision handling
   currently relies on file size, which is blind to fixed-size floppies)

## Tests

```sh
uv run pytest
```
