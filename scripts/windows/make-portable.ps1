param(
  [string]$Configuration = "release",
  [string]$OutputRoot    = "dist",
  [string]$PackageName   = "StemDeck-Windows-x64",
  [string]$PackageVersion,
  [switch]$SkipTauriBuild,
  [switch]$CpuOnly,
  [switch]$PublishUpdaterAssets
)

$ErrorActionPreference = "Stop"
$PSNativeCommandErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
  throw "This packaging script must run on Windows."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Stage = Join-Path $Root "$OutputRoot\$PackageName"
$ZipPath = Join-Path $Root "$OutputRoot\$PackageName.zip"
$ChecksumPath = "$ZipPath.sha256"
$PythonDir = Join-Path $Stage "python"
$PythonExe = Join-Path $PythonDir "Scripts\python.exe"
$BackendDir = Join-Path $Stage "backend"
$DesktopDir = Join-Path $Root "desktop"
$TauriDir = Join-Path $DesktopDir "src-tauri"
$TargetExe = Join-Path $TauriDir "target\$Configuration\stemdeck.exe"

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Required command not found on PATH: $Name"
  }
}

function Copy-Tree([string]$Source, [string]$Destination) {
  if (Test-Path $Destination) {
    Remove-Item -Recurse -Force $Destination
  }
  Copy-Item -Recurse -Force $Source $Destination
}

