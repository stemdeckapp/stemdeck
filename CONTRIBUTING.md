# Contributing to StemDeck

Thanks for your interest in StemDeck - free, local stem separation for musicians. Contributions of
all kinds are welcome, whether you write code or not.

By participating you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** - open a [Bug report](https://github.com/stemdeckapp/stemdeck/issues/new/choose).
  Include your OS, the StemDeck version (Help icon -> About), and clear steps to reproduce.
- **Suggest a feature** - open a [Feature request](https://github.com/stemdeckapp/stemdeck/issues/new/choose).
- **Improve the docs** - fixes and clarifications to the README or these guides are always useful.
- **Write code** - bug fixes and features. StemDeck does not accept pull requests directly (see
  below) - open an issue or a [Discussion](https://github.com/stemdeckapp/stemdeck/discussions)
  first so we can agree on the approach before you invest time.
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

## Contributing code without opening a pull request

StemDeck's CI runs on self-hosted infrastructure for some platforms, so pull requests from
outside contributors are not accepted - a PR opened by anyone other than the maintainer will be
closed unreviewed. This is not a comment on the change itself, just how this repo keeps untrusted
code off its own machines.

If you have a code change:

1. Fork the repo and push your change to a branch on your fork.
2. Open an **issue** describing the change, and include a link to your branch (or a compare URL
   like `https://github.com/<you>/stemdeck/compare/main...<you>:stemdeck:<branch>`).
3. The maintainer reviews the diff there. A public fork branch can be fetched and read without
   any special access, so if it's a good fit, they'll pull it into a branch on this repo and take
   it from there - no action needed on your end.

Branch off `main`, use [Conventional Commits](https://www.conventionalcommits.org/) for messages
(`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`), keep the change focused, and
describe what changed, why, and how you tested it in the issue - the same information a PR
description would carry.

## Tests

New API endpoints and pipeline stages should come with tests under `tests/`. The suite uses
`pytest` with `httpx.AsyncClient`; see the existing `tests/test_*.py` files for patterns.

## License

StemDeck is licensed under the [GNU Affero General Public License v3.0 only](LICENSE)
(AGPL-3.0-only). Contributions are accepted under the same license: by contributing, you agree
that your contributions are licensed under AGPL-3.0-only.

## Questions

Open a [Discussion](https://github.com/stemdeckapp/stemdeck/discussions) or join the
[Discord](https://discord.gg/YhCKsjhcwB). Thanks for helping make StemDeck better.
