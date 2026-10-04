"""
Регрессионные тесты на баги, найденные в финальном аудите Cleaner Pro v1.0 RC.

Запуск (из папки, где лежит cleaner_pro.pyw):
    python test_rc_fixes.py

Как и остальные тесты, импортирует напрямую production-файл cleaner_pro.pyw.
GUI-часть пропускается, если нет дисплея (на Windows дисплей есть всегда).
Ничего за пределами временных папок не удаляется.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, "cleaner_pro.pyw")
ROOT = tempfile.mkdtemp(prefix="cp_rc_")

failures = []
total = 0


def check(name, cond):
    global total
    total += 1
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


def load_module(name, profile):
    os.environ["USERPROFILE"] = profile
    os.environ["LOCALAPPDATA"] = os.path.join(profile, "AppData", "Local")
    loader = SourceFileLoader(name, TARGET_FILE)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    # Изоляция от реальной системы: «Временные файлы Windows», кэш Windows
    # Update и Windows.old указывают в (несуществующие) папки внутри ROOT,
    # Корзина считается пустой. Иначе итоги очистки зависят от того, что
    # лежит в C:\Windows\Temp и в Корзине компьютера, где идут тесты.
    fake_sys = os.path.join(ROOT, "fake_system_" + name)
    for t in module.TARGETS:
        if t["id"] in ("temp_windows", "windows_update", "windows_old"):
            t["path"] = os.path.join(fake_sys, t["id"])
    module.QUICK_CLEANUP_ALLOWLIST = module._build_quick_cleanup_allowlist()
    module.get_recycle_bin_size = lambda: 0
    return module


def mk(path, size=1000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def real_size(path):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


cp = load_module("cp_rc_core", os.path.join(ROOT, "core_profile"))

# ======================================================================
print("=== 1. get_size при поведении Windows: DirEntry.stat() даёт st_ino = st_dev = 0 ===")
# Документированное поведение Python на Windows. Эмулируем его на любой ОС,
# подменяя os.scandir внутри модуля: stat() записей возвращает нули, а
# entry.inode() — настоящее значение (как на Windows).
tree = os.path.join(ROOT, "wintree")
for i in range(6):
    for j in range(3):
        mk(os.path.join(tree, f"dir{i}", f"sub{j}", "deep", "f.bin"), 1000)
mk(os.path.join(tree, "top.bin"), 500)
expected = real_size(tree)
expected_sub = real_size(os.path.join(tree, "dir3"))

_real_scandir = os.scandir


class _WinEntry:
    def __init__(self, e):
        self._e, self.name, self.path = e, e.name, e.path

    def is_symlink(self):
        return self._e.is_symlink()

    def is_dir(self, follow_symlinks=True):
        return self._e.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, follow_symlinks=True):
        return self._e.is_file(follow_symlinks=follow_symlinks)

    def inode(self):
        return self._e.inode()

    def stat(self, follow_symlinks=True):
        st = self._e.stat(follow_symlinks=follow_symlinks)
        return os.stat_result((st.st_mode, 0, 0, st.st_nlink, st.st_uid, st.st_gid, st.st_size,
                               int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))


class _WinScandir:
    def __init__(self, p):
        self._it = _real_scandir(p)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._it.close()

    def __iter__(self):
        return (_WinEntry(e) for e in self._it)


cp.os.scandir = _WinScandir
try:
    cp.clear_size_cache()
    got = cp.get_size(tree)
    check(f"размер при Windows-поведении верный ({got} == {expected}) — до исправления был 0", got == expected)
    cp.clear_size_cache()
    sub = cp.get_size(os.path.join(tree, "dir3"))
    check("подпапка тоже считается полностью", sub == expected_sub)
finally:
    cp.os.scandir = _real_scandir


class _NoInodeEntry(_WinEntry):
    def inode(self):
        return 0


class _NoInodeScandir(_WinScandir):
    def __iter__(self):
        return (_NoInodeEntry(e) for e in self._it)


cp.os.scandir = _NoInodeScandir
try:
    cp.clear_size_cache()
    check("том без inode (часть сетевых/FAT): размер всё равно верный",
          cp.get_size(tree) == expected)
finally:
    cp.os.scandir = _real_scandir

# Защита от зацикливания не ослабла: настоящий цикл через bind mount (если есть права)
loop_root = os.path.join(ROOT, "loop")
mk(os.path.join(loop_root, "f.txt"), 13)
os.makedirs(os.path.join(loop_root, "back"), exist_ok=True)
mounted = subprocess.run(["mount", "--bind", loop_root, os.path.join(loop_root, "back")],
                         capture_output=True).returncode == 0 if os.name != "nt" else False
if mounted:
    try:
        cp.clear_size_cache()
        t0 = time.perf_counter()
        s = cp.get_size(loop_root)
        check(f"реальный цикл (bind mount) не зацикливает подсчёт: {s} байт за {time.perf_counter() - t0:.3f}с",
              s == 13)
    finally:
        subprocess.run(["umount", os.path.join(loop_root, "back")], capture_output=True)
else:
    print("[SKIP] цикл через bind mount: нет прав на mount")

# ======================================================================
print("\n=== 2. Формат размеров ===")
for value, want in ((0, "0 Б"), (512, "512 Б"), (1536, "1,5 КБ"), (1024 ** 2 * 465, "465,0 МБ"),
                    (int(1024 ** 2 * 1023.99), "1,0 ГБ"), (None, "...")):
    got = cp.format_size(value)
    check(f"format_size({value}) = «{got}», ожидалось «{want}»", got == want)


# ======================================================================
def gui_available():
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.01)


if not gui_available():
    print("\n[SKIP] GUI-часть: нет дисплея")
else:
    from tkinter import messagebox

    orig_box = {n: getattr(messagebox, n) for n in ("askyesno", "showinfo", "showwarning", "showerror")}

    def restore_boxes():
        for n, f in orig_box.items():
            setattr(messagebox, n, f)

    def new_app(module):
        app = module.CleanerProApp()
        errors = []
        app.report_callback_exception = lambda *exc: errors.append(exc)
        return app, errors

    # ------------------------------------------------------------------
    print("\n=== 3. «Программы»: одна папка — один подсчёт, итог без двойного счёта ===")
    prof = os.path.join(ROOT, "apps_profile")
    os.makedirs(profile := prof, exist_ok=True)
    shared = os.path.join(ROOT, "pf", "NVIDIA Corporation")
    mk(os.path.join(shared, "a.bin"), 3000)
    mk(os.path.join(shared, "PhysX", "b.bin"), 2000)
    m = load_module("cp_rc_apps", prof)
    m._collect_app_candidates = lambda extra=(): (
        [(f"NVIDIA компонент {i}", shared, "x.exe", "app") for i in range(6)]
        + [("NVIDIA PhysX", os.path.join(shared, "PhysX"), "y.exe", "app")])
    scans = []
    real_gs = m.get_size

    def counting(path, *a, **k):
        if threading.current_thread().name.startswith("cp-bg") and not a:
            scans.append(path)
        elif threading.current_thread().name.startswith("cp-bg") and a and a[0] is None:
            scans.append(path)
        return real_gs(path, *a, **k)

    m.get_size = counting
    app, errs = new_app(m)
    app.show_page("apps")
    pump(app, 2.0)
    apps = app.pages["apps"]
    same = [p for p in scans if m._cache_key(p) == m._cache_key(shared)]
    check(f"6 записей реестра с одним путём -> подсчётов этой папки: {len(same)} (до исправления 6)",
          len(same) == 1)
    status = apps.status_var.get()
    check(f"итог считает папку один раз, вложенную не добавляет: «{status}»",
          m.format_size(5000) in status)
    check("ошибок интерфейса нет", not errs)
    app.destroy()

    # ------------------------------------------------------------------
    print("\n=== 4. «Быстрая очистка»: несуществующие категории не предлагаются ===")
    prof = os.path.join(ROOT, "clean_profile")
    mk(os.path.join(prof, "AppData", "Local", "Temp", "old.tmp"), 4096)
    m = load_module("cp_rc_clean", prof)
    app, errs = new_app(m)
    app.show_page("cleanup")
    pump(app, 1.0)
    cl = app.pages["cleanup"]
    missing = [t for t in m.TARGETS if t["path"] and not os.path.isdir(t["path"])]
    check("в окружении есть и существующая, и несуществующие категории",
          cl.available.get("temp_user") is True and len(missing) >= 3)
    shown = {}
    messagebox.askyesno = lambda title, msg, **k: shown.setdefault("msg", msg) and False
    try:
        cl.clean_selected()
    finally:
        restore_boxes()
    msg = shown.get("msg", "")
    check("в подтверждении есть существующая категория (%TEMP%)", m.TARGETS[0]["name"] in msg)
    check("в подтверждении НЕТ несуществующих категорий",
          not any(t["name"] in msg for t in missing))
    row = cl.rows[missing[0]["id"]]
    before = cl.checked[missing[0]["id"]]
    row.check._toggle()
    check("чекбокс несуществующей категории не переключается", cl.checked[missing[0]["id"]] == before
          and row.check.checked == before)
    check("итог считает только существующие категории", cl.total_var.get() == m.format_size(4096))

    cl._set_all_checked(False)
    cl.checked[missing[0]["id"]] = True
    cl._update_total()
    check("если отмечены только несуществующие — «Очистить» недоступна",
          not cl.btn_clean._enabled and cl.total_var.get() == "ничего не выбрано")

    print("\n=== 5. F5/⟳ во время очистки не запускает пересчёт ===")
    cl._cleaning = True
    tok = cl.scan_token
    cl.scan_all(force=True)
    check("scan_token не изменился во время очистки", cl.scan_token == tok)
    cl._cleaning = False
    check("ошибок интерфейса нет", not errs)
    app.destroy()

    # ------------------------------------------------------------------
    print("\n=== 6. Системная папка: статус не затирается, «Удалить» недоступна ===")
    prof = os.path.join(ROOT, "disk_profile")
    for j in range(4):
        mk(os.path.join(prof, "Downloads", f"s{j}", "f.bin"), 2048)
    m = load_module("cp_rc_disk", prof)
    app, errs = new_app(m)
    pump(app, 0.8)
    disk = app.pages["disk"]
    disk.status_var.set("Считаю размеры • 2 из 9")
    fake = disk.tree.insert("", "end", text="  Windows", values=("…", "Папка"))
    disk.row_by_path[fake] = (r"C:\Windows", True)
    disk._info[fake] = {"name": "Windows", "kind": "dir", "size": None, "type_label": "Папка"}
    disk.tree.selection_set(fake)
    pump(app, 0.1)
    check("статус подсчёта не затёрт", disk.status_var.get() == "Считаю размеры • 2 из 9")
    check("«Удалить» недоступна для системной папки", not disk.btn_delete._enabled)
    check("показана отдельная подсказка", "удаление недоступно" in disk.sel_hint.cget("text"))
    errors_shown, deleted = [], []
    messagebox.showerror = lambda t, msg, *a, **k: errors_shown.append(msg)
    real_del = m.delete_path_resilient
    m.delete_path_resilient = lambda p: deleted.append(p) or (True, [])
    try:
        disk.delete_selected()  # клавиша Delete по-прежнему вызывает метод напрямую
    finally:
        restore_boxes()
        m.delete_path_resilient = real_del
    check("клавиша Delete: показано сообщение о защите, удаление не вызвано",
          len(errors_shown) == 1 and not deleted)
    disk.tree.delete(fake)

    # ------------------------------------------------------------------
    print("\n=== 7. Закрытие окна посреди удаления спрашивает подтверждение ===")
    disk._deleting = True
    asked = []
    messagebox.askyesno = lambda *a, **k: asked.append(1) or False
    try:
        app.on_close()
        pump(app, 0.1)
        alive = bool(app.winfo_exists())
    finally:
        restore_boxes()
    check("при ответе «Нет» окно осталось открытым", alive and len(asked) == 1)
    disk._deleting = False

    # ------------------------------------------------------------------
    print("\n=== 8. Быстрое переключение вкладок, навигация и изменение размера ===")
    t0 = time.perf_counter()
    for i in range(300):
        app.show_page(("disk", "apps", "cleanup")[i % 3])
        if i % 10 == 0:
            app.update()
    pump(app, 0.3)
    check(f"300 переключений вкладок без ошибок ({time.perf_counter() - t0:.2f}с)", not errs)
    a, b = prof, os.path.join(prof, "Downloads")
    t0 = time.perf_counter()
    for i in range(60):
        disk.navigate_to(a if i % 2 else b)
        if i % 5 == 0:
            app.update()
    pump(app, 1.0)
    check(f"60 быстрых переходов между папками без ошибок ({time.perf_counter() - t0:.2f}с)", not errs)
    last = a if 59 % 2 else b
    check("в итоге открыта последняя выбранная папка", os.path.normcase(disk.current_path) == os.path.normcase(last))
    check("подсчёт завершился, статус не завис", "объект" in disk.status_var.get())
    for w, h in ((700, 450), (2400, 1300), (1100, 700)):
        app.geometry(f"{w}x{h}")
        pump(app, 0.1)
    check("изменение размера окна без ошибок", not errs)

    # ------------------------------------------------------------------
    print("\n=== 9. Закрытие во время большого подсчёта: процесс не висит ===")
    big = os.path.join(ROOT, "big")
    for i in range(40):
        for j in range(40):
            mk(os.path.join(big, f"d{i}", f"e{j}", "f.bin"), 10)
    m.clear_size_cache()
    disk.navigate_to(big)
    pump(app, 0.05)
    t0 = time.perf_counter()
    app.destroy()
    deadline = time.time() + 10
    while time.time() < deadline:
        busy = [t for t in threading.enumerate() if t.name.startswith(("cp-disk", "cp-bg", "cp-list"))]
        if not busy:
            break
        time.sleep(0.05)
    left = [t.name for t in threading.enumerate() if t.name.startswith(("cp-disk", "cp-bg", "cp-list"))
            and t.is_alive()]
    check(f"все фоновые потоки завершились после закрытия за {time.perf_counter() - t0:.2f}с", not left)

shutil.rmtree(ROOT, ignore_errors=True)
print("\n" + "=" * 60)
print(f"Всего проверок: {total}. Провалено: {len(failures)}.")
if failures:
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ВСЕ РЕГРЕССИОННЫЕ ТЕСТЫ RC ПРОЙДЕНЫ")
sys.exit(0)
