"""Fail a desktop package build whose torchvision cannot load next to its torch.

Run with the packaged interpreter, so it inspects the tree that ships:

    python check_torch_bundle.py --versions packaging/torch-versions.json [--expect-cpu]

The CPU-only Linux package of 0.20.0 shipped CPU torch beside PyPI's default
Linux torchvision, which is the cu124 build (#778). Its compiled extension
needs libcudart, libc10_cuda and libtorch_cuda, none of which ship, so
`import torchvision` raised "operator torchvision::nms does not exist" and the
karaoke split (audio-separator -> onnx2torch -> torchvision) failed. The old
import check never imported torchvision, so the build passed.

Exits non-zero, listing every problem found, when:
- torch, torchvision or onnx2torch does not import, or torchvision's nms op
  does not run;
- torchvision is not the version packaging/torch-versions.json pairs with the
  installed torch;
- torchvision is a CUDA build beside a torch that is not (always), or either is
  not a +cpu build (with --expect-cpu);
- a compiled torchvision file links a CUDA library, or a CUDA library ships in
  torchvision/ or torchvision.libs/.

Standard library only: it runs before anything else is installed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

# Names of the CUDA runtime pieces a CUDA torchvision links or bundles. Matched
# case-insensitively against NEEDED entries (Linux), import names (Windows) and
# file names. Covers libcudart.so.12, libnvjpeg.02b6d700.so.12,
# cudart64_12.dll, nvjpeg64_12.dll, libc10_cuda.so, c10_cuda.dll and the rest.
CUDA_LIB = re.compile(r"(cudart|nvjpeg|c10_cuda|torch_cuda|cublas|cudnn|nvrtc)", re.IGNORECASE)
COMPILED_SUFFIXES = (".so", ".pyd", ".dll")


def local_tag(version: str) -> str:
    """The part after '+', or '' for a version without one."""
    return version.partition("+")[2]


def public_version(version: str) -> str:
    return version.partition("+")[0]


def linked_libraries(path: Path) -> list[str] | None:
    """What a compiled file links, or None when it cannot be read here."""
    if sys.platform.startswith("linux"):
        readelf = shutil.which("readelf")
        if readelf is None:
            return None
        out = subprocess.run(
            [readelf, "-d", str(path)], capture_output=True, text=True, check=False
        ).stdout
        return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", out)
    if sys.platform == "win32":
        # A PE import table holds each DLL name as plain ASCII, so a byte scan
        # finds every CUDA DLL the file imports. No dumpbin on the build host.
        data = path.read_bytes()
        return sorted(
            {m.decode("ascii") for m in re.findall(rb"[\w.-]+\.dll", data, re.IGNORECASE)}
        )
    return None


def check(versions_file: Path, expect_cpu: bool) -> list[str]:
    problems: list[str] = []
    pairs = json.loads(versions_file.read_text(encoding="utf-8"))["torchvisionForTorch"]

    try:
        import torch
    except Exception as exc:  # noqa: BLE001 - any failure here is the finding
        return [f"import torch failed: {exc!r}"]
    torch_version = torch.__version__
    print(f"torch {torch_version}")

    site = Path(sysconfig.get_paths()["purelib"])
    tv_dir = site / "torchvision"
    tv_version = None
    version_py = tv_dir / "version.py"
    if version_py.is_file():
        found = re.search(
            r"""__version__\s*=\s*['"]([^'"]+)['"]""", version_py.read_text(encoding="utf-8")
        )
        tv_version = found.group(1) if found else None
    if tv_version is None:
        problems.append(f"no torchvision version found at {version_py}")
    else:
        print(f"torchvision {tv_version}")
        expected = pairs.get(public_version(torch_version))
        if expected is None:
            problems.append(
                f"torch {public_version(torch_version)} has no torchvision in {versions_file.name}; add the pair there"
            )
        elif public_version(tv_version) != expected:
            problems.append(
                f"torchvision {tv_version} does not match torch {torch_version}: expected {expected} (#614)"
            )
        if local_tag(tv_version).startswith("cu") and not local_tag(torch_version).startswith("cu"):
            problems.append(
                f"torchvision {tv_version} is a CUDA build beside torch {torch_version} (#778)"
            )
        if expect_cpu:
            for name, version in (("torch", torch_version), ("torchvision", tv_version)):
                if local_tag(version) != "cpu":
                    problems.append(f"{name} {version} is not a +cpu build on a CPU package")

    try:
        import torchvision
        from torchvision.ops import nms

        nms(torch.zeros((1, 4)), torch.zeros(1), 0.5)
        print(f"import torchvision OK ({torchvision.__version__}), nms runs")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"torchvision does not work: {exc!r}")

    try:
        import onnx2torch  # noqa: F401 - the karaoke split's path to torchvision

        print("import onnx2torch OK")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"import onnx2torch failed: {exc!r}")

    for libs_dir in (tv_dir, site / "torchvision.libs"):
        if not libs_dir.is_dir():
            continue
        for path in sorted(libs_dir.rglob("*")):
            if not path.is_file():
                continue
            if CUDA_LIB.search(path.name):
                problems.append(f"CUDA payload shipped: {path.relative_to(site)}")
                continue
            if path.parent == tv_dir and path.name.endswith(COMPILED_SUFFIXES):
                linked = linked_libraries(path)
                if linked is None:
                    problems.append(
                        f"cannot read what {path.name} links (install binutils for readelf)"
                    )
                    continue
                for lib in linked:
                    if CUDA_LIB.search(lib):
                        problems.append(f"{path.relative_to(site)} links {lib}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--versions", required=True, type=Path, help="packaging/torch-versions.json"
    )
    parser.add_argument("--expect-cpu", action="store_true", help="the package bakes CPU torch")
    args = parser.parse_args()
    problems = check(args.versions, args.expect_cpu)
    if problems:
        print("torch bundle check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("torch bundle check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
