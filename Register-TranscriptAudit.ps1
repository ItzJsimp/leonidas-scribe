<#
.SYNOPSIS
    Registers the weekly Leonidas Scribe transcript audit as a silent scheduled task.
.DESCRIPTION
    Runs Audit-Transcripts.py against dictation history written since the last run
    and leaves a review file in projects/leonidas-scribe/audits/. It never edits
    dictionary.json: the user approves each rule with --apply.

    Weekly rather than every 48 hours. At roughly 10 dictations a day the audit
    needs a week of material before the same mishearing repeats often enough to be
    worth a permanent rule, and each run costs Groq calls.
.PARAMETER Unregister
    Removes the scheduled task instead of creating it.
#>
[CmdletBinding()]
param(
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$TaskName = 'LeonidasScribe-TranscriptAudit'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AuditScript = Join-Path $ScriptDir 'Audit-Transcripts.py'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[TranscriptAudit] Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    } else {
        Write-Host "[TranscriptAudit] Task '$TaskName' is not registered." -ForegroundColor DarkGray
    }
    return
}

if (-not (Test-Path -LiteralPath $AuditScript)) {
    throw "Audit script not found: $AuditScript"
}

$PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) { throw 'python.exe not found on PATH.' }
$PythonwExe = Join-Path (Split-Path $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonwExe)) { $PythonwExe = $PythonExe }

$Action = New-ScheduledTaskAction -Execute $PythonwExe `
    -Argument ('"{0}"' -f $AuditScript) `
    -WorkingDirectory $ScriptDir

# Sunday 09:00. the user's focus peaks in the evening, so a morning run means the
# report is already waiting rather than interrupting a working block.
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '09:00'

# S4U plus Hidden: runs with no console window whether or not the user is logged in.
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings `
    -Description 'Weekly audit of Leonidas Scribe dictation transcripts for speech-to-text errors.' `
    -Force | Out-Null

Write-Host "[TranscriptAudit] Registered '$TaskName' (Sundays 09:00, silent)." -ForegroundColor Green
Write-Host "  Report lands in: $(Join-Path $ScriptDir 'audits')" -ForegroundColor Cyan
Write-Host "  Run now:         Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  Approve rules:   python Audit-Transcripts.py --apply" -ForegroundColor Cyan
