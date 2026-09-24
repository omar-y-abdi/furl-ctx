"""Tool hints and the opt-in remote attachment boundary keep stdio compatible."""

import pytest

from furl_ctx.ccr.mcp_server import FurlMCPServer, _ProvidedFileError, _validate_provided_file_url


@pytest.mark.asyncio
async def test_explicit_tool_hints():
    server = FurlMCPServer()
    assert hasattr(server, "route_list_tools")
    tools = await server.route_list_tools()
    for tool in tools:
        assert tool.annotations is not None
        for key in ("readOnlyHint", "destructiveHint", "openWorldHint"):
            assert isinstance(getattr(tool.annotations, key), bool)
    hints = {t.name: t.annotations for t in tools}
    assert hints["furl_purge"].destructiveHint
    assert hints["furl_compress"].destructiveHint, "Capacity eviction can remove prior CCR data"
    assert hints["furl_compress"].openWorldHint
    assert hints["furl_retrieve"].readOnlyHint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/file",
        "https://files.oaiusercontent.com.evil.test/file",
        "https://oaiusercontent.com:444/file",
    ],
)
async def test_remote_allowlist_rejects_destination_before_dns(monkeypatch, url):
    monkeypatch.setenv("FURL_MCP_ALLOWED_FILE_HOSTS", "chatgpt.com,*.oaiusercontent.com")

    def forbidden_dns(*args):
        raise AssertionError("Disallowed destinations must not reach DNS")

    monkeypatch.setattr("socket.getaddrinfo", forbidden_dns)
    with pytest.raises(_ProvidedFileError, match="allowed"):
        await _validate_provided_file_url(url)


@pytest.mark.asyncio
async def test_remote_allowlist_accepts_current_chatgpt_file_host(monkeypatch):
    monkeypatch.setenv("FURL_MCP_ALLOWED_FILE_HOSTS", "chatgpt.com,*.oaiusercontent.com")

    def public_dns(*args):
        import socket

        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("104.18.0.1", 443),
            )
        ]

    monkeypatch.setattr("socket.getaddrinfo", public_dns)
    await _validate_provided_file_url(
        "https://chatgpt.com/backend-api/estuary/content?id=file_123&sig=test"
    )
