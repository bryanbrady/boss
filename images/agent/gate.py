#!/usr/bin/env python3
"""Egress gate for one boss run.

Agent containers sit on an internal Docker network with no route out. The gate is the
only container on both that network and an outside one, so everything an agent reaches
goes through here: an HTTPS proxy (CONNECT only) that admits an allowlist of host:port
pairs.

Every connection attempt is logged to stdout, so `docker logs` on the gate is the run's
egress record.
"""

import argparse
import asyncio
import sys

BUFFER = 65536


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


def parse_allow(entries: list[str]) -> set[tuple[str, int]]:
    allow = set()
    for entry in entries:
        host, _, port = entry.partition(":")
        allow.add((host.lower(), int(port or 443)))
    return allow


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-port", type=int, default=3128)
    parser.add_argument("--allow", action="append", default=[], metavar="HOST[:PORT]")
    args = parser.parse_args()

    allow = parse_allow(args.allow)
    server = await asyncio.start_server(proxy_handler(allow), "0.0.0.0", args.proxy_port)
    log(f"proxy :{args.proxy_port} allows {sorted(f'{h}:{p}' for h, p in allow)}")
    await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
