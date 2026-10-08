"""Meeting store, note + transcript renderer, templates, destinations, accept command,
retention sweep, and SQLite FTS5 index (S4).
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meeting_recorder as mr


def format_timestamp(seconds: float) -> str:
    sec = max(0, int(round(float(seconds or 0.0))))
    mins, rem = divmod(sec, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{rem:02d}"
    return f"{mins:02d}:{rem:02d}"


def _meeting_date_and_slug(meeting_dir: str, title: str = "meeting") -> tuple[str, str]:
    folder = os.path.basename(os.path.abspath(meeting_dir))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-\d{4})?-(.+)$", folder)
    if m:
        return m.group(1), m.group(2)
    today = datetime.date.today().isoformat()
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "meeting").lower()).strip("-") or "meeting"
    return today, slug


def render_transcript_markdown(manifest: dict, segments: list[dict]) -> str:
    title = (manifest.get("title") or "Meeting").replace("\u2014", "-")
    lines = [f"# Transcript: {title}", ""]
    for seg in segments:
        t_str = format_timestamp(float(seg.get("t0", 0.0)))
        spk = (seg.get("speaker_key") or "Them").replace("\u2014", "-")
        conf = " (?)" if seg.get("confidence") == "?" else ""
        text = (seg.get("text") or "").strip().replace("\u2014", ", ")
        lines.append(f"- [{t_str}] **{spk}{conf}:** {text}")
    return "\n".join(lines) + "\n"


def render_note_markdown(
    manifest: dict,
    note_data: dict,
    transcript_filename: str,
    dest_name: str = "",
) -> str:
    title = (note_data.get("title") or manifest.get("title") or "Meeting").replace("\u2014", "-")
    started = (manifest.get("started") or "")[:10] or datetime.date.today().isoformat()
    people = [p.get("name") or p.get("key") for p in (manifest.get("participants") or []) if p.get("name") or p.get("key")]
    src_obj = manifest.get("source") or {}
    source_label = src_obj.get("app") or src_obj.get("kind") or "audio"
    template_id = manifest.get("template_id") or "default"
    dur_s = mr.saved_audio_seconds(manifest.get("_dir", ""), "me") if manifest.get("_dir") else 0.0

    frontmatter = [
        "---",
        f'date: "{started}"',
        f'duration: "{format_timestamp(dur_s)}"',
        f"people: {json.dumps(people, ensure_ascii=False)}",
        f'source: "{source_label}"',
        f'template: "{template_id}"',
        f'destination: "{dest_name}"',
        "scope: personal",
        "---",
        "",
        f"# {title}",
        "",
        f"Full transcript: [{transcript_filename}](./{transcript_filename})",
        "",
        "## Summary",
    ]
    for b in note_data.get("summary") or ["No summary bullets recorded."]:
        frontmatter.append(f"- {str(b).replace(chr(0x2014), ', ')}")

    frontmatter.extend(["", "## Decisions"])
    decisions = note_data.get("decisions") or []
    if decisions:
        for d in decisions:
            t_cite = format_timestamp(float(d.get("t", 0.0)))
            txt = str(d.get("text", "")).replace("\u2014", ", ")
            frontmatter.append(f"- [{t_cite}](./{transcript_filename}) {txt}")
    else:
        frontmatter.append("- None recorded.")

    frontmatter.extend(["", "## Action items"])
    actions = note_data.get("action_items") or []
    if actions:
        for a in actions:
            t_cite = format_timestamp(float(a.get("t", 0.0)))
            owner = a.get("owner") or "Unassigned"
            task_txt = str(a.get("task", "")).replace("\u2014", ", ")
            frontmatter.append(f"- [{t_cite}] **{owner}:** {task_txt}")
    else:
        frontmatter.append("- None recorded.")

    frontmatter.extend(["", "## Open questions"])
    questions = note_data.get("open_questions") or []
    if questions:
        for q in questions:
            frontmatter.append(f"- {str(q).replace(chr(0x2014), ', ')}")
    else:
        frontmatter.append("- None.")

    return "\n".join(frontmatter) + "\n"


def _db_path(store_root: str) -> str:
    os.makedirs(store_root, exist_ok=True)
    return os.path.join(store_root, "meetings.db")


def _ensure_fts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
            meeting_id UNINDEXED,
            kind UNINDEXED,
            speaker_key,
            t0 UNINDEXED,
            t1 UNINDEXED,
            title,
            content
        )
        """
    )


def index_meeting(
    store_root: str,
    meeting_dir: str,
    note_text: str = "",
) -> None:
    manifest = mr.read_manifest(meeting_dir) or {}
    mid = manifest.get("id") or os.path.basename(os.path.abspath(meeting_dir))
    title = manifest.get("title") or mid
    tpath = os.path.join(meeting_dir, "transcript.json")
    segments = mr._read_json(tpath) or []

    conn = sqlite3.connect(_db_path(store_root))
    try:
        with conn:
            _ensure_fts(conn)
            conn.execute("DELETE FROM meetings_fts WHERE meeting_id = ?", (mid,))
            if note_text:
                conn.execute(
                    "INSERT INTO meetings_fts (meeting_id, kind, speaker_key, t0, t1, title, content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (mid, "note", "", 0.0, 0.0, title, note_text),
                )
            for seg in segments:
                conn.execute(
                    "INSERT INTO meetings_fts (meeting_id, kind, speaker_key, t0, t1, title, content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        mid,
                        "segment",
                        seg.get("speaker_key") or "Them",
                        float(seg.get("t0", 0.0)),
                        float(seg.get("t1", 0.0)),
                        title,
                        seg.get("text") or "",
                    ),
                )
    finally:
        conn.close()


