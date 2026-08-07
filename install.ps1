<#
.SYNOPSIS
    Installs local-wisprflow (Windows) — a fully-local dictation tool.

.DESCRIPTION
    Replaces the Linux original's install-system.sh + install-services.sh pair. There is no
    sudo step and nothing system-wide: everything installs per-user, so no Administrator
    rights are needed and nothing outside your profile is touched.

    Order of operations:
      1. find or install Python
      2. create a private virtual environment in this folder
      3. install the Python packages
      4. hand over to wf_setup.py, which walks through the GPU libraries, Ollama, and the
         two models it needs to download (asking before each one)
      5. create Start Menu / Desktop shortcuts
      6. offer to start it

.PARAMETER Yes
    Accept every recommended default without asking. Downloads roughly 7 GB of models.

.PARAMETER NoModels
    Set up the environment and shortcuts but skip the model downloads. Run
    `python wf_setup.py` later to finish.

.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -Yes
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoModels
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvDir = Join-Path $Root '.venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPyw = Join-Path $VenvDir 'Scripts\pythonw.exe'
$IconPath = Join-Path $Root 'assets\wisprflow.ico'

# Python versions we know have wheels for every dependency, most preferred first.
$PreferredPython = @('3.12', '3.11', '3.13', '3.10')

function Write-Header($Text) {
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor Cyan
}

function Write-Step($Number, $Total, $Text) {
    Write-Host ''
    Write-Host "--- Step $Number/$Total : $Text " -ForegroundColor Yellow
}

function Write-Ok($Text)   { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Write-Warn2($Text) { Write-Host "  [!]  $Text" -ForegroundColor Yellow }
function Write-Bad($Text)  { Write-Host "  [X]  $Text" -ForegroundColor Red }

function Confirm-Step($Question, $DefaultYes = $true) {
    if ($Yes) {
        Write-Host "  $Question -> $(if ($DefaultYes) {'yes'} else {'no'}) (auto)"
        return $DefaultYes
    }
    $suffix = if ($DefaultYes) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $a = (Read-Host "  $Question $suffix").Trim().ToLower()
        if ($a -eq '') { return $DefaultYes }
        if ($a -in @('y', 'yes')) { return $true }
        if ($a -in @('n', 'no'))  { return $false }
    }
}

# ---------------------------------------------------------------------------
# Python discovery
# ---------------------------------------------------------------------------
function Get-PythonVersion($Exe, $PreArgs = @()) {
    try {
        $out = & $Exe @PreArgs '-c' 'import sys;print("%d.%d"%sys.version_info[:2])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
    } catch { }
    return $null
}

function Find-Python {
    # The `py` launcher is the reliable way to pick a specific version on Windows;
    # `python` on PATH may be the Microsoft Store stub, which cannot create venvs.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in $PreferredPython) {
            $got = Get-PythonVersion 'py' @("-$v")
            if ($got -eq $v) {
                return [pscustomobject]@{ Exe = 'py'; PreArgs = @("-$v"); Version = $got }
            }
        }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Skip the Store alias: a 0-byte reparse point that only opens the Store.
        if ($cmd.Source -like '*WindowsApps*') { continue }
        $got = Get-PythonVersion $cmd.Source
        if ($got) {
            $parts = $got.Split('.')
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 9) {
                return [pscustomobject]@{ Exe = $cmd.Source; PreArgs = @(); Version = $got }
            }
        }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Bad 'winget is not available, so Python cannot be installed automatically.'
        Write-Host '       Install Python 3.12 from https://www.python.org/downloads/windows/'
        Write-Host '       (tick "Add python.exe to PATH"), then re-run this installer.'
        return $false
    }
    Write-Host '  Installing Python 3.12 via winget...'
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    # winget updates PATH for NEW processes only; refresh it for this one so the freshly
    # installed interpreter is discoverable without asking the user to reopen the window.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'User')
    return $true
}

# ---------------------------------------------------------------------------
# Shortcuts
# ---------------------------------------------------------------------------
function New-Shortcut($Path, $Target, $Arguments, $Description, $WindowStyle = 1) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $Root
    $lnk.Description = $Description
    $lnk.WindowStyle = $WindowStyle
    if (Test-Path $IconPath) { $lnk.IconLocation = $IconPath }
    $lnk.Save()
}

# ===========================================================================
# Main
# ===========================================================================
Write-Header 'local-wisprflow for Windows — installer'
Write-Host 'Fully-local dictation: press a hotkey, speak, and clean punctuated text is'
Write-Host 'typed into whatever app you are using. Nothing is sent to any server.'
Write-Host ''
Write-Host "  Install location : $Root"
Write-Host "  Disk space       : ~2 GB for the environment, ~7 GB more with both models"
if ($NoModels) { Write-Host '  Model downloads  : SKIPPED (-NoModels)' -ForegroundColor Yellow }

$TotalSteps = 5

# --- 1. Python -------------------------------------------------------------
Write-Step 1 $TotalSteps 'Python'
$py = Find-Python
if ($py) {
    Write-Ok "Python $($py.Version) found"
} else {
    Write-Warn2 'No suitable Python (3.9+) found.'
    if (-not (Confirm-Step 'Install Python 3.12 now?')) {
        Write-Bad 'Python is required. Aborting.'
        exit 1
    }
    if (-not (Install-Python)) { exit 1 }
    $py = Find-Python
    if (-not $py) {
        Write-Bad 'Python still not found after installation.'
        Write-Host '       Close this window, open a new PowerShell, and re-run the installer.'
        exit 1
    }
    Write-Ok "Python $($py.Version) installed"
}

