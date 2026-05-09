param(
  [string]$Configuration = "release",
  [string]$OutputRoot    = "dist",
  [string]$PackageName   = "StemDeck-Windows-x64",
  [switch]$SkipTauriBuild,
  [switch]$CpuOnly,
  [switch]$StripVenv
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
if ($CpuOnly) {
  New-Item -ItemType File -Force (Join-Path $Stage "cpu-only") | Out-Null
  New-Item -ItemType File -Force (Join-Path $Stage "data\cpu-only") | Out-Null
}

Copy-Tree (Join-Path $Root "app") (Join-Path $BackendDir "app")
Copy-Tree (Join-Path $Root "static") (Join-Path $BackendDir "static")
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

& $PythonExe -m pip install "$Root"

if ($CpuOnly) {
  # pip strips local version identifiers when resolving requirements, so it installs
  # the CUDA wheel from PyPI even when we pre-install the CPU wheel. Force-reinstall
  # after the fact: uninstalls CUDA torch and replaces it with the CPU-only variant.
  & $PythonExe -m pip install torch==2.6.0+cpu torchaudio==2.6.0+cpu `
      --index-url https://download.pytorch.org/whl/cpu `
      --force-reinstall --no-deps
}

& $PythonExe -c "import fastapi, uvicorn, yt_dlp, demucs, torch, torchaudio, librosa, pyloudnorm, soundfile"

if ($StripVenv) {
  Write-Host "Stripping venv of build-time artifacts..."
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
}

Push-Location $DesktopDir
try {
  if (Test-Path "package-lock.json") {
    npm ci
  } else {
    npm install
  }

  if (-not $SkipTauriBuild) {
    $env:CI = "true"  # Woodpecker sets CI=woodpecker; Tauri only accepts true/false
    rustup default stable
    npm run tauri build
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

$Variant = if ($CpuOnly) { "CPU-only" } else { "CUDA/GPU (NVIDIA)" }
Write-Host "Variant     : $Variant"
Write-Host "Staged at   : $Stage"
Write-Host "Zip created : $ZipPath"
Write-Host "Checksum    : $ChecksumPath"
