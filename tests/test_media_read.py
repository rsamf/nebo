"""Tests for the media read path: client fetch, CLI-facing helpers, and
the MCP image tools.

Until now media only flowed *into* nebo (log_image/log_audio); these
cover the way back out — `client.list_images/list_audio/get_media` and
the `nebo_list_images`/`nebo_get_image` MCP tools, whose image result
must be a genuine MCP image content block (rendered inline by MCP
clients) rather than JSON-with-base64.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

IMAGES_INDEX = {
    "images": {
        "train": [
            {"node": "train", "media_id": "img1", "name": "sample",
             "step": 3, "timestamp": 1.0, "labels": None},
        ],
    },
}
AUDIO_INDEX = {
    "audio": {
        "__global__": [
            {"node": "__global__", "media_id": "aud1", "name": "clip",
             "sr": 16000, "step": None, "timestamp": 2.0},
        ],
    },
}


def _png_bytes() -> bytes:
    from nebo.logging.png import encode_png

    return encode_png(np.arange(48, dtype=np.uint8).reshape(4, 4, 3))


def _wav_bytes() -> bytes:
    from nebo.logging.serializers import serialize_audio

    return serialize_audio(np.zeros(160, dtype=np.int16), 16000)


@pytest.fixture(scope="module")
def media_server():
    """A stdlib HTTP server mimicking the daemon's media read routes."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            routes = {
                "/runs/r1/images": ("application/json",
                                    json.dumps(IMAGES_INDEX).encode()),
                "/runs/r1/audio": ("application/json",
                                   json.dumps(AUDIO_INDEX).encode()),
                "/runs/r1/media/img1": ("image/png", _png_bytes()),
                "/runs/r1/media/aud1": ("audio/wav", _wav_bytes()),
            }
            hit = routes.get(self.path)
            if hit is None:
                body = b'{"error": "not found"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            ctype, body = hit
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output pristine
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class TestClientMediaRead:
    def test_list_images(self, media_server):
        from nebo.client import list_images

        assert list_images("r1", url=media_server) == IMAGES_INDEX

    def test_list_audio(self, media_server):
        from nebo.client import list_audio

        assert list_audio("r1", url=media_server) == AUDIO_INDEX

    def test_get_media_returns_bytes_and_content_type(self, media_server):
        from nebo.client import get_media

        data, ctype = get_media("r1", "img1", url=media_server)
        assert data == _png_bytes()
        assert ctype == "image/png"

    def test_get_media_missing_raises(self, media_server):
        import urllib.error

        from nebo.client import get_media

        with pytest.raises(urllib.error.HTTPError):
            get_media("r1", "nope", url=media_server)


class TestMcpMediaTools:
    @pytest.mark.asyncio
    async def test_list_images(self, media_server):
        from nebo.mcp.tools import list_images

        assert await list_images("r1", server_url=media_server) == IMAGES_INDEX

    @pytest.mark.asyncio
    async def test_get_image_returns_mcp_image_content_block(self, media_server):
        from nebo.mcp.tools import get_image

        result = await get_image("r1", "img1", server_url=media_server)
        blocks = result["_mcp_content"]
        assert len(blocks) == 1
        block = blocks[0]
        assert block["type"] == "image"
        assert block["mimeType"] == "image/png"
        assert base64.b64decode(block["data"]) == _png_bytes()

    @pytest.mark.asyncio
    async def test_get_image_rejects_non_image_media(self, media_server):
        from nebo.mcp.tools import get_image

        result = await get_image("r1", "aud1", server_url=media_server)
        assert "_mcp_content" not in result
        assert "audio/wav" in result["error"]

    @pytest.mark.asyncio
    async def test_get_image_daemon_unreachable(self):
        from nebo.mcp.tools import get_image

        result = await get_image("r1", "img1", server_url="http://localhost:19999")
        assert "error" in result

    def test_tools_registered_and_dispatched(self):
        import asyncio

        from nebo.mcp.server import MCP_TOOLS, handle_tool_call

        names = {t["name"] for t in MCP_TOOLS}
        assert {"nebo_list_images", "nebo_get_image"} <= names
        for name, args in [
            ("nebo_list_images", {"run_id": "r1"}),
            ("nebo_get_image", {"run_id": "r1", "media_id": "img1"}),
        ]:
            result = asyncio.get_event_loop().run_until_complete(
                handle_tool_call(name, args, "http://localhost:19999")
            )
            assert "Unknown tool" not in str(result.get("error", ""))


class TestStdioContentPassthrough:
    """The stdio bridge must emit `_mcp_content` results as real MCP
    content blocks, and keep wrapping everything else as JSON text."""

    @staticmethod
    def _call(monkeypatch, tool_result):
        import asyncio

        from nebo.mcp import server, stdio

        async def fake_handle(name, args, server_url):
            return tool_result

        monkeypatch.setattr(server, "handle_tool_call", fake_handle)
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "nebo_get_image", "arguments": {}},
        }
        return asyncio.get_event_loop().run_until_complete(
            stdio._handle_request(request, "http://localhost:19999")
        )

    def test_mcp_content_passes_through(self, monkeypatch):
        block = {"type": "image", "data": "aGk=", "mimeType": "image/png"}
        response = self._call(monkeypatch, {"_mcp_content": [block]})
        assert response["result"]["content"] == [block]

    def test_plain_results_still_wrap_as_text(self, monkeypatch):
        response = self._call(monkeypatch, {"texts": []})
        content = response["result"]["content"]
        assert content[0]["type"] == "text"
        assert json.loads(content[0]["text"]) == {"texts": []}
