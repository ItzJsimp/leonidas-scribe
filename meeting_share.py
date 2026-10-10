"""Share one meeting: a line to hand an agent, the folder path, the whole thing as Markdown, or a single
self-contained HTML page anyone can open. Qt-free; the Meetings view only wires menu items to these.

A meeting dict here is what MeetingsView holds: dir, title, started, duration_s, speakers, state.
"""
import datetime
import html
import json
import os

import meeting_recorder as mr
import meeting_store as mst


def _clean(text) -> str:
    return str(text or "").replace(chr(0x2014), "-")


def _when(m: dict) -> str:
    try:
        dt = datetime.datetime.fromisoformat(m.get("started")).astimezone()
        return f"{dt:%b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"
    except (TypeError, ValueError):
        return ""


def _length(m: dict) -> str:
    mins = int(round(float(m.get("duration_s") or 0.0) / 60.0))
    return f"{mins} min" if mins else ""


def paths(m: dict) -> dict:
    d = m["dir"]
    return {"folder": d, "transcript": os.path.join(d, "transcript.json"), "note": os.path.join(d, "note.json")}


def agent_mention(m: dict) -> str:
    """Paste into any agent chat: it names the meeting and says exactly which files to read."""
    p = paths(m)
    bits = ", ".join(x for x in (_when(m), _length(m), ", ".join(m.get("speakers") or [])) if x)
    return "\n".join([
        f"Leonidas meeting: \"{_clean(m.get('title') or 'Meeting')}\" ({bits})",
        f"Folder: {p['folder']}",
        f"Transcript (speaker, start second, text per segment): {p['transcript']}",
        f"Summary, decisions and action items: {p['note']}",
        "Read the note first, then the transcript where you need the exact words.",
    ])


def _load(m: dict):
    manifest = mr.read_manifest(m["dir"]) or {}
    segs = mr._read_json(paths(m)["transcript"]) or []
    note = mr._read_json(paths(m)["note"]) or {}
    return manifest, segs, note


def as_markdown(m: dict) -> str:
    manifest, segs, note = _load(m)
    out = [f"# {_clean(m.get('title') or 'Meeting')}", ""]
    meta = ", ".join(x for x in (_when(m), _length(m), ", ".join(m.get("speakers") or [])) if x)
    if meta:
        out += [f"*{meta}*", ""]
    if note:
        body = mst.render_note_markdown(manifest, note, "transcript")
        body = body.split("---", 2)[-1].lstrip()  # drop the vault frontmatter
        out += [body.split("\n", 2)[-1].strip(), ""]  # and the duplicate title line
    out += ["## Transcript", "", mst.render_transcript_markdown(manifest, segs).split("\n", 2)[-1].strip()]
    return "\n".join(out).strip() + "\n"


_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{color-scheme:dark}}
body{{margin:0;background:#07070a;color:#e8e8ee;font:16px/1.55 system-ui,Segoe UI,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:40px 22px 80px}}
h1{{font-size:28px;margin:0 0 6px}} h2{{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#8b8b99;margin:36px 0 10px}}
.meta{{color:#8b8b99;font-size:14px;margin-bottom:18px}}
ul{{padding-left:20px}} li{{margin:6px 0}}
.t{{font:12px ui-monospace,Consolas,monospace;color:#00e5ff;margin-right:8px}}
.who{{font-weight:600}} .seg{{margin:10px 0}} .uncertain{{opacity:.7}}
footer{{margin-top:48px;color:#5f5f6b;font-size:12px}}
</style><main>
<h1>{title}</h1><div class="meta">{meta}</div>
{note}
<h2>Transcript</h2>
{transcript}
<footer>Recorded and transcribed with Leonidas Scribe.</footer></main></html>
"""


def as_html(m: dict) -> str:
    manifest, segs, note = _load(m)
    e = lambda s: html.escape(_clean(s))  # noqa: E731
    meta = ", ".join(x for x in (_when(m), _length(m), ", ".join(m.get("speakers") or [])) if x)
    parts = []
    for heading, key in (("Summary", "summary"), ("Decisions", "decisions"), ("Action items", "action_items"),
                         ("Open questions", "open_questions")):
        items = note.get(key) or []
        if not items:
            continue
        lis = []
        for it in items:
            if isinstance(it, dict):
                who = f"<b>{e(it['owner'])}:</b> " if it.get("owner") else ""
                lis.append(f"<li>{who}{e(it.get('task') or it.get('text') or '')}</li>")
            else:
                lis.append(f"<li>{e(it)}</li>")
        parts.append(f"<h2>{heading}</h2><ul>{''.join(lis)}</ul>")
    lines = []
    for s in segs:
        cls = "seg uncertain" if s.get("confidence") == "?" else "seg"
        lines.append(f'<div class="{cls}"><span class="t">{mst.format_timestamp(float(s.get("t0", 0.0)))}</span>'
                     f'<span class="who">{e(s.get("speaker_key") or "Them")}:</span> {e(s.get("text"))}</div>')
    return _PAGE.format(title=e(m.get("title") or "Meeting"), meta=e(meta), note="".join(parts), transcript="\n".join(lines))


def save_page(m: dict, folder: str) -> str:
    """Write the HTML page into folder and return its path (a name that never overwrites another page)."""
    slug = "".join(c if c.isalnum() else "-" for c in _clean(m.get("title") or "meeting").lower()).strip("-")[:48] or "meeting"
    base = os.path.join(folder, f"{slug}.html")
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{slug}-{n}.html")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(as_html(m))
    return path