def search_meetings(store_root: str, query: str, limit: int = 12) -> list[dict]:
    db = _db_path(store_root)
    if not os.path.exists(db) or not (query or "").strip():
        return []
    safe_q = '"' + query.replace('"', '""').strip() + '"'
    conn = sqlite3.connect(db)
    try:
        _ensure_fts(conn)
        cur = conn.execute(
            """
            SELECT meeting_id, kind, speaker_key, t0, t1, title, content
            FROM meetings_fts
            WHERE meetings_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_q, int(limit)),
        )
        return [
            {
                "meeting_id": r[0],
                "kind": r[1],
                "speaker_key": r[2],
                "t0": float(r[3]),
                "t1": float(r[4]),
                "title": r[5],
                "text": r[6],
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def save_meeting_to_destination(
    meeting_dir: str,
    dest: dict,
    note_data: dict | None = None,
    store_root: str | None = None,
) -> dict:
    manifest = mr.read_manifest(meeting_dir) or mr.init_meeting_dir(meeting_dir)
    manifest["_dir"] = meeting_dir
    segments = mr._read_json(os.path.join(meeting_dir, "transcript.json")) or []
    date_part, slug = _meeting_date_and_slug(meeting_dir, manifest.get("title") or "meeting")
    base_name = f"{date_part}-{slug}"
    dest_folder = dest["path"]
    os.makedirs(dest_folder, exist_ok=True)

    note_path = os.path.join(dest_folder, f"{base_name}.md")
    transcript_filename = f"{base_name}.transcript.md"
    transcript_path = os.path.join(dest_folder, transcript_filename)

    ndata = note_data or {
        "title": manifest.get("title") or "Meeting",
        "summary": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
    }
    note_md = render_note_markdown(manifest, ndata, transcript_filename, dest_name=dest.get("name", ""))
    trans_md = render_transcript_markdown(manifest, segments)

    with open(note_path, "w", encoding="utf-8") as f:
        f.write(note_md)
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(trans_md)

    manifest.pop("_dir", None)
    manifest["destination_id"] = dest.get("id") or dest.get("name")
    mr.write_manifest(meeting_dir, manifest)

    root = store_root or os.path.dirname(os.path.abspath(meeting_dir))
    index_meeting(root, meeting_dir, note_text=note_md)

    after_cmd = (dest.get("after_save_cmd") or "").strip()
    if after_cmd:
        formatted = after_cmd.format(
            meeting=manifest.get("title") or base_name,
            note_path=note_path,
            transcript_path=transcript_path,
        )
        subprocess.run(formatted, shell=True, capture_output=True, text=True, timeout=30)

    return {"note_path": note_path, "transcript_path": transcript_path}


def accept_action_item(
    meeting_dir: str,
    dest: dict,
    task: str,
    owner: str = "",
    note_path: str | None = None,
) -> dict:
    clean_task = (task or "").strip().replace("\u2014", "-")
    clean_owner = (owner or "").strip().replace("\u2014", "-")
    manifest = mr.read_manifest(meeting_dir) or {}
    meeting_title = (manifest.get("title") or os.path.basename(meeting_dir)).replace("\u2014", "-")

    if note_path and os.path.exists(note_path):
        with open(note_path, "a", encoding="utf-8") as f:
            suffix = f" ({clean_owner})" if clean_owner else ""
            f.write(f"\n- [ ] {clean_task}{suffix}\n")

    cmd_template = (dest.get("on_accept_cmd") or "").strip()
    if not cmd_template:
        return {"returncode": 0, "stdout": "", "stderr": ""}

    cmd = cmd_template.format(
        task=clean_task,
        owner=clean_owner,
        meeting=meeting_title,
        note_path=note_path or "",
    )
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _parse_iso(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except ValueError:
        return None


def audio_retention_badge(
    manifest: dict,
    retention_days: int = 30,
    now: datetime.datetime | None = None,
) -> str | None:
    if not manifest or manifest.get("pinned") or manifest.get("audio_deleted_at"):
        return None
    started = _parse_iso(manifest.get("started"))
    if not started:
        return None
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    age_days = (now_dt - started).total_seconds() / 86400.0
    rem_days = int(round(float(retention_days) - age_days))
    if 0 <= rem_days <= 7:
        return f"Audio {max(1, rem_days)}d"
    return None


def sweep_retention(
    store_root: str,
    retention_days: int = 30,
    now: datetime.datetime | None = None,
) -> dict:
    """Delete audio/ folder ONLY for unpinned meetings older than retention_days.
    Never deletes transcripts or notes."""
    if not os.path.isdir(store_root):
        return {"deleted_count": 0, "deleted_meetings": []}
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    deleted = []
    for entry in sorted(os.listdir(store_root)):
        mdir = os.path.join(store_root, entry)
        if not os.path.isdir(mdir):
            continue
        manifest = mr.read_manifest(mdir)
        if not manifest or manifest.get("pinned") or manifest.get("audio_deleted_at"):
            continue
        started = _parse_iso(manifest.get("started"))
        if not started:
            continue
        age_days = (now_dt - started).total_seconds() / 86400.0
        if age_days > float(retention_days):
            audio_dir = os.path.join(mdir, "audio")
            if os.path.isdir(audio_dir):
                shutil.rmtree(audio_dir, ignore_errors=True)
            for track in ("me", "them"):
                tdir = os.path.join(mdir, track)
                if os.path.isdir(tdir):
                    shutil.rmtree(tdir, ignore_errors=True)
            manifest["audio_deleted_at"] = now_dt.isoformat()
            mr.write_manifest(mdir, manifest)
            deleted.append(manifest["id"])
    return {"deleted_count": len(deleted), "deleted_meetings": deleted}
