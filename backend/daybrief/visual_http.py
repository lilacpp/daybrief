"""Bounded public-page fetching for visuals, with robots and redirect checks."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "DayBriefVisuals/1.0"


class VisualFetchError(ValueError):
    """Safe, non-secret fetch failure; skip this visual candidate."""


def public_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username or url.password or url.port not in (None, 80, 443)):
            raise ValueError
        if any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError
        # Reject literal private addresses before any DNS or HTTP calls.
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if url.hostname.lower() in ("localhost", "metadata.google.internal"):
            raise ValueError
        return urlunsplit((url.scheme, url.netloc, url.path or "/", url.query, ""))
    except (ValueError, TypeError):
        raise VisualFetchError("Invalid public image/article URL") from None


async def public_address(url):
    host = urlsplit(public_url(url)).hostname
    port = urlsplit(url).port or (443 if url.startswith("https:") else 80)
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = sorted({answer[4][0] for answer in answers})
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise VisualFetchError("Non-public image/article destination")
        return addresses[0]
    except OSError:
        raise VisualFetchError("Image/article DNS lookup failed") from None


@dataclass
class Fetched:
    url: str
    status: int
    headers: dict
    body: bytes


class PublicWeb:
    def __init__(self, timeout):
        self.timeout = timeout
        self.robots = {}

    async def _once(self, url, limit):
        # Pin the validated address; DNS cannot resolve differently at connection time.
        address = await asyncio.wait_for(public_address(url), timeout=self.timeout)
        original = httpx.URL(url)
        pinned = original.copy_with(host=address)
        async with (
            httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client,
            client.stream(
                "GET", pinned,
                headers={"Host": original.netloc.decode(), "User-Agent": USER_AGENT},
                extensions={"sni_hostname": original.host},
            ) as response,
        ):
            if response.is_redirect or response.status_code >= 400:
                body = b""
            else:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise VisualFetchError("Image/article exceeds download limit")
            return Fetched(url, response.status_code, dict(response.headers), bytes(body))

    async def _request(self, url, limit):
        for attempt in range(3):
            try:
                response = await asyncio.wait_for(self._once(url, limit), timeout=self.timeout)
                if response.status != 429 and response.status < 500:
                    return response
            except (httpx.HTTPError, TimeoutError):
                if attempt == 2:
                    raise VisualFetchError("Image/article unavailable") from None
            if attempt < 2:
                await asyncio.sleep(0.25 * 2**attempt)
        raise VisualFetchError("Image/article unavailable")

    async def allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            try:
                response = await self.fetch(robots_url, limit=256_000, check_robots=False)
                parser = RobotFileParser()
                if response.status == 404:
                    parser.parse(["User-agent: *", "Disallow:"])
                elif response.status == 200:
                    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
                else:
                    parser.parse(["User-agent: *", "Disallow: /"])
                self.robots[origin] = parser
            except VisualFetchError:
                self.robots[origin] = None
        parser = self.robots[origin]
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    async def fetch(self, url, limit, check_robots=True):
        url = public_url(url)
        for _ in range(4):
            if check_robots and not await self.allowed(url):
                raise VisualFetchError("Robots policy does not allow this request")
            response = await self._request(url, limit)
            if response.status in (301, 302, 303, 307, 308):
                url = public_url(urljoin(url, response.headers.get("location", "")))
                continue
            return response
        raise VisualFetchError("Too many image/article redirects")
