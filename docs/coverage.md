# Test coverage

`app/` is at **100% statement and branch coverage**: 5520 statements and 1612
branches, none uncovered, across 1840 tests.

The frontend is covered separately by `tests/js/` (pure-function unit tests run
under bare `node`) and `tests/e2e/` (Playwright against the real backend);
neither is measured here.

## Measuring it

```
COVERAGE_CORE=sysmon python3.12 -m pytest tests/ --cov=app --cov-report=term-missing
```

Everything else is configured in `pyproject.toml` under `[tool.coverage.*]` --
branch mode, the source root, and the exclusions below.

**Use the 3.12 tracer, and set `COVERAGE_CORE=sysmon`.** This is not a
preference. On Python 3.11 coverage falls back to its C tracer, which records
only the suspend/resume points inside an `async` FastAPI handler and reports the
rest of the body as unreached *even when it demonstrably ran* -- a handler can
return the correct response, with the test asserting on it, and still show as
untraced. That understated `app/api/*.py` and `app/main.py` by roughly four
points overall and invented gaps that no test could close. `sys.monitoring`
(3.12+) has no such blind spot. A `--cov-fail-under` gate configured from a 3.11
run measures the tracer, not the suite.

The number CI enforces is the test suite passing, not the coverage figure: CI
runs `pytest` on 3.12 without `--cov`, because pytest-cov is not in the locked
dependency set. Re-measure locally with the command above after adding code.

ffmpeg must be on PATH. The export, mixdown and click-render tests use the real
binary against real audio and fail rather than skip without it. CI installs it.

## What is deliberately not measured

Three exclusions, each with its reason recorded next to it in
`pyproject.toml`:

- **`app/_vendor/*`** -- yt-dlp's JavaScript challenge solver, copied verbatim
  from PyPI by `scripts/update_vendored_ejs.py`. Already excluded from ruff for
  the same reason: its tests live upstream, and anything written against it here
  would be overwritten by the next refresh.
- **`if __name__ == "__main__":`** -- the four worker entry points
  (`demucs_worker`, `vocal_split_worker`, `section_worker`, `warmup`). Each is
  exercised, but through a real subprocess: the workers speak a `@@DONE@@` /
  `@@ERROR@@` protocol over stderr that only exists across a process boundary,
  and the parent's coverage run cannot see into the child.
- **Two `# pragma: no cover` guards in `app/pipeline/section_refine.py`** --
  both provably unreachable as written. `_nearest_beat`'s empty-slice check
  cannot fire because the list is non-empty three lines above and
  `bisect_left` returns `0..len`; the degenerate-boundary check cannot fire
  because `set()` has already collapsed equal positions. They are left in
  place with the reason recorded above each. Whether to keep them is a
  maintainer's call rather than a coverage one.

Two `# pragma: no branch` markers exist for the same kind of reason -- a loop
that always returns or raises before it can complete (`download._with_retries`),
and a `None` check against a helper's signature rather than its behaviour
(`registry.restore`).

## What the suite actually pins

Coverage says every line ran; it does not say the tests are good. What the
Python suite is built around:

- **Contracts, not implementations.** The demucs normalisation test asserts the
  exact relationship the code promises (normalise by the mono mixdown's
  standard deviation) rather than a number that happened to come out.
- **The failure paths, at the same weight as the happy ones.** Kill-and-reap,
  quarantine, partial-file cleanup, the stall watchdogs, torn SSE snapshots,
  symlink escape from the jobs directory. These are the paths that run while
  something has already gone wrong, and each of them leaks a file, a process or
  a GPU allocation if it regresses.
- **Real ffmpeg and real audio where the DSP is the point.** The mixdown tests
  assert on peak amplitude, because `amix` without `normalize=0` silently halves
  every stem and no structural assertion notices.
- **The branch that does not run on the happy path.** The header that is
  absent, the lookup that finds nothing, the cache that already holds the file,
  the loop that ends without breaking. `tests/test_api_branch_edges.py` and
  `tests/test_pipeline_branch_edges.py` exist for exactly these; statement
  coverage cannot see them.

## Keeping it there

There is no gate, so the discipline is manual: run the command at the top of
this file before opening a PR, and treat a new uncovered line as a missing test
rather than a number to adjust. If a line genuinely cannot be reached, say why
in a `# pragma` comment next to it rather than lowering a threshold.
