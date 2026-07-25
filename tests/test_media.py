from vintage_utm_mcp.qmp import shape_drives

# Realistic query-block payload for a DOS-era VM: one IDE hard disk, one
# floppy (empty), one CD-ROM with an ISO inserted.
QUERY_BLOCK = [
    {
        "device": "ide0-hd0",
        "locked": False,
        "removable": False,
        "inserted": {"file": "/vm/dos.qcow2", "drv": "qcow2"},
    },
    {
        "device": "floppy0",
        "locked": False,
        "removable": True,
        "tray_open": False,
    },
    {
        "device": "",
        "qdev": "/machine/peripheral-anon/device[0]",
        "locked": True,
        "removable": True,
        "tray_open": False,
        "inserted": {"file": "/vm/tools.iso", "drv": "raw"},
    },
]


def test_shape_drives_removable_first_and_identifiers() -> None:
    drives = shape_drives(QUERY_BLOCK)
    assert [d["removable"] for d in drives] == [True, True, False]
    names = {d["drive"] for d in drives}
    # Legacy name used when present; qdev fallback when device is empty.
    assert "floppy0" in names
    assert "/machine/peripheral-anon/device[0]" in names


def test_shape_drives_image_and_lock_state() -> None:
    drives = {d["drive"]: d for d in shape_drives(QUERY_BLOCK)}
    assert drives["floppy0"]["image"] is None
    assert drives["/machine/peripheral-anon/device[0]"]["image"] == "/vm/tools.iso"
    assert drives["/machine/peripheral-anon/device[0]"]["locked"] is True
