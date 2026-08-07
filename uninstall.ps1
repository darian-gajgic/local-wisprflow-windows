<#
.SYNOPSIS
    Removes local-wisprflow from this machine.

.DESCRIPTION
    Stops the daemon, removes the shortcuts, the virtual environment and the local state.
    Deliberately does NOT remove things you may want for other reasons:
      * Ollama itself and its models (uninstall via Settings > Apps, or `ollama rm gemma3:4b`)
      * the Hugging Face cache holding the Whisper model (~/.cache/huggingface)
      * your meeting transcripts
    Each is reported at the end with the command to remove it, so nothing several-GB-sized
    disappears without you asking for it.

.PARAMETER KeepConfig
    Leave %APPDATA%\wisprflow\config.json in place (useful when reinstalling).
#>
[CmdletBinding()]
param([switch]$KeepConfig)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Write-Ok($Text)   { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Write-Warn2($Text) { Write-Host "  [!]  $Text" -ForegroundColor Yellow }

Write-Host ''
Write-Host ('=' * 72) -ForegroundColor Cyan
Write-Host 'local-wisprflow — uninstall' -ForegroundColor Cyan
Write-Host ('=' * 72) -ForegroundColor Cyan

# --- stop the daemon -------------------------------------------------------
Write-Host ''
Write-Host 'Stopping the daemon...'
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    & $venvPy (Join-Path $Root 'wf_toggle.py') shutdown 2>$null | Out-Null
    Start-Sleep -Seconds 2
}
Write-Ok 'daemon stopped (if it was running)'

# --- shortcuts -------------------------------------------------------------
$targets = @(
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\wisprflow'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\wisprflow.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'wisprflow.lnk')
)
foreach ($t in $targets) {
    if (Test-Path $t) {
        Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue
        Write-Ok "removed $t"
    }
}

# --- virtual environment ---------------------------------------------------
$venv = Join-Path $Root '.venv'
if (Test-Path $venv) {
    Remove-Item -Recurse -Force $venv -ErrorAction SilentlyContinue
    Write-Ok 'removed the virtual environment'
}

# --- local state -----------------------------------------------------------
$localState = Join-Path $env:LOCALAPPDATA 'wisprflow'
if (Test-Path $localState) {
    Remove-Item -Recurse -Force $localState -ErrorAction SilentlyContinue
    Write-Ok 'removed logs and runtime state'
}

$configDir = Join-Path $env:APPDATA 'wisprflow'
if (Test-Path $configDir) {
    if ($KeepConfig) {
        Write-Warn2 "kept your configuration at $configDir"
    } else {
        Remove-Item -Recurse -Force $configDir -ErrorAction SilentlyContinue
        Write-Ok 'removed the configuration'
    }
}

# --- what was deliberately left behind -------------------------------------
Write-Host ''
Write-Host 'Left in place on purpose:' -ForegroundColor Cyan
Write-Host '  * Ollama and its models      -> uninstall in Settings > Apps, or: ollama rm gemma3:4b'
$hf = if ($env:HF_HOME) { $env:HF_HOME } else { Join-Path $env:USERPROFILE '.cache\huggingface' }
Write-Host "  * The Whisper model cache    -> $hf"
Write-Host '  * Meeting transcripts        -> Documents\wf-meetings'
Write-Host "  * This folder                -> $Root  (delete it yourself when you are done)"
Write-Host ''
Write-Host 'Uninstall complete.' -ForegroundColor Green
