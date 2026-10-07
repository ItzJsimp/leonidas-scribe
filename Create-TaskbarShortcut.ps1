<#
.SYNOPSIS
    Creates or repairs the Leonidas Scribe Desktop, Start Menu, and pinned taskbar shortcuts.
.DESCRIPTION
    Targets pythonw.exe running LeonidasScribe.py directly. The PyInstaller onefile
    build (dist\LeonidasScribe.exe) is NOT used: a onefile exe re-extracts itself to
    a random temp folder on every launch and resolves its own file paths from that
    temp folder, not the project directory. That meant every run used a frozen
    Sept-1 copy of config.json and dictionary.json (none of that day's fixes ever
    took effect) and wrote history.jsonl into a temp folder that gets deleted when
    the process exits, so dictation history vanished between launches. It was also
    slow to start (extracting ~300MB every time) and, being a stale build, had none
    of the single-instance protection added later, so it could run alongside a
    pythonw instance as a second, unguarded process.

    This script also stamps the shortcuts' own System.AppUserModel.ID property via
    IPropertyStore so Explorer groups a pinned-icon launch with the already-running
    instance into ONE taskbar button, and shows "Leonidas Scribe" (not "Python") at
    the top of the JumpList. Without this explicit property, Explorer infers an
    identity from the target executable, which is pythonw.exe, shared by every
    other Python-launched tool on the machine.

    Safe to re-run any time the shortcuts need repairing: it repairs the pinned
    taskbar shortcut in place along with Desktop and Start Menu, so nothing needs
    to be manually unpinned and re-pinned.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$AppUserModelId = 'Leonidas.Scribe.VoiceDictation.1.0'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DaemonScript = Join-Path $ScriptDir 'LeonidasScribe.py'
$LauncherExe = Join-Path $ScriptDir 'LeonidasScribe.exe'
$IconPath = Join-Path $ScriptDir 'icon.ico'

if (-not (Test-Path $LauncherExe)) {
    Write-Host "[LeonidasScribe] Compiling native launcher LeonidasScribe.exe..." -ForegroundColor Cyan
    & (Join-Path $ScriptDir 'Build-LeonidasScribeLauncher.ps1')
}

$PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) { throw 'python.exe not found on PATH.' }
$PythonwExe = Join-Path (Split-Path $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonwExe)) { $PythonwExe = $PythonExe }

Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ShortcutAppId
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    private interface IShellLinkW
    {
        void GetPath(System.Text.StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription(System.Text.StringBuilder pszName, int cch);
        void SetDescription(string pszName);
        void GetWorkingDirectory(System.Text.StringBuilder pszDir, int cch);
        void SetWorkingDirectory(string pszDir);
        void GetArguments(System.Text.StringBuilder pszArgs, int cch);
        void SetArguments(string pszArgs);
        void GetHotkey(out short wHotkey);
        void SetHotkey(short wHotkey);
        void GetShowCmd(out int iShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation(System.Text.StringBuilder pszIconPath, int cch, out int piIcon);
        void SetIconLocation(string pszIconPath, int iIcon);
        void SetRelativePath(string pszPathRel, uint dwReserved);
        void Resolve(IntPtr hwnd, uint fFlags);
        void SetPath(string pszFile);
    }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")]
    private interface IPropertyStore
    {
        void GetCount(out uint cProps);
        void GetAt(uint iProp, out PROPERTYKEY pkey);
        void GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
        void SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROPERTYKEY
    {
        public Guid fmtid;
        public uint pid;
        public PROPERTYKEY(Guid g, uint p) { fmtid = g; pid = p; }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROPVARIANT
    {
        public ushort vt;
        public ushort r1, r2, r3;
        public IntPtr p;
        public int p2;
    }

    [DllImport("ole32.dll")]
    private static extern int PropVariantClear(ref PROPVARIANT pvar);

    private const ushort VT_LPWSTR = 31;

    // System.AppUserModel.ID
    private static readonly PROPERTYKEY PKEY_AppUserModel_ID =
        new PROPERTYKEY(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

    public static void SetAppId(string lnkPath, string appId)
    {
        var link = (IShellLinkW)new ShellLink();
        var store = (IPropertyStore)link;
        var persist = (System.Runtime.InteropServices.ComTypes.IPersistFile)link;
        persist.Load(lnkPath, 2); // STGM_READWRITE, needed to Commit() the property store

        // Built by hand: propsys's InitPropVariantFromString is not reliably
        // P/Invoke-able across Windows builds, and a VT_LPWSTR variant is only a
        // tag plus a pointer, cheap to construct directly.
        var pv = new PROPVARIANT();
        pv.vt = VT_LPWSTR;
        pv.p = Marshal.StringToCoTaskMemUni(appId);
        try
        {
            var key = PKEY_AppUserModel_ID;
            store.SetValue(ref key, ref pv);
            store.Commit();
        }
        finally
        {
            PropVariantClear(ref pv);
        }
        persist.Save(lnkPath, true);
    }
}
'@

function New-ScribeShortcut([string]$Path) {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($Path)
    $lnk.TargetPath = $LauncherExe
    $lnk.Arguments = ""
    $lnk.WorkingDirectory = $ScriptDir
    $lnk.IconLocation = "$IconPath, 0"
    $lnk.Description = 'Leonidas Scribe: Voice Dictation for AIOS'
    $lnk.Save()
    [ShortcutAppId]::SetAppId($Path, $AppUserModelId)
}

$Targets = @(
    (Join-Path ([System.Environment]::GetFolderPath('Desktop')) 'Leonidas Scribe.lnk'),
    (Join-Path ([System.Environment]::GetFolderPath('Programs')) 'Leonidas Scribe.lnk')
)

# Repair the pinned taskbar shortcut in place, if it exists, so nothing needs
# unpinning and re-pinning.
$PinnedDir = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
if (Test-Path -LiteralPath $PinnedDir) {
    $pinned = Get-ChildItem -LiteralPath $PinnedDir -Filter 'Leonidas Scribe*.lnk' -ErrorAction SilentlyContinue
    foreach ($p in $pinned) { $Targets += $p.FullName }
}

foreach ($t in $Targets) {
    New-ScribeShortcut $t
    Write-Host "[LeonidasScribe] Repaired: $t" -ForegroundColor Green
}

Write-Host ''
Write-Host '[LeonidasScribe] All shortcuts now target LeonidasScribe.exe native launcher directly.' -ForegroundColor Cyan
Write-Host '[LeonidasScribe] AppUserModelID stamped so the pinned icon groups with a running instance.' -ForegroundColor Cyan
if (-not $Targets -or $Targets.Count -eq 2) {
    Write-Host "[LeonidasScribe] No pinned taskbar shortcut found at $PinnedDir." -ForegroundColor Yellow
    Write-Host '  If Leonidas Scribe is pinned, right-click it -> Unpin, then re-pin the Desktop shortcut once.' -ForegroundColor Yellow
}
