"""Connect timeout survives two SYN retransmits; the binary check does not flag mislabelled text,
ANSI logs or short latin-1 bodies, but still catches random/gzip/PNG/UTF-16 bodies."""
import asyncio
import gzip
import random
import time

import httpx
import pytest

from fastweb_firecrawl import fetchers, heuristics


def decoded(data: bytes, enc: str = "utf-8") -> str:
    return data.decode(enc, errors="replace")


def test_connect_timeout_covers_second_syn_retransmit():
    assert 3.0 < fetchers.CONNECT_TIMEOUT < 4.0  # Linux SYN retries at 1 s and 3 s


def cp1252_rss() -> bytes:
    item = "<item><title>Café – “naïve” résumé à l’été</title></item>\n"
    return ('<?xml version="1.0"?><rss><channel>' + item * 200 + "</channel></rss>").encode("cp1252")


@pytest.mark.parametrize("name,data", [
    ("cp1252-rss-as-utf8", cp1252_rss()),
    ("ansi-ci-log", b"".join(b"\x1b[32mPASS\x1b[0m test_%d \x1b[1m\x1b[33mok\x1b[0m\n" % i for i in range(300))),
    ("short-latin1-json", '{"name": "José Müller", "city": "Köln"}'.encode("latin-1")),
    ("formfeed-text", b"page one\x0c" * 200),
])
def test_text_bodies_are_not_binary(name, data):
    assert not heuristics.is_binary(data[:16], decoded(data)), name


def utf16_nobom() -> bytes:
    return ("plain ascii text in utf-16 " * 200).encode("utf-16-le")


@pytest.mark.parametrize("name,data", [
    ("random", random.Random(34).randbytes(8192)),
    ("gzip", gzip.compress(b"hello world " * 1000, mtime=0)),
    ("png", b"\x89PNG\r\n\x1a\n" + random.Random(1).randbytes(4000)),
    ("utf16-no-bom", utf16_nobom()),
])
def test_binary_bodies_are_binary(name, data):
    assert heuristics.is_binary(data[:16], decoded(data)), name


def test_fffd_alone_counts_above_quarter():
    # bin-fffd-off mutant: a body that is almost all U+FFFD (undecodable high bytes, no NUL/C0) is still binary
    data = bytes(random.Random(2).randrange(0x80, 0x100) for _ in range(4096))
    assert heuristics.is_binary(data[:16], decoded(data))
    mostly_text = ("abc�" * 1000)  # 25 % exactly: not binary
    assert not heuristics.is_binary(b"abc", mostly_text)


def test_short_body_ignores_control_chars_but_not_nul():
    assert not heuristics.is_binary(b"x", "x\x01\x02\x03" * 10)
    assert heuristics.is_binary(b"x", "x\x00\x00\x00" * 10)


@pytest.mark.slow
def test_dead_port_fails_under_4s():
    """fastweb's async client bounds the whole connect (all addresses) by CONNECT_TIMEOUT."""
    async def go():
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=fetchers.CONNECT_TIMEOUT)) as c:
            with pytest.raises((httpx.ConnectTimeout, httpx.ConnectError)):
                await c.get("http://example.com:81/")
    t = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - t < 6.5  # one CONNECT_TIMEOUT for all addresses, not one each
