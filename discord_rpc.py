"""Discord local RPC client (raw named-pipe IPC) for Scribe Meetings.

Layers: PipeTransport (Windows pipe, one owner thread) <- DiscordRPC (protocol,
auth, normalized events). Tests swap the transport for an in-memory fake.
Spike: python discord_rpc.py --spike --client-id ID --seconds 120 --out f.jsonl --avatars dir
"""
import argparse
import base64
import hashlib
import ctypes
import json
import os
import queue
import struct
import sys
import threading
import time
import uuid

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_URL = "https://discord.com/api/oauth2/token"
REDIRECT_URI = "http://127.0.0.1"
SCOPES = ["rpc", "rpc.voice.read"]
OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4
CHANNEL_EVENTS = ("SPEAKING_START", "SPEAKING_STOP", "VOICE_STATE_CREATE", "VOICE_STATE_DELETE")


class DiscordRPCError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- frames
def encode_frame(op, payload):
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", op, len(data)) + data


def decode_frame(buf):
    """-> (op, payload, rest) or None when buf holds no complete frame."""
    if len(buf) < 8:
        return None
    op, n = struct.unpack("<II", buf[:8])
    if len(buf) < 8 + n:
        return None
    return op, json.loads(buf[8:8 + n].decode("utf-8")), buf[8 + n:]


# ---------------------------------------------------------------- transport
class PipeTransport:
    """Windows named pipe. One thread owns the handle: a blocked ReadFile on a
    synchronous handle would block WriteFile, so we PeekNamedPipe before reading
    and drain the outbound queue between polls. Interface: send / recv / close."""

    def __init__(self, path=None):
        k = ctypes.windll.kernel32
        from ctypes import wintypes as w
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.PeekNamedPipe.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, w.LPVOID, ctypes.POINTER(w.DWORD), w.LPVOID]
        k.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
        k.WriteFile.argtypes = [w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
        k.CloseHandle.argtypes = [w.HANDLE]
        self._k, self._w = k, w
        paths = [path] if path else [r"\\.\pipe\discord-ipc-%d" % i for i in range(10)]
        self._h = None
        for p in paths:
            h = k.CreateFileW(p, 0xC0000000, 0, None, 3, 0, None)  # GENERIC_READ|WRITE, OPEN_EXISTING
            if h not in (None, ctypes.c_void_p(-1).value):
                self._h = h
                break
        if self._h is None:
            raise DiscordRPCError("Discord is not running (no discord-ipc pipe found)")
        self._out, self._in = queue.Queue(), queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="discord-pipe", daemon=True)
        self._thread.start()

    def send(self, op, payload):
        self._out.put(encode_frame(op, payload))

    def recv(self, timeout):
        try:
            item = self._in.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            self._in.put(None)
            raise ConnectionError("Discord pipe closed")
        return item

    def close(self):
        self._stop.set()
        self._thread.join(2)

    def _run(self):
        k, w, h = self._k, self._w, self._h
        buf = b""
        scratch = ctypes.create_string_buffer(65536)
        try:
            while not self._stop.is_set():
                busy = False
                avail = w.DWORD(0)
                if not k.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
                    break
                if avail.value:
                    got = w.DWORD(0)
                    if not k.ReadFile(h, scratch, min(avail.value, 65536), ctypes.byref(got), None):
                        break
                    buf += scratch.raw[:got.value]
                    busy = True
                    while (fr := decode_frame(buf)) is not None:
                        self._in.put(fr[:2])
                        buf = fr[2]
                while True:
                    try:
                        data = self._out.get_nowait()
                    except queue.Empty:
                        break
                    busy = True
                    while data:
                        wrote = w.DWORD(0)
                        if not k.WriteFile(h, data, len(data), ctypes.byref(wrote), None):
                            return
                        data = data[wrote.value:]
                if not busy:
                    time.sleep(0.01)
        finally:
            k.CloseHandle(h)
            self._in.put(None)


