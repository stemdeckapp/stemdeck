# Two-stage build:
#   * builder  -- has gcc/build-essential/curl/unzip/pip etc.; produces
#                 an isolated /opt/venv with all Python deps installed
#                 and a fetched deno binary.
#   * runner   -- only the runtime needs: Python (slim), ffmpeg,
#                 ca-certificates, the venv copied from the builder,
#                 and our app code. No compilers, no pip cache, no
#                 download tooling -- shaves ~150-300 MB off the
#                 single-stage image and reduces attack surface.

# ─── Stage 1: builder ────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for pip wheels that fall back to source compilation
# (numpy/scipy ship manylinux wheels, but smaller deps occasionally
# need a compiler). Plus curl/unzip to fetch deno.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# deno -- yt-dlp uses it as the JS runtime for YouTube format
# extraction. Staged here so the runner doesn't need curl/unzip.
ARG TARGETARCH
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) DENO_ARCH=x86_64-unknown-linux-gnu ;; \
        arm64) DENO_ARCH=aarch64-unknown-linux-gnu ;; \
        *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && /usr/local/bin/deno --version

# Isolated venv -- the runner stage copies /opt/venv wholesale,
# inheriting all installed deps without dragging in pip caches,
# /var/lib/apt state, or build tools.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml ./
COPY app/ ./app/
COPY static/ ./static/
RUN pip install . --timeout 300

# ─── Stage 2: runner ─────────────────────────────────────────────
FROM python:3.12-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TORCH_HOME=/cache/torch \
    XDG_CACHE_HOME=/cache

# Runtime-only OS deps. ffmpeg has many shared libs (libav*, libsw*,
# libpostproc), so we install via apt rather than copy-pasting binaries
# and chasing transitive .so dependencies. ca-certificates is needed
# for HTTPS to YouTube.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno binary fetched in stage 1 (single self-contained file).
COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno

# Pre-built Python virtualenv with all our deps. The PATH env above
# means `python`, `uvicorn`, `pip`, etc. resolve into the venv with
# no further configuration.
COPY --from=builder /opt/venv /opt/venv

# Application code -- copied to /app rather than only relied-on via
# site-packages because app/core/config.py derives STATIC_DIR from
# __file__'s parent chain, and the cwd-rooted /app/app version sits
# earlier on sys.path than the venv install of `app`. Without the
# COPY, STATIC_DIR would resolve into site-packages where static/
# wasn't included by hatchling's wheel target.
WORKDIR /app
COPY app/ ./app/
COPY static/ ./static/

# Run as a non-root user. /app is owned by app so the process can
# write nothing surprising; /cache is writable for torch/demucs model
# downloads and /jobs is the standard mount point for job artifacts.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home /app --no-create-home app \
    && mkdir -p /cache /jobs \
    && chown -R app:app /app /cache /jobs
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
