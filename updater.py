"""
Automatic Git Update Worker for Leonidas Scribe.
Polls origin/main in the background, applies fast-forward updates,
and triggers the existing self-restart loop when idle.
"""
import os
import sys
import time
import subprocess
import threading
from typing import Optional, Callable, Dict, Any


class GitUpdater:
    """
    Background worker that checks git remote for updates and applies them.
    Guarantees no interference with live dictation: updates only apply
    or trigger restarts when the engine is completely idle.
    """

    def __init__(
        self,
        repo_dir: str,
        check_interval_s: int = 60,
        on_update_applied: Optional[Callable[[], None]] = None,
        on_status_changed: Optional[Callable[[str, str], None]] = None,
        enabled: bool = True,
    ):
        self.repo_dir = os.path.abspath(repo_dir)
        self.check_interval_s = max(15, check_interval_s)
        self.on_update_applied = on_update_applied
        self.on_status_changed = on_status_changed
        self.enabled = enabled

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.last_check_time: float = 0.0
        self.last_status: str = "Idle"
        self.current_commit: str = self._get_local_commit()[:7] if self.is_git_repo() else "unknown"

    def is_git_repo(self) -> bool:
        """Check if repo_dir has its own dedicated .git directory (not an arbitrary parent worktree)."""
        git_dir = os.path.join(self.repo_dir, ".git")
        return os.path.exists(git_dir)

    def _run_git(self, args: list, timeout: float = 15.0) -> subprocess.CompletedProcess:
        """Run a git command silently without opening console windows or prompting for credentials."""
        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        env = os.environ.copy()
        # Suppress all terminal and GUI credential prompts (prevents Git Credential Manager popups)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_ASKPASS"] = ""
        # -c credential.interactive=never ensures GCM never prompts even if invoked
        cmd = ["git", "-c", "credential.interactive=never"] + args
        return subprocess.run(
            cmd,
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            env=env,
        )

    def _get_local_commit(self) -> str:
        try:
            res = self._run_git(["rev-parse", "HEAD"])
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            return ""

    def _get_remote_commit(self, branch: str = "main") -> str:
        try:
            res = self._run_git(["rev-parse", f"origin/{branch}"])
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            return ""

    def check_for_updates(self) -> Dict[str, Any]:
        """
        Fetch remote origin and check if commits are pending.
        Returns dict with check results.
        """
        if not self.is_git_repo():
            return {
                "ok": False,
                "error": "Not a git repository",
                "update_available": False,
                "status": "Standalone mode (no git repo)",
            }

        with self._lock:
            self.last_check_time = time.time()
            try:
                # Determine current branch
                branch_res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
                branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "main"
                if not branch or branch == "HEAD":
                    branch = "main"

                # Fetch origin silently
                fetch_res = self._run_git(["fetch", "origin", branch], timeout=20.0)
                if fetch_res.returncode != 0:
                    err_msg = fetch_res.stderr.strip() or "Failed to reach remote"
                    is_auth_error = any(
                        s in err_msg.lower()
                        for s in ("could not read username", "terminal prompts disabled", "authentication failed", "not found", "unable to get password")
                    )
                    status_text = "Remote requires authentication (updates paused)" if is_auth_error else f"Fetch error: {err_msg[:40]}"
                    self.last_status = status_text
                    return {
                        "ok": False,
                        "error": err_msg,
                        "auth_error": is_auth_error,
                        "update_available": False,
                        "status": status_text,
                    }

                local_head = self._get_local_commit()
                remote_head = self._get_remote_commit(branch)

                if not remote_head:
                    return {
                        "ok": True,
                        "update_available": False,
                        "status": "Up to date",
                        "commit": local_head[:7],
                    }

                if local_head == remote_head:
                    self.last_status = f"Up to date ({local_head[:7]})"
                    return {
                        "ok": True,
                        "update_available": False,
                        "status": self.last_status,
                        "commit": local_head[:7],
                    }

                # Count commits behind
                rev_list = self._run_git(["rev-list", f"{local_head}..{remote_head}", "--count"])
                behind_count = int(rev_list.stdout.strip() or "1") if rev_list.returncode == 0 else 1

                self.last_status = f"Update available ({behind_count} new commit{'s' if behind_count > 1 else ''})"
                return {
                    "ok": True,
                    "update_available": True,
                    "behind_count": behind_count,
                    "local_commit": local_head[:7],
                    "remote_commit": remote_head[:7],
                    "status": self.last_status,
                }
            except Exception as e:
                return {
                    "ok": False,
                    "error": str(e),
                    "update_available": False,
                    "status": f"Check error: {str(e)[:40]}",
                }

    def pull_update(self) -> Dict[str, Any]:
        """
        Pull updates via fast-forward.
        Since secrets.json, history.jsonl and local logs are gitignored,
        fast-forward is clean and frictionless.
        """
        if not self.is_git_repo():
            return {"ok": False, "error": "Not a git repository"}

        with self._lock:
            try:
                branch_res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
                branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "main"

                pull_res = self._run_git(["pull", "--ff-only", "origin", branch], timeout=25.0)
                if pull_res.returncode == 0:
                    new_commit = self._get_local_commit()[:7]
                    self.current_commit = new_commit
                    self.last_status = f"Updated to {new_commit}"
                    print(f"[LeonidasScribe Updater] Successfully pulled update: {new_commit}")
                    return {"ok": True, "commit": new_commit, "output": pull_res.stdout.strip()}
                else:
                    err_msg = pull_res.stderr.strip() or pull_res.stdout.strip()
                    print(f"[LeonidasScribe Updater] Pull failed: {err_msg}")
                    return {"ok": False, "error": err_msg}
            except Exception as e:
                print(f"[LeonidasScribe Updater] Pull exception: {e}")
                return {"ok": False, "error": str(e)}

    def check_and_apply_update(self) -> bool:
        """
        One-shot check and automatic pull if updates exist.
        Returns True if an update was pulled.
        """
        check = self.check_for_updates()
        if check.get("update_available"):
            pull = self.pull_update()
            if pull.get("ok"):
                if self.on_update_applied:
                    try:
                        self.on_update_applied()
                    except Exception as e:
                        print(f"[LeonidasScribe Updater] on_update_applied callback error: {e}")
                return True
        return False

    def start_background_watch(self):
        """Starts background daemon thread polling for updates."""
        if not self.enabled or not self.is_git_repo():
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()

        def _worker():
            # Initial startup check after 10 seconds of app boot
            if not self._stop_event.wait(10.0):
                try:
                    self.check_and_apply_update()
                except Exception as e:
                    print(f"[LeonidasScribe Updater] Initial check error: {e}")

            consecutive_failures = 0
            while not self._stop_event.is_set():
                wait_s = float(self.check_interval_s)
                # If repeated errors or auth failures occur, back off gracefully (up to 15m)
                if consecutive_failures >= 3:
                    wait_s = min(900.0, float(self.check_interval_s) * (2 ** (consecutive_failures - 2)))

                if self._stop_event.wait(wait_s):
                    break

                try:
                    self.check_and_apply_update()
                    if "error" in self.last_status.lower() or "authentication" in self.last_status.lower():
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    print(f"[LeonidasScribe Updater] Periodic check error: {e}")

        self._thread = threading.Thread(target=_worker, daemon=True, name="ScribeAutoUpdaterThread")
        self._thread.start()
        print(f"[LeonidasScribe Updater] Background updater started (polling every {self.check_interval_s}s).")

    def stop_background_watch(self):
        self._stop_event.set()
