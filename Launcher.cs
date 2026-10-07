using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace LeonidasScribe
{
    static class Program
    {
        [DllImport("user32.dll")]
        static extern bool AllowSetForegroundWindow(int dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
        static extern IntPtr OpenEvent(uint dwDesiredAccess, bool bInheritHandle, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool SetEvent(IntPtr hEvent);

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool CloseHandle(IntPtr hObject);

        const string SHOW_EVENT_NAME = @"Local\LeonidasScribe_ShowDashboard_Event";
        const uint EVENT_MODIFY_STATE = 0x0002;

        [STAThread]
        static void Main(string[] args)
        {
            // 1. Try to signal existing running instance
            IntPtr hEvent = OpenEvent(EVENT_MODIFY_STATE, false, SHOW_EVENT_NAME);
            if (hEvent != IntPtr.Zero)
            {
                AllowSetForegroundWindow(-1);
                SetEvent(hEvent);
                CloseHandle(hEvent);
                return;
            }

            // 2. Launch background pythonw process
            string scriptDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            string scriptPath = Path.Combine(scriptDir, "LeonidasScribe.py");
            if (!File.Exists(scriptPath))
            {
                scriptDir = Environment.CurrentDirectory;
                scriptPath = Path.Combine(scriptDir, "LeonidasScribe.py");
            }

            // Icon-stamped pythonw copy (Build-ScribeRuntime.ps1) so Task Manager shows the Scribe icon.
            string pythonw = @"C:\Python314\pythonw-leonidas-scribe.exe";
            if (!File.Exists(pythonw)) pythonw = @"C:\Python314\pythonw.exe";
            if (!File.Exists(pythonw))
            {
                pythonw = "pythonw.exe";
            }

            string argString = "\"" + scriptPath + "\"";
            if (args != null && args.Length > 0)
            {
                argString += " " + string.Join(" ", args);
            }

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = pythonw;
            psi.Arguments = argString;
            psi.WorkingDirectory = scriptDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;

            try
            {
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Failed to launch Leonidas Scribe: " + ex.Message, "Leonidas Scribe Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }
}
