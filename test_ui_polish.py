"""Проверки финального UI-polish Cleaner Pro v1.0 Free.

Отдельно от остальных наборов: только интерфейс (масштаб, анимации, вёрстка).
Backend здесь не проверяется — для этого test_security / test_performance /
test_regression / test_rc_fixes.
"""
import os
import sys
import re
import time
import tempfile
import importlib.util
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, "cleaner_pro.pyw")

passed = failed = 0


def check(name, ok):
    global passed, failed
    if ok:
        passed += 1
        print(f"[OK] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}")


def load_module(name, profile):
    os.environ["USERPROFILE"] = profile
    loader = SourceFileLoader(name, TARGET_FILE)
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


ROOT = tempfile.mkdtemp(prefix="cp_ui_")
prof = os.path.join(ROOT, "profile")
for n in ("Desktop", "Documents"):
    os.makedirs(os.path.join(prof, n), exist_ok=True)
with open(os.path.join(prof, "Desktop", "a.bin"), "wb") as f:
    f.write(b"x" * 1000)

m = load_module("cp_ui", prof)
# Не читаем реальный реестр/диски этого ПК: проверяем интерфейс, а не сканирование
m._collect_app_candidates = lambda extra=(): []
src = open(TARGET_FILE, encoding="utf-8").read()

print("\n=== 1. Масштаб интерфейса для разных экранов и масштабов Windows ===")
cases = {
    "1920×1080, 100%": ((1920, 1080, 1.0), 1.22),
    "2560×1440, 100%": ((2560, 1440, 1.0), 1.22),
    "1920×1080, 125%": ((1920, 1080, 1.25), None),
    "1920×1080, 150%": ((1920, 1080, 1.5), 1.0),
    "2560×1440, 125%": ((2560, 1440, 1.25), 1.22),
    "2560×1440, 150%": ((2560, 1440, 1.5), None),
}
for name, ((w, h, dpi), expected) in cases.items():
    b = m._ui_boost(w, h, dpi)
    in_range = 1.0 <= b <= m.UI_BOOST_MAX
    if expected is not None:
        in_range = in_range and abs(b - expected) < 1e-6
    # Наше увеличение (b > 1) не должно выталкивать базовое окно за экран.
    # При b == 1 размер окна как раньше (его ограничивает сама программа).
    fits = b == 1.0 or (1120 * dpi * b <= w * 0.9 + 1 and 740 * dpi * b <= h * 0.88 + 1)
    # Минимальное окно (860×560) после масштаба тоже помещается
    fits_min = 860 * dpi * b <= w and 560 * dpi * b <= h
    check(f"{name}: увеличение ×{b} в допустимых пределах, окно помещается", in_range and fits and fits_min)
check("на 1440p при 100% интерфейс крупнее на 20–25%", 1.20 <= m._ui_boost(2560, 1440, 1.0) <= 1.25)
check("при 150% на 1080p дополнительного увеличения нет (всё влезает)", m._ui_boost(1920, 1080, 1.5) == 1.0)
check("битые размеры экрана не ломают запуск", m._ui_boost(0, 0, 0) == 1.0)

print("\n=== 2. Никаких sleep() и новых потоков в интерфейсе ===")
ui_src = src.split("#                               ИНТЕРФЕЙС")[1]
code_only = "\n".join(line.split("#", 1)[0] for line in ui_src.splitlines())
check("time.sleep() не используется в коде интерфейса", "sleep(" not in code_only)
check("анимации не создают потоков (класс _Tween без threading)",
      "threading" not in src[src.index("class _Tween"):src.index("class Theme")])
check(f"длительность анимаций в пределах 100–180 мс (ANIM_MS={m.ANIM_MS})", 100 <= m.ANIM_MS <= 180)
durations = [int(x) for x in re.findall(r"\.to\([^\n]*?,\s*(\d+)(?:,|\))", ui_src)]
check(f"все явные длительности переходов ≤ 180 мс ({sorted(set(durations))})",
      durations and max(durations) <= 180)

try:
    import tkinter as tk
    r = tk.Tk()
    r.destroy()
    gui = True
except Exception:
    gui = False

if not gui:
    print("\n[SKIP] GUI-часть: нет дисплея")
