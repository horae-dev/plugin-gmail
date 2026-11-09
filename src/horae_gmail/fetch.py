# SPDX-License-Identifier: Apache-2.0
"""A bounded HTTPS client for Google's JSON APIs, whose requests carry credentials.

A request body can hold the client secret and a header a bearer token, so this module never
logs, returns or puts either in an error: every failure is a `FetchError` with a fixed
`reason` code (`timeout`, `http_500`, `redirect_refused`...), raised `from None` so that no
exception chain carries an `httpx` message. The `httpx` and `httpcore` loggers are held at
`WARNING`: at `INFO`, `httpx` logs every request line.

**Bounds.** `https` only, the default port, no user info, and a host that is one of the
allowed hosts (`check_url`); `trust_env=False` (no proxy, `.netrc` or CA bundle from the
environment); a timeout for connecting (5 s) and for each read (8 s), a budget for each
attempt (9 s) and a total deadline (20 s); at most `max_bytes` (1 MiB) after decompression;
**no redirects at all** (a 3xx is `redirect_refused`: the bearer token must never follow one);
one retry after a timeout, a connection error or a 500, 502, 503 or 504, when the deadline
allows it.
"""

import asyncio
import logging
import ssl
import time
from collections.abc import Collection, Mapping
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_BYTES = 1024 * 1024
TIMEOUT_S = 20.0
ATTEMPT_TIMEOUT_S = 9.0
READ_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 5.0
BACKOFF_S = 0.5
MIN_ATTEMPT_S = 1.0
"""A retry is made only when this much of the deadline is left after the pause."""
MAX_URL = 4096
USER_AGENT = "horae-plugin-gmail"
_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY_STATUS = {500, 502, 503, 504}
_RETRY_REASONS = {"timeout", "connect_failed", "transport_failed"}
_ANSWERS = frozenset({200, 400, 401, 403, 404, 429})

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class FetchError(Exception):
    """A call that failed; `reason` is a fixed code, never a URL, a token or Google's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Api(Protocol):
    """Where the Google answers come from (`HttpFetcher`; the tests give a fake one)."""

    async def api(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        """The status and body of a JSON API call. A status of 200, 400, 401, 403, 404 or 429
        is returned (the caller reads the error's body); any other is a `FetchError`. A
        redirect is never followed."""
        ...


def check_url(url: str, hosts: Collection[str]) -> str:
    """`url` when it is an `https` URL to one of `hosts` on the default port with no user
    info; `FetchError` otherwise (`invalid_url`, `not_https`, `host_not_allowed`)."""
    if not url or len(url) > MAX_URL:
        raise FetchError("invalid_url")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in url):
        raise FetchError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise FetchError("invalid_url") from None
    if parts.scheme.lower() != "https":
        raise FetchError("not_https")
    if "@" in parts.netloc or not host or not host.isascii():
        raise FetchError("invalid_url")
    if host.lower() not in hosts:
        raise FetchError("host_not_allowed")
    if port not in (None, 443):
        raise FetchError("port_not_allowed")
    return urlunsplit(("https", host.lower(), parts.path or "/", parts.query, ""))


class HttpFetcher:
    """Calls Google's APIs with `httpx` (see the module docstring for the bounds).

    `verify` and `schemes` exist for the tests (a plain-HTTP server on 127.0.0.1); the plugin
    uses the defaults: the `certifi` CA bundle and `https` only."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_BYTES,
        timeout_s: float = TIMEOUT_S,
        attempt_timeout_s: float = ATTEMPT_TIMEOUT_S,
        read_timeout_s: float = READ_TIMEOUT_S,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        backoff_s: float = BACKOFF_S,
        attempts: int = 2,
        verify: ssl.SSLContext | bool = True,
        schemes: Collection[str] = ("https",),
    ) -> None:
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.attempt_timeout_s = attempt_timeout_s
        self.read_timeout_s = read_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.backoff_s = backoff_s
        self.attempts = attempts
        self.verify = verify
        self.schemes = tuple(schemes)

    async def api(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        if not url or urlsplit(url).scheme.lower() not in self.schemes:
            raise FetchError("not_https")
        deadline = time.monotonic() + self.timeout_s
        timeout = httpx.Timeout(
            min(self.read_timeout_s, self.timeout_s),
            connect=min(self.connect_timeout_s, self.timeout_s),
        )
        try:
            async with (
                asyncio.timeout(self.timeout_s),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    verify=self.verify,
                    timeout=timeout,
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                ) as client,
            ):
                for attempt in range(1, self.attempts + 1):
                    outcome = await self._attempt(client, method, url, headers, body, deadline)
                    if isinstance(outcome, tuple):
                        return outcome
                    retry = outcome.reason in _RETRY_REASONS or outcome.reason in {
                        f"http_{status}" for status in _RETRY_STATUS
                    }
                    left = deadline - time.monotonic() - self.backoff_s
                    if not retry or attempt == self.attempts or left < MIN_ATTEMPT_S:
                        raise outcome
                    await asyncio.sleep(self.backoff_s)
        except FetchError:
            raise
        except TimeoutError:
            raise FetchError("timeout") from None
        except Exception:
            raise FetchError("transport_failed") from None
        raise FetchError("transport_failed")  # pragma: no cover (attempts >= 1)

    async def _attempt(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        deadline: float,
    ) -> tuple[int, bytes] | FetchError:
        """One attempt within its own budget: the answer, or why it failed."""
        budget = min(self.attempt_timeout_s, deadline - time.monotonic())
        try:
            async with asyncio.timeout(budget):
                return await self._once(client, method, url, headers, body)
        except FetchError as error:
            return error
        except TimeoutError:
            return FetchError("timeout")

    async def _once(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, bytes]:
        try:
            async with client.stream(method, url, content=body, headers=headers) as response:
                if response.status_code in _REDIRECTS:
                    raise FetchError("redirect_refused")
                if response.status_code not in _ANSWERS:
                    raise FetchError(f"http_{response.status_code}")
                return response.status_code, await self._body(response)
        except FetchError:
            raise
        except httpx.TimeoutException:
            raise FetchError("timeout") from None
        except httpx.DecodingError:
            raise FetchError("decode_failed") from None
        except httpx.ConnectError as error:
            raise FetchError(_connect_reason(error)) from None
        except (httpx.HTTPError, httpx.InvalidURL, ssl.SSLError, OSError):
            raise FetchError("transport_failed") from None

    async def _body(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            raise FetchError("too_large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.max_bytes:
                raise FetchError("too_large")
            chunks.append(chunk)
        return b"".join(chunks)


def _connect_reason(error: BaseException) -> str:
    """`tls_failed` when the connection failed on TLS (a certificate, a handshake)."""
    seen: BaseException | None = error
    for _ in range(8):
        if seen is None:
            break
        if isinstance(seen, ssl.SSLError):
            return "tls_failed"
        seen = seen.__cause__ or seen.__context__
    return "connect_failed"
