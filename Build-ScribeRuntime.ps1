#Requires -Version 7.0
<#
.SYNOPSIS
    Builds pythonw-leonidas-scribe.exe: a copy of pythonw.exe carrying the Leonidas Scribe icon.
.DESCRIPTION
    Task Manager takes a process's icon from its image file, so Scribe running under pythonw.exe
    shows the generic Python icon. This copies pythonw.exe next to python314.dll (Python finds its
    Lib\ relative to the exe) and swaps the icon resource for icon.ico. The name keeps "python" in
    it on purpose: leonidas-voice's call gate ignores mic holders whose exe path contains "python",
    and would otherwise treat Scribe's always-open mic as a meeting.
    Idempotent. Re-run after a Python upgrade (the copy does not follow the install).
#>
[CmdletBinding()]
param(
    [string]$PythonwExe = 'C:\Python314\pythonw.exe',
    [string]$Icon = (Join-Path $PSScriptRoot 'icon.ico')
)

$target = Join-Path (Split-Path -LiteralPath $PythonwExe) 'pythonw-leonidas-scribe.exe'
Copy-Item -LiteralPath $PythonwExe -Destination $target -Force

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
public static class IconStamp {
    delegate bool EnumProc(IntPtr h, IntPtr type, IntPtr name, IntPtr lp);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr LoadLibraryEx(string f, IntPtr h, uint flags);
    [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr h);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool EnumResourceNames(IntPtr h, IntPtr type, EnumProc p, IntPtr lp);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr BeginUpdateResource(string f, bool del);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool UpdateResource(IntPtr h, IntPtr type, IntPtr name, ushort lang, byte[] data, uint cb);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool EndUpdateResource(IntPtr h, bool discard);

    public static void Stamp(string exe, string ico) {
        var groups = new List<long>();
        IntPtr mod = LoadLibraryEx(exe, IntPtr.Zero, 0x2); // LOAD_LIBRARY_AS_DATAFILE
        EnumResourceNames(mod, (IntPtr)14, (h, t, n, l) => { groups.Add((long)n); return true; }, IntPtr.Zero);
        FreeLibrary(mod);
        if (groups.Count == 0) groups.Add(1);

        byte[] d = File.ReadAllBytes(ico);
        int count = BitConverter.ToUInt16(d, 4);
        var grp = new byte[6 + 14 * count];
        Array.Copy(d, 0, grp, 0, 6);
        IntPtr u = BeginUpdateResource(exe, false);
        if (u == IntPtr.Zero) throw new Exception("BeginUpdateResource failed: " + Marshal.GetLastWin32Error());
        for (int i = 0; i < count; i++) {
            int e = 6 + 16 * i, size = BitConverter.ToInt32(d, e + 8), off = BitConverter.ToInt32(d, e + 12);
            var img = new byte[size];
            Array.Copy(d, off, img, 0, size);
            ushort id = (ushort)(200 + i); // clear of the ids the stock exe already uses
            UpdateResource(u, (IntPtr)3, (IntPtr)id, 1033, img, (uint)size);
            Array.Copy(d, e, grp, 6 + 14 * i, 12);
            BitConverter.GetBytes(id).CopyTo(grp, 6 + 14 * i + 12);
        }
        foreach (long g in groups) UpdateResource(u, (IntPtr)14, (IntPtr)g, 1033, grp, (uint)grp.Length);
        if (!EndUpdateResource(u, false)) throw new Exception("EndUpdateResource failed: " + Marshal.GetLastWin32Error());
    }
}
'@

[IconStamp]::Stamp($target, $Icon)
Write-Host "[Scribe] Built $target with $Icon" -ForegroundColor Green
