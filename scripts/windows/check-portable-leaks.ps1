<#
.SYNOPSIS
  Runs an extracted portable StemDeck once and lists everything it wrote
  outside its own folder. Exits 1 when it wrote anything there.

.DESCRIPTION
  A portable StemDeck keeps everything in its own folder, except the library
  folder the user picks in Settings. This checks that on a real launch:

  1. Lists, before and after the run, every file changed today and every
     folder under %LOCALAPPDATA%, %APPDATA%, %USERPROFILE%, Documents and
     %ProgramData%, the entries beside the package folder, and every
     HKEY_CURRENT_USER registry entry that names StemDeck.
  2. Starts StemDeck.exe from the package folder, with the environment of a
     double-click (no STEMDECK_*, PYTHON*, XDG_*, TORCH_HOME, HF_*, PIP_* or
     TMPDIR from this shell), drives one ordinary session through
     drive-portable.mjs (setup with the model step skipped, or run in full with
     -WithModels; Settings opened; Export logs pressed and its save dialog
     closed; one short WAV imported and split), then closes the window the
     normal way.
  3. With -RedirectProfile, also points LOCALAPPDATA, APPDATA, TEMP, TMP,
     USERPROFILE (and HOME when set) at an empty folder under -WorkDir. Anything
     that finds the user profile through the environment then writes there,
     where nothing else on the machine writes, so every file in it is the
     package's own leak.

  Other programs write to the same profile folders while this runs. Each
  change is therefore sorted: "StemDeck" (a path that names StemDeck, its
  identifier or this package folder, a file the window downloaded by itself,
  or anything in the redirected profile: the check fails), "Windows" (what
  Windows and the graphics driver write for every program: listed, cannot be
  prevented), "Review" (a path that names a tool StemDeck uses, such as pip,
  torch or WebView2: read it), and "Other" (everything else, written to the
  report file only).

  The driver connects over the DevTools port, and Playwright then sends any
  download the window makes by itself into -WorkDir\driver-temp instead of the
  Downloads folder. Those files are listed as leaks: without the driver they
  would have gone to Downloads unasked.

  Windows only, and never in CI: it needs a desktop session, WebView2 and a
  built package. Uses robocopy /L (lists only, copies nothing) and reg query.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/check-portable-leaks.ps1 `
    -PackageDir data\leak\pkg -WorkDir data\leak\work -Wav data\leak\six-seconds.wav
#>
param(
    [Parameter(Mandatory)][string]$PackageDir,
    [Parameter(Mandatory)][string]$WorkDir,
    [string]$Wav,
    [switch]$RedirectProfile,
    [switch]$WithModels,
    [switch]$Manual,
    [int]$DevToolsPort = 9339,
    [int]$Minutes = 20
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackageDir = (Resolve-Path $PackageDir).Path.TrimEnd('\')
$exe = Join-Path $PackageDir "StemDeck.exe"
if (-not (Test-Path (Join-Path $PackageDir "portable.txt")) -or -not (Test-Path $exe)) {
    throw "$PackageDir is not an extracted portable package (no StemDeck.exe and portable.txt)"
}
New-Item -ItemType Directory -Force $WorkDir | Out-Null
$WorkDir = (Resolve-Path $WorkDir).Path.TrimEnd('\')
if ($WorkDir.StartsWith("$PackageDir\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "-WorkDir must be outside the package folder"
}
if (Get-ChildItem $WorkDir -Force) {
    throw "-WorkDir must be empty: $WorkDir"
}
if (-not $Manual -and -not $Wav) {
    throw "-Wav is needed unless -Manual is given"
}
if (Get-ChildItem (Join-Path $PackageDir "data") -Recurse -File -Force -ErrorAction SilentlyContinue) {
    Write-Warning "data\ already holds files from an earlier run, so this is not a first run"
}

$documents = [Environment]::GetFolderPath("MyDocuments")
$roots = @(
    @{ Path = $env:LOCALAPPDATA; Exclude = @() },
    @{ Path = $env:APPDATA; Exclude = @() },
    @{ Path = $env:USERPROFILE; Exclude = @((Join-Path $env:USERPROFILE "AppData")) },
    @{ Path = $env:ProgramData; Exclude = @() }
)
if (-not $documents.StartsWith("$env:USERPROFILE\", [StringComparison]::OrdinalIgnoreCase)) {
    $roots += @{ Path = $documents; Exclude = @() }
}

function Invoke-RobocopyList {
    param([string]$Root, [string[]]$Exclude, [string[]]$Options)
    $arguments = @($Root, (Join-Path $WorkDir "no-such-destination"), "/L", "/S", "/NJH", "/NJS",
        "/NC", "/FP", "/XJ", "/R:0", "/W:0") + $Options
    if ($Exclude) { $arguments += @("/XD") + $Exclude }
    $lines = & robocopy @arguments
    # robocopy: 0 to 7 is success, 8 means some entries could not be read
    # (in use or access denied), 16 is a fatal error.
    if ($LASTEXITCODE -ge 16) { throw "robocopy failed on $Root with exit code $LASTEXITCODE" }
    $lines
}

# Files changed since the day before the run, as "path|time|size", and all folders.
function Get-ProfileState {
    param([string]$Since)
    $files = @{}
    $folders = @{}
    foreach ($root in $roots) {
        if (-not (Test-Path $root.Path)) { continue }
        foreach ($line in (Invoke-RobocopyList $root.Path $root.Exclude @("/NDL", "/BYTES", "/TS", "/MAXAGE:$Since"))) {
            if ($line -match '^\s*(\d+)\s+(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\s+(.+)$') {
                $files[$Matches[3]] = "$($Matches[2])|$($Matches[1])"
            }
        }
        foreach ($line in (Invoke-RobocopyList $root.Path $root.Exclude @("/E", "/NFL", "/NS"))) {
            $folder = $line.Trim()
            if ($folder) { $folders[$folder.TrimEnd('\')] = $true }
        }
    }
    $beside = @{}
    foreach ($entry in Get-ChildItem (Split-Path $PackageDir) -Force) {
        if ($entry.FullName -in @($PackageDir, $WorkDir)) { continue }
        $beside[$entry.FullName] = "$($entry.LastWriteTimeUtc.Ticks)"
    }
    # reg query prints a key, then its values under it: keep each value with
    # its key, so a value that changed is reported under the right key.
    $registry = @{}
    $key = ""
    foreach ($line in (& reg query HKCU /s /f stemdeck 2>$null)) {
        if ($line -match '^HKEY_') { $key = $line; $registry[$key] = $true }
        elseif ($line.Trim() -and $line -notmatch '^End of search') { $registry["$key :: $($line.Trim())"] = $true }
    }
    @{ Files = $files; Folders = $folders; Beside = $beside; Registry = $registry }
}

# A path the package's own processes would write to: StemDeck by name, its
# Tauri identifier, or this package folder in Windows' own records of it.
$ownPattern = '(?i)(\\StemDeck(\\|$)|\\app\.stemdeck\.desktop(\\|$)|\\StemDeck[^\\]*\.[a-z0-9]{2,5}$|' +
    [regex]::Escape($PackageDir) + '|' + [regex]::Escape($PackageDir.Replace('\', '#')) + ')'
# A tool StemDeck runs, whose cache or crash dump could be StemDeck's or
# another program's: listed for a person to read.
$reviewPattern = '(?i)(\\pip\\|matplotlib|\\torch\\|\\hub\\|huggingface|yt-dlp|\\deno\\|numba|' +
    '__pycache__|EBWebView|EdgeWebView|msedgewebview2|CrashDumps\\(python|ffmpeg|ffprobe|uv)|\\\.cache\\)'
# Claude Code's own folders carry the repository name, never StemDeck's writes.
$toolingPattern = '(?i)(\\\.claude\\|\\Temp\\claude\\|\\claude-cli-nodejs\\)'
# Keys Windows itself writes for every program a user starts (launch counts,
# the Program Compatibility Assistant, the shell's name cache, privacy
# consent). No program can keep Windows from writing them.
$windowsPattern = '(?i)^HKEY_CURRENT_USER\\Software\\(Microsoft\\Windows\\CurrentVersion\\Explorer\\|' +
    'Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\|Classes\\Local Settings\\Software\\Microsoft\\Windows\\Shell\\MuiCache|' +
    'Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\|Microsoft\\Windows\\CurrentVersion\\Search\\)'

# Files Windows and the graphics driver keep for every program that draws or
# looks up a folder: shader caches (the WebView2 GPU process compiles shaders),
# the shell's folder caches, WinINet's cache and the account cache. They follow
# the profile in the environment, so a redirected run finds them there.
$windowsFilePattern = '(?i)\\AppData\\(Local|LocalLow)\\(NVIDIA|AMD|Intel)\\|\\D3DSCache\\|' +
    '\\Microsoft\\(Windows\\(Caches|INetCache|INetCookies|History)|IdentityCache|Feeds Cache|Internet Explorer)\\'

function Get-Category {
    param([string]$Path)
    if ($Path -match $toolingPattern) { return "Other" }
    if ($Path -match $windowsPattern) { return "Windows" }
    if ($Path -match $ownPattern) { return "StemDeck" }
    if ($Path -match $reviewPattern) { return "Review" }
    "Other"
}

$since = (Get-Date).AddDays(-1).ToString("yyyyMMdd")
Write-Host "Taking the 'before' snapshot (a few minutes)..."
$before = Get-ProfileState $since
$started = Get-Date

$profileDir = Join-Path $WorkDir "profile"
$info = New-Object System.Diagnostics.ProcessStartInfo $exe
$info.WorkingDirectory = $PackageDir
$info.UseShellExecute = $false
$removed = @()
foreach ($name in @($info.EnvironmentVariables.Keys)) {
    if ($name -match '^(STEMDECK_|PYTHON|VIRTUAL_ENV|CONDA|XDG_|TORCH_HOME$|HF_|HUGGINGFACE|PIP_|UV_|MPLCONFIGDIR$|NUMBA_|TMPDIR$|WEBVIEW2_)') {
        $info.EnvironmentVariables.Remove($name)
        $removed += $name
    }
}
if ($RedirectProfile) {
    $redirect = @{
        LOCALAPPDATA = Join-Path $profileDir "Local"
        APPDATA      = Join-Path $profileDir "Roaming"
        TEMP         = Join-Path $profileDir "Temp"
        TMP          = Join-Path $profileDir "Temp"
        USERPROFILE  = Join-Path $profileDir "home"
    }
    if ($info.EnvironmentVariables.ContainsKey("HOME")) { $redirect.HOME = Join-Path $profileDir "home" }
    foreach ($name in $redirect.Keys) {
        New-Item -ItemType Directory -Force $redirect[$name] | Out-Null
        $info.EnvironmentVariables[$name] = $redirect[$name]
    }
}
if (-not $Manual) {
    $info.EnvironmentVariables["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=$DevToolsPort"
}

Write-Host "Starting $exe"
$app = [System.Diagnostics.Process]::Start($info)
$driven = "not driven (-Manual)"
if ($Manual) {
    Write-Host "Use StemDeck, then close its window."
    $app.WaitForExit()
} else {
    $driver = Join-Path $PSScriptRoot "drive-portable.mjs"
    # The driver's own temporary files (Playwright's) go to -WorkDir, so they
    # are not mistaken for the package's.
    $driverTemp = Join-Path $WorkDir "driver-temp"
    New-Item -ItemType Directory -Force $driverTemp | Out-Null
    $userTemp = $env:TEMP, $env:TMP
    $env:TEMP = $driverTemp
    $env:TMP = $driverTemp
    $models = if ($WithModels) { "warmup" } else { "skip" }
    & node $driver $DevToolsPort $app.Id (Resolve-Path $Wav).Path $Minutes $models 2>&1 |
        Tee-Object -FilePath (Join-Path $WorkDir "drive.log") | Write-Host
    $driverExit = $LASTEXITCODE
    $env:TEMP, $env:TMP = $userTemp
    $driven = "drive-portable.mjs exit code $driverExit"
    if ($driverExit -eq 3) {
        # No DevTools port: setup runs by itself with -WithModels (with Skip it
        # waits for a press), so wait for the backend it starts and run the
        # same session over HTTP: the settings Settings shows, then one import.
        $until = (Get-Date).AddMinutes($Minutes)
        $port = $null
        while (-not $port -and (Get-Date) -lt $until) {
            $backend = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
                $_.CommandLine -and $_.CommandLine.StartsWith("`"$PackageDir\", [StringComparison]::OrdinalIgnoreCase) -and
                $_.CommandLine -match 'uvicorn .*--port (\d+)'
            } | Select-Object -First 1
            if ($backend -and $backend.CommandLine -match '--port (\d+)') { $port = $Matches[1] } else { Start-Sleep -Seconds 2 }
        }
        if (-not $port) { throw "the package's backend did not start within $Minutes minutes" }
        $base = "http://127.0.0.1:$port"
        while ((Get-Date) -lt $until) {
            try { Invoke-RestMethod "$base/api/health" -TimeoutSec 5 | Out-Null; break } catch { Start-Sleep -Seconds 2 }
        }
        Invoke-RestMethod "$base/api/settings" | Out-Null
        $job = (& curl.exe -s -F "file=@$((Resolve-Path $Wav).Path)" "$base/api/jobs" | ConvertFrom-Json).job_id
        if (-not $job) { throw "the import over HTTP was refused" }
        do {
            Start-Sleep -Seconds 3
            $status = (Invoke-RestMethod "$base/api/jobs/$job").status
        } while ($status -notin @("done", "error", "failed", "cancelled", "stopped") -and (Get-Date) -lt $until)
        $driven += ", session over HTTP instead: settings read, job $job $status"
    }
    Write-Host "Closing the window"
    [void]$app.CloseMainWindow()
    if (-not $app.WaitForExit(90000)) {
        Write-Warning "StemDeck did not exit within 90 s of closing its window; stopping it"
        & taskkill /PID $app.Id /T /F | Out-Null
        $driven += ", window close did not end the app (taskkill used)"
    }
}

