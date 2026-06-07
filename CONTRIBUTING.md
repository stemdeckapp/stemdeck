# Contributing to StemDeck

Thanks for your interest in StemDeck - free, local stem separation for musicians. Contributions of
all kinds are welcome, whether you write code or not.

By participating you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** - open a [Bug report](https://github.com/stemdeckapp/stemdeck/issues/new/choose).
  Include your OS, the StemDeck version (Help icon -> About), and clear steps to reproduce.
- **Suggest a feature** - open a [Feature request](https://github.com/stemdeckapp/stemdeck/issues/new/choose).
- **Improve the docs** - fixes and clarifications to the README or these guides are always useful.
- **Write code** - bug fixes and features. For anything large, please open an issue or a
  [Discussion](https://github.com/stemdeckapp/stemdeck/discussions) first so we can agree on the
  approach before you invest time.
- **Found a security issue?** Do not open a public issue - see [SECURITY.md](SECURITY.md).

## Project layout

- `app/` - FastAPI backend (the audio pipeline, job registry, and HTTP API).
- `static/` - the web UI: plain ES-module JavaScript, HTML, and CSS. No build step, no bundler.
- `desktop/` - the Tauri v2 desktop shell (Rust) that wraps the backend + UI.
- `scripts/` - packaging scripts (macOS app/dmg, Windows portable, runtime pack).
- `tests/` - the pytest suite for the backend.

## Development setup

You need [`uv`](https://docs.astral.sh/uv/) and `ffmpeg` on your PATH.

```bash
uv sync --python 3.12        # install the Python environment
./run.sh start               # start the backend at http://localhost:8000
```

Other helpers:

```bash
./run.sh restart             # restart after backend changes
./run.sh stop                # stop the server
./run.sh status              # is it running?
```

Open `http://localhost:8000` in your browser to use the app. Frontend changes are picked up on
reload (no build step).

## Before you open a pull request

Please run the same checks CI runs:

```bash
uv run ruff check app/ tests/        # lint
uv run ruff format --check app/ tests/   # formatting
node --check static/js/<changed>.js  # syntax-check any JS you touched
uv run pytest tests/ -q              # backend tests
```

For Rust changes in `desktop/`, also run `cargo fmt --check` and `cargo clippy -- -D warnings`.

## Pull request guidelines

- Branch off `main`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for messages
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`).
- Open the PR as a **draft** until it is ready for review.
- Keep PRs focused - one logical change per PR makes review faster.
- Describe what changed and why, and how you tested it.

## Tests

New API endpoints and pipeline stages should come with tests under `tests/`. The suite uses
`pytest` with `httpx.AsyncClient`; see the existing `tests/test_*.py` files for patterns.

## License

StemDeck is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your
contributions are licensed under the same terms.

## Questions

Open a [Discussion](https://github.com/stemdeckapp/stemdeck/discussions) or join the
[Discord](https://discord.gg/2MVsWqaPRe). Thanks for helping make StemDeck better.