function Copy-TreeContents([string]$Source, [string]$Destination, [string[]]$ExcludeNames = @()) {
  New-Item -ItemType Directory -Force $Destination | Out-Null
  Get-ChildItem -LiteralPath $Source -Force |
    Where-Object { $ExcludeNames -notcontains $_.Name } |
    ForEach-Object {
      Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

function Set-PyvenvValue([string]$ConfigPath, [string]$Key, [string]$Value) {
  $content = Get-Content -LiteralPath $ConfigPath
  $pattern = "^\s*$([regex]::Escape($Key))\s*="
  $line = "$Key = $Value"
  $found = $false
  $updated = foreach ($entry in $content) {
    if ($entry -match $pattern) {
      $found = $true
      $line
    } else {
      $entry
    }
  }
  if (-not $found) {
    $updated += $line
  }
  $utf8NoBom = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllLines($ConfigPath, [string[]]$updated, $utf8NoBom)
}

function Get-PackageVersion {
  if ($PackageVersion) {
    return $PackageVersion.TrimStart("v")
  }
  $tauriConfig = Get-Content -LiteralPath (Join-Path $TauriDir "tauri.conf.json") -Raw |
    ConvertFrom-Json
  return [string]$tauriConfig.version
}

function Bundle-PythonRuntime([string]$VenvDir, [string]$VenvPython) {
  $baseExecutable = (& $VenvPython -c "import sys; print(getattr(sys, '_base_executable', sys.executable))").Trim()
  if (-not (Test-Path $baseExecutable)) {
    throw "Could not locate base Python executable: $baseExecutable"
  }
  $baseHome = Split-Path -Parent $baseExecutable
  $portableBaseHome = Join-Path $VenvDir "base"
  $baseLib = Join-Path $baseHome "Lib"
  $baseDlls = Join-Path $baseHome "DLLs"
  if (-not (Test-Path $baseLib)) {
    throw "Could not locate base Python standard library: $baseLib"
  }

  Write-Host "Bundling Python runtime from $baseHome..."
  New-Item -ItemType Directory -Force $portableBaseHome | Out-Null
  Copy-Item -Force $baseExecutable (Join-Path $portableBaseHome "python.exe")
  $basePythonw = Join-Path $baseHome "pythonw.exe"
  if (Test-Path $basePythonw) {
    Copy-Item -Force $basePythonw (Join-Path $portableBaseHome "pythonw.exe")
  }
  Get-ChildItem -LiteralPath $baseHome -Filter "*.dll" -File -Force |
    Copy-Item -Destination $portableBaseHome -Force
  if (Test-Path $baseDlls) {
    Copy-Tree $baseDlls (Join-Path $portableBaseHome "DLLs")
  }
  Copy-TreeContents $baseLib (Join-Path $portableBaseHome "Lib") @("site-packages")

  $cfg = Join-Path $VenvDir "pyvenv.cfg"
  Set-PyvenvValue $cfg "home" $portableBaseHome
  Set-PyvenvValue $cfg "executable" (Join-Path $portableBaseHome "python.exe")
}

function Invoke-TauriBuild {
  $TauriCli = Join-Path $DesktopDir "node_modules\@tauri-apps\cli\tauri.js"
  if (-not (Test-Path $TauriCli)) {
    throw "Tauri CLI not found at $TauriCli. npm install/ci may have omitted devDependencies."
  }
  & node $TauriCli build
}

function Assert-Fresh-TauriBuild {
  if (-not (Test-Path $TargetExe)) {
    throw "Tauri executable not found at $TargetExe. Remove -SkipTauriBuild or build the NVIDIA package first."
  }

  $exe = Get-Item $TargetExe
  $newerSources = @(
    Get-ChildItem -Path (Join-Path $DesktopDir "ui") -File -Recurse
    Get-ChildItem -Path (Join-Path $TauriDir "src") -File -Recurse
    Get-Item (Join-Path $TauriDir "Cargo.toml")
    Get-Item (Join-Path $TauriDir "tauri.conf.json")
  ) | Where-Object { $_.LastWriteTimeUtc -gt $exe.LastWriteTimeUtc }

  if ($newerSources.Count -gt 0) {
    $list = ($newerSources | Select-Object -First 8 | ForEach-Object { "  - $($_.FullName)" }) -join "`n"
    throw @"
-SkipTauriBuild would package a stale StemDeck.exe.

The existing executable is older than desktop UI/Tauri source files:
$list

Remove -SkipTauriBuild or run the NVIDIA package build first so the CPU package reuses a fresh executable.
"@
  }
}

Require-Command "node"
Require-Command "npm"
Require-Command "cargo"

if (-not (Get-Command "py" -ErrorAction SilentlyContinue) -and -not (Get-Command "python" -ErrorAction SilentlyContinue)) {
  throw "Python launcher not found. Install Python 3.12 on the Windows build agent."
}

if (Test-Path $Stage) {
  Remove-Item -Recurse -Force $Stage
}
if (Test-Path $ZipPath) {
  Remove-Item -Force $ZipPath
}
if (Test-Path $ChecksumPath) {
  Remove-Item -Force $ChecksumPath
}

New-Item -ItemType Directory -Force $Stage | Out-Null
New-Item -ItemType Directory -Force $BackendDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $Stage "data") | Out-Null
foreach ($Dir in @("cache", "downloads", "ffmpeg", "jobs", "logs", "models")) {
  New-Item -ItemType Directory -Force (Join-Path $Stage "data\$Dir") | Out-Null
}
# Portable marker: present in every zip (CPU and NVIDIA alike) so double-
# clicking StemDeck.exe uses .\data next to the exe for ffmpeg/models/config/
# logs instead of AppData (#399). Root-only trust, mirroring cpu-only below.
New-Item -ItemType File -Force (Join-Path $Stage "portable.txt") | Out-Null
if ($CpuOnly) {
  # Root marker only: the app trusts cpu-only solely in the app root (#247).
  # A data\cpu-only copy used to leak into the shared per-user data dir and
  # silently forced later NVIDIA installs onto CPU.
  New-Item -ItemType File -Force (Join-Path $Stage "cpu-only") | Out-Null
}

Copy-Tree (Join-Path $Root "app") (Join-Path $BackendDir "app")
Copy-Tree (Join-Path $Root "static") (Join-Path $BackendDir "static")
$PackageVersion = Get-PackageVersion
$VersionJson = @{ version = $PackageVersion } | ConvertTo-Json -Compress
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path $BackendDir "static\version.json"), $VersionJson + "`n", $utf8NoBom)
Copy-Item -Force (Join-Path $Root "pyproject.toml") (Join-Path $BackendDir "pyproject.toml")
Copy-Item -Force (Join-Path $Root "uv.lock") (Join-Path $BackendDir "uv.lock")
Copy-Item -Force (Join-Path $Root "packaging\windows\README-WINDOWS.txt") (Join-Path $Stage "README-WINDOWS.txt")
Copy-Item -Force (Join-Path $Root "packaging\windows\THIRD_PARTY_NOTICES.txt") (Join-Path $Stage "THIRD_PARTY_NOTICES.txt")

