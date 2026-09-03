"""Text is compressed on the way out; audio and event streams are not.

Opening the phone UI costs about 456 KB of JavaScript, CSS and HTML, and gzip
takes that to roughly 126 KB. The saving is worth having, but the middleware
that provides it sits in front of every response StemDeck makes, so most of
what is pinned here is what it must leave alone.

Two responses in particular. A five-second window of a stem comes back as
`206 Partial Content`; compressing one rewrites `Content-Length` while
`Content-Range` still describes the uncompressed bytes, which browsers do not
agree on how to read. And the job and queue progress streams are
`text/event-stream`, where a compressor's buffer is a stall.

Both failures are quiet -- a stalled stream and a mangled range window both look
like the audio engine misbehaving -- so they are pinned here rather than left to
be noticed.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from app.core.compression import TextGZipMiddleware

# Comfortably over MINIMUM_SIZE, and repetitive enough that a failure to
# compress is unmistakable rather than marginal.
BIG_TEXT = "export const table = {};\n" * 2000
PCM = bytes(range(256)) * 400  # ~100 KB of audio-shaped bytes


def _app() -> Starlette:
    async def script(_request):
        return Response(BIG_TEXT, media_type="application/javascript")

    async def page(_request):
        return Response(f"<!doctype html>{BIG_TEXT}", media_type="text/html")

    async def small(_request):
        return PlainTextResponse("tiny")

    async def audio_range(_request):
        # What FileResponse produces for a Range request on a stem.
        return Response(
            PCM,
            status_code=206,
            media_type="audio/wav",
            headers={
                "Content-Range": f"bytes 0-{len(PCM) - 1}/99999999",
                "Accept-Ranges": "bytes",
            },
        )

    async def audio_full(_request):
        return Response(PCM, media_type="audio/wav")

    async def events(_request):
        async def stream():
            for i in range(3):
                yield f"data: {i}\n\n".encode()

        return StreamingResponse(stream(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/script.js", script),
            Route("/page", page),
            Route("/small", small),
            Route("/range", audio_range),
            Route("/audio", audio_full),
            Route("/events", events),
        ]
    )
    app.add_middleware(TextGZipMiddleware, minimum_size=1024, compresslevel=6)
    return app


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://testserver",
        # httpx decodes transparently, which would hide the header under test.
        headers={"Accept-Encoding": "gzip"},
    )


async def test_javascript_is_compressed(client) -> None:
    async with client as c:
        resp = await c.get("/script.js")
    assert resp.headers["content-encoding"] == "gzip"
    # The decoded body is still the file that was asked for.
    assert resp.text == BIG_TEXT
    assert int(resp.headers["content-length"]) < len(BIG_TEXT) / 3


async def test_html_is_compressed(client) -> None:
    async with client as c:
        resp = await c.get("/page")
    assert resp.headers["content-encoding"] == "gzip"


async def test_a_range_window_of_audio_is_left_alone(client) -> None:
    """The one that would break playback rather than merely slow it.

    Content-Range describes the uncompressed representation. Rewriting the body
    underneath it leaves the two disagreeing, and the chunked engine reads the
    bytes raw.
    """
    async with client as c:
        resp = await c.get("/range")
    assert resp.status_code == 206
    assert "content-encoding" not in resp.headers
    assert resp.content == PCM
    assert resp.headers["content-range"] == f"bytes 0-{len(PCM) - 1}/99999999"
    assert int(resp.headers["content-length"]) == len(PCM)


async def test_whole_audio_files_are_left_alone(client) -> None:
    """PCM does not compress, and the host may be running Demucs."""
    async with client as c:
        resp = await c.get("/audio")
    assert "content-encoding" not in resp.headers
    assert resp.content == PCM


async def test_event_streams_are_left_alone(client) -> None:
    """A compressor's buffer on an SSE stream is a stall with no error."""
    async with client as c:
        resp = await c.get("/events")
    assert "content-encoding" not in resp.headers
    assert resp.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"


async def test_small_responses_are_not_worth_it(client) -> None:
    async with client as c:
        resp = await c.get("/small")
    assert "content-encoding" not in resp.headers
    assert resp.text == "tiny"


async def test_a_client_that_did_not_ask_gets_plain_bytes() -> None:
    """Curl without a header, and anything older than gzip itself."""
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/script.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in resp.headers
    assert resp.text == BIG_TEXT


async def test_the_real_app_compresses_its_javascript() -> None:
    """The middleware is actually registered, not merely importable.

    i18n.js is the single largest asset the phone loads and the reason this
    exists. StaticFiles streams it, so there is no Content-Length to read here
    -- the size win is pinned above, on a response that has one.
    """
    from app.core.config import STATIC_DIR
    from app.main import app

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/js/i18n.js", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    # Decoded, it is still byte-for-byte the file on disk.
    assert resp.content == (STATIC_DIR / "js" / "i18n.js").read_bytes()


async def test_the_real_app_leaves_a_stem_range_alone(tmp_path, monkeypatch) -> None:
    """End to end on the route the phone actually streams audio through."""
    from app.core.models import Job
    from app.core.registry import _jobs, register
    from app.main import app

    job_id = "abcdefabcdef"
    stems = tmp_path / job_id / "stems"
    stems.mkdir(parents=True)
    (stems / "drums.wav").write_bytes(PCM)
    monkeypatch.setattr("app.api.stems.JOBS_DIR", tmp_path)
    _jobs.clear()
    register(Job(id=job_id, status="done", title="Range"))
    try:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.get(
                f"/api/jobs/{job_id}/stems/drums.wav",
                headers={"Accept-Encoding": "gzip", "Range": "bytes=0-999"},
            )
    finally:
        _jobs.clear()
    assert resp.status_code == 206
    assert "content-encoding" not in resp.headers
    assert resp.content == PCM[:1000]
