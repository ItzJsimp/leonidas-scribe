"""
Leonidas Scribe - Zero-Pollution Clipboard & Text Injection Engine
"""
import re
import time
import threading
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
GHND = GMEM_MOVEABLE | GMEM_ZEROINIT

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL

kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID

kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL

user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL

user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL

user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE

user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT

# Formats Win+V (clipboard history, cloud sync) and managers like CopyQ honor, so no
# Scribe write (sentinel, restore, paste) ever lands in the user's clipboard history.
CF_EXCLUDE_MONITOR = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
CF_CAN_INCLUDE_HISTORY = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
CF_CAN_UPLOAD_CLOUD = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")

KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
VK_C = 0x43


class ClipboardInjector:
    def __init__(self):
        self._lock = threading.Lock()

    def get_current_clipboard_text(self) -> str:
        text = ""
        for _ in range(3):
            if user32.OpenClipboard(None):
                try:
                    if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                        h_global = user32.GetClipboardData(CF_UNICODETEXT)
                        if h_global:
                            p_global = kernel32.GlobalLock(h_global)
                            if p_global:
                                try:
                                    text = ctypes.c_wchar_p(p_global).value or ""
                                finally:
                                    kernel32.GlobalUnlock(h_global)
                            break
                finally:
                    user32.CloseClipboard()
            time.sleep(0.01)
        return text

    def get_selected_text_fast(self) -> str:
        """Grab highlighted text in active window via non-polluting Ctrl+C sentinel check."""
        with self._lock:
            orig_clip = self.get_current_clipboard_text()
            sentinel = f"__SCRIBE_SENTINEL_{time.time()}__"
            self.set_clipboard_text(sentinel)
            time.sleep(0.01)

        # Clear modifiers before firing Ctrl+C
        for mod in (0x12, 0x5B, 0x5C, 0x10):
            user32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)

        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.01)
        user32.keybd_event(VK_C, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)

        new_clip = self.get_current_clipboard_text()
        if new_clip and new_clip != sentinel and new_clip.strip():
            # If text was highlighted, restore original clipboard in background
            if orig_clip:
                self.restore_clipboard_text(orig_clip)
            return new_clip
        else:
            # If nothing was selected, restore original clipboard
            if orig_clip:
                self.restore_clipboard_text(orig_clip)
            return ""

    @staticmethod
    def _set_global(fmt: int, data: bytes):
        h = kernel32.GlobalAlloc(GHND, len(data))
        if h:
            p = kernel32.GlobalLock(h)
            if p:
                ctypes.memmove(p, data, len(data))
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(fmt, h)

    def set_clipboard_text(self, text: str) -> bool:
        for _ in range(3):
            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    self._set_global(CF_UNICODETEXT, (text + "\0").encode("utf-16le"))
                    zero_dword = (0).to_bytes(4, "little")
                    self._set_global(CF_EXCLUDE_MONITOR, zero_dword)
                    self._set_global(CF_CAN_INCLUDE_HISTORY, zero_dword)
                    self._set_global(CF_CAN_UPLOAD_CLOUD, zero_dword)
                    return True
                finally:
                    user32.CloseClipboard()
            time.sleep(0.01)
        return False

    def restore_clipboard_text(self, text: str):
        if text:
            self.set_clipboard_text(text)

    def _clear_clipboard(self):
        """No prior clipboard text to restore: leave it empty rather than leaving
        the just-pasted dictation sitting there for a clipboard manager to pick up."""
        for _ in range(3):
            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    return
                finally:
                    user32.CloseClipboard()
            time.sleep(0.01)

    def send_ctrl_v(self, target_hwnd=None):
        if target_hwnd:
            try:
                user32.SetForegroundWindow(target_hwnd)
                time.sleep(0.01)
            except Exception:
                pass

        for mod_vk in (0x5B, 0x5C, 0x12, 0x10):
            user32.keybd_event(mod_vk, 0, KEYEVENTF_KEYUP, 0)

        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.005)
        user32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.010)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.005)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def paste_text(self, text: str, restore_original: bool = False, target_hwnd=None):
        # Last line of defence: the pause marker (U+23F8) is internal and must never
        # reach the target app, whichever path produced this text.
        # Not stripped: a leading space joins this dictation to the previous one.
        text = re.sub(r" *⏸ *", " ", text or "") if "⏸" in (text or "") else text
        if not text:
            return

        with self._lock:
            original_text = self.get_current_clipboard_text() if restore_original else None

            if self.set_clipboard_text(text):
                time.sleep(0.005)
                self.send_ctrl_v(target_hwnd=target_hwnd)

                if restore_original:
                    # Let the paste keystroke land before overwriting the clipboard again.
                    time.sleep(0.2)
                    if original_text:
                        self.restore_clipboard_text(original_text)
                    else:
                        self._clear_clipboard()