if (Get-Command "py" -ErrorAction SilentlyContinue) {
  & py -3.12 -m venv $PythonDir
} else {
  & python -m venv $PythonDir
}

& $PythonExe -m pip install --upgrade pip

# The project version is git-derived (hatch-vcs). Pin it from $PackageVersion so
# the install doesn't depend on git tags in the build checkout (#169).
if ($PackageVersion) {
  $env:SETUPTOOLS_SCM_PRETEND_VERSION = ($PackageVersion -replace '^v', '')
}
& $PythonExe -m pip install "$Root"

if ($CpuOnly) {
  # Force the slim CPU-only wheel. On Windows the default PyPI torch wheel is
  # already CPU-only, but this also downgrades a build host that resolved a CUDA
  # wheel (e.g. via a cuXXX index in the runner's pip config), so the CPU package
  # is deterministic regardless of the host.
  & $PythonExe -m pip install torch==2.6.0+cpu torchaudio==2.6.0+cpu `
      --index-url https://download.pytorch.org/whl/cpu `
      --force-reinstall --no-deps
}
# Do NOT bundle CUDA torch into the NVIDIA (non-CpuOnly) package. It ships base
# torch and the desktop app installs the CUDA build on first run via
# ensure_torch_device, which picks the cuXXX index matching the detected GPU's
# compute capability / driver. Bundling the CUDA wheel here (~2.5 GB installed)
# pushes the zip past GitHub's 2 GiB release-asset cap and loses that adaptive
# versioning. (Reverts #318; the real GPU-detection fix is #317.)

# audio_separator/onnxruntime: vocal split (#275). Missing here would have
# caught #407 (librosa 1.0.0 dropping audioread, which audio-separator still
# needs) before release instead of after.
& $PythonExe -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile, audio_separator, onnxruntime"

Bundle-PythonRuntime $PythonDir $PythonExe
& $PythonExe -c "import sys, fastapi, uvicorn; print('Portable Python:', sys.executable)"

Write-Host "Stripping venv of build-time and dead-weight artifacts..."
Get-ChildItem -Path $PythonDir -Filter "__pycache__" -Recurse -Directory -Force |
  Remove-Item -Recurse -Force
