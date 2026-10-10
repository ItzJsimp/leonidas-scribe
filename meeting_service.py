"""Qt-free glue for Scribe Meetings (S5b): Discord RPC lifecycle, other-app call polling, auto-stop,
worker start, speaking.jsonl writer, crash recovery, post-call re-run and the retention sweep.

Everything runs on background threads. The GUI layer hands in plain callbacks and gets plain callbacks back
(any thread: marshal through the Qt bridge). Closing the service never stops a detached recorder worker.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meeting_detector as md
import meeting_recorder as mr
import meeting_store as mst

POLL_S = 5.0
RECOVERY_EVERY_S = 10.0
SWEEP_EVERY_S = 6 * 3600.0
ACTIVITY_PROBE = {"meetings": {"enabled": True, "call_prompts": True}}  # app activity ignores the prompt/never switches


def list_audio_sessions(exe: str | None = None, timeout: float = 10.0):
    """ScribeCapture --list-sessions -> list of session dicts, or None when the helper is missing or fails."""
    exe = exe or os.path.join(HERE, "ScribeCapture.exe")
    if not os.path.exists(exe):
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        r = subprocess.run([exe, "--list-sessions"], capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def discord_source(sessions) -> dict | None:
    """Process loopback on Discord when it has a render session (active first), else None.
    Device loopback only hears the Windows default output; Discord often plays to its own device
    (2026-10-09: Discord on the headset, the default elsewhere, so the call track was silence)."""
    found = [s for s in sessions or [] if (s.get("process") or "").lower().replace(".exe", "") == "discord"
             and s.get("flow") == "render" and int(s.get("pid") or 0) > 0]
    found.sort(key=lambda s: s.get("state") != "active")
    if not found:
        return None
    return {"kind": "app", "app": "Discord", "pid": int(found[0]["pid"]), "device": None,
            "render_device": found[0].get("device")}


def prefer_call_audio(src: dict, sessions_fn=None) -> dict:
    """A manual 'computer audio' source becomes Discord process loopback while a Discord call is live."""
    if src.get("kind") != "device" or (src.get("device") or "default") != "default":
        return src
    sessions = (sessions_fn or list_audio_sessions)()
    live = [s for s in sessions or [] if s.get("state") == "active"]
    ds = discord_source(live)
    return {**src, **ds} if ds else src


def discord_fields(ev: dict | None) -> dict | None:
    """The manifest 'discord' block for an RPC channel event (None when not in a voice channel)."""
    if not ev or not ev.get("channel_id"):
        return None
    return {k: ev.get(k) for k in ("guild_id", "guild", "channel_id", "channel")}


def note_channel(mdir: str, ev: dict, t: float) -> None:
    """Record where the call is: manifest.discord = the current channel, manifest.discord_channels = every
    switch with its offset in seconds (a None channel means you left voice). New voices join participants."""
    man = mr.read_manifest(mdir)
    if not man:
        return
    here = discord_fields(ev)
    hops = man.setdefault("discord_channels", [])
    if hops and hops[-1].get("channel_id") == (here or {}).get("channel_id"):
        return
    hops.append(dict(here or {"channel_id": None, "channel": None, "guild_id": None, "guild": None}, t=round(max(0.0, t), 1)))
    if here:
        man["discord"] = dict(man.get("discord") or {}, **here)
        known = {p.get("discord_id") or p.get("name") for p in man.get("participants") or []}
        for p in ev.get("participants") or []:
            if (p.get("discord_id") or p.get("name")) not in known:
                man.setdefault("participants", []).append(
                    {"key": p.get("name") or p.get("discord_id"), "name": p.get("name"),
                     "discord_id": p.get("discord_id"), "avatar_path": None})
    mr.write_manifest(mdir, man)  # ponytail: read-modify-write; the worker only writes at start and stop


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        k.CloseHandle(ctypes.c_void_p(h))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _default_rpc_factory(client_id, on_event):
    import discord_rpc

    return discord_rpc.DiscordRPC(client_id, on_event)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "meeting").lower()).strip("-")[:40] or "meeting"


class MeetingService:
    """Callbacks (all optional, called from background threads):
    on_prompt(place, destination, record_cb, never_cb)   a call was detected and the rule says Ask
    on_recovery(message, resume_cb, decline_cb)          a recording died; offer to continue it
    on_status(kind, info)                                recording, stopped, auto_stopped, finalized, needs_key, failed, discord_*
    on_start(source)                                     a recording began (source dict + meeting_dir, title)
    """

    def __init__(self, get_config, save_config=None, store_root=None, on_prompt=None, on_recovery=None,
                 on_status=None, on_start=None, rpc_factory=None, sessions_fn=None, finalize_fn=None,
                 worker_kwargs=None, poll_s=POLL_S, retry_s=30.0, now=time.monotonic):
        self._get_config = get_config
        self._save_config = save_config
        self.store_root = store_root or mr.default_store_root()
        self.on_prompt, self.on_recovery, self.on_status, self.on_start = on_prompt, on_recovery, on_status, on_start
        self._rpc_factory = rpc_factory or _default_rpc_factory
        self._sessions = sessions_fn or list_audio_sessions
        self._finalize_fn = finalize_fn
        self._worker_kwargs = worker_kwargs or {}
        self._poll_s, self._retry_s, self._now = poll_s, retry_s, now
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._rpc_down = threading.Event()
        self._threads: list[threading.Thread] = []
        self._rpc = None
        self._channel: dict | None = None
        self._prompted: set = set()
        self._announced: set = set()
        self._finalizing: set = set()
        self._active: dict | None = None  # {dir, id, kind: discord|app|manual, key}
        self._log_f = None
        self._tracker = md.CallLifecycleTracker()
        self._last_recovery = self._last_sweep = 0.0
        self._t0_wall = time.time()
        self.running = False
        self._pre: dict | None = None  # the running retroactive buffer {dir, pid, channel_id}
        self._pre_lock = threading.Lock()
        self._pre_hold_until = 0.0
        self.preroll_dir = os.path.join(os.path.dirname(os.path.abspath(self.store_root)), "preroll")  # next to meetings/, so tests never touch the real one

    # ------------------------------------------------------------ retroactive buffer (pre-roll)
    def _preroll_minutes(self) -> float:
        return float(self._m().get("preroll_minutes", 60) or 0)

    def preroll_status(self) -> dict | None:
        """{seconds} of audio buffered right now, or None when no buffer is running (the pill's indicator)."""
        pre = self._pre
        if not pre:
            return None
        st = mr.read_status(pre["dir"]) or {}
        if st.get("state") != "buffering":
            return None
        pruned = float((mr.read_manifest(pre["dir"]) or {}).get("ring_pruned_s") or 0.0)
        return {"seconds": max(0.0, float(st.get("me_seconds") or 0.0) - pruned), "channel": (self._channel or {}).get("channel")}

    def _preroll_sync(self):
        """Keep a rolling buffer of the call while you are in a Discord voice channel and not recording, so a
        missed Record prompt (2026-10-10: a day-long call lost) costs nothing. It transcribes nothing, expires
        after preroll_minutes, is deleted when you leave the channel, and Record promotes it into the meeting."""
        if not self._pre_lock.acquire(blocking=False):
            return
        try:
            want = (self._enabled() and self._preroll_minutes() > 0 and bool(self._channel) and not self._active
                    and time.time() >= self._pre_hold_until and not self._stop.is_set())
            pre = self._pre
            if pre and self._rpc is None and pid_alive((mr.read_status(pre["dir"]) or {}).get("pid")):
                return  # Discord RPC is down or still reconnecting after a restart: neither drop nor renew the lease
            if pre and (not pid_alive((mr.read_status(pre["dir"]) or {}).get("pid"))
                        or pre.get("channel_id") != (self._channel or {}).get("channel_id")):
                self._preroll_drop_locked()  # dead worker, or you moved to another channel: that audio is not this call's
                pre = None
            if want and not pre:
                self._preroll_start_locked()
            elif pre and not want:
                self._preroll_drop_locked()
            elif pre:
                mr.send_worker_command(pre["dir"], "lease")  # the worker stops itself if these stop (app died)
        except Exception as e:
            print(f"[MeetingService] preroll sync failed: {e}")
        finally:
            self._pre_lock.release()

    def _preroll_start_locked(self):
        src = discord_source(self._sessions() or []) or {"kind": "device", "app": None, "pid": None, "device": "default"}
        shutil.rmtree(self.preroll_dir, ignore_errors=True)
        mr.init_meeting_dir(self.preroll_dir, title="buffer", source=src, discord=discord_fields(self._channel))
        pid = mr.start_worker(self.preroll_dir, preroll_s=self._preroll_minutes() * 60.0)
        mr.send_worker_command(self.preroll_dir, "lease")
        self._pre = {"dir": self.preroll_dir, "pid": pid, "channel_id": (self._channel or {}).get("channel_id")}
        self._status("buffering", {"channel": (self._channel or {}).get("channel")})

    def _preroll_drop_locked(self):
        pre, self._pre = self._pre, None
        if pre:
            mr.stop_buffer(pre["dir"], timeout=4.0)
            shutil.rmtree(pre["dir"], ignore_errors=True)  # leaving the channel deletes what was buffered
            self._status("buffer_dropped", {})

    def promote_preroll(self, mdir: str) -> float:
        """Record pressed: move the buffer into meeting folder mdir (call before its worker starts). Seconds adopted."""
        with self._pre_lock:
            pre, self._pre = self._pre, None
            self._pre_hold_until = time.time() + 20.0  # until the new worker is attached, do not restart a buffer
        if not pre:
            return 0.0
        try:
            return mr.promote_preroll(pre["dir"], mdir)
        finally:
            shutil.rmtree(pre["dir"], ignore_errors=True)

    # ------------------------------------------------------------ config / helpers
    def _m(self) -> dict:
        return (self._get_config() or {}).get("meetings") or {}

    def _enabled(self) -> bool:
        return bool(self._m().get("enabled", True))

    def _call(self, cb, *args):
        if cb is None:
            return
        try:
            cb(*args)
        except Exception as e:
            print(f"[MeetingService] callback error: {e}")

    def _status(self, kind, info=None):
        self._call(self.on_status, kind, info or {})

    def _spawn(self, fn, *args, name="meeting-job"):
        t = threading.Thread(target=fn, args=args, name=name, daemon=True)
        t.start()
        return t

    def _finalize(self, mdir):
        if self._finalize_fn is None:
            import meeting_pipeline as mp

            self._finalize_fn = mp.finalize_meeting
        return self._finalize_fn(mdir)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        """Idempotent. Does nothing unless meetings.enabled."""
        with self._lock:
            if self.running or not self._enabled():
                return
            self.running = True
            self._stop.clear()
        MeetingService.current = self  # the Meetings view asks the live service for the Discord channel
        self._threads = [self._spawn(self._boot, name="meeting-boot"),
                         self._spawn(self._rpc_loop, name="meeting-rpc"),
                         self._spawn(self._poll_loop, name="meeting-poll")]

    def refresh(self):
        """Call after settings change: start or stop to match meetings.enabled."""
        if self._enabled():
            self.start()
        elif self.running:
            self.stop()

    def stop(self):
        """Shut the service down. An in-progress detached recorder worker is deliberately left running."""
        with self._lock:
            self.running = False
            self._stop.set()
            rpc, self._rpc = self._rpc, None
            if self._log_f:
                try:
                    self._log_f.close()
                except OSError:
                    pass
                self._log_f = None
        if rpc is not None:
            t = self._spawn(rpc.close, name="meeting-rpc-close")
            t.join(1.0)

    # ------------------------------------------------------------ boot: recovery, finalize re-run, retention
    def _adopt_running_worker(self):
        """A worker outlives a Scribe restart (source-watch self-restart, crash). Re-attach to it, or its Discord
        speaking events stop being logged (2026-10-10: speakers lost after minute 16 of a 41 minute call) and the
        call prompt offers to record the same call a second time."""
        pre_st = mr.read_status(self.preroll_dir) or {}
        if not self._pre and pre_st.get("state") == "buffering" and pid_alive(pre_st.get("pid")):
            man = mr.read_manifest(self.preroll_dir) or {}  # the buffer survives a restart too: keep its hour
            self._pre = {"dir": self.preroll_dir, "pid": pre_st["pid"], "channel_id": (man.get("discord") or {}).get("channel_id")}
        if self._active:
            return
        for mdir, m in list(self._iter_meetings()):
            if m.get("state") not in ("recording", "paused"):
                continue
            st = mr.read_status(mdir) or {}
            if pid_alive(st.get("pid")) and time.time() - float(st.get("heartbeat_at") or 0) < 15:
                if self.attach(mdir):
                    self._status("adopted", {"meeting_dir": mdir})
                return

    def _boot(self):
        try:
            self._adopt_running_worker()
            self._recovery_scan(first=True)
            self._rerun_pending()
            self._sweep()
        except Exception as e:
            print(f"[MeetingService] boot error: {e}")

    def _sweep(self):
        self._last_sweep = self._now()
        days = int(self._m().get("retention_days", 30) or 30)
        mst.sweep_retention(self.store_root, days)

    def _iter_meetings(self):
        if not os.path.isdir(self.store_root):
            return
        for entry in sorted(os.listdir(self.store_root)):
            mdir = os.path.join(self.store_root, entry)
            if os.path.isdir(mdir):
                m = mr.read_manifest(mdir)
                if m:
                    yield mdir, m

    def _rerun_pending(self):
        """Meetings left in 'processing' (worker or Scribe died mid-pipeline) or 'needs_key' get the idempotent pipeline again."""
        for mdir, m in list(self._iter_meetings()):
            if self._stop.is_set():
                return
            stuck_audio = m.get("state") == "ready" and m.get("audio_pending")  # quit or crash mid-Opus
            if m.get("state") not in ("processing", "needs_key") and not stuck_audio:
                continue
            st = mr.read_status(mdir) or {}
            if (m.get("state") == "processing" or stuck_audio) and pid_alive(st.get("pid")):
                continue  # its own worker is still running the pipeline
            self._finalizing.add(mdir)  # tick() reports the outcome
            self._finalize(mdir)

    def _recovery_scan(self, first=False):
        self._last_recovery = self._now()
        if self._active and not mr.read_status(self._active["dir"]):
            return  # worker just spawned and has not written its first heartbeat yet: not dead
        found = mr.check_recovery(self.store_root)
        if first:  # needs_resume left unanswered by a previous Scribe session
            for mdir, m in self._iter_meetings():
                if m.get("state") == "needs_resume" and mdir not in {t["meeting_dir"] for t in found}:
                    secs = mr.saved_audio_seconds(mdir, "me")
                    found.append({"meeting_dir": mdir, "message":
                                  f"Recording stopped unexpectedly ({mr._format_saved_duration(secs)} saved). Resume recording this call?"})
        for t in found:
            if t["meeting_dir"] in self._announced:
                continue
            self._announced.add(t["meeting_dir"])
            mdir = t["meeting_dir"]
            self._call(self.on_recovery, t["message"],
                       lambda d=mdir: self._spawn(self._resume, d, name="meeting-resume"),
                       lambda d=mdir: self._spawn(self._decline, d, name="meeting-decline"))

    def _resume(self, mdir):
        if self._active:
            return
        mr.resume_meeting(mdir, **self._worker_kwargs)
        m = mr.read_manifest(mdir) or {}
        self._begin(mdir, m, kind="manual", key=None)

    def _decline(self, mdir):
        self._finalizing.add(mdir)
        mr.decline_resume(mdir, pipeline_fn=self._finalize)

    # ------------------------------------------------------------ recording
    def start_recording(self, source=None, title="Meeting", discord=None, participants=None,
                        destination_id=None, template_id="default", kind="manual", key=None):
        """Create the meeting folder + manifest and start the detached worker. Returns the folder or None if already recording."""
        with self._lock:
            if self._active:
                return None
            stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
            base = os.path.join(self.store_root, f"{stamp}-{_slug(title)}")
            mdir, n = base, 1
            while os.path.exists(mdir):
                n += 1
                mdir = f"{base}-{n}"
            manifest = mr.init_meeting_dir(mdir, title=title, source=source, discord=discord,
                                           participants=participants, destination_id=destination_id, template_id=template_id)
            if self.promote_preroll(mdir):
                manifest = mr.read_manifest(mdir) or manifest  # the call started where the buffered audio starts
            self._begin(mdir, manifest, kind=kind, key=key, spawn=True)
            return mdir

    def _begin(self, mdir, manifest, kind, key, spawn=False):
        with self._lock:
            if spawn:
                mr.start_worker(mdir, **self._worker_kwargs)
            self._active = {"dir": mdir, "id": manifest.get("id") or os.path.basename(mdir), "kind": kind, "key": key}
            self._tracker = md.CallLifecycleTracker()
            self._tracker.on_active(self._active["id"], self._now())
            self._t0_wall = time.time()
            if self._log_f:
                self._log_f.close()
            self._log_f = open(os.path.join(mdir, "speaking.jsonl"), "a", encoding="utf-8")
            if self._channel:
                self._log_event(self._channel)  # who was already in the room
                self._note(mdir, self._channel)
        info = dict(manifest.get("source") or {}, meeting_dir=mdir, title=manifest.get("title"))
        self._status("recording", info)
        self._call(self.on_start, info)

    def current_channel(self) -> dict | None:
        """The voice channel Discord RPC last reported (guild, channel, participants), or None."""
        ch = self._channel
        return dict(ch) if ch and ch.get("channel_id") else None

    def attach(self, mdir):
        """Track a recording the Meetings view started itself (manual Record): channel switches and
        speaking events land in its manifest. Never spawns a worker or calls on_start."""
        with self._lock:
            if self._active:
                return False
            m = mr.read_manifest(mdir) or {}
            self._active = {"dir": mdir, "id": m.get("id") or os.path.basename(mdir), "kind": "manual", "key": None}
            self._tracker = md.CallLifecycleTracker()
            self._tracker.on_active(self._active["id"], self._now())
            self._t0_wall = float(m.get("record_started_wall") or time.time())
            if self._log_f:
                self._log_f.close()
            self._log_f = open(os.path.join(mdir, "speaking.jsonl"), "a", encoding="utf-8")
            if self._channel:
                self._log_event(self._channel)
                self._note(mdir, self._channel)
        return True

    def _note(self, mdir, ev):
        try:
            note_channel(mdir, ev, mr.wall_to_audio(mr.read_manifest(mdir) or {}, time.time()))
        except Exception as e:
            print(f"[MeetingService] channel note failed: {e}")

    def stop_recording(self, auto=False):
        """Ask the worker to stop (it finishes segments and the pipeline on its own). Non-blocking."""
        with self._lock:
            act, self._active = self._active, None
            if self._log_f:
                self._log_f.close()
                self._log_f = None
        if not act:
            return None
        mr.send_worker_command(act["dir"], "stop")
        self._finalizing.add(act["dir"])
        self._status("auto_stopped" if auto else "stopped", {"meeting_dir": act["dir"]})
        return act["dir"]

    @property
    def active_meeting_dir(self):
        return self._active["dir"] if self._active else None

    def _log_event(self, ev):
        """Append a Discord event to speaking.jsonl with wall time (finalize aligns it to the audio via record_started_wall)."""
        with self._lock:
            if self._log_f:
                wall = time.time()
                self._log_f.write(json.dumps(dict(ev, wall=wall, t=round(wall - self._t0_wall, 3)), ensure_ascii=False) + "\n")
                self._log_f.flush()  # ponytail: no fsync per event; a power cut loses the last few speaking events only
            elif self._pre:
                # the retroactive buffer needs speakers too, or its minutes come out as "Them" (2026-10-10: the first
                # 8.5 minutes of a call). promote_preroll carries this file into the meeting.
                try:
                    with open(os.path.join(self._pre["dir"], "speaking.jsonl"), "a", encoding="utf-8") as f:
                        f.write(json.dumps(dict(ev, wall=time.time()), ensure_ascii=False) + "\n")
                except OSError:
                    pass

    # ------------------------------------------------------------ prompts
    def _destination_name(self, dest_id):
        for d in self._m().get("destinations") or []:
            if dest_id and dest_id in (d.get("id"), d.get("name")):
                return d.get("name") or ""
        return ""

    def _ask(self, key, place, dest_id, record_fn, never_fn):
        self._prompted.add(key)
        self._call(self.on_prompt, place, self._destination_name(dest_id),
                   lambda dest=None: self._spawn(record_fn, dest, name="meeting-record"),  # the toast passes its folder
                   lambda: self._spawn(never_fn, name="meeting-never"))

    def _persist(self, cfg):
        if self._save_config:
            self._save_config(cfg)

    def _discord_source(self):
        return discord_source(self._sessions()) or {"kind": "device", "app": "Discord", "pid": None, "device": "default"}

    def _record_discord(self, ev, res):
        local = getattr(self._rpc, "user_id", None)
        discord = {"guild_id": ev.get("guild_id"), "guild": ev.get("guild"), "channel_id": ev.get("channel_id"),
                   "channel": ev.get("channel"), "local_user_id": local}
        people = [{"key": p.get("name") or p.get("discord_id"), "name": p.get("name"), "discord_id": p.get("discord_id"),
                   "avatar_path": None} for p in ev.get("participants") or []]
        self.start_recording(self._discord_source(), title=ev.get("guild") or ev.get("channel") or "Discord call",
                             discord=discord, participants=people, destination_id=res.get("destination_id"),
                             template_id=res.get("template_id") or "default", kind="discord", key=("discord", ev.get("channel_id")))

    def _never_discord(self, ev):
        cfg = self._get_config()
        md.apply_never_here(cfg, ev)
        self._persist(cfg)

    def _record_app(self, det, dest=None):
        self.start_recording({"kind": "app", "app": det["app"], "pid": det["pid"], "device": None}, destination_id=dest,
                             title=f"{det['app']} call", kind="app", key=("app", det["process"].lower()))

    def _never_app(self, det):
        cfg = self._get_config()
        cfg.setdefault("meetings", {}).setdefault("app_prompts", {})[det["process"].lower()] = "never"
        self._persist(cfg)

    # ------------------------------------------------------------ Discord RPC
    def _rpc_loop(self):
        while not self._stop.is_set():
            cid = str((self._m().get("discord") or {}).get("client_id") or "")
            if cid and self._enabled():
                rpc = None
                try:
                    self._rpc_down.clear()
                    rpc = self._rpc_factory(cid, self._on_rpc_event)
                    rpc.connect()
                    self._rpc = rpc
                    self._status("discord_connected", {"user_id": getattr(rpc, "user_id", None)})
                    while not (self._stop.is_set() or self._rpc_down.is_set() or getattr(rpc, "error", None)):
                        self._stop.wait(1.0)
                except Exception as e:  # Discord closed or not running: retry quietly
                    self._status("discord_error", {"error": str(e)})
                finally:
                    self._rpc = None
                    if rpc is not None:
                        try:
                            rpc.close()
                        except Exception:
                            pass
            self._stop.wait(self._retry_s)

    def _on_rpc_event(self, ev):
        e = ev.get("event")
        if e == "disconnected":
            self._rpc_down.set()
            self._status("discord_disconnected", {})
            return
        if e == "channel":
            self._on_channel(ev)
            self._spawn(self._preroll_sync, name="meeting-preroll")
        if e in ("channel", "start", "stop", "join", "leave"):
            self._log_event(ev)

    def _on_channel(self, ev):
        cid = ev.get("channel_id")
        with self._lock:
            self._channel = ev if cid else None
            act = self._active
            if not cid:
                if act:
                    self._note(act["dir"], ev)
                self._prompted = {k for k in self._prompted if k[0] != "discord"}
                if act and act["kind"] == "discord":
                    self._tracker.on_inactive(self._now())
                return
            if act:
                self._note(act["dir"], ev)  # switched channels mid-call: same meeting, new place
                if act["kind"] == "discord":
                    self._tracker.on_active(act["id"], self._now())  # rejoin inside 2 min continues the same meeting
                return
        res = md.evaluate_discord_call_event(ev, self._get_config())
        key = ("discord", cid)
        if res["should_prompt"] and key not in self._prompted:
            place = (res.get("toast") or {}).get("place") or ev.get("guild") or ev.get("channel") or "Direct Message"
            self._ask(key, place, res.get("destination_id"),
                      lambda dest: self._record_discord(ev, dict(res, destination_id=dest or res.get("destination_id"))),
                      lambda: self._never_discord(ev))

    # ------------------------------------------------------------ polling: other apps, auto-stop, recovery, sweep
    def _poll_loop(self):
        while not self._stop.wait(self._poll_s):
            try:
                self.tick()
            except Exception as e:
                print(f"[MeetingService] poll error: {e}")

    def tick(self):
        """One poll cycle (public so tests can drive it without threads)."""
        cfg = self._get_config()
        act = self._active
        if act:
            m = mr.read_manifest(act["dir"]) or {}
            if m.get("state") not in ("recording", "paused"):  # worker ended on its own (stop from the UI pill, crash)
                with self._lock:
                    self._active = None
                self._status("stopped", {"meeting_dir": act["dir"]})
                act = None
        sessions = self._sessions() if (act and act["kind"] == "app") or (not act and self._m().get("call_prompts", True)) else None
        if sessions is not None:
            if act and act["kind"] == "app":
                live = {d["process"].lower() for d in md.detect_other_app_calls(sessions, ACTIVITY_PROBE)}
                if act["key"][1] in live:
                    self._tracker.on_active(act["id"], self._now())
                else:
                    self._tracker.on_inactive(self._now())
            elif not act:
                found = md.detect_other_app_calls(sessions, cfg)
                self._prompted = {k for k in self._prompted if k[0] != "app" or k[1] in {d["process"].lower() for d in found}}
                for det in found:
                    key = ("app", det["process"].lower())
                    if key not in self._prompted:
                        self._ask(key, det["app"], None, lambda dest, d=det: self._record_app(d, dest), lambda d=det: self._never_app(d))
        if act and self._tracker.check(self._now()).get("action") == "auto_stop":
            self.stop_recording(auto=True)
        self._watch_finalizing()
        now = self._now()
        self._preroll_sync()
        if now - self._last_recovery >= RECOVERY_EVERY_S:
            self._adopt_running_worker()
            self._recovery_scan()
        if now - self._last_sweep >= SWEEP_EVERY_S:
            self._sweep()

    def _watch_finalizing(self):
        for mdir in list(self._finalizing):
            state = (mr.read_manifest(mdir) or {}).get("state")
            if state in ("ready", "failed", "needs_key"):
                self._finalizing.discard(mdir)
                self._status("finalized" if state == "ready" else state, {"meeting_dir": mdir})