else:
    import tkinter as tk
    from tkinter import ttk

    errors = []

    def new_app():
        app = m.CleanerProApp()
        app.report_callback_exception = lambda *e: errors.append(e)
        return app

    def pump(app, sec):
        end = time.time() + sec
        while time.time() < end:
            app.update()
            time.sleep(0.01)  # это тестовый цикл, не код приложения

    def pending_anim_timers(app):
        """Отложенные кадры анимаций (_Tween._step) — в простое их быть не должно."""
        names = [app.tk.call("after", "info", i)[0] for i in app.tk.splitlist(app.tk.call("after", "info"))]
        return [n for n in names if str(n).endswith("_step")]

    app = new_app()
    pump(app, 1.0)
    th = app.theme

    print("\n=== 3. Пропорциональное увеличение ===")
    check(f"scale = DPI × boost ({th.dpi_scale:.2f} × {th.ui_boost})",
          abs(th.scale - th.dpi_scale * th.ui_boost) < 1e-9)
    check("шрифт основного текста увеличен тем же множителем",
          th.f_body[1] == max(1, int(round(11 * th.ui_boost))))
    check("заголовок и навигация увеличены согласованно",
          th.f_title[1] > th.f_nav[1] >= th.f_small[1] and th.f_total[1] > th.f_body[1])
    rh = int(ttk.Style(app).lookup("Cp.Treeview", "rowheight"))
    check(f"высота строк = px(36) ({rh})", rh == th.px(36))
    check("иконки списка увеличены вместе с интерфейсом", app.icons["dir"].width() == th.px(18))
    cleanup = app.pages["cleanup"]
    row = next(iter(cleanup.rows.values()))
    check("чекбокс быстрой очистки крупнее прежнего", int(row.check.cget("width")) == th.px(22))
    check("кнопка «Очистить» не меньше px(42)", int(cleanup.btn_clean.cget("height")) >= th.px(42))

    print("\n=== 4. Анимации доходят до конечного состояния и не оставляют таймеров ===")
    disk = app.pages["disk"]
    btn = disk.btn_reveal
    btn._set(hover=True)
    pump(app, 0.35)
    check("hover кнопки: итоговый цвет = цвет hover", btn._tween.cur == btn._colors())
    btn._set(hover=False)
    pump(app, 0.35)
    check("уход курсора: цвет вернулся к обычному", btn._tween.cur[0] == th.SURFACE_2)

    cb = row.check
    before_checked = cb.checked
    cb._toggle()
    check("состояние чекбокса меняется сразу (логика не ждёт анимацию)", cb.checked != before_checked)
    pump(app, 0.3)
    check("заполнение чекбокса дошло до конца", cb._p == (1.0 if cb.checked else 0.0))
    cb._toggle()
    pump(app, 0.3)

    app.show_page("apps")
    pump(app, 0.4)
    lbl = app.nav._items["apps"]
    ind_x = app.nav._ind.winfo_x()
    exp_x = int(round(app.nav._indicator_target("apps")[0]))
    check(f"индикатор вкладки доехал под активный пункт ({ind_x} ≈ {exp_x})", abs(ind_x - exp_x) <= 1)
    check("цвет текста активной вкладки — основной", str(lbl.cget("fg")).lower() == th.TEXT.lower())

    card = disk.card
    card.set_busy(True)
    pump(app, 0.3)
    check("прогресс: при работе — бегущая полоса", str(card.progress.cget("mode")) == "indeterminate")
    card.set_busy(False)
    pump(app, 0.4)
    check("прогресс: после окончания плавно погас и остановился",
          str(card.progress.cget("mode")) == "determinate")

    app.show_page("disk")
    pump(app, 1.0)
    for _ in range(20):
        btn._set(hover=True)
        btn._set(hover=False)
        app.show_page("cleanup")
        app.show_page("disk")
        app.update()
    check("сразу после серии hover/переключений анимации идут", bool(pending_anim_timers(app)))
    pump(app, 0.6)
    left = pending_anim_timers(app)
    check(f"в простое не осталось анимационных таймеров ({len(left)})", not left)

    t0 = time.process_time()
    pump(app, 2.0)
    cpu_ms = (time.process_time() - t0) * 1000 / 2
    check(f"CPU в простое: {cpu_ms:.0f} мс/с (вместе с самим тестовым циклом)", cpu_ms < 100)

    print("\n=== 5. Вёрстка: кнопки видны и не сжаты на маленьком экране ===")
    for w, h in ((860, 560), (1120, 740), (1600, 1000)):
        app.geometry(f"{th.px(w)}x{th.px(h)}")
        for key in ("disk", "apps", "cleanup"):
            app.show_page(key)
            pump(app, 0.15)
        ok = True
        for b in (disk.btn_delete, disk.btn_reveal, app.pages["apps"].btn_uninstall, cleanup.btn_clean):
            page = b.winfo_toplevel()
            ok = ok and b.winfo_height() >= int(b.cget("height")) - 1
        check(f"окно {w}×{h} (логич.): кнопки действий полной высоты", ok)
    check("нет ошибок в обработчиках интерфейса", not errors)
    app.destroy()

    print("\n=== 6. Симуляция 1920×1080 при 150% ===")
    orig_init = m.Theme.__init__
    orig_sw, orig_sh = tk.Misc.winfo_screenwidth, tk.Misc.winfo_screenheight

    def init_150(self, root):
        root.tk.call("tk", "scaling", 1.5 * 96 / 72)
        orig_init(self, root)

    m.Theme.__init__ = init_150
    tk.Misc.winfo_screenwidth = lambda self: 1920
    tk.Misc.winfo_screenheight = lambda self: 1080
    try:
        app = new_app()
        pump(app, 0.8)
        th = app.theme
        check(f"масштаб Windows распознан (DPI ×{th.dpi_scale:.2f}), без лишнего увеличения",
              abs(th.dpi_scale - 1.5) < 0.02 and th.ui_boost == 1.0)
        geo = app.geometry().split("+")[0].split("x")
        check(f"окно помещается в экран ({app.winfo_width()}×{app.winfo_height()})",
              app.winfo_width() <= 1920 and app.winfo_height() <= 1080)
        ok = True
        for key, btns in (("disk", (app.pages["disk"].btn_delete,)),
                          ("apps", (app.pages["apps"].btn_uninstall,)),
                          ("cleanup", (app.pages["cleanup"].btn_clean,))):
            app.show_page(key)
            pump(app, 0.2)
            for b in btns:
                ok = ok and b.winfo_ismapped() and b.winfo_height() >= int(b.cget("height")) - 1
        check("все кнопки действий видимы и полной высоты", ok)
        check("нет ошибок в обработчиках интерфейса", not errors)
        app.destroy()
    finally:
        m.Theme.__init__ = orig_init
        tk.Misc.winfo_screenwidth, tk.Misc.winfo_screenheight = orig_sw, orig_sh

print("\n" + "=" * 60)
print(f"Всего проверок: {passed + failed}. Провалено: {failed}.")
if failed:
    sys.exit(1)
print("Все проверки UI-polish пройдены")
