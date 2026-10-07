<#
.SYNOPSIS
    Automated Setup and Initializer for Leonidas Scribe.
.DESCRIPTION
    1. Checks Python 3.10+ requirement.
    2. Installs dependencies from requirements.txt.
    3. Builds the native LeonidasScribe.exe Windows launcher.
    4. Initializes local secrets.json template if missing.
    5. Registers the Windows Task Scheduler startup task (user logon).
    6. Creates a Desktop shortcut.
    7. Launches Leonidas Scribe.
#>
[CmdletBinding()]
param(
    [switch]$SkipLaunch,
    [switch]$NoAutoStart
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "         Leonidas Scribe - Setup & Launch         " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check Python
Write-Host "[1/6] Checking Python installation..." -ForegroundColor Yellow
$PythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    Write-Host "[ERROR] Python was not found in your PATH." -ForegroundColor Red
    Write-Host "Please install Python 3.10+ from https://www.python.org/downloads/ or run:" -ForegroundColor Yellow
    Write-Host "    winget install Python.Python.3.11" -ForegroundColor Green
    Write-Host "Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Yellow
    exit 1
}

$PyVersion = & python.exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "  Found Python $PyVersion at $($PythonCmd.Source)" -ForegroundColor Green

# 2. Install Dependencies
Write-Host "[2/6] Installing required dependencies..." -ForegroundColor Yellow
$ReqFile = Join-Path $ScriptDir "requirements.txt"
if (Test-Path $ReqFile) {
    & python.exe -m pip install -r $ReqFile --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARNING] pip install returned non-zero code. Retrying with verbose output..." -ForegroundColor Yellow
        & python.exe -m pip install -r $ReqFile
    }
    Write-Host "  Dependencies installed successfully." -ForegroundColor Green
} else {
    Write-Host "  requirements.txt not found, skipping pip install." -ForegroundColor DarkGray
}

# 3. Build Native Launcher
Write-Host "[3/6] Compiling native Windows launcher (LeonidasScribe.exe)..." -ForegroundColor Yellow
$BuildScript = Join-Path $ScriptDir "Build-LeonidasScribeLauncher.ps1"
if (Test-Path $BuildScript) {
    try {
        & pwsh -NoProfile -File $BuildScript
    } catch {
        # Fallback to powershell.exe if pwsh is not in PATH
        & powershell.exe -NoProfile -File $BuildScript
    }
}

# 4. Initialize secrets.json
Write-Host "[4/6] Checking local secrets configuration..." -ForegroundColor Yellow
$SecretsPath = Join-Path $ScriptDir "secrets.json"
$TemplatePath = Join-Path $ScriptDir "secrets.template.json"
if (-not (Test-Path $SecretsPath)) {
    if (Test-Path $TemplatePath) {
        Copy-Item $TemplatePath $SecretsPath
        Write-Host "  Initialized fresh secrets.json template." -ForegroundColor Green
    } else {
        '{ "api": { "groq_api_key": "", "cerebras_api_key": "", "gemini_api_key": "" } }' | Out-File -FilePath $SecretsPath -Encoding utf8
        Write-Host "  Created empty secrets.json." -ForegroundColor Green
    }
} else {
    Write-Host "  Existing secrets.json detected." -ForegroundColor Green
}

# 5. Register Startup Task & Shortcuts
Write-Host "[5/6] Configuring Windows auto-start & shortcuts..." -ForegroundColor Yellow
if (-not $NoAutoStart) {
    $InstallScript = Join-Path $ScriptDir "Install-LeonidasScribe.ps1"
    if (Test-Path $InstallScript) {
        try {
            & powershell.exe -NoProfile -File $InstallScript
        } catch {
            Write-Host "  Warning: could not register Windows Task Scheduler task: $_" -ForegroundColor DarkGray
        }
    }
}

# Desktop Shortcut
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutTarget = Join-Path $ScriptDir "LeonidasScribe.exe"
if (-not (Test-Path $ShortcutTarget)) {
    $ShortcutTarget = (Join-Path (Split-Path $PythonCmd.Source) "pythonw.exe")
}
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut((Join-Path $DesktopPath "Leonidas Scribe.lnk"))
if ($ShortcutTarget -like "*LeonidasScribe.exe") {
    $Shortcut.TargetPath = $ShortcutTarget
    $Shortcut.WorkingDirectory = $ScriptDir
} else {
    $Shortcut.TargetPath = $ShortcutTarget
    $Shortcut.Arguments = "`"$(Join-Path $ScriptDir 'LeonidasScribe.py')`""
    $Shortcut.WorkingDirectory = $ScriptDir
    $IconPath = Join-Path $ScriptDir "icon.ico"
    if (Test-Path $IconPath) { $Shortcut.IconLocation = "$IconPath, 0" }
}
$Shortcut.Save()
Write-Host "  Desktop shortcut created." -ForegroundColor Green

# 6. Launch App
Write-Host "[6/6] Ready!" -ForegroundColor Green
if (-not $SkipLaunch) {
    Write-Host ""
    Write-Host "Launching Leonidas Scribe..." -ForegroundColor Cyan
    $LauncherExe = Join-Path $ScriptDir "LeonidasScribe.exe"
    if (Test-Path $LauncherExe) {
        Start-Process -FilePath $LauncherExe -WorkingDirectory $ScriptDir
    } else {
        Start-Process -FilePath "python.exe" -ArgumentList "`"$ScriptDir\LeonidasScribe.py`"" -WorkingDirectory $ScriptDir
    }
}

Write-Host ""
Write-Host "Setup complete! If this is your first run:" -ForegroundColor Green
Write-Host "1. Paste your Groq API key in Settings > Models." -ForegroundColor Cyan
Write-Host "2. Click 'Save changes' at the bottom." -ForegroundColor Cyan
Write-Host "3. Press Mouse5 or Ctrl + Shift + Space to dictate anywhere!" -ForegroundColor Cyan
