"""
Render every Scribe screen and HUD state to PNGs for visual review (offscreen-safe,
real config and history). Usage: python render_screens.py <out_dir>
"""
import os, sys, time
sys.path.insert(0, os.getcwd())
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
from ui_dashboard import DashboardWindow
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ["TEMP"], "scribe-shots")
os.makedirs(out, exist_ok=True)
def pump(sec=0.6):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(); time.sleep(0.02)
w = DashboardWindow(); w.resize(1200, 840); w.show(); pump(1.2)
names = [n.lower() for _, n in w.NAV]
for i,n in enumerate(names):
    w._select_page(i); pump(1.0)
    w.grab().save(os.path.join(out, f"{i}-{n}.png"))
w.select_view(w.insights_view); pump(0.8)
sa = w.insights_view.stack.widget(1)
bar = sa.verticalScrollBar()
i_insights = names.index("insights")  # filenames follow the live NAV index, not a hardcoded guess
for k,frac in enumerate((0.5,1.0)):
    bar.setValue(int(bar.maximum()*frac)); pump(0.4)
    w.grab().save(os.path.join(out, f"{i_insights}-insights-scroll{k}.png"))
w.select_view(w.dictionary_view); w.dictionary_view.btn_tab_vocab.click(); pump(0.4)
i_dictionary = names.index("dictionary")
w.grab().save(os.path.join(out, f"{i_dictionary}-dictionary-vocab.png"))
w.select_view(w.settings_view)
i_settings = names.index("settings")
for i in range(7):
    w.settings_view.select_section(i); pump(0.4)
    w.grab().save(os.path.join(out, f"{i_settings}-settings-s{i}.png"))
w.settings_view.chk_hud.toggle(); pump(0.4)
w.grab().save(os.path.join(out, f"{i_settings}-settings-dirty.png"))
w.settings_view.chk_hud.toggle(); pump(0.3)
w.select_view(w.history_view); pump(0.5)
w.toast("Transcript deleted", "info", "Undo", lambda: None, lambda: None); pump(0.6)
w.grab().save(os.path.join(out, "5-toast.png"))
w.open_palette(); pump(0.5)
w.palette.grab().save(os.path.join(out, "6-palette.png"))
w.palette.close()
w.is_really_quitting = True; w.tray_icon.hide(); w.hide()
from hud_qt import FloatingHUDWidget
h = FloatingHUDWidget()
h.set_level_provider(lambda: 0.12)
for st in ("show_listening","show_transcribing","show_success","show_error"):
    getattr(h, st)(*(("Groq rate limit, retrying on Cerebras",) if st=="show_error" else ()))
    for _ in range(14): h._on_anim_frame()
    app.processEvents()
    h.grab().save(os.path.join(out, f"hud-{st}.png"))
h.show_listening(); h.update_live_transcript("okay so the new design system uses crimson for the one action")
for _ in range(20): h._on_anim_frame()
h.grab().save(os.path.join(out, "hud-live.png"))
print("ok")
