"""Regression test: tools returning images must survive FastMCP's real
serialization path (call_tool), not just direct function calls.

Context: returning [Image, dict] made the SDK treat the result as
structured output and fail with "Unable to serialize unknown type".
"""

import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage

from vintage_utm_mcp.screen import ppm_to_png


def _tiny_png() -> bytes:
    ppm = b"P6\n2 2\n255\n" + b"\x10\x20\x30" * 4
    png, _, _, _ = ppm_to_png(ppm, scale=1)
    return png


def test_image_return_serializes_through_call_tool() -> None:
    probe = FastMCP("probe")

    @probe.tool()
    def fake_screenshot() -> McpImage:
        return McpImage(data=_tiny_png(), format="png")

    async def scenario():
        result = await probe.call_tool("fake_screenshot", {})
        # Depending on SDK version, call_tool returns the content list or
        # a (content, structured) tuple.
        content = result[0] if isinstance(result, tuple) else result
        block = content[0]
        assert getattr(block, "type", None) == "image"
        assert getattr(block, "mimeType", "").startswith("image/png")

    asyncio.run(scenario())
