"""QMP connectivity for UTM VMs.

Each VM is expected to expose a QMP endpoint via extra QEMU arguments
(UTM: VM settings -> QEMU -> Arguments). The default convention is
loopback TCP with a deterministic per-VM port:

    -qmp tcp:127.0.0.1:<port>,server,nowait

where <port> = 4600 + crc32(slug(vm_name)) % 100. Use qemu_arg_for(vm)
(or the check_vm tool) to get the exact string.

Why TCP and not a unix socket: UTM's sandbox denies binding unix sockets
outside its own container ("Operation not permitted"). Loopback TCP works
and stays local-only; never bind QMP to non-loopback interfaces, as QMP
has no authentication.

To use a different endpoint for a VM (e.g. on a port collision, or a unix
socket somewhere the sandbox allows), override it in ~/.vintage-utm-mcp/qmp.toml:

    [vms]
    "MS-DOS 6.22" = "tcp:127.0.0.1:4501"
    "Mac OS 9.2.2 (ppc)" = "unix:/some/writable/path.sock"

Note: QEMU accepts a single client per QMP endpoint, so this module keeps
one persistent connection per VM and reconnects transparently when the VM
restarts.
"""

from __future__ import annotations

import asyncio
import re
import tomllib
import zlib
from pathlib import Path
from typing import Any, Callable

from qemu.qmp import QMPClient

try:
    from qemu.qmp import ExecuteError
except ImportError:  # pragma: no cover - layout differences across versions
    from qemu.qmp.qmp_client import ExecuteError

BASE_DIR = Path.home() / ".vintage-utm-mcp"
CONFIG_FILE = BASE_DIR / "qmp.toml"
CONNECT_TIMEOUT = 5.0
PORT_BASE = 4600
PORT_RANGE = 100

#: Either a unix socket path or a (host, port) pair.
Address = str | tuple[str, int]


class QmpError(RuntimeError):
    """QMP connection or command failure."""


def slugify(name: str) -> str:
    """Turn a VM name into an identifier-friendly slug.

    "MS-DOS 6.22" -> "ms-dos-6.22"; "Mac OS 9.2.2 (ppc)" -> "mac-os-9.2.2-ppc".
    """
    slug = re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-.")
    return slug or "vm"


def default_port(vm: str) -> int:
    """Deterministic per-VM loopback port in [PORT_BASE, PORT_BASE+PORT_RANGE)."""
    return PORT_BASE + zlib.crc32(slugify(vm).encode()) % PORT_RANGE


def default_address(vm: str) -> Address:
    return ("127.0.0.1", default_port(vm))


def qemu_arg_for(vm: str) -> str:
    """The exact string to paste into the VM's extra QEMU arguments."""
    return f"-qmp tcp:127.0.0.1:{default_port(vm)},server,nowait"


def parse_address(text: str) -> Address:
    """Parse "unix:<path>", "tcp:<host>:<port>" or a bare path."""
    if text.startswith("unix:"):
        return str(Path(text[len("unix:"):]).expanduser())
    if text.startswith("tcp:"):
        host, _, port = text[len("tcp:"):].rpartition(":")
        if not host or not port.isdigit():
            raise QmpError(f"invalid tcp address {text!r}; expected tcp:<host>:<port>")
        return (host, int(port))
    return str(Path(text).expanduser())


def resolve_address(vm: str) -> Address:
    """Per-VM override from ~/.vintage-utm-mcp/qmp.toml, else the default TCP port."""
    if CONFIG_FILE.exists():
        data = tomllib.loads(CONFIG_FILE.read_text())
        override = data.get("vms", {}).get(vm)
        if override:
            return parse_address(override)
    return default_address(vm)


def describe(address: Address) -> str:
    if isinstance(address, str):
        return address
    return f"tcp:{address[0]}:{address[1]}"


