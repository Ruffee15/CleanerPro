"""Проверки иконок, чекбоксов и анимационного таймера Cleaner Pro (UI v2).

Только интерфейс. Backend проверяют test_security / test_performance /
test_regression / test_rc_fixes.
"""
import os
import sys
import time
import zlib
import struct
import tempfile
import importlib.util
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, "cleaner_pro.pyw")
passed = failed = 0
skipped = 0


def check(name, ok):
    global passed, failed
    if ok:
        passed += 1
        print(f"[OK] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}")


ROOT = tempfile.mkdtemp(prefix="cp_icons_")
os.environ["USERPROFILE"] = ROOT
loader = SourceFileLoader("cp_icons", TARGET_FILE)
spec = importlib.util.spec_from_loader("cp_icons", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
m._collect_app_candidates = lambda extra=(): []


def png_alphas(data):
    """Значения альфа-канала из PNG, который выдаёт _Raster (RGBA, фильтр 0)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w = struct.unpack(">I", body[:4])[0]
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    raw = zlib.decompress(idat)
    stride = 1 + w * 4
    return [raw[r * stride + 1 + x * 4 + 3] for r in range(len(raw) // stride) for x in range(w)]


print("\n=== 1. Сглаживание: растр векторных фигур ===")
for size in (22, 27, 33):           # 100%, 125%, 150% (с учётом увеличения UI)
    R = m._Raster(size)

    class T:
        SURFACE_4, TEXT_3, ACCENT = "#343a45", "#6f7684", "#4c8dff"
    m._draw_checkbox(R, 1.0, T)
    alphas = png_alphas(R.png())
    partial = sum(1 for a in alphas if 0 < a < 255)
    check(f"чекбокс {size}px: края сглажены (полупрозрачных пикселей: {partial})", partial >= size)
    check(f"чекбокс {size}px: углы прозрачны (скругление)", alphas[0] == 0 and alphas[-1] == 0)

try:
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
except Exception as e:      # нет дисплея (CI, сервер без рабочего стола)
    root = None
    skipped = 2
    print(f"\n[SKIP] разделы 2–3 (иконки и окно): нет дисплея — {e}")


def gui_checks():
    global passed, failed
    print("\n=== 2. Выбор иконки по имени / расширению без обращения к диску ===")
    theme = m.Theme(root)
    icons = m.IconSet(theme)
    calls = []
    real = {n: getattr(os.path, n) for n in ("exists", "isdir", "isfile", "islink", "getsize")}
    real_stat, real_scandir = os.stat, os.scandir


    def spy(name, fn):
        def wrapper(*a, **k):
            calls.append(name)
            return fn(*a, **k)
        return wrapper


    for n, fn in real.items():
        setattr(os.path, n, spy(n, fn))
    os.stat, os.scandir = spy("stat", real_stat), spy("scandir", real_scandir)
    try:
        cases = [
            (("Documents", "dir"), "folder:documents"), (("Загрузки", "dir"), "folder:downloads"),
            (("Pictures", "dir"), "folder:pictures"), (("Музыка", "dir"), "folder:music"),
            (("Videos", "dir"), "folder:videos"), (("Projects", "dir"), "folder"),
            (("setup.EXE", "file"), "app"), (("notes.txt", "file"), "text"), (("report.pdf", "file"), "pdf"),
            (("photo.jpeg", "file"), "image"), (("backup.7z", "file"), "archive"), (("song.flac", "file"), "audio"),
            (("movie.mkv", "file"), "video"), (("main.py", "file"), "code"), (("driver.sys", "file"), "system"),
            (("readme", "file"), "file"), (("LinkToProjects", "link"), "link"), (("doc.pdf", "link"), "link_file"),
        ]
        ok_all = True
        for (name, kind), expected in cases:
            got = icons.for_entry(name, kind)
            if got is not icons.get(expected):
                ok_all = False
                print("   несовпадение:", name, kind, "->", expected)
        check(f"{len(cases)} имён/расширений получают свою иконку", ok_all)
    finally:
        for n, fn in real.items():
            setattr(os.path, n, fn)
        os.stat, os.scandir = real_stat, real_scandir
    check(f"при выборе иконок не было обращений к диску ({len(calls)})", not calls)
    check("изображения кэшируются: одна картинка на тип",
          icons.for_entry("a.txt", "file") is icons.for_entry("b.TXT", "file"))
    distinct = {id(icons.get(n)) for n in ("folder", "app", "file", "text", "pdf", "image", "archive",
                                           "audio", "video", "code", "system", "link")}
    check("у каждого типа своя картинка", len(distinct) == 12)
    check("старые ключи dir/file/link/app/folder работают", all(icons[k] for k in ("dir", "file", "link", "app", "folder")))
    t0 = time.perf_counter()
    for n in ("folder", "app", "text"):
        icons.get(n)
    check("повторный запрос иконки — из кэша (мгновенно)", time.perf_counter() - t0 < 0.005)
    root.destroy()

    print("\n=== 3. Окно: кнопки навигации, чекбоксы, общий таймер анимаций ===")
    errors = []
    app = m.CleanerProApp()
    app.report_callback_exception = lambda *e: errors.append(e)


    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            app.update()
            time.sleep(0.005)  # тестовый цикл, не код приложения


    def timers():
        names = [str(app.tk.call("after", "info", i)[0]) for i in app.tk.splitlist(app.tk.call("after", "info"))]
        return [n for n in names if n.endswith("_step")]


    pump(0.8)
    disk = app.pages["disk"]
    nav_btns = (disk.btn_back, disk.btn_fwd, disk.btn_up, disk.btn_refresh)
    check("кнопки ← → ↑ ⟳ — картинки, а не символы шрифта",
          all(b._img_item is not None and b._text_item is None for b in nav_btns))
    img_normal = disk.btn_refresh.itemcget(disk.btn_refresh._img_item, "image")
    disk.btn_refresh._set(hover=True)
    img_hover = disk.btn_refresh.itemcget(disk.btn_refresh._img_item, "image")
    check("при наведении значок сразу светлеет", img_normal != img_hover)
    disk.btn_refresh._set(hover=False)
    check("неактивная кнопка «назад» показывает приглушённый значок",
          not disk.btn_back.enabled and disk.btn_back.itemcget(disk.btn_back._img_item, "image") != img_normal)

    cl = app.pages["cleanup"]
    app.show_page("cleanup")
    pump(0.5)
    row = next(r for r in cl.rows.values() if r.check.enabled)
    cb = row.check
    was = cb.checked
    cb._toggle()
    check("чекбокс: состояние меняется сразу", cb.checked != was and cl.checked[row.target["id"]] == cb.checked)
    check("чекбокс: анимация идёт через общий таймер", len(timers()) == 1)
    pump(0.3)
    check("чекбокс: показан конечный кадр", cb._shown is cb._frames[-1 if cb.checked else 0])
    cb._toggle()
    cl._set_all_checked(False)
    cl._set_all_checked(True)
    check("10 чекбоксов анимируются одним таймером, а не десятью", len(timers()) <= 1)
    pump(0.4)
    check("после анимаций таймеров не осталось", not timers() and not app._cp_animator.active)

    for _ in range(50):          # быстрые наведения: старые переходы отменяются
        disk.btn_reveal._set(hover=True)
        disk.btn_reveal._set(hover=False)
    check("быстрые наведения не копят переходы", len(app._cp_animator.active) <= 3)
    pump(0.4)
    check("…и после них таймеров тоже нет", not timers())
    check("нет ошибок в обработчиках интерфейса", not errors)
    app.destroy()


if root is not None:
    gui_checks()

print("\n" + "=" * 60)
print(f"Всего проверок: {passed + failed}. Провалено: {failed}."
      + (f" Пропущено разделов: {skipped}." if skipped else ""))
if failed:
    sys.exit(1)
print("Все проверки иконок и анимаций пройдены")
