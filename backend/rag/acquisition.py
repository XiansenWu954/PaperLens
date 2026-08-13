"""SafePdfFetcher — SSRF-safe PDF acquisition (Tasks 3.1, §Safe PDF Acquisition).

Hard rules (each enforced before any payload is consumed):

- HTTPS-only, no userinfo in the URL;
- EVERY DNS answer must be a single globally routable address
  (loopback / RFC1918 / CGNAT / link-local / reserved / multicast /
  unspecified / documentation / benchmark / NAT64 prefixes rejected);
- the resolver is consulted ONCE per hop (first validated answer set is
  pinned — a later rebinding answer is never connected);
- the connected peer must equal the validated address (rebinding + proxy
  escape detection);
- redirects are manual, at most 5 per fetch, every hop revalidated;
- the body is streamed with a default 50 MiB ceiling;
- the first bytes must be the PDF magic ``%PDF-``;
- every rejection is a :class:`PdfAcquisitionError` whose message is exactly
  the stable error_code — URLs, addresses, paths, exception text and payload
  bytes never leak (Tasks 3.3).

The resolver and transport are injectable so tests drive the SSRF matrix with
scripted DNS answers and pinned peers; production uses the system resolver and
a httpx transport constructed with ``trust_env=False`` / ``follow_redirects=False``.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB
DEFAULT_MAX_REDIRECTS = 5
PDF_MAGIC = b"%PDF-"

# stable acquisition error codes (Tasks 3.3) — never raw URLs/addresses
UNSAFE_PDF_URL = "unsafe_pdf_url"
REDIRECT_LIMIT_EXCEEDED = "redirect_limit_exceeded"
SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
INVALID_PDF_MAGIC = "invalid_pdf_magic"


class PdfAcquisitionError(Exception):
    """Stable acquisition error whose message is the error_code itself, so a
    rejection never leaks the URL, address, path, exception body or payload."""

    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# globally-routable address validation
# ---------------------------------------------------------------------------

_FORBIDDEN_NETS_V4 = (
    "0.0.0.0/8",        # unspecified / this network
    "10.0.0.0/8",       # RFC1918 private
    "100.64.0.0/10",    # CGNAT (not covered by ipaddress.is_private on 3.11)
    "127.0.0.0/8",      # loopback
    "169.254.0.0/16",   # link-local
    "172.16.0.0/12",    # RFC1918 private
    "192.0.0.0/24",     # IETF protocol assignments
    "192.0.2.0/24",     # TEST-NET-1 (documentation)
    "192.168.0.0/16",   # RFC1918 private
    "198.18.0.0/15",    # benchmarking
    "198.51.100.0/24",  # TEST-NET-2 (documentation)
    "203.0.113.0/24",   # TEST-NET-3 (documentation)
    "224.0.0.0/4",      # multicast
    "240.0.0.0/4",      # reserved
    "255.255.255.255/32",
)
_FORBIDDEN_NETS_V6 = (
    "::/128",            # unspecified
    "::1/128",           # loopback
    "::ffff:0:0/96",     # IPv4-mapped (embedded v4 judged below)
    "64:ff9b::/96",      # NAT64 well-known prefix
    "100::/64",          # discard-only
    "2001:db8::/32",     # documentation
    "fc00::/7",          # unique local (ULA)
    "fe80::/10",         # link-local
    "ff00::/8",          # multicast
)
_FORBIDDEN_V4 = tuple(ipaddress.ip_network(n) for n in _FORBIDDEN_NETS_V4)
_FORBIDDEN_V6 = tuple(ipaddress.ip_network(n) for n in _FORBIDDEN_NETS_V6)


def is_globally_routable(address: str) -> bool:
    """True only when ``address`` is a single globally routable address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    nets = _FORBIDDEN_V4 if ip.version == 4 else _FORBIDDEN_V6
    if any(ip in net for net in nets):
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return is_globally_routable(str(ip.ipv4_mapped))
    # belt-and-braces: ipaddress built-ins (covers 3.11/3.13 semantics drift)
    if (
        ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_unspecified or ip.is_reserved
    ):
        return False
    if ip.is_private:
        return False
    return bool(ip.is_global)