foreach ($rel in @("torch\include", "torch\share\cmake", "torch\test")) {
  $p = Join-Path $PythonDir "Lib\site-packages\$rel"
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
# Remove C++ static link libraries from torch — needed only for building C++ extensions,
# never for running Python. dnnl.lib alone is ~623 MB.
Get-ChildItem -Path (Join-Path $PythonDir "Lib\site-packages\torch") `
    -Filter "*.lib" -Recurse -File -Force |
  Remove-Item -Force

# The stdlib's own test suite (Lib/test) and every package's bundled test/tests
# directory are never imported by the running app -- pure dead weight that is a
# large share of the ~20k loose files in the shipped venv (#421).
$stdlibTest = Join-Path $PythonDir "base\Lib\test"
if (Test-Path $stdlibTest) { Remove-Item -Recurse -Force $stdlibTest }
Get-ChildItem -Path (Join-Path $PythonDir "Lib\site-packages") -Directory -Force |
  ForEach-Object {
    foreach ($name in @("test", "tests")) {
      $p = Join-Path $_.FullName $name
      if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
  }

# NOTE: do NOT strip .dist-info RECORD files to save space. pip needs RECORD to
# uninstall or replace a package, and the NVIDIA build pip-installs CUDA torch
# into this very venv on the user's first run (install_cuda_torch). Without
# RECORD that turns into "Failed to uninstall ... due to missing RECORD file.
# Installation may result in an incomplete environment" -- a broken torch on the
# machines that most need a working one, to save a few hundred KB.

# The strip above widened what ships (#421), so re-verify the packaged
# interpreter can still import everything the pipeline needs -- the earlier
# import check ran pre-strip and pre-bundle, and would not catch a strip that
# removed something load-bearing. Same rationale as that check: catch it here
# rather than in a release (#407).
& $PythonExe -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile, audio_separator, onnxruntime; print('Post-strip import check OK')"

# Runtime fingerprint, shipped INSIDE python/ in every package (#421).
#
# This is the in-app updater's SAFETY GATE, not a download trigger. The updater
# only ever replaces StemDeck.exe + backend/; it never touches python/, because
# an NVIDIA install rewrites python/ with CUDA torch at first run
# (install_cuda_torch) and swapping the directory would silently drop that user
# back to CPU. So an app-only update is safe exactly when the new release needs
# the same Python dependencies the install already has -- and this id is how the
# updater checks that. When it differs, the updater stands down and points the
# user at the full package download instead.
#
# Derived from uv.lock (the dependency set, identical for both variants) plus
# the bundled interpreter's major.minor. Deliberately NOT the package version,
# which changes every release and would make every update look incompatible;
# and deliberately not the installed wheel list, which differs between the CPU
# and NVIDIA variants by torch's local version tag alone (2.6.0+cpu vs 2.6.0)
# even though their dependency requirements are identical.
$PyMajorMinor = (& $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
$LockHash = (Get-FileHash -Algorithm SHA256 -Path (Join-Path $Root "uv.lock")).Hash.Substring(0, 16).ToLower()
$RuntimeId = "py$PyMajorMinor-$LockHash"
$RuntimeIdJson = @{ runtimeId = $RuntimeId } | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText((Join-Path $PythonDir "runtime-version.json"), $RuntimeIdJson + "`n", $utf8NoBom)
Write-Host "Runtime id  : $RuntimeId"

Push-Location $DesktopDir
try {
  if (Test-Path "package-lock.json") {
    npm ci --include=dev
  } else {
    npm install --include=dev
  }

  if (-not $SkipTauriBuild) {
    $env:CI = "true"  # Woodpecker sets CI=woodpecker; Tauri only accepts true/false
    rustup default stable
    Invoke-TauriBuild
  } else {
    Assert-Fresh-TauriBuild
  }
} finally {
  Pop-Location
}

if (-not (Test-Path $TargetExe)) {
  throw "Tauri executable not found at $TargetExe"
}

Copy-Item -Force $TargetExe (Join-Path $Stage "StemDeck.exe")

Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $ZipPath -Force
$Hash = Get-FileHash -Algorithm SHA256 $ZipPath
Set-Content -Path $ChecksumPath -Value "$($Hash.Hash)  $PackageName.zip"

# Slim "app layer" asset for the in-app updater (#421). The full zip above is
# untouched and remains the only thing a fresh install ever needs; this is an
# extra, much smaller asset used only by an already-installed app updating
# itself, so it never has to re-extract the ~20k-file Python runtime to pick up
# a release that only changed app code.
#
# StemDeck.exe and backend/ are byte-identical between the CPU and NVIDIA
# variants -- the only per-variant difference in the package is the `cpu-only`
# marker at the package root, which the updater never touches -- so this is
# built once, from whichever invocation passes -PublishUpdaterAssets, and needs
# no variant suffix.
#
# There is deliberately NO runtime asset: the updater never replaces python/.
# See the runtime-id note above for why. runtime-version.json is published on
# its own so the updater can check compatibility before offering to update.
if ($PublishUpdaterAssets) {
  $UpdaterAppZipName = "StemDeck-Windows-x64-app"
  $UpdaterAppZipPath = Join-Path $Root "$OutputRoot\$UpdaterAppZipName.zip"
  Compress-Archive -Path (Join-Path $Stage "StemDeck.exe"), (Join-Path $Stage "backend") `
      -DestinationPath $UpdaterAppZipPath -Force
  $AppHash = Get-FileHash -Algorithm SHA256 $UpdaterAppZipPath
  Set-Content -Path "$UpdaterAppZipPath.sha256" -Value "$($AppHash.Hash)  $UpdaterAppZipName.zip"

  $RuntimeIdAssetPath = Join-Path $Root "$OutputRoot\StemDeck-Windows-x64-runtime-version.json"
  [System.IO.File]::WriteAllText($RuntimeIdAssetPath, $RuntimeIdJson + "`n", $utf8NoBom)
}

$Variant = if ($CpuOnly) { "CPU-only" } else { "CUDA/GPU (NVIDIA)" }
Write-Host "Variant     : $Variant"
Write-Host "Staged at   : $Stage"
Write-Host "Zip created : $ZipPath"
Write-Host "Checksum    : $ChecksumPath"
if ($PublishUpdaterAssets) {
  Write-Host "Updater app pack : $UpdaterAppZipPath"
}
