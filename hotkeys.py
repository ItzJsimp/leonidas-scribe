"""
Leonidas Scribe - Native Win32 Low-Level Keyboard & Mouse Hook Engine
"""
import time
import ctypes
from ctypes import wintypes
import threading

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_XBUTTONDOWN = 0x020B
WM_MBUTTONDOWN = 0x0207
WM_RBUTTONDOWN = 0x0204
WM_QUIT = 0x0012

VK_MAP = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "ESCAPE": 0x1B, "CAPSLOCK": 0x14, "SCROLLLOCK": 0x91, "PAUSE": 0x13,
    "SPACE": 0x20, "TAB": 0x09, "BACKQUOTE": 0xC0, "GRAVE": 0xC0,
    "TILDE": 0xC0, "MINUS": 0xBD, "EQUALS": 0xBB, "BRACKETLEFT": 0xDB, "BRACKETRIGHT": 0xDD,
    "SEMICOLON": 0xBA, "APOSTROPHE": 0xDE, "COMMA": 0xBC, "PERIOD": 0xBE, "SLASH": 0xBF, "BACKSLASH": 0xDC
}

XBUTTON1 = 0x0001 # Mouse4
XBUTTON2 = 0x0002 # Mouse5

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK

user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_longlong

user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_longlong

user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]


LLKHF_INJECTED = 0x10   # KBDLLHOOKSTRUCT.flags: event came from SendInput/keybd_event
LLMHF_INJECTED = 0x01   # MSLLHOOKSTRUCT.flags: same for mouse
WM_LBUTTONDOWN = 0x0201
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

# Pure modifier keys never count: the Ctrl+Win dictation chord must not block stitching.
_MODIFIER_VKS = frozenset((0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C))

# Time (time.time()) of the last real, non-injected keystroke or click/wheel. Read by
# stitch.should_merge: input after the last paste means the user moved on, so no merge.
last_user_input = 0.0


def stamp_user_input(flags, injected_mask):
    """Stamp last_user_input unless the event is injected (Scribe's own Ctrl+V)."""
    global last_user_input
    if not flags & injected_mask:
        last_user_input = time.time()


