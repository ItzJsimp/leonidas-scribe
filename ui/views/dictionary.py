"""
Dictionary: Replacements | Vocabulary as page tabs. One full-height table per
tab with its filter and add row pinned above, so there is exactly one scroll
region. Deletes happen at once and Undo re-adds.
"""
import json
import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QStackedWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QButtonGroup, QApplication, QLabel,
)

from ..icons import glyph_icon
from ..styles import T
from ..widgets import page_header, button, icon_button, run_async, EmptyState

APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(40)  # comfortable density
    hh = t.horizontalHeader()
    hh.setHighlightSections(False)
    hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    hh.setFixedHeight(32)
    for c in range(len(headers) - 1):
        hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
    hh.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Fixed)
    t.setColumnWidth(len(headers) - 1, 52)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return t


def _search(placeholder):
    s = QLineEdit()
    s.setPlaceholderText(placeholder)
    s.setClearButtonEnabled(True)
    s.setMinimumHeight(34)
    s.addAction(glyph_icon("search", 14, T["ink_dim"]), QLineEdit.ActionPosition.LeadingPosition)
    return s


class DictionaryView(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 24, 20)
        layout.setSpacing(16)

        btn_audit = button("Audit transcripts", "", "sync",
                           "Scan recent dictations for mishearings and propose rules")
        btn_audit.clicked.connect(self.run_transcript_audit)
        layout.addLayout(page_header("Dictionary", "Corrections applied after polishing, and words Whisper should already know.",
                                     [btn_audit]))

        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.tab_group = QButtonGroup(self)
        self.btn_tab_rules = button("Replacements", "Segment")
        self.btn_tab_vocab = button("Vocabulary", "Segment")
        self.btn_tab_macros = button("Suggestions", "Segment")
        self.btn_tab_rules.setProperty("first", True)
        self.btn_tab_vocab.setProperty("mid", True)
        self.btn_tab_macros.setProperty("last", True)
        for i, b in enumerate((self.btn_tab_rules, self.btn_tab_vocab, self.btn_tab_macros)):
            b.setCheckable(True)
            self.tab_group.addButton(b, i)
            seg.addWidget(b)
        seg.addStretch(1)
        layout.addLayout(seg)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.tab_group.idClicked.connect(self.stack.setCurrentIndex)

        # --- Replacements --------------------------------------------------
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        add = QHBoxLayout()
        add.setSpacing(8)
        self.in_from = QLineEdit()
        self.in_from.setPlaceholderText("Heard as (e.g. Jaysun)")
        self.in_from.setMinimumHeight(34)
        arrow = QLabel("→")
        arrow.setObjectName("Meta")
        self.in_to = QLineEdit()
        self.in_to.setPlaceholderText("Write as (e.g. Jason)")
        self.in_to.setMinimumHeight(34)
        btn_add_rule = button("Add rule", "", "add")
        btn_add_rule.clicked.connect(self._add_replacement_rule)
        self.in_to.returnPressed.connect(self._add_replacement_rule)
        add.addWidget(self.in_from, 1)
        add.addWidget(arrow)
        add.addWidget(self.in_to, 1)
        add.addWidget(btn_add_rule)
        v.addLayout(add)
        self.filter_rules = _search("Filter rules")
        self.filter_rules.textChanged.connect(lambda _: self.refresh())
        v.addWidget(self.filter_rules)
        self.table_replacements = _table(["HEARD AS", "WRITE AS", ""])
        v.addWidget(self.table_replacements, 1)
        self.stack.addWidget(pane)

        # --- Vocabulary ----------------------------------------------------
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        add = QHBoxLayout()
        add.setSpacing(8)
        self.in_vocab = QLineEdit()
        self.in_vocab.setPlaceholderText("Proper noun or domain word Whisper keeps missing")
        self.in_vocab.setMinimumHeight(34)
        btn_add_vocab = button("Add term", "", "add")
        btn_add_vocab.clicked.connect(self._add_vocab_term)
        self.in_vocab.returnPressed.connect(self._add_vocab_term)
        add.addWidget(self.in_vocab, 1)
        add.addWidget(btn_add_vocab)
        v.addLayout(add)
        self.filter_vocab = _search("Filter vocabulary")
        self.filter_vocab.textChanged.connect(lambda _: self.refresh())
        v.addWidget(self.filter_vocab)
        self.table_vocab = _table(["TERM", ""])
        v.addWidget(self.table_vocab, 1)
        self.stack.addWidget(pane)

        # --- Suggestions: one review queue (review_queue.py) ----------------
        # Shortcuts mined from repeated dictations, words you fixed after pasting, and
        # transcript-audit mishearings all land here; Y approves, N rejects.
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        hint = QLabel("Things Scribe noticed and wants to learn. Select a row and press Y to approve or N to reject.")
        hint.setObjectName("Helper")
        hint.setWordWrap(True)
        self.btn_mine = button("Find repeats", "", "refresh", "Scan your dictation history for passages you repeat word for word")
        self.btn_mine.clicked.connect(self.mine_macros)
        bar.addWidget(hint, 1)
        bar.addWidget(self.btn_mine)
        v.addLayout(bar)
        self.macro_stack = QStackedWidget()
        self.table_macros = _table(["SUGGESTION", "BECOMES", "WHY", ""])
        hh = self.table_macros.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table_macros.setColumnWidth(0, 240)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table_macros.setColumnWidth(2, 190)
        self.table_macros.setColumnWidth(3, 84)
        self.macro_stack.addWidget(self.table_macros)
        for key, slot in ((Qt.Key.Key_Y, self._approve_current), (Qt.Key.Key_N, self._reject_current)):
            sc = QShortcut(QKeySequence(key), self.table_macros)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(slot)
        find = button("Find repeats", "PrimaryBtn", "refresh")
        find.clicked.connect(self.mine_macros)
        self.macro_empty = EmptyState("bulb", "Nothing to review",
                                      "Scribe adds suggestions here when you repeat a passage, fix a word right after it pastes, "
                                      "or run Audit transcripts.", find)
        self.macro_stack.addWidget(self.macro_empty)
        v.addWidget(self.macro_stack, 1)
        self.stack.addWidget(pane)

        self.macros = []
        self.btn_tab_rules.setChecked(True)
        self._load_macros()
        self.refresh()

    # ------------------------------------------------------------------
    def _delete_button(self, slot):
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(6, 0, 6, 0)
        btn = icon_button("delete", "", "Remove", "DangerBtn")
        btn.clicked.connect(slot)
        box.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
        return holder

    def refresh(self):
        data = self.host.dict_data
        replacements = data.get("replacements", {})
        vocab = data.get("vocabulary", [])
        self.btn_tab_rules.setText(f"Replacements  {len(replacements)}")
        self.btn_tab_vocab.setText(f"Vocabulary  {len(vocab)}")

        q = self.filter_rules.text().strip().lower()
        rows = [(k, v) for k, v in replacements.items() if not q or q in k.lower() or q in v.lower()]
        self.table_replacements.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.table_replacements.setItem(r, 0, QTableWidgetItem(k))
            self.table_replacements.setItem(r, 1, QTableWidgetItem(v))
            self.table_replacements.setCellWidget(
                r, 2, self._delete_button(lambda _=False, key=k, val=v: self._delete_replacement(key, val)))

        q = self.filter_vocab.text().strip().lower()
        words = [w for w in vocab if not q or q in w.lower()]
        self.table_vocab.setRowCount(len(words))
        for r, w in enumerate(words):
            self.table_vocab.setItem(r, 0, QTableWidgetItem(w))
            self.table_vocab.setCellWidget(r, 1, self._delete_button(lambda _=False, word=w: self._delete_vocab(word)))

        self._render_macros()

    # --- Suggestions queue ----------------------------------------------
    WHY = {"shortcut": "Repeated {n}\u00d7", "correction": "You fixed it {n}\u00d7", "mishearing": "Transcript audit"}

    def _load_macros(self):
        try:
            import review_queue
            self.macros = review_queue.load_all()
        except ImportError:
            try:  # review_queue not deployed yet: shortcuts only
                import macro_miner
                self.macros = [{"kind": "shortcut", "id": s["id"], "title": " / ".join(s.get("prefixes") or []),
                                "detail": s.get("text", ""), "count": s.get("count", 0)}
                               for s in macro_miner.load_pending()]
            except Exception as e:
                print(f"[Dictionary] suggestions unavailable: {e}")
                self.macros = []
        except Exception as e:
            print(f"[Dictionary] suggestions unavailable: {e}")
            self.macros = []

    def _render_macros(self):
        n = len(self.macros)
        self.btn_tab_macros.setText(f"Suggestions  {n}" if n else "Suggestions")
        self.macro_stack.setCurrentIndex(0 if n else 1)
        self.table_macros.setRowCount(n)
        for r, s in enumerate(self.macros):
            kind = s.get("kind", "shortcut")
            title = f"Say \u201c{s.get('title', '')}\u201d" if kind == "shortcut" else f"\u201c{s.get('title', '')}\u201d"
            detail = s.get("detail", "")
            why = self.WHY.get(kind, "{n}").format(n=s.get("count", 0))
            for c, val in enumerate((title, detail, why)):
                item = QTableWidgetItem(val)
                item.setToolTip(s.get("source", "") if c == 2 else val)
                self.table_macros.setItem(r, c, item)
            self.table_macros.setCellWidget(r, 3, self._macro_actions(s))
        if n and self.table_macros.currentRow() < 0:
            self.table_macros.selectRow(0)

    def _macro_actions(self, s):
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(6, 0, 6, 0)
        box.setSpacing(4)
        ok = icon_button("check", "", "Approve (Y)", color=T["cyan"])
        ok.clicked.connect(lambda _=False, it=s: self._approve_macro(it))
        no = icon_button("close", "", "Reject, never suggest again (N)")
        no.clicked.connect(lambda _=False, it=s: self._reject_macro(it))
        box.addWidget(ok)
        box.addWidget(no)
        return holder

    def _current(self):
        r = self.table_macros.currentRow()
        return (r, self.macros[r]) if 0 <= r < len(self.macros) else (r, None)

    def _approve_current(self):
        r, it = self._current()
        if it:
            self._approve_macro(it, keep_row=r)

    def _reject_current(self):
        r, it = self._current()
        if it:
            self._reject_macro(it, keep_row=r)

    def _after_decision(self, keep_row):
        self._load_macros()
        self._render_macros()
        if self.macros and keep_row is not None:
            self.table_macros.selectRow(min(keep_row, len(self.macros) - 1))
            self.table_macros.setFocus()

    def mine_macros(self):
        self.btn_mine.setEnabled(False)
        self.btn_mine.setText("  Scanning\u2026")

        def work():
            try:
                import macro_miner
                macro_miner.mine()
                return None
            except Exception as e:
                return e

        def done(result):
            self.btn_mine.setEnabled(True)
            self.btn_mine.setText("  Find repeats")
            if isinstance(result, Exception):
                self.host.toast(f"Scan failed: {result}", "error")
                return
            before = {(s.get("kind"), s["id"]) for s in self.macros}
            self._load_macros()
            self._render_macros()
            new = sum(1 for s in self.macros if (s.get("kind"), s["id"]) not in before)
            self.host.toast(f"Found {new} new suggestion{'s' if new != 1 else ''}." if new
                            else "No new repeats in your recent dictations.", "success")

        run_async(work, done)

    def _approve_macro(self, it, keep_row=None):
        try:
            try:
                import review_queue
                msg = review_queue.approve(it.get("kind", "shortcut"), it["id"])
            except ImportError:
                import macro_miner
                name = macro_miner.approve(it["id"])
                msg = f"Shortcut added. Say \u201cleonidas {name.replace('_', ' ')}\u201d to type it."
        except Exception as e:
            self.host.toast(f"Couldn't approve: {e}", "error")
            return
        if it.get("kind") in ("correction", "mishearing"):
            self.host.load_data()  # new replacement rule: refresh the Replacements tab too
        self._after_decision(keep_row)
        self.refresh()
        self.host.toast(msg or "Approved.", "success")

    def _reject_macro(self, it, keep_row=None):
        try:
            try:
                import review_queue
                review_queue.reject(it.get("kind", "shortcut"), it["id"])
            except ImportError:
                import macro_miner
                macro_miner.reject(it["id"])
        except Exception as e:
            self.host.toast(f"Couldn't reject: {e}", "error")
            return
        self._after_decision(keep_row)

    def _changed(self):
        self.host.load_data()
        self.refresh()

    def _add_replacement_rule(self):
        f, t = self.in_from.text().strip(), self.in_to.text().strip()
        if not (f and t):
            self.host.toast("Fill in both what Whisper hears and what to write.", "warn")
            return
        ok, msg = self.host.controller.add_replacement(f, t)
        if ok:
            self.in_from.clear()
            self.in_to.clear()
            self.in_from.setFocus()
            self._changed()
            self.host.toast(f"Rule added: “{f}” → “{t}”", "success")
        else:
            self.host.toast(msg, "error")

    def _delete_replacement(self, key, value):
        ok, msg = self.host.controller.delete_replacement(key)
        if not ok:
            self.host.toast(msg, "error")
            return
        self._changed()

        def undo():
            self.host.controller.add_replacement(key, value)
            self._changed()

        self.host.toast(f"Rule removed: “{key}” → “{value}”", "info", "Undo", undo)

    def _add_vocab_term(self):
        w = self.in_vocab.text().strip()
        if not w:
            return
        ok, msg = self.host.controller.add_vocabulary(w)
        if ok:
            self.in_vocab.clear()
            self._changed()
            self.host.toast(f"Added “{w}” to your vocabulary", "success")
        else:
            self.host.toast(msg, "error")

    def _delete_vocab(self, word):
        ok, msg = self.host.controller.delete_vocabulary(word)
        if not ok:
            self.host.toast(msg, "error")
            return
        self._changed()

        def undo():
            self.host.controller.add_vocabulary(word)
            self._changed()

        self.host.toast(f"Removed “{word}”", "info", "Undo", undo)

    # ------------------------------------------------------------------
    def run_transcript_audit(self):
        """Run Audit-Transcripts.py, then offer its candidate corrections."""
        script = os.path.join(APP_DIR, "Audit-Transcripts.py")
        if not os.path.exists(script):
            self.host.toast("Audit-Transcripts.py is missing from the Scribe folder.", "error")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=30)
            state = os.path.join(APP_DIR, ".audit-state.json")
            pending = []
            if os.path.exists(state):
                with open(state, "r", encoding="utf-8") as f:
                    pending = json.load(f).get("pending", [])
        except Exception as e:
            self.host.toast(f"Transcript audit failed: {e}", "error")
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not pending:
            self.host.toast("Audit complete: no new mishearings in your recent dictations.", "success")
            return
        self.btn_tab_macros.click()
        self._after_decision(0)
        self.host.toast(f"Found {len(pending)} possible mishearing{'s' if len(pending) != 1 else ''}. "
                        "Review them here: Y approves, N rejects.", "info")
