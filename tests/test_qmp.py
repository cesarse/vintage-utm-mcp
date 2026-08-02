import asyncio
import json

from vintage_utm_mcp.qmp import (
    QmpManager,
    connect_hint,
    default_port,
    parse_address,
    slugify,
)

# --- pure helpers ---------------------------------------------------------


def test_slugify_real_vm_names() -> None:
    assert slugify("MS-DOS 6.22") == "ms-dos-6.22"
    assert slugify("Mac OS 9.2.2 (ppc)") == "mac-os-9.2.2-ppc"


def test_slugify_never_empty() -> None:
    assert slugify("***") == "vm"


def test_default_port_deterministic_and_in_range() -> None:
    for name in ("MS-DOS 6.22", "Mac OS 9.2.2 (ppc)"):
        port = default_port(name)
        assert port == default_port(name)  # stable across calls
        assert 4600 <= port < 4700
    # The two real VMs must not collide with each other.
    assert default_port("MS-DOS 6.22") != default_port("Mac OS 9.2.2 (ppc)")


def test_parse_address_forms(tmp_path) -> None:
    assert parse_address("unix:/tmp/a.sock") == "/tmp/a.sock"
    assert parse_address("tcp:127.0.0.1:4501") == ("127.0.0.1", 4501)
    assert parse_address(str(tmp_path / "b.sock")) == str(tmp_path / "b.sock")


# --- round-trip against a fake QMP server ---------------------------------

GREETING = {
    "QMP": {
        "version": {
            "qemu": {"major": 9, "minor": 1, "micro": 2},
            "package": "fake",
        },
        "capabilities": [],
    }
}


async def _fake_qmp(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    writers: list[asyncio.StreamWriter],
) -> None:
    """Minimal QMP endpoint.

    Real QMP does NOT newline-terminate messages: the wire is a stream of
    concatenated JSON documents, so we parse incrementally.
    """
    writers.append(writer)
    decoder = json.JSONDecoder()
    buffer = ""

    def send(obj: dict) -> None:
        # Real QEMU newline-delimits server->client messages (and qemu.qmp
        # relies on it); only client->server traffic lacks framing.
        writer.write((json.dumps(obj) + "\r\n").encode())

    try:
        send(GREETING)
        await writer.drain()
        while chunk := await reader.read(4096):
            buffer += chunk.decode()
            while buffer.lstrip():
                try:
                    msg, consumed = decoder.raw_decode(buffer.lstrip())
                except json.JSONDecodeError:
                    break  # incomplete document; wait for more bytes
                buffer = buffer.lstrip()[consumed:]
                response: dict = {"return": {}}
                if msg.get("execute") == "query-status":
                    response = {"return": {"status": "running", "running": True}}
                if "id" in msg:
                    response["id"] = msg["id"]
                send(response)
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass


def test_manager_roundtrip_and_reconnect() -> None:
    async def start_server(writers: list, port: int = 0) -> asyncio.Server:
        return await asyncio.start_server(
            lambda r, w: _fake_qmp(r, w, writers), host="127.0.0.1", port=port
        )

    async def kill_server(server: asyncio.Server, writers: list) -> None:
        for w in writers:  # close established connections, not just the listener
            w.close()
        server.close()
        await server.wait_closed()

    async def scenario() -> None:
        writers: list[asyncio.StreamWriter] = []
        server = await start_server(writers)
        port = server.sockets[0].getsockname()[1]
        manager = QmpManager(resolver=lambda vm: ("127.0.0.1", port))

        result = await manager.execute("fake-vm", "query-status")
        assert result["status"] == "running"

        # Simulate a VM restart: kill the server and every connection, then
        # bring one up again on the same port. The next execute() must
        # notice the stale connection and reconnect transparently — no
        # manual intervention.
        await kill_server(server, writers)
        writers.clear()
        server = await start_server(writers, port=port)

        result = await manager.execute("fake-vm", "query-status")
        assert result["status"] == "running"

        await manager.close_all()
        await kill_server(server, writers)

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))


# --- what a failed connect should say ---------------------------------
#
# The commonest failure used to produce a message ending in a bare colon:
# asyncio.TimeoutError stringifies to "", and a timeout is exactly what a
# second QMP client sees.


def test_timeout_is_reported_as_another_client_holding_the_socket() -> None:
    hint = connect_hint(asyncio.TimeoutError(), ("127.0.0.1", 4693))
    assert "one QMP client at a time" in hint
    assert "lsof -nP -iTCP:4693" in hint  # names the port to look at


def test_refusal_is_reported_as_nothing_listening() -> None:
    hint = connect_hint(
        ConnectionRefusedError(61, "Connection refused"), ("127.0.0.1", 4693)
    )
    assert "nothing is listening" in hint
    assert "check_vm" in hint


def test_an_unexpected_error_still_names_itself() -> None:
    # Never an empty explanation, whatever went wrong.
    assert connect_hint(OSError("host is down"), ("127.0.0.1", 4693)) == (
        "OSError: host is down"
    )
    assert connect_hint(OSError(), ("127.0.0.1", 4693)) == "OSError"


def test_hint_works_for_a_unix_socket_address() -> None:
    hint = connect_hint(asyncio.TimeoutError(), "/tmp/qmp.sock")
    assert "/tmp/qmp.sock" in hint


def test_refusal_is_recognised_through_the_library_wrapper() -> None:
    # qemu.qmp does not re-raise ConnectionRefusedError; it wraps it, so
    # the top-level type says nothing and the chain has to be walked.
    wrapper = RuntimeError(
        "Failed to establish connection: [Errno 61] "
        "Connect call failed ('127.0.0.1', 4698)"
    )
    hint = connect_hint(wrapper, ("127.0.0.1", 4698))
    assert "nothing is listening" in hint


def test_refusal_is_recognised_from_a_chained_cause() -> None:
    try:
        try:
            raise ConnectionRefusedError(61, "Connection refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("Failed to establish connection") from inner
    except RuntimeError as outer:
        assert "nothing is listening" in connect_hint(outer, ("127.0.0.1", 4698))


def test_a_port_answering_but_not_speaking_qmp_is_called_out() -> None:
    # Measured against a stand-in that accepted and closed at once.
    wrapper = RuntimeError("Failed to establish session: EOFError")
    hint = connect_hint(wrapper, ("127.0.0.1", 4699))
    assert "not QEMU's QMP" in hint