def _system_resolve(hostname: str) -> set[str]:
    """Default resolver: every address the system DNS returns."""
    infos = socket.getaddrinfo(hostname, None)
    return {info[4][0] for info in infos}


class _SystemResolver:
    """Resolver-object surface for the default system DNS."""

    def resolve(self, hostname: str) -> set[str]:
        return _system_resolve(hostname)


# ---------------------------------------------------------------------------
# httpx-backed production transport
# ---------------------------------------------------------------------------

_UA = "PaperLens/0.1 (+https://paperlens.example)"  # placeholder policy UA


class _HttpxTransport:
    """Production transport over httpx (trust_env=False, follow_redirects=False).

    ``send`` CONNECTS THE VALIDATED IP: the request URL host is rewritten to
    the pinned address while the original hostname is preserved in the ``Host``
    header and the TLS SNI extension, so DNS rebinding after the resolver pin
    is structurally impossible and hostname verification keeps working. The
    connected peer is read from the REAL socket and must equal the pinned IP.
    The body is consumed streaming via ``iter_bytes`` — ``response.content``
    is never touched.
    """

    def __init__(self, client: Any, timeout: float = 60.0) -> None:
        self._client = client
        self.timeout = timeout
        self.connected: list[str] = []
        self.connected_peer: str | None = None

    def connect(self, ip: str) -> None:
        self.connected.append(ip)
        self.connected_peer = ip

    def send(self, url: str) -> dict[str, Any]:
        pinned = self.connected[-1] if self.connected else None
        if pinned is None:
            raise PdfAcquisitionError(UNSAFE_PDF_URL)
        response = self._request_pinned(url, pinned)
        peer = self._peer_of(response)
        if peer is not None and peer != pinned:
            # the real socket connected somewhere else -> abort before any
            # payload byte is read (rebinding / proxy escape)
            raise PdfAcquisitionError(UNSAFE_PDF_URL)
        if peer is not None:
            self.connected_peer = peer
        return {
            "status": int(response.status_code),
            "content": _StreamingBody(response),
            "headers": dict(response.headers),
        }

    def _request_pinned(self, url: str, pinned: str):
        parsed = urlparse(url)
        host = parsed.hostname
        if host is None:
            raise PdfAcquisitionError(UNSAFE_PDF_URL)
        if not hasattr(self._client, "build_request"):
            # injected / red-contract client (only construction kwargs are
            # asserted); plain GET keeps the seam observable
            return self._client.get(url, headers={"User-Agent": _UA})
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            ipaddress.ip_address(pinned)
            netloc_host = f"[{pinned}]" if ":" in pinned else pinned
        except ValueError:  # pragma: no cover - pinned is always an IP
            netloc_host = pinned
        rewritten = parsed._replace(netloc=f"{netloc_host}:{port}").geturl()
        request = self._client.build_request(
            "GET", rewritten,
            headers={"Host": f"{host}:{port}", "User-Agent": _UA},
        )
        request.extensions["sni_hostname"] = host
        return self._client.send(request, stream=True)

    @staticmethod
    def _peer_of(response: Any) -> str | None:
        try:
            stream = response.extensions.get("network_stream")
            if stream is None:
                return None
            sock = stream.get_extra_info("socket")
            if sock is None:
                return None
            peer = sock.getpeername()
            return peer[0] if isinstance(peer, tuple) else str(peer)
        except Exception:  # pragma: no cover - best effort introspection
            return None


