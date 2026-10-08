// ScribeCapture.exe : WASAPI capture helper for Leonidas Scribe Meetings (spec section 3).
//   --list-sessions        one JSON line on stdout: audio sessions on all active render+capture endpoints
//   --pid N                process loopback (target process tree), 16 kHz mono s16le on stdout
//   --device default|<id>  device loopback on a render endpoint, same output
// Stdout = raw PCM, one packet per write, zero-filled in real time while the source is silent
// (bytes = time). Stderr = JSON lines. Exits when stdin closes, stdout breaks, or Ctrl+C/Break.
// Keep stdin OPEN (pipe) while capturing. Built with csc C# 5: no interpolation, no ?., no out var.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace ScribeCapture
{
    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator
    {
        void EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
        void GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
        void GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
        void RegisterEndpointNotificationCallback(IntPtr client);
        void UnregisterEndpointNotificationCallback(IntPtr client);
    }

    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection
    {
        void GetCount(out uint count);
        void Item(uint index, out IMMDevice device);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice
    {
        void Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        void OpenPropertyStore(int access, out IPropertyStore props);
        void GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        void GetState(out int state);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PropertyKey { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Sequential)]
    struct PropVariant { public ushort vt; public ushort r1; public ushort r2; public ushort r3; public IntPtr p; public IntPtr p2; }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        void GetCount(out uint count);
        void GetAt(uint i, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionManager2
    {
        void GetAudioSessionControl(IntPtr groupId, uint flags, [MarshalAs(UnmanagedType.IUnknown)] out object ctl);
        void GetSimpleAudioVolume(IntPtr groupId, uint flags, [MarshalAs(UnmanagedType.IUnknown)] out object vol);
        void GetSessionEnumerator(out IAudioSessionEnumerator e);
    }

    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionEnumerator
    {
        void GetCount(out int count);
        void GetSession(int index, out IAudioSessionControl session);
    }

    [ComImport, Guid("F4B1A599-7266-4319-A8CA-E70ACB11E8CD"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl
    {
        void GetState(out int state);
        void GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string s);
        void SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string s, ref Guid ctx);
        void GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string s);
        void SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string s, ref Guid ctx);
        void GetGroupingParam(out Guid g);
        void SetGroupingParam(ref Guid g, ref Guid ctx);
        void RegisterAudioSessionNotification(IntPtr n);
        void UnregisterAudioSessionNotification(IntPtr n);
    }

    [ComImport, Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl2
    {
        void GetState(out int state);
        void GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string s);
        void SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string s, ref Guid ctx);
        void GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string s);
        void SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string s, ref Guid ctx);
        void GetGroupingParam(out Guid g);
        void SetGroupingParam(ref Guid g, ref Guid ctx);
        void RegisterAudioSessionNotification(IntPtr n);
        void UnregisterAudioSessionNotification(IntPtr n);
        void GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string s);
        void GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string s);
        void GetProcessId(out uint pid);
        [PreserveSig] int IsSystemSoundsSession();
        void SetDuckingPreference(bool optOut);
    }

    [ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        void Initialize(int shareMode, uint flags, long bufferDuration, long periodicity, IntPtr format, IntPtr sessionGuid);
        void GetBufferSize(out uint frames);
        void GetStreamLatency(out long latency);
        void GetCurrentPadding(out uint padding);
        void IsFormatSupported(int shareMode, IntPtr format, out IntPtr closest);
        void GetMixFormat(out IntPtr format);
        void GetDevicePeriod(out long def, out long min);
        void Start();
        void Stop();
        void Reset();
        void SetEventHandle(IntPtr h);
        void GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object svc);
    }

    [ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        void GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devPos, out ulong qpcPos);
        void ReleaseBuffer(uint frames);
        void GetNextPacketSize(out uint frames);
    }

    [ComImport, Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAgileObject { }

    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        void GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op);
    }

    class Handler : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        public ManualResetEvent Done = new ManualResetEvent(false);
        public IActivateAudioInterfaceAsyncOperation Op;
        public void ActivateCompleted(IActivateAudioInterfaceAsyncOperation op) { Op = op; Done.Set(); }
    }

    // Converts float mono at inRate to 16 kHz s16 with linear interpolation (fallback path only).
    class Resampler
    {
        double step; double pos; float prev;
        public Resampler(int inRate) { step = inRate / 16000.0; }
        public void Process(float[] x, int n, List<byte> o)
        {
            while (pos < n - 1)
            {
                double v;
                if (pos < 0) v = prev + (x[0] - prev) * (pos + 1);
                else { int i = (int)pos; double f = pos - i; v = x[i] * (1 - f) + x[i + 1] * f; }
                if (v > 1) v = 1; if (v < -1) v = -1;
                short s = (short)(v * 32767);
                o.Add((byte)(s & 0xFF)); o.Add((byte)((s >> 8) & 0xFF));
                pos += step;
            }
            pos -= n;
            if (n > 0) prev = x[n - 1];
        }
    }

    static class Program
    {
        static Guid IID_IAudioClient = new Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2");
        static Guid IID_IAudioCapture = new Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317");
        static Guid IID_ISessionMgr2 = new Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F");
        const uint F_LOOPBACK = 0x00020000, F_EVENT = 0x00040000, F_AUTOCONVERT = 0x80000000, F_SRC = 0x08000000;
        static volatile bool stop;
        static Stream stdout;
        static long total;
        static readonly object errLock = new object();

        [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = false)]
        static extern void ActivateAudioInterfaceAsync([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid riid,
            IntPtr activationParams, IActivateAudioInterfaceCompletionHandler handler, out IActivateAudioInterfaceAsyncOperation op);
        [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant pv);

        static string J(string s)
        {
            StringBuilder b = new StringBuilder("\"");
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') { b.Append('\\'); b.Append(c); }
                else if (c < 32) b.Append("\\u" + ((int)c).ToString("x4"));
                else b.Append(c);
            }
            return b.Append('"').ToString();
        }

        static void Err(string msg, int hr)
        {
            lock (errLock)
            {
                Console.Error.WriteLine("{\"error\":" + J(msg) + ",\"hresult\":\"0x" + hr.ToString("X8") + "\"}");
                Console.Error.Flush();
            }
        }

        [MTAThread]
        static int Main(string[] args)
        {
            try
            {
                if (args.Length >= 1 && args[0] == "--list-sessions") return ListSessions();
                if (args.Length >= 2 && args[0] == "--pid")
                {
                    uint pid;
                    if (!uint.TryParse(args[1], out pid)) { Err("bad pid", 0); return 2; }
                    return Capture(pid, null);
                }
                if (args.Length >= 2 && args[0] == "--device") return Capture(0, args[1]);
                Err("usage: ScribeCapture.exe --list-sessions | --pid N | --device default|<endpoint id>", 0);
                return 2;
            }
            catch (COMException ex) { Err(ex.Message.Trim(), ex.ErrorCode); return 3; }
            catch (Exception ex) { Err(ex.GetType().Name + ": " + ex.Message, ex.HResult); return 1; }
        }

        static IMMDeviceEnumerator Enumerator()
        {
            return (IMMDeviceEnumerator)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")));
        }

        static string FriendlyName(IMMDevice dev)
        {
            try
            {
                IPropertyStore ps; dev.OpenPropertyStore(0, out ps);
                PropertyKey k = new PropertyKey();
                k.fmtid = new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"); k.pid = 14;
                PropVariant v; ps.GetValue(ref k, out v);
                string s = v.vt == 31 ? Marshal.PtrToStringUni(v.p) : "";
                PropVariantClear(ref v);
                return s ?? "";
            }
            catch (Exception) { return ""; }
        }

        static int ListSessions()
        {
            IMMDeviceEnumerator en = Enumerator();
            List<string> items = new List<string>();
            string[] flows = new string[] { "render", "capture" };
            for (int flow = 0; flow < 2; flow++)
            {
                IMMDeviceCollection col; en.EnumAudioEndpoints(flow, 1, out col);
                uint n; col.GetCount(out n);
                for (uint i = 0; i < n; i++)
                {
                    IMMDevice dev; col.Item(i, out dev);
                    string dname = FriendlyName(dev);
                    try
                    {
                        object o; dev.Activate(ref IID_ISessionMgr2, 23, IntPtr.Zero, out o);
                        IAudioSessionEnumerator se; ((IAudioSessionManager2)o).GetSessionEnumerator(out se);
                        int sc; se.GetCount(out sc);
                        for (int s = 0; s < sc; s++)
                        {
                            IAudioSessionControl c; se.GetSession(s, out c);
                            IAudioSessionControl2 c2 = (IAudioSessionControl2)c;
                            uint pid; c2.GetProcessId(out pid);
                            int st; c2.GetState(out st);
                            string pn = "";
                            if (pid == 0) pn = "System Sounds";
                            else { try { pn = Process.GetProcessById((int)pid).ProcessName; } catch (Exception) { } }
                            items.Add("{\"pid\":" + pid + ",\"process\":" + J(pn) + ",\"state\":\"" +
                                (st == 1 ? "active" : st == 2 ? "expired" : "inactive") + "\",\"flow\":\"" + flows[flow] +
                                "\",\"device\":" + J(dname) + "}");
                        }
                    }
                    catch (COMException ex) { Err("session enum failed on " + dname + ": " + ex.Message.Trim(), ex.ErrorCode); }
                }
            }
            Console.Out.WriteLine("[" + string.Join(",", items.ToArray()) + "]");
            Console.Out.Flush();
            return 0;
        }

        static IAudioClient ActivateProcess(uint pid)
        {
            // PROPVARIANT VT_BLOB (x64 layout) -> AUDIOCLIENT_ACTIVATION_PARAMS {type=1 PROCESS_LOOPBACK, pid, mode=0 INCLUDE_TREE}
            IntPtr blob = Marshal.AllocHGlobal(12);
            Marshal.WriteInt32(blob, 0, 1); Marshal.WriteInt32(blob, 4, (int)pid); Marshal.WriteInt32(blob, 8, 0);
            IntPtr pv = Marshal.AllocHGlobal(24);
            for (int i = 0; i < 24; i += 8) Marshal.WriteInt64(pv, i, 0);
            Marshal.WriteInt16(pv, 0, 65);
            Marshal.WriteInt32(pv, 8, 12);
            Marshal.WriteIntPtr(pv, 16, blob);
            Handler h = new Handler();
            IActivateAudioInterfaceAsyncOperation op;
            Guid iid = IID_IAudioClient;
            ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iid, pv, h, out op);
            if (!h.Done.WaitOne(5000)) { Err("process loopback activation timed out", unchecked((int)0x80004005)); return null; }
            int hr; object o;
            h.Op.GetActivateResult(out hr, out o);
            if (hr < 0 || o == null) { Err("process loopback activation failed (needs Windows build 20348+; pid must have an audio session)", hr); return null; }
            return (IAudioClient)o;
        }

        static IntPtr Fmt16k()
        {
            IntPtr f = Marshal.AllocHGlobal(18);
            Marshal.WriteInt16(f, 0, 1); Marshal.WriteInt16(f, 2, 1); Marshal.WriteInt32(f, 4, 16000);
            Marshal.WriteInt32(f, 8, 32000); Marshal.WriteInt16(f, 12, 2); Marshal.WriteInt16(f, 14, 16); Marshal.WriteInt16(f, 16, 0);
            return f;
        }

        static void Out(byte[] b, int n)
        {
            try { stdout.Write(b, 0, n); stdout.Flush(); total += n; }
            catch (IOException) { stop = true; }
            catch (ObjectDisposedException) { stop = true; }
        }

        static int Capture(uint pid, string device)
        {
            bool pidMode = device == null;
            if (pidMode)
            {
                try { Process.GetProcessById((int)pid); }
                catch (ArgumentException) { Err("no such process: " + pid, unchecked((int)0x80070057)); return 3; }
            }
            stdout = Console.OpenStandardOutput();
            Console.CancelKeyPress += delegate(object s, ConsoleCancelEventArgs e) { e.Cancel = true; stop = true; };

            IAudioClient client; IntPtr fmt = Fmt16k();
            bool convert = false; int inRate = 16000, inCh = 1, inBits = 16, inBlock = 2; bool inFloat = false;
            uint flags = F_LOOPBACK | F_EVENT | F_AUTOCONVERT | F_SRC;
            AutoResetEvent ev = new AutoResetEvent(false);

            if (pidMode)
            {
                client = ActivateProcess(pid);
                if (client == null) return 3;
                client.Initialize(0, flags, 0, 0, fmt, IntPtr.Zero);
            }
            else
            {
                IMMDeviceEnumerator en = Enumerator(); IMMDevice dev;
                if (device == "default") en.GetDefaultAudioEndpoint(0, 0, out dev); else en.GetDevice(device, out dev);
                object o; dev.Activate(ref IID_IAudioClient, 23, IntPtr.Zero, out o);
                client = (IAudioClient)o;
                try { client.Initialize(0, flags, 0, 0, fmt, IntPtr.Zero); }
                catch (COMException ex)
                {
                    // ponytail: fallback only on format rejection; needs a fresh client, so re-activate
                    Err("16k mono rejected, falling back to mix format: " + ex.Message.Trim(), ex.ErrorCode);
                    dev.Activate(ref IID_IAudioClient, 23, IntPtr.Zero, out o);
                    client = (IAudioClient)o;
                    IntPtr mf; client.GetMixFormat(out mf);
                    int tag = (ushort)Marshal.ReadInt16(mf, 0);
                    inCh = Marshal.ReadInt16(mf, 2); inRate = Marshal.ReadInt32(mf, 4);
                    inBlock = Marshal.ReadInt16(mf, 12); inBits = Marshal.ReadInt16(mf, 14);
                    inFloat = tag == 3;
                    if (tag == 0xFFFE) inFloat = Marshal.ReadInt32(mf, 24) == 3; // SubFormat first dword: 1 pcm, 3 float
                    convert = true;
                    client.Initialize(0, F_LOOPBACK | F_EVENT, 0, 0, mf, IntPtr.Zero);
                }
            }
            client.SetEventHandle(ev.SafeWaitHandle.DangerousGetHandle());
            object svc; client.GetService(ref IID_IAudioCapture, out svc);
            IAudioCaptureClient cap = (IAudioCaptureClient)svc;

            // parent-death watch: stdin EOF
            Thread t = new Thread(delegate()
            {
                try { Stream si = Console.OpenStandardInput(); byte[] b = new byte[256]; while (si.Read(b, 0, b.Length) > 0) { } stop = true; }
                catch (Exception) { }
            });
            t.IsBackground = true; t.Start();

            Console.Error.WriteLine("{\"rate\":16000,\"channels\":1,\"format\":\"s16\",\"mode\":\"" + (pidMode ? "pid" : "device") + "\"}");
            Console.Error.Flush();

            Resampler rs = convert ? new Resampler(inRate) : null;
            Stopwatch sw = Stopwatch.StartNew();
            client.Start();
            byte[] buf = new byte[65536];
            while (!stop)
            {
                ev.WaitOne(10);
                uint np; cap.GetNextPacketSize(out np);
                while (np > 0 && !stop)
                {
                    IntPtr data; uint frames, fl; ulong dp, qp;
                    cap.GetBuffer(out data, out frames, out fl, out dp, out qp);
                    bool silent = (fl & 2) != 0;
                    if (!convert)
                    {
                        int bytes = (int)frames * 2;
                        if (buf.Length < bytes) buf = new byte[bytes];
                        if (silent) Array.Clear(buf, 0, bytes); else Marshal.Copy(data, buf, 0, bytes);
                        Out(buf, bytes);
                    }
                    else
                    {
                        float[] mono = new float[frames];
                        for (int i = 0; !silent && i < frames; i++)
                        {
                            float acc = 0;
                            for (int c = 0; c < inCh; c++)
                            {
                                IntPtr p = new IntPtr(data.ToInt64() + (long)i * inBlock + c * (inBits / 8));
                                if (inFloat) acc += BitConverter.ToSingle(BitConverter.GetBytes(Marshal.ReadInt32(p)), 0);
                                else if (inBits == 16) acc += Marshal.ReadInt16(p) / 32768f;
                                else if (inBits == 32) acc += Marshal.ReadInt32(p) / 2147483648f;
                                else acc += (Marshal.ReadInt32(p) & 0xFFFFFF00) / 2147483648f; // 24-bit in 32 container or packed (approx)
                            }
                            mono[i] = acc / inCh;
                        }
                        List<byte> o = new List<byte>();
                        rs.Process(mono, (int)frames, o);
                        byte[] ob = o.ToArray(); Out(ob, ob.Length);
                    }
                    cap.ReleaseBuffer(frames);
                    cap.GetNextPacketSize(out np);
                }
                // real-time zero fill: keep bytes = elapsed time * 32000 B/s (30 ms slack to avoid splitting packets)
                long behind = sw.ElapsedMilliseconds * 32 - total;
                if (behind > 32 * 30 && !stop)
                {
                    int pad = (int)(behind & ~1L);
                    if (buf.Length < pad) buf = new byte[pad];
                    Array.Clear(buf, 0, pad);
                    Out(buf, pad);
                }
            }
            try { client.Stop(); } catch (Exception) { }
            return 0;
        }
    }
}
