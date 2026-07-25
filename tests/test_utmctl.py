from vintage_utm_mcp.utmctl import parse_list

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
