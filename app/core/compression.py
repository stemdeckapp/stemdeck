"""Compress the text StemDeck sends, and nothing else.

Opening the phone UI pulls about 456 KB of JavaScript, CSS and HTML, of which
`static/js/i18n.js` alone is 328 KB -- eleven language tables shipped to every
user so that one of them can be read. Over loopback in the desktop webview that
is invisible. Over Wi-Fi to a phone it is the load time, and it gets worse on
https: browsers generally refuse to keep a disk cache for an origin with a
certificate error, so the revalidation that would normally answer 304 fetches
the whole thing again on every visit. Gzip takes that 456 KB to 126 KB.

Starlette ships `GZipMiddleware` and it is nearly right: it already declines
`text/event-stream`, so the job and queue streams keep flowing. What it does
not do is look at the status code or the content type of anything else, and
two of StemDeck's responses must not be touched.

**Range responses.** The phone plays audio through
`static/js/chunkedAudioEngine.js`, which asks for five-second windows with a
`Range` header and gets `206 Partial Content` back. Compressing one rewrites
`Content-Length` while leaving `Content-Range` describing the uncompressed
bytes, and the two disagreeing is a specification corner browsers do not handle
alike. The gain would have been nothing anyway: the payload is PCM.

**Audio and video generally.** WAV does not compress, and paying deflate on a
40 MB stem -- on the same machine that is running Demucs -- costs real time to
save nothing.

So the rule here is an allowlist by content type plus a hard `200`-only gate,
rather than a list of paths, which would silently start compressing a stem the
first time a route moved.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send

# Everything StemDeck serves that is text under the hood. Matched as a prefix,
# so the charset parameter ("text/html; charset=utf-8") does not need listing.
COMPRESSIBLE_TYPES: tuple[str, ...] = (
    "text/",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
)

# Level 9 buys about 2% over level 6 on this content and costs several times the
# CPU. The server doing this may also be separating a track.
COMPRESS_LEVEL = 6

# Below this a gzip header and trailer are most of what gets sent, and the round
# trip dominates either way.
MINIMUM_SIZE = 1024


class _TextOnlyGZipResponder(GZipResponder):
    """Starlette's responder, with a look at the response before committing.

    The decision needs the status and content type, which only exist once the
    application has answered, so it is made here on the way out rather than
    from the request.
    """

    _pass_through = False

    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            # 200 only. That rules out 206 (a range window of audio) and 304
            # (no body to compress), without naming either as a special case.
            self._pass_through = message["status"] != 200 or not content_type.startswith(
                COMPRESSIBLE_TYPES
            )
        if self._pass_through:
            await self.send(message)
            return
        await super().send_with_compression(message)


class TextGZipMiddleware(GZipMiddleware):
    """`GZipMiddleware` restricted to text, and only when the client asked."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("Accept-Encoding", ""):
            # Starlette would run its IdentityResponder here purely to add a
            # Vary header. Nothing between us and the browser on a LAN caches
            # on our behalf, and buffering every response to add one header is
            # not worth it.
            await self.app(scope, receive, send)
            return
        responder = _TextOnlyGZipResponder(
            self.app, self.minimum_size, compresslevel=self.compresslevel
        )
        await responder(scope, receive, send)