# ---------------------------------------------------------------- protocol
def avatar_url(user_id, avatar_hash):
    if not avatar_hash:
        return None
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"


def cache_avatar(user_id, avatar_hash, folder):
    """Download once into folder/<user_id>.png; returns path or None. Network: call off the GUI thread."""
    url = avatar_url(user_id, avatar_hash)
    if not url:
        return None
    path = os.path.join(folder, f"{user_id}.png")
    if os.path.exists(path):
        return path
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except requests.RequestException:
        return None


def _person(vs):
    u = vs.get("user") or {}
    name = vs.get("nick") or u.get("global_name") or u.get("username")
    return {"discord_id": u.get("id"), "name": name, "avatar_hash": u.get("avatar")}


class DiscordRPC:
    def __init__(self, client_id, on_event, clock=None, transport_factory=None,
                 token_path=None, client_secret=None, secrets_path=None):
        t0 = time.monotonic()
        self.client_id = str(client_id)
        self.on_event = on_event
        self.clock = clock or (lambda: time.monotonic() - t0)
        self._factory = transport_factory or PipeTransport
        self.token_path = token_path or os.path.join(HERE, "discord_token.secret.json")
        self._secret = client_secret
        self._secrets_path = secrets_path or os.path.join(HERE, "secrets.json")
        self.user_id = None
        self.auth_path = None  # cached | refresh | public | secret
        self.error = None
        self._t = None
        self._cid = None
        self._pending = {}
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._connected = False
        self._thread = None

    # -- lifecycle
    def connect(self, timeout=15):
        self._t = self._factory()
        self._thread = threading.Thread(target=self._loop, name="discord-rpc", daemon=True)
        self._thread.start()
        self._t.send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        if not self._ready.wait(timeout):
            self.close()
            raise DiscordRPCError("Discord did not answer the handshake")
        if self.error:
            self.close()
            raise DiscordRPCError(self.error)
        try:
            self._authenticate()
            self._send("SUBSCRIBE", {}, "VOICE_CHANNEL_SELECT")
            self._send("GET_SELECTED_VOICE_CHANNEL", {}, cb=self._on_selected)
        except Exception:
            self.close()
            raise
        self._connected = True

    def close(self):
        self._stop.set()
        if self._t:
            try:
                self._t.send(OP_CLOSE, {})
            except Exception:
                pass
            self._t.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                m = self._t.recv(0.2)
            except ConnectionError as e:
                if not self._stop.is_set():
                    self._fail(str(e))
                return
            if m:
                try:
                    self._handle(*m)
                except Exception as e:  # keep the reader alive
                    self.error = self.error or f"handler error: {e}"

    def _fail(self, msg):
        if self._stop.is_set():
            return
        self.error = self.error or msg
        self._ready.set()
        for nonce in list(self._pending):
            cb = self._pending.pop(nonce, None)
            if cb:
                cb(None, {"message": msg})
        if self._connected:
            self._connected = False
            self._emit({"event": "disconnected", "error": msg})

    def _emit(self, ev):
        self.on_event(ev)

    # -- wire
    def _send(self, cmd, args, evt=None, cb=None):
        nonce = str(uuid.uuid4())
        if cb:
            self._pending[nonce] = cb
        msg = {"cmd": cmd, "args": args, "nonce": nonce}
        if evt:
            msg["evt"] = evt
        self._t.send(OP_FRAME, msg)

    def _call(self, cmd, args, timeout=15):
        box, done = [], threading.Event()
        self._send(cmd, args, cb=lambda d, e: (box.append((d, e)), done.set()))
        if not done.wait(timeout):
            raise DiscordRPCError(f"{cmd} timed out")
        data, err = box[0]
        if err:
            raise DiscordRPCError(err.get("message", f"{cmd} failed"), err.get("code"))
        return data

    def _handle(self, op, p):
        if op == OP_PING:
            self._t.send(OP_PONG, p)
        elif op == OP_CLOSE:
            self._fail(f"Discord closed the connection: {p.get('message', p)}")
        elif op == OP_FRAME:
            evt, nonce = p.get("evt"), p.get("nonce")
            if nonce in self._pending:
                cb = self._pending.pop(nonce)
                if evt == "ERROR":
                    cb(None, p.get("data") or {})
                else:
                    cb(p.get("data"), None)
            elif p.get("cmd") == "DISPATCH":
                self._dispatch(evt, p.get("data") or {})

    def _dispatch(self, evt, d):
        if evt == "READY":
            self.user_id = (d.get("user") or {}).get("id")
            self._ready.set()
        elif evt == "VOICE_CHANNEL_SELECT":
            self._select(d.get("channel_id"))
        elif evt in ("SPEAKING_START", "SPEAKING_STOP"):
            self._emit({"event": "start" if evt.endswith("START") else "stop",
                        "user_id": d.get("user_id"), "t": self.clock()})
        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_DELETE"):
            p = _person(d)
            self._emit({"event": "join" if evt.endswith("CREATE") else "leave",
                        "user_id": p["discord_id"], "name": p["name"], "avatar_hash": p["avatar_hash"]})

    # -- channel tracking
    def _on_selected(self, d, err):
        if d and not err and d.get("id"):
            self._select(d["id"], d)

    def _select(self, cid, obj=None):
        old, self._cid = self._cid, cid
        if old:
            for ev in CHANNEL_EVENTS:
                self._send("UNSUBSCRIBE", {"channel_id": old}, ev)
        if not cid:
            self._emit({"event": "channel", "channel_id": None, "channel": None,
                        "guild_id": None, "guild": None, "participants": []})
        elif obj is not None:
            self._got_channel(cid, obj, None)
        else:
            self._send("GET_CHANNEL", {"channel_id": cid}, cb=lambda d, e: self._got_channel(cid, d, e))

    def _got_channel(self, cid, d, err):
        if err or not d or cid != self._cid:
            return
        for ev in CHANNEL_EVENTS:
            self._send("SUBSCRIBE", {"channel_id": cid}, ev)
        gid = d.get("guild_id")
        people = [_person(vs) for vs in d.get("voice_states") or []]

        def done(guild):
            if cid == self._cid:
                self._emit({"event": "channel", "channel_id": cid, "channel": d.get("name"),
                            "guild_id": gid, "guild": guild, "participants": people})
        if gid:
            self._send("GET_GUILD", {"guild_id": gid},
                       cb=lambda g, e: done(None if e or not g else g.get("name")))
        else:
            done(None)

    # -- auth
    def _client_secret(self):
        if self._secret is None:
            self._secret = ""
            try:
                with open(self._secrets_path, encoding="utf-8") as f:
                    s = json.load(f)
                self._secret = s.get("DISCORD_CLIENT_SECRET") or (s.get("api") or {}).get("DISCORD_CLIENT_SECRET") or ""
            except (OSError, ValueError):
                pass
        return self._secret

    def _token_request(self, data):
        """Try public client first, then with the secret if one is configured."""
        last = None
        tries = [("public", {})]
        if self._client_secret():
            tries.append(("secret", {"client_secret": self._client_secret()}))
        for path, extra in tries:
            try:
                r = requests.post(TOKEN_URL, data={"client_id": self.client_id, **data, **extra}, timeout=15)
            except requests.RequestException as e:
                last = f"token endpoint unreachable: {e}"
                continue
            if r.status_code == 200:
                j = r.json()
                tok = {"access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
                       "expires_at": time.time() + int(j.get("expires_in", 604800))}
                self.auth_path = path
                return tok
            last = f"token exchange ({path}) failed: HTTP {r.status_code} {r.text[:120]}"
        raise DiscordRPCError(last)

    def _authenticate(self):
        tok = self._load_token()
        if tok and tok.get("expires_at", 0) - 300 < time.time():
            tok = self._refresh(tok)
        if tok:
            try:
                return self._auth_with(tok)
            except DiscordRPCError:
                pass
        # Public clients must use PKCE: Discord rejects the exchange with 'Missing "code_verifier"' otherwise.
        verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        code = self._call("AUTHORIZE", {"client_id": self.client_id, "scopes": SCOPES,
                                        "code_challenge": challenge, "code_challenge_method": "S256"},
                          timeout=180)["code"]
        tok = self._token_request({"grant_type": "authorization_code", "code": code,
                                   "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        self._save_token(tok)
        self._auth_with(tok)

    def _refresh(self, tok):
        if not tok.get("refresh_token"):
            return None
        try:
            new = self._token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        except DiscordRPCError:
            return None
        self._save_token(new)
        return new

    def _auth_with(self, tok):
        d = self._call("AUTHENTICATE", {"access_token": tok["access_token"]})
        self.user_id = ((d or {}).get("user") or {}).get("id") or self.user_id
        if self.auth_path is None:
            self.auth_path = "cached"

    def _load_token(self):
        try:
            with open(self.token_path, encoding="utf-8") as f:
                t = json.load(f)
            return t if t.get("access_token") else None
        except (OSError, ValueError):
            return None

    def _save_token(self, tok):
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(tok, f)


# ---------------------------------------------------------------- spike CLI
def spike(args):
    cid = args.client_id
    if not cid:
        try:
            with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
                cid = json.load(f)["meetings"]["discord"]["client_id"]
        except (OSError, ValueError, KeyError, TypeError):
            raise DiscordRPCError("no client id: pass --client-id or set meetings.discord.client_id in config.json")
    names, avatars, intervals, open_at = {}, {}, {}, {}
    out = open(args.out, "w", encoding="utf-8") if args.out else None

    def write(rec):
        if out:
            out.write(json.dumps(rec) + "\n")
            out.flush()

    def on_event(ev):
        e = ev["event"]
        if e in ("start", "stop"):
            write({"user_id": ev["user_id"], "event": e, "t": ev["t"]})
            if e == "start":
                open_at[ev["user_id"]] = ev["t"]
            elif ev["user_id"] in open_at:
                intervals.setdefault(ev["user_id"], []).append((open_at.pop(ev["user_id"]), ev["t"]))
        else:
            write(dict(ev, t=rpc.clock()))
            for p in ev.get("participants") or [ev]:
                uid = p.get("discord_id") or p.get("user_id")
                names[uid] = p.get("name")
                avatars[uid] = p.get("avatar_hash")

    rpc = DiscordRPC(cid, on_event)
    rpc.connect()
    try:
        print(f"connected; local user id {rpc.user_id}; auth path: {rpc.auth_path}; listening {args.seconds}s")
        end = time.monotonic() + args.seconds
        while time.monotonic() < end and not rpc.error:
            time.sleep(0.2)
    finally:
        rpc.close()
        if out:
            out.close()
    now = rpc.clock()
    for uid, s in open_at.items():
        intervals.setdefault(uid, []).append((s, now))
    if args.avatars:
        got = sum(1 for u, h in avatars.items() if cache_avatar(u, h, args.avatars))
        print(f"avatars cached: {got}/{len(avatars)}")
    for uid, iv in intervals.items():
        total = sum(b - a for a, b in iv)
        print(f"user {uid} ({names.get(uid)}): talk {total:.1f}s in {len(iv)} intervals")
    if not intervals:
        print("no speaking events seen")
    print(f"auth path used: {rpc.auth_path}")
    if rpc.error:
        raise DiscordRPCError(rpc.error)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Discord local RPC client")
    ap.add_argument("--spike", action="store_true")
    ap.add_argument("--client-id")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--out")
    ap.add_argument("--avatars")
    args = ap.parse_args(argv)
    if not args.spike:
        ap.error("only --spike is supported from the command line")
    try:
        spike(args)
    except DiscordRPCError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
