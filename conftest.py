"""pytest isolation. Tests must never touch the user's real meetings.

2026-10-10: a pytest run during a live call built a MeetingsView over the real store, which adopts any live
recording it finds, and a later test stopped and finalized it (about 11 minutes of that call were lost).
Every meetings path honors SCRIBE_MEETINGS_DIR, so point it at a throwaway folder for the whole session.
"""
import os
import tempfile

os.environ["SCRIBE_MEETINGS_DIR"] = tempfile.mkdtemp(prefix="scribe-test-meetings-")