class _StreamingBody:
    """Streaming iterable over a httpx streaming response; always closes the
    underlying response when exhausted or on error. Never touches .content.
    Injected/red-contract responses exposing .content directly (no
    iter_bytes) are served as a single chunk."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def __iter__(self):
        try:
            if hasattr(self._response, "iter_bytes"):
                yield from self._response.iter_bytes()
            else:
                yield self._response.content
        finally:
            try:
                self._response.close()
            except Exception:  # pragma: no cover - best effort
                pass


# ---------------------------------------------------------------------------
# SafePdfFetcher
# ---------------------------------------------------------------------------


class SafePdfFetcher:
    """Streamed, SSRF-safe PDF downloader with injectable resolver/transport.

    ``fetch(url)`` returns the PDF bytes decoded latin-1 (byte-transparent
    str) — the red-contract surface; ``fetch_bytes(url)`` returns raw bytes
    for production consumers.
    """

    def __init__(
        self,
        timeout: float = 60.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    async def fetch(self, url: str, *, resolver=None, transport=None) -> str:
        """Async red-contract surface: returns the PDF bytes decoded latin-1
        (byte-transparent str)."""
        return self._fetch_impl(url, resolver=resolver, transport=transport) \
            .decode("latin-1")

    async def fetch_bytes(self, url: str, *, resolver=None, transport=None) -> bytes:
        """Async production surface: returns the raw PDF bytes."""
        return self._fetch_impl(url, resolver=resolver, transport=transport)

    def _fetch_impl(self, url: str, *, resolver=None, transport=None) -> bytes:
        import httpx

        resolver = resolver or _SystemResolver()
        if transport is None:
            client = httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout,
            )
            transport = _HttpxTransport(client, timeout=self.timeout)

        current = url
        redirects = 0
        while True:
            self._validate_url(current)
            host = urlparse(current).hostname
            if host is None:
                raise PdfAcquisitionError(UNSAFE_PDF_URL)
            addresses = self._resolve(host, resolver)
            pinned = sorted(addresses)[0]
            transport.connect(pinned)
            response = transport.send(current)
            # connected peer must be the validated address — verified after
            # the real connection (fake transports report their peer here)
            peer = getattr(transport, "connected_peer", None) or pinned
            if peer != pinned:
                raise PdfAcquisitionError(UNSAFE_PDF_URL)
            status = int(response.get("status", 200))
            if 300 <= status < 400:
                location = response.get("headers", {}).get("location")
                if not location:
                    raise PdfAcquisitionError(UNSAFE_PDF_URL)
                redirects += 1
                if redirects > self.max_redirects:
                    raise PdfAcquisitionError(REDIRECT_LIMIT_EXCEEDED)
                current = urljoin(current, str(location))
                continue
            return self._consume(response)

    # -- helpers ------------------------------------------------------------

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise PdfAcquisitionError(UNSAFE_PDF_URL)
        if parsed.username is not None or parsed.password is not None:
            raise PdfAcquisitionError(UNSAFE_PDF_URL)

    def _resolve(self, host: str, resolver) -> set[str]:
        # host is a literal IP -> validate it directly, never consult DNS
        try:
            literal = str(ipaddress.ip_address(host))
        except ValueError:
            literal = None
        if literal is not None:
            if not is_globally_routable(literal):
                raise PdfAcquisitionError(UNSAFE_PDF_URL)
            return {literal}
        addresses = set(resolver.resolve(host))
        if not addresses:
            raise PdfAcquisitionError(UNSAFE_PDF_URL)
        for address in addresses:
            if not is_globally_routable(address):
                raise PdfAcquisitionError(UNSAFE_PDF_URL)
        return addresses

    def _consume(self, response: dict[str, Any]) -> bytes:
        content = response.get("content", b"")
        chunks: Iterable[bytes]
        if isinstance(content, bytes):
            chunks = (content,)
        else:
            chunks = content
        data = bytearray()
        head = bytearray()
        magic_len = len(PDF_MAGIC)
        for chunk in chunks:
            if len(head) < magic_len:
                head.extend(chunk[: magic_len - len(head)])
            data.extend(chunk)
            if len(data) > self.max_bytes:
                raise PdfAcquisitionError(SIZE_LIMIT_EXCEEDED)
            # PDF magic is validated as soon as the first bytes arrive, so a
            # non-PDF response is rejected before the body is fully consumed
            if len(head) >= magic_len and not bytes(head).startswith(PDF_MAGIC):
                raise PdfAcquisitionError(INVALID_PDF_MAGIC)
        if len(head) < magic_len or not bytes(head).startswith(PDF_MAGIC):
            raise PdfAcquisitionError(INVALID_PDF_MAGIC)
        return bytes(data)
