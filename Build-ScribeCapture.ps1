<#
.SYNOPSIS
    Compiles the WASAPI capture helper (ScribeCapture.exe) used by Scribe Meetings.
.DESCRIPTION
    Console exe (needs stdout/stdin pipes), built with the in-box C# 5 compiler like Launcher.cs.
    x64 because the process-loopback activation PROPVARIANT is laid out for 64-bit.
#>
[CmdletBinding()]
param()

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CscPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$SourceFile = Join-Path $ScriptDir 'ScribeCapture.cs'
$OutputFile = Join-Path $ScriptDir 'ScribeCapture.exe'

if (-not (Test-Path -LiteralPath $CscPath)) {
    throw "C# compiler not found at $CscPath"
}

Write-Host "[ScribeCapture] Compiling capture helper: $OutputFile" -ForegroundColor Cyan
& $CscPath /nologo /target:exe /platform:x64 /out:$OutputFile $SourceFile

if ($LASTEXITCODE -eq 0) {
    Write-Host "[ScribeCapture] Built ScribeCapture.exe" -ForegroundColor Green
} else {
    throw "Failed to compile ScribeCapture.exe."
}
