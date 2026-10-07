<#
.SYNOPSIS
    Compiles the native, lightweight Windows GUI launcher (LeonidasScribe.exe).
.DESCRIPTION
    Builds a 200KB standalone C# executable that embeds the official Leonidas icon,
    handles single-instance IPC signaling (bringing the existing dashboard to focus),
    and launches pythonw.exe LeonidasScribe.py silently without console flicker.
#>
[CmdletBinding()]
param()

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CscPath = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$SourceFile = Join-Path $ScriptDir 'Launcher.cs'
$OutputFile = Join-Path $ScriptDir 'LeonidasScribe.exe'
$IconFile = Join-Path $ScriptDir 'icon.ico'

if (-not (Test-Path $CscPath)) {
    throw "C# compiler not found at $CscPath"
}

Write-Host "[LeonidasScribe] Compiling native Windows launcher: $OutputFile" -ForegroundColor Cyan
& $CscPath /nologo /target:winexe /win32icon:$IconFile /out:$OutputFile $SourceFile

if ($LASTEXITCODE -eq 0) {
    Write-Host "[LeonidasScribe] Successfully built native launcher LeonidasScribe.exe (< 1s build, ~200KB)" -ForegroundColor Green
} else {
    throw "Failed to compile LeonidasScribe.exe."
}