def shape_drives(blocks: list[dict]) -> list[dict]:
    """Shape a QMP query-block payload into the list_drives tool output.

    Depending on how UTM wired the QEMU command line, a drive is named by
    the legacy "device" field or by a blockdev/qdev id; expose whichever
    exists as the drive identifier.
    """
    drives = []
    for block in blocks:
        inserted = block.get("inserted") or {}
        drives.append(
            {
                "drive": block.get("device") or block.get("qdev") or "",
                "removable": block.get("removable", False),
                "image": inserted.get("file"),
                "tray_open": block.get("tray_open", False),
                "locked": block.get("locked", False),
            }
        )
    # Removable drives first: they are what insert/eject act on.
    drives.sort(key=lambda d: not d["removable"])
    return drives


def connect_hint(exc: BaseException, address: Address) -> str:
    """Turn a failed connect into something worth reading.

    The two failures mean opposite things and the exception text does not
    say which — a timeout stringifies to nothing at all, so the commonest
    problem used to surface as a message ending in a bare colon.

    A **timeout** is the signature of a second client: QEMU accepts the
    TCP connection and then never sends its greeting, because it serves
    one QMP client at a time and someone else already has it. A
    **refusal** means nothing is listening at all.
    """
    port = address[1] if isinstance(address, tuple) else address
    # qemu.qmp wraps the real cause in its own ConnectError, so the useful
    # exception is somewhere down the chain rather than the one raised.
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and not any(c is current for c in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    text = " ".join(str(c) for c in chain)

    if any(isinstance(c, (asyncio.TimeoutError, TimeoutError)) for c in chain):
        return (
            f"connected but got no QMP greeting within {CONNECT_TIMEOUT:g}s. "
            "QEMU serves one QMP client at a time and a second one is not "
            "refused, it simply waits — so this usually means another "
            "server or a debug script still holds the socket. Find it with "
            f"`lsof -nP -iTCP:{port}` and stop it."
        )
    if any(isinstance(c, ConnectionRefusedError) for c in chain) or (
        "Connect call failed" in text or "refused" in text.lower()
    ):
        return (
            "nothing is listening there. The VM may be stopped, or it may "
            "be missing the QMP argument — check_vm prints the exact line "
            "to add to the VM's extra QEMU arguments."
        )
    if "EOFError" in text or isinstance(exc, EOFError):
        return (
            "the endpoint accepted the connection and then dropped it "
            "without a QMP greeting. Something is listening on that port "
            "that is not QEMU's QMP, or the VM shut down mid-handshake."
        )
    return f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__


class QmpManager:
    """One persistent QMP connection per VM, with reconnect-once semantics."""

    def __init__(self, resolver: Callable[[str], Address] = resolve_address) -> None:
        self._resolver = resolver
        self._clients: dict[str, QMPClient] = {}
        self._lock = asyncio.Lock()  # coarse; fine for a single-user server

    async def _connect(self, vm: str) -> QMPClient:
        address = self._resolver(vm)
        client = QMPClient(f"vintage-utm-mcp-{slugify(vm)}")
        try:
            await asyncio.wait_for(client.connect(address), CONNECT_TIMEOUT)
        except Exception as exc:
            raise QmpError(
                f"cannot reach QMP for {vm!r} at {describe(address)}: "
                + connect_hint(exc, address)
            ) from exc
        self._clients[vm] = client
        return client

    async def _drop(self, vm: str) -> None:
        client = self._clients.pop(vm, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def execute(self, vm: str, cmd: str, args: dict | None = None) -> Any:
        """Run a QMP command; reconnect once if the connection went stale."""
        async with self._lock:
            client = self._clients.get(vm)
            if client is None:
                client = await self._connect(vm)
            try:
                return await client.execute(cmd, args)
            except ExecuteError as exc:
                # The command reached QEMU and was rejected: not a connection
                # problem, so no reconnect.
                raise QmpError(f"QMP command {cmd!r} failed on {vm!r}: {exc}") from exc
            except QmpError:
                raise
            except Exception:
                # Stale connection (VM restarted, socket replaced): retry once.
                await self._drop(vm)
                client = await self._connect(vm)
                try:
                    return await client.execute(cmd, args)
                except ExecuteError as exc:
                    raise QmpError(
                        f"QMP command {cmd!r} failed on {vm!r}: {exc}"
                    ) from exc

    async def close_all(self) -> None:
        for vm in list(self._clients):
            await self._drop(vm)
