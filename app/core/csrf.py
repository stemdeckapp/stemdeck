"""Cross-site request guard: the browser-facing half of "no authentication".

StemDeck has no login by design (see the `security` skill). That is safe only
while the one thing that can talk to it is the user's own browser tab on
StemDeck's own page. Without this guard, any web page the user visits can talk
to it too:

- A simple cross-site POST needs no CORS permission. `fetch(url, {mode:
  "no-cors", method: "POST", body})` sends a text/plain body that the browser
  never asks about, and the handlers that call `request.json()` used to parse it
  regardless of the Content-Type. POST /api/reset wiped the library, POST
  /api/settings turned network access on, and on the desktop POST
  /api/settings/stems-location moved the library to a folder the page chose.
- DNS rebinding turns that into read access too: evil.example resolves first to
  the attacker and then to 127.0.0.1, so the attacker's page is "same origin"
  with StemDeck in the browser's eyes. Only the Host header tells them apart.

Three checks, each cheap and each about one of those holes:

1. Unsafe methods (POST, PUT, PATCH, DELETE) from another site are refused.
   `Sec-Fetch-Site: cross-site` is refused outright, and `same-origin` is
   accepted outright: the browser computes it and a page cannot forge it. For a
   browser that sends no Sec-Fetch headers (older Safari), an `Origin` header,
   which browsers always send on a cross-origin POST, must name this server.
   A request with neither header is not from a web page: curl, the Rust shell,
   Python clients and the test suite. Those stay allowed, because CSRF is an
   attack through a victim's browser and they have nothing to protect.

2. The Host header must be a name this server answers to. This is the DNS
   rebinding check, and it is deliberately scoped:
   - Network access OFF (the desktop default): only the host machine is served
     anyway, so the legitimate names are known. Loopback (localhost, *.localhost,
     127.0.0.0/8, ::1), the machine's own IPs and its own host names.
   - Network access ON (the server, Docker and Unraid default): any Host is
     accepted. A Docker container cannot know the address the user typed
     (http://<unraid-ip>:8000 arrives as a Host that is none of the container's
     own IPs), and a reverse proxy may forward any public name. Refusing those
     would lock real users out on upgrade, and a LAN-reachable server with no
     authentication is already readable by every device on that LAN.
   - STEMDECK_ALLOWED_HOSTS set: the list is strict, with network access on or
     off, on top of the names above. That is the opt-in for a proxied or Docker
     install that wants the rebinding protection too. "*" disables the check.

3. A body sent to /api with a Content-Type must be JSON, except on the two
   routes that take something else (the multipart file import and the raw
   take-part upload). This is the belt to check 1's braces: text/plain and form
   bodies are exactly what a no-cors page can send, and no StemDeck route reads
   one. A body with no Content-Type is let through; see _content_type_ok.

Same-origin is judged against the Host header, or X-Forwarded-Host when a proxy
set it. A cross-site page cannot add that header: it is not a simple header, so
the browser would first send a CORS preflight, which StemDeck never answers.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Origins of the desktop shell's own pages (its setup screen), never of the
# main window, which loads http://127.0.0.1:<port> and is plainly same-origin.
# tauri://localhost on macOS and Linux, http(s)://tauri.localhost on Windows.
# Accepted only when the Tauri shell spawned this backend.
TAURI_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)

# The routes whose body is not JSON. Everything else under /api that carries a
# body must send application/json (or a +json type).
_RAW_BODY_ROUTES: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    # File import. The same route takes a JSON link import, which the generic
    # rule covers; only multipart is the extra case.
    ("POST", re.compile(r"^/api/jobs/?$"), "multipart/form-data"),
    # A recorded take arrives as raw float32 or video bytes, any type.
    ("PUT", re.compile(r"^/api/jobs/[^/]+/takes/[^/]+/parts/[^/]+$"), None),
)

# Kept generic on purpose: the reason is for the log, not for the caller.
REFUSED_CROSS_SITE = "cross-site request refused"
REFUSED_HOST = "unknown host"
REFUSED_CONTENT_TYPE = "Content-Type must be application/json"


@dataclass(frozen=True)
class Refusal:
    status: int
    detail: str
    reason: str  # what the log says; never sent to the client


def split_host(value: str) -> tuple[str, int | None]:
    """A Host header (or netloc) as (lowercase host, port or None).

    Handles "[::1]:8000", "::1" is not a valid Host but is tolerated, and a
    trailing dot ("localhost.") which names the same host."""
    value = value.strip().lower()
    port: int | None = None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host, rest = value[1:end], value[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return host, port
    if value.count(":") == 1:
        host, _, raw_port = value.partition(":")
        if raw_port.isdigit():
            port = int(raw_port)
        value = host
    return value.rstrip("."), port


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme)


def _is_loopback_name(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@lru_cache(maxsize=1)
def own_hostnames() -> frozenset[str]:
    """The names the machine itself goes by: what a user on the host machine
    might type instead of localhost. Best-effort; empty on failure."""
    names: set[str] = set()
    try:
        short = socket.gethostname().strip().lower()
        if short:
            names.update({short, f"{short}.local"})
        fqdn = socket.getfqdn().strip().lower()
        if fqdn:
            names.add(fqdn)
            names.add(fqdn.split(".", 1)[0])
    except OSError:
        pass
    return frozenset(n.rstrip(".") for n in names if n)


def host_allowed(
    host: str,
    *,
    allow_network: bool,
    extra_hosts: Iterable[str],
    local_ips: Iterable[str],
) -> bool:
    """Whether `host` (a bare host name, no port) is a name this server answers to.

    See the module docstring for why network access ON and no extra hosts
    accepts anything."""
    extra = frozenset(extra_hosts)
    if "*" in extra:
        return True
    if not extra and allow_network:
        return True
    if not host:
        return False
    if host in extra or _is_loopback_name(host):
        return True
    ips = {ip.split("%", 1)[0].lower() for ip in local_ips}
    if host.split("%", 1)[0] in ips:
        return True
    return host in own_hostnames()


def _header(headers: Mapping[str, str], name: str) -> str:
    return (headers.get(name) or "").strip()


def _forwarded_host(headers: Mapping[str, str]) -> str:
    """The client-facing Host a proxy recorded, first hop only."""
    xfh = _header(headers, "x-forwarded-host")
    if xfh:
        return xfh.split(",")[0].strip()
    rfc7239 = _header(headers, "forwarded")
    if rfc7239:
        for part in rfc7239.split(",")[0].split(";"):
            key, _, value = part.strip().partition("=")
            if key.lower() == "host":
                return value.strip().strip('"')
    return ""


def origin_matches(origin: str, headers: Mapping[str, str], extra_hosts: Iterable[str]) -> bool:
    """Whether `origin` is this server, as the browser was addressing it.

    The origin's host must equal the Host header's (or the proxy's forwarded
    host). Ports must agree when the Host header names one, so a different
    local app on localhost:3000 is not StemDeck on localhost:8000. When the Host
    carries no port, a proxy terminated the scheme and the default port applies
    on the far side, so only the name is compared. An origin whose host is in
    STEMDECK_ALLOWED_HOSTS matches by name, since proxies differ in what they
    forward."""
    try:
        parts = urlsplit(origin)
        o_host = (parts.hostname or "").lower().rstrip(".")
        o_port = parts.port or _default_port(parts.scheme.lower())
    except ValueError:
        return False
    if not o_host or parts.scheme.lower() not in ("http", "https"):
        return False
    extra = frozenset(extra_hosts)
    if o_host in extra and "*" not in extra:
        return True
    for candidate in (_header(headers, "host"), _forwarded_host(headers)):
        if not candidate:
            continue
        h_host, h_port = split_host(candidate)
        if h_host == o_host and (h_port is None or h_port == o_port):
            return True
    return False


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


def _has_body(headers: Mapping[str, str]) -> bool:
    declared = _header(headers, "content-length")
    if declared:
        return not (declared.isdigit() and int(declared) == 0)
    return "chunked" in _header(headers, "transfer-encoding").lower()


def _content_type_ok(method: str, path: str, headers: Mapping[str, str]) -> bool:
    if not path.startswith("/api/") and path != "/api":
        return True
    if not _has_body(headers):
        return True
    ctype = _header(headers, "content-type")
    if not ctype:
        # No type at all is what curl --data-binary, Python clients and the
        # test suite send, and FastAPI itself reads it as JSON. A browser sends
        # it too (a no-cors fetch of an untyped Blob), but never without an
        # Origin when cross-site, so check 1 has already refused that one.
        return True
    if _is_json(ctype):
        return True
    media = ctype.split(";", 1)[0].strip().lower()
    for route_method, pattern, wanted in _RAW_BODY_ROUTES:
        if method == route_method and pattern.match(path):
            return wanted is None or media == wanted
    return False


def check_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    *,
    allow_network: bool,
    desktop: bool,
    extra_hosts: Iterable[str],
    local_ips: Iterable[str],
) -> Refusal | None:
    """None when the request may proceed, else why it may not.

    `headers` is case-insensitive (Starlette's Headers). Pure: every input that
    can change at runtime is passed in, so tests need no monkeypatching."""
    method = method.upper()
    extra = tuple(extra_hosts)

    # 2. Host, for every method: a rebinding attack reads with GET.
    host_header = _header(headers, "host")
    if host_header:
        host, _ = split_host(host_header)
        if not host_allowed(
            host, allow_network=allow_network, extra_hosts=extra, local_ips=local_ips
        ):
            return Refusal(400, REFUSED_HOST, f"host {host!r} not allowed")

    if method not in UNSAFE_METHODS:
        return None

    # 1. Cross-site writes.
    fetch_site = _header(headers, "sec-fetch-site").lower()
    if fetch_site == "cross-site":
        return Refusal(403, REFUSED_CROSS_SITE, "sec-fetch-site cross-site")
    if fetch_site != "same-origin":
        origin = _header(headers, "origin")
        if origin:
            if origin.lower() == "null":
                return Refusal(403, REFUSED_CROSS_SITE, "opaque origin")
            tauri = desktop and origin.lower() in TAURI_ORIGINS
            if not tauri and not origin_matches(origin, headers, extra):
                return Refusal(403, REFUSED_CROSS_SITE, f"origin {origin!r} not this server")

    # 3. Bodies are JSON.
    if not _content_type_ok(method, path, headers):
        return Refusal(415, REFUSED_CONTENT_TYPE, "non-JSON body")
    return None
