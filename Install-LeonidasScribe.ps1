<#
.SYNOPSIS
    Installs or updates the Leonidas Scribe background service in Windows Task Scheduler.
.DESCRIPTION
    Registers Leonidas Scribe to start automatically at user logon using pythonw.exe (windowless).
.PARAMETER Start
    Immediately launches the service after registration.
.PARAMETER Uninstall
    Removes the Scheduled Task from Windows Task Scheduler.
#>
[CmdletBinding()]
param(
    [switch]$Start,
    [switch]$Uninstall
)

$TaskName = "LeonidasScribe"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DaemonScript = Join-Path $ScriptDir "LeonidasScribe.py"

# Find pythonw.exe
$PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
$PythonwExe = Join-Path (Split-Path $PythonExe) "pythonw.exe"
if (-not (Test-Path $PythonwExe)) {
    $PythonwExe = $PythonExe
}

if ($Uninstall) {
    Write-Host "[LeonidasScribe] Removing scheduled task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Stop-Process -Name "pythonw", "pythonw-leonidas-scribe" -ErrorAction SilentlyContinue
    Write-Host "[LeonidasScribe] Uninstalled successfully." -ForegroundColor Green
    return
}

# Run under an icon-stamped copy of pythonw so Task Manager shows the Scribe icon, not Python's.
if ((Split-Path $PythonwExe -Leaf) -eq 'pythonw.exe') {
    & (Join-Path $ScriptDir 'Build-ScribeRuntime.ps1') -PythonwExe $PythonwExe
    $PythonwExe = Join-Path (Split-Path $PythonwExe) 'pythonw-leonidas-scribe.exe'
}

Write-Host "[LeonidasScribe] Registering Task Scheduler task '$TaskName'..." -ForegroundColor Cyan
Write-Host "  Daemon:  $DaemonScript"
Write-Host "  Binary:  $PythonwExe"

# Create Scheduled Task
$Action = New-ScheduledTaskAction -Execute $PythonwExe -Argument "`"$DaemonScript`" --hidden" -WorkingDirectory $ScriptDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0) -Priority 4 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -Hidden
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

# Unregister existing if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal | Out-Null
Write-Host "[LeonidasScribe] Task registered successfully at user logon." -ForegroundColor Green

if ($Start) {
    Write-Host "[LeonidasScribe] Starting task..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "[LeonidasScribe] Service started in background." -ForegroundColor Green
}