class HotkeyManager:
    def __init__(self, on_ptt_start=None, on_ptt_stop=None, on_toggle_trigger=None,
                 on_capture_trigger=None, on_cancel=None, on_transform_trigger=None,
                 on_quiet_toggle_trigger=None, on_clipboard_send=None,
                 on_copy_quickpick=None, copy_quickpick_button="Ctrl + Alt + V",
                 is_active_cb=None, is_gaming_mode_cb=None, max_toggle_seconds=300,
                 toggle_debounce_seconds=0.15, toggle_button="Mouse5",
                 secondary_toggle_button=None, capture_button="Mouse4",
                 ptt_chord="Left Ctrl + Left Win", transform_button="Ctrl + Alt + R",
                 quiet_toggle_button="Ctrl + Shift + Q",
                 clipboard_send_button="Ctrl + Shift + C"):
        self.on_ptt_start = on_ptt_start
        self.on_ptt_stop = on_ptt_stop
        self.on_toggle_trigger = on_toggle_trigger
        self.on_capture_trigger = on_capture_trigger
        self.on_cancel = on_cancel
        self.on_transform_trigger = on_transform_trigger
        self.on_quiet_toggle_trigger = on_quiet_toggle_trigger
        self.on_clipboard_send = on_clipboard_send
        self.on_copy_quickpick = on_copy_quickpick
        self.is_active_cb = is_active_cb
        self.is_gaming_mode_cb = is_gaming_mode_cb
        self.max_toggle_seconds = max_toggle_seconds
        self.toggle_debounce_seconds = toggle_debounce_seconds

        self.toggle_button = toggle_button or "Mouse5"
        self.secondary_toggle_button = secondary_toggle_button
        self.capture_button = capture_button or "Mouse4"
        self.ptt_chord = ptt_chord or "Left Ctrl + Left Win"
        self.transform_button = transform_button or "Ctrl + Alt + R"
        self.quiet_toggle_button = quiet_toggle_button or "Ctrl + Shift + Q"
        self.clipboard_send_button = clipboard_send_button or "Ctrl + Shift + C"
        self.copy_quickpick_button = copy_quickpick_button or "Ctrl + Alt + V"

        self.ctrl_down = False
        self.lwin_down = False
        self.alt_down = False
        self.shift_down = False
        self.ptt_active = False
        self.suppress_lwin_up = False
        self.toggle_timer = None
        self._last_toggle_time = 0.0

        self.keyboard_hook = None
        self.mouse_hook = None
        self.hook_thread = None
        self.hook_thread_id = None
        self.running = False
        self._ready_event = threading.Event()

        self._kb_proc = HOOKPROC(self._keyboard_hook_proc)
        self._ms_proc = HOOKPROC(self._mouse_hook_proc)

    def update_bindings(self, toggle_button="Mouse5", capture_button="Mouse4",
                        ptt_chord="Left Ctrl + Left Win", transform_button="Ctrl + Alt + R",
                        secondary_toggle_button=None, is_gaming_mode_cb=None,
                        quiet_toggle_button="Ctrl + Shift + Q",
                        clipboard_send_button="Ctrl + Shift + C",
                        copy_quickpick_button="Ctrl + Alt + V"):
        self.toggle_button = toggle_button or "Mouse5"
        self.capture_button = capture_button or "Mouse4"
        self.ptt_chord = ptt_chord or "Left Ctrl + Left Win"
        self.transform_button = transform_button or "Ctrl + Alt + R"
        self.quiet_toggle_button = quiet_toggle_button or "Ctrl + Shift + Q"
        self.clipboard_send_button = clipboard_send_button or "Ctrl + Shift + C"
        self.copy_quickpick_button = copy_quickpick_button or "Ctrl + Alt + V"
        if secondary_toggle_button is not None:
            self.secondary_toggle_button = secondary_toggle_button
        if is_gaming_mode_cb is not None:
            self.is_gaming_mode_cb = is_gaming_mode_cb
        print(f"[LeonidasScribe Hotkeys] Bindings live-updated: Toggle={self.toggle_button}, Secondary={self.secondary_toggle_button}, Capture={self.capture_button}, PTT={self.ptt_chord}, Transform={self.transform_button}, QuietToggle={self.quiet_toggle_button}, ClipSend={self.clipboard_send_button}")

    def _match_key(self, vk, binding_str):
        if not binding_str or binding_str.lower() in ("disabled", "none"):
            return False
        
        # Check chord e.g. "Ctrl + F8" or "Alt + F9"
        parts = [p.strip().upper() for p in binding_str.split("+")]
        target_key = parts[-1].replace("KEY_", "")
        required_ctrl = "CTRL" in parts[:-1]
        required_alt = "ALT" in parts[:-1]
        required_shift = "SHIFT" in parts[:-1]
        required_win = any(w in parts[:-1] for w in ("WIN", "META", "LEFT WIN", "RIGHT WIN"))

        # Verify modifier states if required
        if required_ctrl and not (self.ctrl_down or bool(user32.GetAsyncKeyState(0x11) & 0x8000)):
            return False
        if required_alt and not (self.alt_down or bool(user32.GetAsyncKeyState(0x12) & 0x8000)):
            return False
        if required_shift and not (self.shift_down or bool(user32.GetAsyncKeyState(0x10) & 0x8000)):
            return False
        if required_win and not (self.lwin_down or bool(user32.GetAsyncKeyState(0x5B) & 0x8000) or bool(user32.GetAsyncKeyState(0x5C) & 0x8000)):
            return False

        if target_key in VK_MAP and vk == VK_MAP[target_key]:
            return True
        if len(target_key) == 1 and vk == ord(target_key):
            return True
        return False

    def _keyboard_hook_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            kb = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = kb.vkCode

            # Track Modifiers
            if vk in (0xA2, 0xA3, 0x11):
                self.ctrl_down = (wParam in (WM_KEYDOWN, WM_SYSKEYDOWN))
            elif vk in (0xA4, 0xA5, 0x12):
                self.alt_down = (wParam in (WM_KEYDOWN, WM_SYSKEYDOWN))
            elif vk in (0xA0, 0xA1, 0x10):
                self.shift_down = (wParam in (WM_KEYDOWN, WM_SYSKEYDOWN))
            elif vk in (0x5B, 0x5C):
                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self.lwin_down = True
                elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                    self.lwin_down = False
                    if self.suppress_lwin_up:
                        self.suppress_lwin_up = False
                        return 1

            # Escape Cancel
            if vk == 0x1B and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if self.ptt_active or self._is_active():
                    self._handle_cancel()
                    return 1

            # Dynamic Keyboard Key Triggers
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if self._match_key(vk, self.toggle_button) or (self.secondary_toggle_button and self._match_key(vk, self.secondary_toggle_button)):
                    self._handle_toggle(mode="hands-free")
                    return 1
                elif self._match_key(vk, self.capture_button):
                    self._handle_toggle(mode="capture")
                    return 1
                elif self._match_key(vk, self.transform_button):
                    if self.on_transform_trigger and not self._is_active():
                        threading.Thread(target=self.on_transform_trigger, daemon=True).start()
                    return 1
                elif self._match_key(vk, self.quiet_toggle_button):
                    if self.on_quiet_toggle_trigger:
                        threading.Thread(target=self.on_quiet_toggle_trigger, daemon=True).start()
                    return 1
                elif self._match_key(vk, self.clipboard_send_button):
                    if self.on_clipboard_send:
                        threading.Thread(target=self.on_clipboard_send, daemon=True).start()
                    return 1
                elif self._match_key(vk, self.copy_quickpick_button):
                    if self.on_copy_quickpick:
                        threading.Thread(target=self.on_copy_quickpick, daemon=True).start()
                    return 1

            # Real typing only: key-downs the dictation hotkeys did not consume above
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) and vk not in _MODIFIER_VKS:
                stamp_user_input(kb.flags, LLKHF_INJECTED)

            # PTT Chord Detection
            if self.ptt_chord != "Disabled":
                ctrl_pressed = self.ctrl_down or bool(user32.GetAsyncKeyState(0x11) & 0x8000)
                win_pressed = self.lwin_down or bool(user32.GetAsyncKeyState(0x5B) & 0x8000)

                if ctrl_pressed and win_pressed:
                    if not self.ptt_active and not self._is_active():
                        self.ptt_active = True
                        self.suppress_lwin_up = True
                        if self.on_ptt_start:
                            threading.Thread(target=self.on_ptt_start, daemon=True).start()
                else:
                    if self.ptt_active:
                        self.ptt_active = False
                        if self.on_ptt_stop:
                            threading.Thread(target=self.on_ptt_stop, daemon=True).start()

        return user32.CallNextHookEx(self.keyboard_hook, nCode, wParam, lParam)

    def _mouse_hook_proc(self, nCode, wParam, lParam):
        if nCode >= 0:
            # If Gaming Mode is active, bypass mouse button interception so the game receives native clicks
            if wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP):
                if self.is_gaming_mode_cb:
                    try:
                        if self.is_gaming_mode_cb():
                            if wParam in (WM_XBUTTONDOWN, WM_MBUTTONDOWN):
                                self._log_gaming_mode_suppression(wParam, lParam)
                            return user32.CallNextHookEx(self.mouse_hook, nCode, wParam, lParam)
                    except Exception:
                        pass

            if wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                ms = MSLLHOOKSTRUCT.from_address(lParam)
                btn = (ms.mouseData >> 16) & 0xFFFF
                btn_name = "Mouse5" if btn == XBUTTON2 else ("Mouse4" if btn == XBUTTON1 else "")

                is_toggle = (btn_name and btn_name.lower() == self.toggle_button.lower()) or \
                            (self.toggle_button == "Mouse5" and btn == XBUTTON2) or \
                            (self.toggle_button == "Mouse4" and btn == XBUTTON1) or \
                            (self.secondary_toggle_button and btn_name and btn_name.lower() == self.secondary_toggle_button.lower())
                is_capture = (btn_name and btn_name.lower() == self.capture_button.lower()) or \
                             (self.capture_button == "Mouse4" and btn == XBUTTON1) or \
                             (self.capture_button == "Mouse5" and btn == XBUTTON2)

                if is_toggle:
                    if wParam == WM_XBUTTONDOWN:
                        self._handle_toggle(mode="hands-free")
                    return 1  # Swallow both Down and Up so target window never receives mouse click or navigation
                elif is_capture:
                    if wParam == WM_XBUTTONDOWN:
                        self._handle_toggle(mode="capture")
                    return 1  # Swallow both Down and Up
            elif wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                is_toggle = self.toggle_button.lower() == "middleclick" or (self.secondary_toggle_button and self.secondary_toggle_button.lower() == "middleclick")
                is_capture = self.capture_button.lower() == "middleclick"
                if is_toggle:
                    if wParam == WM_MBUTTONDOWN:
                        self._handle_toggle(mode="hands-free")
                    return 1
                elif is_capture:
                    if wParam == WM_MBUTTONDOWN:
                        self._handle_toggle(mode="capture")
                    return 1


            # Clicks and wheel count as user activity; plain moves (noise) and the
            # Mouse4/Mouse5 (XBUTTON) dictation buttons do not.
            if wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                stamp_user_input(MSLLHOOKSTRUCT.from_address(lParam).flags, LLMHF_INJECTED)

        return user32.CallNextHookEx(self.mouse_hook, nCode, wParam, lParam)

    def _log_gaming_mode_suppression(self, wParam, lParam):
        """One line, only for the button actually bound to hands-free toggle: 'ignored
        Mouse5', not every click a game receives while Gaming Mode is on."""
        if wParam == WM_XBUTTONDOWN:
            ms = MSLLHOOKSTRUCT.from_address(lParam)
            btn = (ms.mouseData >> 16) & 0xFFFF
            btn_name = "Mouse5" if btn == XBUTTON2 else ("Mouse4" if btn == XBUTTON1 else "")
        elif wParam == WM_MBUTTONDOWN:
            btn_name = "MiddleClick"
        else:
            btn_name = ""
        if not btn_name or btn_name.lower() != self.toggle_button.lower():
            return
        try:
            from context_detector import foreground_process_name
            exe = foreground_process_name() or "fullscreen app"
        except Exception:
            exe = "fullscreen app"
        print(f"[LeonidasScribe] Game Mode: ignored {btn_name} in {exe}")

    def _is_active(self) -> bool:
        if not self.is_active_cb:
            return False
        try:
            return bool(self.is_active_cb())
        except Exception:
            return False

    def _cancel_safety_timer(self):
        if self.toggle_timer:
            self.toggle_timer.cancel()
            self.toggle_timer = None

    def _handle_toggle(self, mode="hands-free"):
        if self.ptt_active:
            return

        now = time.monotonic()
        if now - self._last_toggle_time < self.toggle_debounce_seconds:
            return
        self._last_toggle_time = now

        was_active = self._is_active()

        if mode == "capture" and self.on_capture_trigger:
            threading.Thread(target=self.on_capture_trigger, daemon=True).start()
        elif self.on_toggle_trigger:
            threading.Thread(target=lambda: self.on_toggle_trigger(mode="hands-free"), daemon=True).start()

        self._cancel_safety_timer()
        if not was_active and self.max_toggle_seconds > 0:
            self.toggle_timer = threading.Timer(self.max_toggle_seconds, self._handle_auto_stop)
            self.toggle_timer.daemon = True
            self.toggle_timer.start()

    def _handle_auto_stop(self):
        if self._is_active() and self.on_toggle_trigger:
            print("[LeonidasScribe Hotkeys] Maximum dictation duration reached. Stopping.")
            self.on_toggle_trigger()

    def _handle_cancel(self):
        self._cancel_safety_timer()
        self.ptt_active = False
        if self.on_cancel:
            threading.Thread(target=self.on_cancel, daemon=True).start()

    def _run_hook_loop(self):
        self.hook_thread_id = kernel32.GetCurrentThreadId()

        self.keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0)
        self.mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, None, 0)

        if not self.keyboard_hook or not self.mouse_hook:
            print(f"[LeonidasScribe Hotkeys] Low-level hook registration error: KB={self.keyboard_hook}, MS={self.mouse_hook}, Err={kernel32.GetLastError()}")
        else:
            print(f"[LeonidasScribe Hotkeys] Win32 hooks registered successfully! Toggle={self.toggle_button}, Capture={self.capture_button}")

        self.running = True
        self._ready_event.set()

        msg = wintypes.MSG()
        while self.running:
            b_ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if b_ret == 0 or b_ret == -1 or msg.message == WM_QUIT:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self.keyboard_hook:
            user32.UnhookWindowsHookEx(self.keyboard_hook)
            self.keyboard_hook = None
        if self.mouse_hook:
            user32.UnhookWindowsHookEx(self.mouse_hook)
            self.mouse_hook = None

    def start(self):
        self.hook_thread = threading.Thread(target=self._run_hook_loop, daemon=True)
        self.hook_thread.start()
        self._ready_event.wait(timeout=2.0)

    def stop(self):
        self.running = False
        self._cancel_safety_timer()
        if self.hook_thread_id:
            user32.PostThreadMessageW(self.hook_thread_id, WM_QUIT, 0, 0)
