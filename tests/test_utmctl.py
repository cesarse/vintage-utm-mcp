import pytest

from vintage_utm_mcp import utmctl
from vintage_utm_mcp.utmctl import canonical_name, forget_identity, parse_list

SAMPLE = """\
UUID                                 Status   Name
12345678-ABCD-4321-9876-ABCDEF012345 stopped  MS-DOS 6.22
87654321-DCBA-1234-6789-FEDCBA543210 started  Windows 3.11 for Workgroups
"""


def test_parse_list_keeps_spaces_in_names() -> None:
    vms = parse_list(SAMPLE)
    assert vms == [
        {
            "name": "MS-DOS 6.22",
            "uuid": "12345678-ABCD-4321-9876-ABCDEF012345",
            "status": "stopped",
        },
        {
            "name": "Windows 3.11 for Workgroups",
            "uuid": "87654321-DCBA-1234-6789-FEDCBA543210",
            "status": "started",
        },
    ]


def test_parse_list_header_only() -> None:
    assert parse_list("UUID Status Name\n") == []


def test_parse_list_ignores_blank_lines() -> None:
    assert parse_list(SAMPLE + "\n\n") == parse_list(SAMPLE)


# --- cached name/UUID index -------------------------------------------
#
# `utmctl list` measured 200 ms per call, and every tool used to pay it
# just to turn a name it already had into the same name. These cover the
# cache that removes that cost without ever handing back a stale status.


@pytest.fixture
def listings(monkeypatch):
    """Stand in for the utmctl subprocess and count how often it runs."""
    state = {"vms": parse_list(SAMPLE), "calls": 0}

    def fake_list_vms():
        state["calls"] += 1
        return state["vms"]

    monkeypatch.setattr(utmctl, "list_vms", fake_list_vms)
    forget_identity()  # the index is module-global; never leak between tests
    yield state
    forget_identity()


def test_resolves_a_name_and_a_uuid_to_the_canonical_name(listings) -> None:
    assert canonical_name("MS-DOS 6.22") == "MS-DOS 6.22"
    assert (
        canonical_name("12345678-ABCD-4321-9876-ABCDEF012345") == "MS-DOS 6.22"
    )


def test_second_lookup_within_the_ttl_runs_no_subprocess(listings) -> None:
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 1
    for _ in range(5):
        canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 1


def test_a_miss_refreshes_even_when_the_index_is_warm(listings) -> None:
    # A VM added moments ago has to resolve straight away, so only hits
    # may be served from the cache.
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 1
    listings["vms"] = parse_list(
        SAMPLE + "11111111-2222-3333-4444-555555555555 stopped  OS/2 Warp 4\n"
    )
    assert canonical_name("OS/2 Warp 4") == "OS/2 Warp 4"
    assert listings["calls"] == 2


def test_index_is_reread_once_it_expires(listings, monkeypatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(utmctl.time, "monotonic", lambda: clock["now"])
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 1
    clock["now"] += utmctl.IDENTITY_TTL / 2
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 1  # still fresh
    clock["now"] += utmctl.IDENTITY_TTL
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 2  # expired, read again


def test_forget_identity_forces_a_reread(listings) -> None:
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 1
    forget_identity()
    canonical_name("MS-DOS 6.22")
    assert listings["calls"] == 2


def test_renamed_vm_resolves_again_after_the_index_expires(
    listings, monkeypatch
) -> None:
    # The one staleness window the TTL leaves open: UTM's own interface
    # renamed a VM, so the old name keeps resolving until the index ages
    # out. The symptom is an unreachable QMP endpoint, not a wrong action.
    clock = {"now": 500.0}
    monkeypatch.setattr(utmctl.time, "monotonic", lambda: clock["now"])
    assert canonical_name("MS-DOS 6.22") == "MS-DOS 6.22"
    listings["vms"] = parse_list(
        "UUID                                 Status   Name\n"
        "12345678-ABCD-4321-9876-ABCDEF012345 stopped  MS-DOS 6.22 (old)\n"
    )
    assert canonical_name("MS-DOS 6.22") == "MS-DOS 6.22"  # stale hit
    clock["now"] += utmctl.IDENTITY_TTL + 1
    assert canonical_name("MS-DOS 6.22") is None  # gone; caller falls back


def test_unknown_vm_resolves_to_nothing(listings) -> None:
    assert canonical_name("Nonexistent VM") is None
