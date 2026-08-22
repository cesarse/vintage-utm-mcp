"""Regression test: tools returning images must survive the SDK's real
serialization path (call_tool), not just direct function calls.

Context: returning [Image, dict] made the SDK treat the result as
structured output and fail with "Unable to serialize unknown type".
"""

import asyncio

from mcp.server.mcpserver import Image as McpImage
from mcp.server.mcpserver import MCPServer

from vintage_utm_mcp.screen import ppm_to_png


def _tiny_png() -> bytes:
    ppm = b"P6\n2 2\n255\n" + b"\x10\x20\x30" * 4
    png, _, _, _ = ppm_to_png(ppm, scale=1)
    return png


def test_image_return_serializes_through_call_tool() -> None:
    probe = MCPServer("probe")

    @probe.tool()
    def fake_screenshot() -> McpImage:
        return McpImage(data=_tiny_png(), format="png")

    async def scenario():
        result = await probe.call_tool("fake_screenshot", {})
        # SDK 2.x returns a CallToolResult; a tool that blew up on
        # serialization comes back as is_error with a text block, so
        # check that before reading the image out.
        assert not result.is_error, result.content
        block = result.content[0]
        assert block.type == "image"
        assert block.mime_type.startswith("image/png")

    asyncio.run(scenario())
