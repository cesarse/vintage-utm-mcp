# vintage-utm-mcp

An MCP server for driving vintage operating systems running in UTM on
macOS. The guests cannot run an agent, so everything goes through
`utmctl` (lifecycle) and QMP (anything inside the guest).

**Read `README.md` first.** It carries the architecture and, more
usefully, the constraints that were found the hard way.

## The guests

| VM | notes |
|---|---|
| `MS-DOS 6.22` | sits at `C:\>` in text mode; `win` starts Windows 3.11 |
| `Mac OS 9.2.2 (ppc)` | boots to the Finder |

Their keyboard layouts differ, and the difference is invisible until
punctuation comes out wrong — letters and digits survive any Latin
layout:

- **DOS** — plain `us`.
- **Windows 3.11** — US-International (`SYSTEM.INI` has
  `keyboard.dll=kbdusx.dll`), so `type_text(layout="us-intl")`.
- **Mac OS 9** — US-International installed; `us-intl` works, and
  `mac-option` reaches accents through Option chords instead.

`detect_layout` reads x86 text mode, so it can answer for DOS and not for
the ppc guest. It says so rather than guessing.

## Things that cost hours

- **QEMU accepts one QMP client per endpoint**, and a second one does not
  get a clean refusal — it waits. A forgotten server or debug script
  shows up as an inexplicably slow first call.
- **An unanswered macOS permission dialog is indistinguishable from a
  hang.** A single screendump blocking for minutes is a dialog, not a
  bug. Time a fresh call with a short watchdog to tell them apart.
- **Sprite learning needs a still screen; locating does not.** A blinking
  caret breaks learning and nothing else, so learn on a quiet screen and
  everything afterwards is robust.
- **Pointing is slow on Mac OS 9** — roughly 3 s a gesture against 0.4 s
  on Windows 3.11, because it damps motion and banks the remainder. Drive
  anything long from the keyboard.
- **`utmctl` does not work over SSH or before login** (AppleEvents).

## How to work here

Small, request-driven increments. Tests green before moving on. One
variable at a time.

Anything non-obvious earns a test built from a **real captured screen**,
never an invented one.

Say what was verified and what was assumed. "The architecture says it
works" is not "we watched it work" — and in this project that distinction
has repeatedly mattered: several confident conclusions turned out wrong
the moment they were measured, including some written down as fact in
this repo. Prefer a quick experiment over a plausible explanation.

Code and comments in English.

## Commits

Short, past tense, big picture — "Added drag and drop support",
"Removed the mouse calibration table". Not imperative, no metrics.

## Debugging

Either register the MCP server, or talk to QMP directly from a Python
script. The script is better for investigation because it allows
arbitrary QMP commands — but only one of the two can hold the socket at a
time.

For screenshots, write the PNG to a file and read the file rather than
returning the image through a tool.
