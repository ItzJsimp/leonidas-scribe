<#
.SYNOPSIS
    Retrieves and recopies recent voice dictation transcriptions from Leonidas Scribe history.
.DESCRIPTION
    Reads history.jsonl produced by Leonidas Scribe, displays recent items, and copies the latest (or selected) text to the Windows clipboard.
.PARAMETER Count
    Number of recent transcriptions to show. Defaults to 1 (which automatically copies to clipboard).
.PARAMETER Index
    1-based index from the recent list to copy back to the clipboard.
.PARAMETER Raw
    Copy and display the raw Whisper transcription before LLM polish.
.EXAMPLE
    # View and copy the very last transcription back to the clipboard:
    .\Get-LastTranscription.ps1

    # Copy the raw unpolished text of the last dictation:
    .\Get-LastTranscription.ps1 -Raw

    # View the last 5 transcriptions with raw text:
    .\Get-LastTranscription.ps1 -Count 5 -Raw

    # Copy the 2nd most recent transcription:
    .\Get-LastTranscription.ps1 -Count 5 -Index 2
#>
[CmdletBinding()]
param(
    [int]$Count = 1,
    [int]$Index = 1,
    [switch]$Raw
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$HistoryFile = Join-Path $ScriptDir "history.jsonl"

if (-not (Test-Path -LiteralPath $HistoryFile)) {
    Write-Warning "[LeonidasScribe] No history.jsonl found at $HistoryFile yet. Dictate something first!"
    return
}

$Lines = Get-Content -LiteralPath $HistoryFile -Encoding UTF8
if ($Lines.Count -eq 0) {
    Write-Warning "[LeonidasScribe] History file is currently empty."
    return
}

$Items = @()
foreach ($Line in $Lines) {
    if (-not [string]::IsNullOrWhiteSpace($Line)) {
        try {
            $Items += ($Line | ConvertFrom-Json)
        } catch { }
    }
}

if ($Items.Count -eq 0) {
    Write-Warning "[LeonidasScribe] No valid JSON entries found in history."
    return
}

# Reverse items to have most recent first
$Recent = [System.Linq.Enumerable]::Reverse($Items) | Select-Object -First $Count

if ($Count -eq 1) {
    $Target = $Recent | Select-Object -First 1
    $TextToCopy = if ($Raw) { $Target.raw_text } else { $Target.cleaned_text }
    Set-Clipboard -Value $TextToCopy
    $Kind = if ($Raw) { "raw unpolished transcription" } else { "last transcription" }
    Write-Host "[LeonidasScribe] Copied $Kind to clipboard ($($Target.timestamp)):" -ForegroundColor Green
    Write-Host "`"$TextToCopy`"" -ForegroundColor Cyan
    Write-Host "  Latency: $($Target.metrics.total_ms)ms (STT: $($Target.metrics.stt_ms)ms, Polish: $($Target.metrics.polish_ms)ms)" -ForegroundColor DarkGray
    return
}

Write-Host "`n=== Leonidas Scribe: Last $Count Transcriptions ===" -ForegroundColor Yellow
$i = 1
foreach ($item in $Recent) {
    $prefix = if ($i -eq $Index) { "-> [$i]" } else { "   [$i]" }
    Write-Host "$prefix $($item.timestamp) [$($item.mode.ToUpper())] ($($item.metrics.total_ms)ms)" -ForegroundColor White
    if ($Raw) {
        Write-Host "     RAW:     `"$($item.raw_text)`"" -ForegroundColor Magenta
        Write-Host "     POLISHED:`"$($item.cleaned_text)`"" -ForegroundColor DarkGray
    } else {
        Write-Host "     `"$($item.cleaned_text)`"" -ForegroundColor Cyan
    }
    $i++
}

# Select target item to copy
$TargetItem = ($Recent | Select-Object -Index ($Index - 1))
if ($TargetItem) {
    $ChosenText = if ($Raw) { $TargetItem.raw_text } else { $TargetItem.cleaned_text }
    Set-Clipboard -Value $ChosenText
    $Kind = if ($Raw) { "raw" } else { "cleaned" }
    Write-Host "`n[Copied item #$Index ($Kind) to Clipboard]" -ForegroundColor Green
}

