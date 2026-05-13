#!/usr/bin/env bash
# stemdeck dev server control: setup | start | stop | restart | status

set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
RELOAD="${RELOAD:-0}"
FOREGROUND="${FOREGROUND:-0}"
PID_FILE=".run/uvicorn.pid"
LOG_FILE=".run/uvicorn.log"
UVICORN=".venv/bin/uvicorn"

mkdir -p .run

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start() {
    if is_running; then
        echo "already running (pid $(cat "$PID_FILE"))"
        return 0
    fi
    if [[ ! -x "$UVICORN" ]]; then
        echo "uvicorn not found at $UVICORN — run: uv sync" >&2
        exit 1
    fi
    echo "starting on http://$HOST:$PORT"
    local args=(app.main:app --host "$HOST" --port "$PORT")
    if [[ "$RELOAD" == "1" ]]; then
        args+=(--reload)
    fi
    if [[ "$FOREGROUND" == "1" ]]; then
        echo $$ >"$PID_FILE"
        exec "$UVICORN" "${args[@]}"
    fi
    nohup "$UVICORN" "${args[@]}" >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in {1..10}; do
        sleep 0.5
        grep -q "Application startup complete\|Uvicorn running on" "$LOG_FILE" 2>/dev/null && break
        is_running || break
    done
    if is_running; then
        echo "started (pid $(cat "$PID_FILE"), log: $LOG_FILE)"
    else
        echo "failed to start — see $LOG_FILE" >&2
        exit 1
    fi
}

stop() {
    if ! is_running; then
        echo "not running"
        # also sweep any stray uvicorn for this app
        pkill -f "uvicorn app.main:app" 2>/dev/null || true
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "stopping pid $pid"
    kill "$pid" 2>/dev/null || true
    for _ in {1..10}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "force-killing pid $pid"
        kill -9 "$pid" 2>/dev/null || true
    fi
    # kill any in-flight demucs children spawned by the app
    pkill -f "python -m demucs" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "stopped"
}

status() {
    if is_running; then
        echo "running (pid $(cat "$PID_FILE")) on http://$HOST:$PORT"
    else
        echo "not running"
    fi
}

setup() {
    local os
    case "$(uname -s)" in
        Darwin) os="macos" ;;
        Linux)  os="linux" ;;
        *)
            echo "setup: unsupported OS '$(uname -s)' — install ffmpeg + uv manually" >&2
            exit 1
            ;;
    esac
    echo "detected: $os"

    if [[ "$os" == "macos" ]]; then
        if ! command -v brew >/dev/null 2>&1; then
            echo "setup: Homebrew not found — install from https://brew.sh first" >&2
            exit 1
        fi
        if ! command -v ffmpeg >/dev/null 2>&1; then
            echo "==> brew install ffmpeg"
            brew install ffmpeg
        else
            echo "ffmpeg: already installed ($(ffmpeg -version | head -n1))"
        fi
        if ! command -v uv >/dev/null 2>&1; then
            echo "==> brew install uv"
            brew install uv
        else
            echo "uv: already installed ($(uv --version))"
        fi
    else
        # linux: assume Debian/Ubuntu (apt). Other distros fall through to manual.
        if ! command -v apt-get >/dev/null 2>&1; then
            echo "setup: non-apt Linux detected — install ffmpeg + uv via your package manager" >&2
            exit 1
        fi
        if ! command -v ffmpeg >/dev/null 2>&1; then
            echo "==> sudo apt-get update && sudo apt-get install -y ffmpeg"
            sudo apt-get update
            sudo apt-get install -y ffmpeg
        else
            echo "ffmpeg: already installed ($(ffmpeg -version | head -n1))"
        fi
        if ! command -v uv >/dev/null 2>&1; then
            echo "==> installing uv via astral.sh installer"
            curl -LsSf https://astral.sh/uv/install.sh | sh
            # installer drops uv in ~/.local/bin; surface it for this shell
            export PATH="$HOME/.local/bin:$PATH"
        else
            echo "uv: already installed ($(uv --version))"
        fi
    fi

    echo "==> uv sync"
    uv sync

    echo
    echo "setup complete. start the server with: ./run.sh start"
}

case "${1:-}" in
    setup)   setup ;;
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    *)
        echo "usage: $0 {setup|start|stop|restart|status}" >&2
        exit 2
        ;;
esac
