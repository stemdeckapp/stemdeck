"""Measure backend coverage, including the worker subprocesses.

The separation, vocal split, section and transcription workers run as
`python -m app.pipeline.<name>_worker` child processes. Plain `pytest --cov`
only traces the test process, so they read 0% however well their code is
exercised. This runs the suite under `coverage run` with `patch = subprocess`,
puts a sitecustomize.py on PYTHONPATH so every child Python starts coverage
too (the `.pth` coverage installs only works from site-packages), then combines
the data files.

coverage is not a project dependency. Run it with one on hand, for example:

    uv run --with "coverage>=7.10" python scripts/coverage_run.py tests/ -q

Arguments go to pytest. Output: a terminal report and data/coverage-run/coverage.json.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "coverage-run"

SITECUSTOMIZE = """\
import os
if os.getenv("COVERAGE_PROCESS_START") or os.getenv("COVERAGE_PROCESS_CONFIG"):
    try:
        import coverage
    except ImportError:
        pass
    else:
        coverage.process_startup()
"""


def main() -> int:
    try:
        import coverage
    except ImportError:
        print('coverage is not installed: uv run --with "coverage>=7.10" python ' + __file__)
        return 2
    if tuple(int(p) for p in coverage.__version__.split(".")[:2]) < (7, 10):
        print(f"coverage {coverage.__version__} has no patch = subprocess; 7.10 or later needed")
        return 2

    shutil.rmtree(OUT, ignore_errors=True)
    site = OUT / "site"
    site.mkdir(parents=True)
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    # Absolute paths throughout: a child may run from another working directory.
    rc = OUT / "coveragerc"
    rc.write_text(
        "[run]\n"
        f"source = {ROOT / 'app'}\n"
        "branch = True\n"
        "parallel = True\n"
        "patch = subprocess\n"
        f"data_file = {OUT / '.coverage'}\n",
        encoding="utf-8",
    )
    # Coverage's import site and the sitecustomize must both be importable in
    # every child, whatever environment the test hands it.
    cov_site = str(Path(coverage.__file__).resolve().parents[1])
    paths = [str(site), cov_site, os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in paths if p)}

    cov = [sys.executable, "-m", "coverage"]
    run = subprocess.run(
        [*cov, "run", f"--rcfile={rc}", "-m", "pytest", *sys.argv[1:]], cwd=ROOT, env=env
    )
    subprocess.run([*cov, "combine", f"--rcfile={rc}", "-q"], cwd=ROOT, env=env)
    subprocess.run(
        [*cov, "json", f"--rcfile={rc}", "-q", "-o", str(OUT / "coverage.json")], cwd=ROOT, env=env
    )
    subprocess.run([*cov, "report", f"--rcfile={rc}", "--skip-covered"], cwd=ROOT, env=env)
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
