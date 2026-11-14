#!/usr/bin/env python3
"""Egress gate for one boss run.

Agent containers sit on an internal Docker network with no route out. The gate is the
only container on both that network and an outside one, so everything an agent reaches
goes through here: an HTTPS proxy (CONNECT only) that admits an allowlist of host:port
pairs.

The gate also holds the run's Claude credential, so no agent ever does. The agent's API
requests come here over plain HTTP, carrying a key that's only good for this run. The gate
checks the key, swaps in the real credential and forwards the request to Anthropic over
TLS. Only the message endpoints are forwarded, and a request is rebuilt from headers that
parse cleanly, so nothing an agent sends can reach Anthropic with the credential except a
single well-formed request to one of them.

Every connection attempt is logged to stdout, so `docker logs` on the gate is the run's
egress record. Credentials are never logged.
"""

import argparse
import asyncio
import hmac
import os
import re
import ssl
import sys

BUFFER = 65536
API_HOST = "api.anthropic.com"
API_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
RUN_KEY_ENV = "BOSS_RUN_KEY"
TARGET = re.compile(r"[!-~]+")
HEADER = re.compile(r"([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(.*?)[ \t]*")
# Replaced by the gate, or meaningful only on the hop between the agent and the gate.
DROPPED_HEADERS = {
    "authorization", "connection", "content-length", "host", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
    "x-api-key",
}  # fmt: skip


def log(message: str) -> None:
    print(message, flush=True)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def splice(
    a: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    b: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    await asyncio.gather(pipe(a[0], b[1]), pipe(b[0], a[1]))


async def reply(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
    await writer.drain()
    writer.close()


def proxy_handler(allow: set[tuple[str, int]]):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            return
        request = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request.split()
        if len(parts) != 3 or parts[0] != "CONNECT":
            log(f"deny  {request!r} (only CONNECT is proxied)")
            await reply(writer, "405 Method Not Allowed")
            return
        host, _, port_text = parts[1].rpartition(":")
        host = host.strip("[]").lower()
        if not port_text.isdigit() or (host, int(port_text)) not in allow:
            log(f"deny  {parts[1]}")
            await reply(writer, "403 Forbidden")
            return
        try:
            upstream = await asyncio.wait_for(asyncio.open_connection(host, int(port_text)), 15)
        except (OSError, TimeoutError) as error:
            log(f"fail  {parts[1]}: {error}")
            await reply(writer, "502 Bad Gateway")
            return
        log(f"allow {parts[1]}")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await splice((reader, writer), upstream)

    return handle


class BadRequest(Exception):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason


def parse_api_request(head: bytes, run_key: str) -> tuple[str, int, list[tuple[str, str]]]:
    """Check a request's head. Returns its target, body length and the headers to keep."""
    lines = head.decode("latin-1").removesuffix("\r\n\r\n").split("\r\n")
    # A stray CR or LF left in a line would start a new header or request upstream.
    if any("\r" in line or "\n" in line for line in lines):
        raise BadRequest("400 Bad Request", "stray line break")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1" or not TARGET.fullmatch(parts[1]):
        raise BadRequest("400 Bad Request", f"bad request line {lines[0]!r}")
    method, target, _ = parts
    if method != "POST" or target.partition("?")[0] not in API_PATHS:
        raise BadRequest("403 Forbidden", f"{method!r} {target.partition('?')[0]!r}")
    headers = []
    for line in lines[1:]:
        match = HEADER.fullmatch(line)
        if not match:
            raise BadRequest("400 Bad Request", "malformed header")
        headers.append((match[1], match[2]))

    def found(name: str) -> list[str]:
        return [value for key, value in headers if key.lower() == name]

    auth, lengths = found("authorization"), found("content-length")
    if len(auth) != 1 or not hmac.compare_digest(
        auth[0].encode("latin-1"), f"Bearer {run_key}".encode()
    ):
        raise BadRequest("401 Unauthorized", "missing or wrong run key")
    # Bodies have an exact length, so bytes after one can't be read as a second request.
    if found("transfer-encoding") or len(lengths) != 1 or not re.fullmatch("[0-9]+", lengths[0]):
        raise BadRequest("411 Length Required", "no single Content-Length")
    kept = [(k, v) for k, v in headers if k.lower() not in DROPPED_HEADERS]
    return target, int(lengths[0]), kept


def upstream_head(target: str, length: int, headers: list[tuple[str, str]], token: str) -> bytes:
    lines = [
        f"POST {target} HTTP/1.1",
        f"Host: {API_HOST}",
        f"Authorization: Bearer {token}",
        f"Content-Length: {length}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in headers),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def copy_exactly(reader, writer, length: int) -> None:
    while length:
        data = await reader.read(min(BUFFER, length))
        if not data:
            raise ConnectionError("the agent closed the connection mid-request")
        writer.write(data)
        await writer.drain()
        length -= len(data)


def connect_to_api():
    return asyncio.open_connection(API_HOST, 443, ssl=ssl.create_default_context())


def api_handler(run_key: str, token: str, connect=connect_to_api):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            return
        try:
            target, length, headers = parse_api_request(head, run_key)
        except BadRequest as error:
            log(f"deny  api {error.reason}")
            await reply(writer, error.status)
            return
        path = target.partition("?")[0]
        try:
            up_reader, up_writer = await asyncio.wait_for(connect(), 15)
        except (OSError, TimeoutError) as error:
            log(f"fail  api {path}: {error}")
            await reply(writer, "502 Bad Gateway")
            return
        try:
            up_writer.write(upstream_head(target, length, headers, token))
            await copy_exactly(reader, up_writer, length)
            first = await up_reader.read(BUFFER)
            status = first.split(b"\r\n", 1)[0].decode("latin-1")
            log(f"allow api {path} -> {status!r}")
            writer.write(first)
            await writer.drain()
            await pipe(up_reader, writer)
        except OSError as error:
            log(f"fail  api {path}: {type(error).__name__}")
        finally:
            up_writer.close()
            writer.close()

    return handle


def parse_allow(entries: list[str]) -> set[tuple[str, int]]:
    allow = set()
    for entry in entries:
        host, _, port = entry.partition(":")
        allow.add((host.lower(), int(port or 443)))
    return allow


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-port", type=int, default=3128)
    parser.add_argument("--api-port", type=int, default=8080)
    parser.add_argument("--allow", action="append", default=[], metavar="HOST[:PORT]")
    args = parser.parse_args()

    allow = parse_allow(args.allow)
    server = await asyncio.start_server(proxy_handler(allow), "0.0.0.0", args.proxy_port)
    log(f"proxy :{args.proxy_port} allows {sorted(f'{h}:{p}' for h, p in allow)}")
    servers = [server.serve_forever()]
    token, run_key = os.environ.get(TOKEN_ENV), os.environ.get(RUN_KEY_ENV)
    if token and run_key:
        api = await asyncio.start_server(api_handler(run_key, token), "0.0.0.0", args.api_port)
        log(f"api :{args.api_port} forwards to {API_HOST}")
        servers.append(api.serve_forever())
    await asyncio.gather(*servers)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