# --- 2. Virtual environment ------------------------------------------------
Write-Step 2 $TotalSteps 'Virtual environment'
if (Test-Path $VenvPy) {
    Write-Ok "reusing the existing environment at $VenvDir"
} else {
    Write-Host "  creating $VenvDir ..."
    & $py.Exe @($py.PreArgs) '-m' 'venv' $VenvDir
    if (-not (Test-Path $VenvPy)) {
        Write-Bad 'Could not create the virtual environment.'
        exit 1
    }
    Write-Ok 'virtual environment created'
}

# --- 3. Python packages ----------------------------------------------------
Write-Step 3 $TotalSteps 'Python packages'
Write-Host '  upgrading pip ...'
& $VenvPy -m pip install --upgrade pip --quiet
Write-Host '  installing faster-whisper, sounddevice, numpy, requests (a few minutes) ...'
& $VenvPy -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Bad 'pip failed. Check your internet connection and re-run the installer.'
    exit 1
}
Write-Ok 'packages installed'

# --- 4. Models and configuration (delegated to wf_setup.py) ----------------
Write-Step 4 $TotalSteps 'GPU, Ollama and models'
if ($NoModels) {
    Write-Warn2 'Skipped. Run this later to download the models:'
    Write-Host  "       $VenvPy `"$(Join-Path $Root 'wf_setup.py')`""
} else {
    Write-Host '  Handing over to the setup wizard — it will ask before downloading anything.'
    $setupArgs = @((Join-Path $Root 'wf_setup.py'))
    if ($Yes) { $setupArgs += '--yes' }
    & $VenvPy @setupArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 'Setup finished with warnings — see the summary above.'
    }
}

# --- 5. Shortcuts ----------------------------------------------------------
Write-Step 5 $TotalSteps 'Shortcuts'
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\wisprflow'
# WindowStyle 7 = minimized: the daemon itself has no window, but the launcher shim
# would otherwise flash a console.
New-Shortcut (Join-Path $StartMenu 'wisprflow.lnk') $VenvPyw `
    "`"$(Join-Path $Root 'wf_daemon.py')`"" 'Start local-wisprflow dictation' 7
New-Shortcut (Join-Path $StartMenu 'wisprflow — stop.lnk') $VenvPy `
    "`"$(Join-Path $Root 'wf_toggle.py')`" shutdown" 'Stop local-wisprflow' 7
New-Shortcut (Join-Path $StartMenu 'wisprflow — setup.lnk') $VenvPy `
    "`"$(Join-Path $Root 'wf_setup.py')`"" 'Re-run the wisprflow setup wizard' 1
New-Shortcut (Join-Path $StartMenu 'wisprflow — diagnostics.lnk') $VenvPy `
    "`"$(Join-Path $Root 'wf_doctor.py')`"" 'Diagnose wisprflow problems' 1
Write-Ok "Start Menu shortcuts created ($StartMenu)"

$Desktop = [Environment]::GetFolderPath('Desktop')
if ((Test-Path $Desktop) -and (Confirm-Step 'Add a Desktop shortcut?')) {
    New-Shortcut (Join-Path $Desktop 'wisprflow.lnk') $VenvPyw `
        "`"$(Join-Path $Root 'wf_daemon.py')`"" 'Start local-wisprflow dictation' 7
    Write-Ok 'Desktop shortcut created'
}

# --- Done ------------------------------------------------------------------
Write-Header 'Installation complete'
$cfgHotkey = 'Ctrl + Alt + Space'
try {
    # PYTHONPATH rather than a cwd change: the installer may be invoked from anywhere,
    # and `python -c` only puts the *current* directory on sys.path.
    $probe = 'import wf_paths,wf_hotkey;print(wf_hotkey.describe(wf_paths.load_config().get("hotkey","")))'
    $env:PYTHONPATH = $Root
    $got = & $VenvPy -c $probe 2>$null
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -eq 0 -and $got) { $cfgHotkey = $got.Trim() }
} catch { }

Write-Host ''
Write-Host '  How to use it:' -ForegroundColor Cyan
Write-Host "    1. Press $cfgHotkey  -> a 'Listening' pill appears at the bottom of the screen"
Write-Host '    2. Speak'
Write-Host "    3. Press $cfgHotkey again -> the cleaned-up text is typed into the focused app"
Write-Host ''
Write-Host '  Useful commands:' -ForegroundColor Cyan
Write-Host '    wf-start.cmd      start the daemon'
Write-Host '    wf-stop.cmd       stop it'
Write-Host '    wf-doctor.cmd     diagnose problems'
Write-Host '    wf-setup.cmd      re-run the setup wizard (models, hotkey, autostart)'
Write-Host ''
Write-Host "  Config : $(Join-Path $env:APPDATA 'wisprflow\config.json')"
Write-Host "  Logs   : $(Join-Path $env:LOCALAPPDATA 'wisprflow\logs\daemon.log')"
Write-Host ''

if (Confirm-Step 'Start wisprflow now?') {
    Start-Process -FilePath $VenvPyw -ArgumentList "`"$(Join-Path $Root 'wf_daemon.py')`"" `
        -WorkingDirectory $Root
    Write-Ok 'started — look for the wisprflow icon in the system tray'
    Write-Host '       (the first start takes a minute while the speech model loads)'
}