# The backend and WebView2 end shortly after the window: wait for every
# process started from, or keeping its profile in, the package folder.
$deadline = (Get-Date).AddSeconds(60)
do {
    $left = @(Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and $_.CommandLine.IndexOf($PackageDir, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $_.CommandLine -notmatch 'check-portable-leaks\.ps1'
        })
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
if ($left.Count -gt 0) {
    $driven += ", $($left.Count) process(es) still running from the package 60 s after exit"
}
Start-Sleep -Seconds 3
$ended = Get-Date
# Another StemDeck (an installed one, another portable folder) writes to the
# same places under its own name: its writes would look like this package's.
$others = @(Get-CimInstance Win32_Process -Filter "Name = 'StemDeck.exe'" | Where-Object {
        $_.CreationDate -lt $ended -and $_.ExecutablePath -ne $exe
    } | ForEach-Object { $_.ExecutablePath })
if ($others) {
    $driven += ", ANOTHER StemDeck ran during the check: $($others -join ', ')"
}

Write-Host "Taking the 'after' snapshot (a few minutes)..."
$after = Get-ProfileState $since

$changes = New-Object System.Collections.Generic.List[object]
$startedText = $started.AddSeconds(-1).ToString("yyyy/MM/dd HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
foreach ($path in $after.Files.Keys) {
    $state = $after.Files[$path]
    if ($before.Files[$path] -eq $state) { continue }
    # Only what changed while StemDeck ran, not what another program wrote
    # while the 'before' snapshot was being taken.
    if (($state.Split('|')[0]) -lt $startedText) { continue }
    $kind = if ($before.Files.ContainsKey($path)) { "changed file" } else { "new file" }
    $changes.Add([pscustomobject]@{ Category = Get-Category $path; Kind = $kind; Path = $path })
}
foreach ($folder in $after.Folders.Keys) {
    if (-not $before.Folders.ContainsKey($folder)) {
        $changes.Add([pscustomobject]@{ Category = Get-Category $folder; Kind = "new folder"; Path = $folder })
    }
}
foreach ($path in $after.Beside.Keys) {
    if ($before.Beside[$path] -ne $after.Beside[$path]) {
        $changes.Add([pscustomobject]@{ Category = "StemDeck"; Kind = "beside the package"; Path = $path })
    }
}
foreach ($entry in $after.Registry.Keys) {
    if (-not $before.Registry.ContainsKey($entry)) {
        $changes.Add([pscustomobject]@{ Category = Get-Category $entry; Kind = "registry"; Path = $entry })
    }
}
if (-not $Manual) {
    foreach ($download in Get-ChildItem $driverTemp -Directory -Filter "playwright-artifacts-*" | Get-ChildItem -File) {
        $changes.Add([pscustomobject]@{ Category = "StemDeck"; Kind = "window download"; Path = $download.FullName })
    }
}
if ($RedirectProfile) {
    # Folders are listed only when empty: one that holds files is listed
    # through them.
    $entries = Get-ChildItem @($redirect.Values | Sort-Object -Unique) -Recurse -Force | Where-Object {
        -not $_.PSIsContainer -or -not (Get-ChildItem $_.FullName -Force)
    }
    foreach ($entry in $entries) {
        $category = if ($entry.FullName -match $windowsFilePattern) { "Windows" } else { "StemDeck" }
        $changes.Add([pscustomobject]@{ Category = $category; Kind = "redirected profile"; Path = $entry.FullName })
    }
}

$sections = [ordered]@{
    StemDeck = "written outside the package folder by the package. Each one is a leak."
    Windows  = "written by Windows or the graphics driver for every program that starts, draws or looks up a folder; no program can prevent these."
    Review   = "names a tool StemDeck uses, but another program on this machine may have written it."
    Other    = "unrelated programs on this machine."
}
$body = @(
    "Portable leak check"
    "Package    : $PackageDir"
    "Run        : $($started.ToUniversalTime().ToString('u')) to $($ended.ToUniversalTime().ToString('u'))"
    "Profile    : $(if ($RedirectProfile) { "redirected to $profileDir" } else { 'the real one' })"
    "Session    : $driven"
    "Env removed: $(if ($removed) { $removed -join ', ' } else { 'none' })"
)
$shown = @()
foreach ($category in $sections.Keys) {
    $entries = @($changes | Where-Object Category -eq $category | Sort-Object Path)
    $lines = @("", "$category ($($entries.Count)): $($sections[$category])") +
        @($entries | ForEach-Object { "  [$($_.Kind)] $($_.Path)" })
    $body += $lines
    if ($category -ne "Other") { $shown += $lines }
}
$report = Join-Path $WorkDir "report.txt"
Set-Content -Path $report -Value $body -Encoding utf8
($body[0..5] + $shown) | Write-Host
Write-Host "Full report: $report"
if (@($changes | Where-Object Category -eq "StemDeck").Count -gt 0) { exit 1 }
exit 0
