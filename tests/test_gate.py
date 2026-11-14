"""The gate's API forward, against a local stand-in for Anthropic."""

import asyncio
import contextlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "boss_gate", Path(__file__).parents[1] / "images/agent/gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

KEY = "run-key"
TOKEN = "real-token"
RESPONSE = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nstreamed"


def request(*, line="POST /v1/messages?beta=true HTTP/1.1", auth=f"Bearer {KEY}", body=b"{}",
            extra=()):  # fmt: skip
    headers = [line, "Host: gate:8080", "Connection: keep-alive", "x-api-key: agent-key",
               "anthropic-beta: oauth-2025-04-20", f"Content-Length: {len(body)}",
               *extra]  # fmt: skip
    if auth:
        headers.append(f"Authorization: {auth}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + body


def exchange(raw: bytes) -> tuple[bytes, bytes | None]:
    """Send raw bytes through the gate. Returns the reply and what reached upstream."""

    async def go():
        received = []

        async def upstream(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":")[1]) for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length"))  # fmt: skip
            received.append(head + await reader.readexactly(length))
            # Anything further the agent sent would arrive here if the gate passed it on.
            with contextlib.suppress(TimeoutError):
                received.append(await asyncio.wait_for(reader.read(4096), 0.1))
            writer.write(RESPONSE)
            await writer.drain()
            writer.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = up.sockets[0].getsockname()[1]
        connect = lambda: asyncio.open_connection("127.0.0.1", port)  # noqa: E731
        server = await asyncio.start_server(gate.api_handler(KEY, TOKEN, connect), "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        writer.write(raw)
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        server.close()
        up.close()
        return answer, b"".join(received) if received else None

    return asyncio.run(go())


def test_swaps_the_run_key_for_the_token_and_streams_the_reply():
    answer, sent = exchange(request(body=b'{"a":1}'))
    assert answer == RESPONSE
    assert sent.startswith(b"POST /v1/messages?beta=true HTTP/1.1\r\n")
    head = sent.split(b"\r\n\r\n")[0].split(b"\r\n")
    assert f"Authorization: Bearer {TOKEN}".encode() in head
    assert b"Host: api.anthropic.com" in head
    assert b"Connection: close" in head
    assert b"anthropic-beta: oauth-2025-04-20" in head
    assert sent.endswith(b'\r\n\r\n{"a":1}')
    assert KEY.encode() not in sent
    assert b"agent-key" not in sent
    assert b"gate:8080" not in sent
    assert b"keep-alive" not in sent


def test_the_token_is_never_logged(capsys):
    exchange(request())
    exchange(request(auth="Bearer wrong"))
    out = capsys.readouterr().out
    assert "allow api /v1/messages" in out
    assert TOKEN not in out
    assert KEY not in out


def test_bytes_after_the_body_do_not_reach_anthropic():
    smuggled = b"POST /v1/files HTTP/1.1\r\nHost: x\r\n\r\n"
    _, sent = exchange(request() + smuggled)
    assert b"/v1/files" not in sent


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (request(auth=None), b"401"),
        (request(auth="Bearer wrong"), b"401"),
        (request(extra=[f"Authorization: Bearer {KEY}"]), b"401"),
        (request(line="POST /v1/files HTTP/1.1"), b"403"),
        (request(line="GET /v1/messages HTTP/1.1"), b"403"),
        (request(line="POST /v1/messages/../files HTTP/1.1"), b"403"),
        (request(extra=["Transfer-Encoding: chunked"]), b"411"),
        (request(extra=["Content-Length: 0"]), b"411"),
        (request(line="POST /v1/messages?a\nX-Evil:1 HTTP/1.1"), b"400"),
        (request(extra=["X-Evil: a\nHost: evil"]), b"400"),
        (request(extra=["X-Evil : a"]), b"400"),
        (request(line="POST /v1/messages HTTP/1.0"), b"400"),
    ],
)
def test_refused_requests_never_reach_anthropic(raw, status):
    answer, sent = exchange(raw)
    assert answer.split(b" ")[1] == status
    assert sent is None
