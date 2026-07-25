"""Thin wrapper around the `utmctl` CLI bundled with UTM.

Flags and output formats verified against utmctl/UTMCtl.swift (UTM main branch):
- `list` prints a header line "UUID  Status  Name"; status is one of
  stopped/starting/started/pausing/paused/resuming/stopping.
- `stop` accepts exactly one of --force (default), --kill, --request.
- `start` accepts --disposable (snapshot mode, changes not saved).
- `suspend` accepts --save-state.
- VMs are identified by UUID or complete name.
- utmctl does NOT work from SSH sessions or before login (AppleEvents limit).
"""

from __future__ import annotations

import shutil
import subprocess

_FALLBACK = "/Applications/UTM.app/Contents/MacOS/utmctl"

_STOP_FLAGS = {
    "force": "--force",      # hard power-off event (utmctl's own default)
    "request": "--request",  # polite ACPI request; needs guest support
    "kill": "--kill",        # kill the QEMU process
}


class UtmctlError(RuntimeError):
    """A utmctl invocation failed."""


def _binary() -> str:
    return shutil.which("utmctl") or _FALLBACK


def _run(*args: str, timeout: float = 60.0) -> str:
    cmd = [_binary(), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise UtmctlError(
            "utmctl not found. Add it to PATH or install UTM at /Applications/UTM.app."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise UtmctlError(f"utmctl {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise UtmctlError(f"utmctl {' '.join(args)} failed: {detail}")
    return proc.stdout


def parse_list(output: str) -> list[dict[str, str]]:
    """Parse `utmctl list` output.

    VM names may contain spaces, so the line is split at most twice
    (UUID and status are always single tokens).
    """
    vms: list[dict[str, str]] = []
    for line in output.splitlines()[1:]:  # skip the header line
        if not line.strip():
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        uuid, status, name = parts
        vms.append({"name": name.strip(), "uuid": uuid, "status": status})
    return vms


def list_vms() -> list[dict[str, str]]:
    return parse_list(_run("list"))


def status(vm: str) -> dict[str, str]:
    return {"vm": vm, "status": _run("status", vm).strip()}


def start(vm: str, *, disposable: bool = False) -> None:
    args = ["start"]
    if disposable:
        args.append("--disposable")
    _run(*args, vm)


def stop(vm: str, *, mode: str = "force") -> None:
    flag = _STOP_FLAGS.get(mode)
    if flag is None:
        raise UtmctlError(
            f"invalid stop mode {mode!r}; expected one of {sorted(_STOP_FLAGS)}"
        )
    _run("stop", flag, vm)


def suspend(vm: str, *, save_state: bool = False) -> None:
    args = ["suspend"]
    if save_state:
        args.append("--save-state")
    _run(*args, vm)
