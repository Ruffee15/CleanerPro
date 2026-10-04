"""
Регрессионные и UI-тесты Cleaner Pro v1.0 Free — по багам, найденным на
финальном аудите перед релизом.

Запуск (из папки с cleaner_pro.pyw):
    python test_regression.py

Тест НИКОГДА не удаляет реальные системные или пользовательские файлы:
все удаления — только во временных папках, созданных самим тестом.
GUI-часть пропускается, если нет дисплея (на Windows дисплей есть всегда).
"""
import importlib.util
import ntpath
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


def load_module(name="cleaner_pro"):
    loader = SourceFileLoader(name, TARGET_FILE)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cp = load_module()
failures = []
total = 0


def check(name, cond):
    global total
    total += 1
    print(f"[{'OK' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


def can_symlink():
    d = tempfile.mkdtemp()
    try:
        os.symlink(d, os.path.join(d, "l"), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


ROOT = tempfile.mkdtemp(prefix="cp_regr_")


def mk(path, size=100):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


print("=== 1. Отменённый подсчёт не должен отравлять кэш ===")
tree = os.path.join(ROOT, "cancel_tree")
for i in range(6):
    for j in range(5):
        mk(os.path.join(tree, f"d{i}", f"s{j}", "f.bin"), 1000)
real_size = 30 * 1000
cp.clear_size_cache()
calls = {"n": 0}


def cancel_soon():
    calls["n"] += 1
    return calls["n"] > 4


partial = cp.get_size(tree, cancel_check=cancel_soon)
check(f"прерванный подсчёт вернул частичный размер ({partial} < {real_size})", partial < real_size)
after = cp.get_size(tree)  # кэш НЕ очищаем — именно это и проверяем
check(f"следующий обычный подсчёт видит полный размер ({after} == {real_size})", after == real_size)

print("\n=== 2. Удаление во время подсчёта: старый размер не попадает в кэш ===")
race = os.path.join(ROOT, "race_tree")
for i in range(4):
    mk(os.path.join(race, f"d{i}", "f.bin"), 5000)
cp.clear_size_cache()
done = {"calls": 0}


def delete_mid_scan():
    # Срабатывает внутри рекурсии ПОСЛЕ того, как первая подпапка уже
    # посчитана: имитируем удаление, которое пользователь сделал, пока поток
    # подсчёта ещё работал. Старая версия записывала в кэш размер с уже
    # удалёнными данными.
    done["calls"] += 1
    if done["calls"] == 4:
        for i in range(4):
            shutil.rmtree(os.path.join(race, f"d{i}"), ignore_errors=True)
        cp.invalidate_size_cache(race)
    return False


cp.get_size(race, cancel_check=delete_mid_scan)
check("после инвалидации посреди подсчёта корень НЕ записан в кэш", cp._cache_get(race) is None)
check("повторный подсчёт даёт актуальный размер (всё удалено → 0)", cp.get_size(race) == 0)

print("\n=== 3. Удаление не проходит сквозь ссылки (symlink / junction) ===")
if can_symlink():
    outside = os.path.join(ROOT, "outside_target")
    mk(os.path.join(outside, "precious.txt"), 10)
    victim = os.path.join(ROOT, "victim")
    mk(os.path.join(victim, "junk.txt"), 10)
    os.symlink(outside, os.path.join(victim, "link_to_outside"), target_is_directory=True)
    ok, failed = cp.delete_path_resilient(victim)
    check("папка с вложенной ссылкой удалена", ok and not os.path.lexists(victim))
    check("файлы ЦЕЛИ ссылки не тронуты", os.path.exists(os.path.join(outside, "precious.txt")))

    link_only = os.path.join(ROOT, "just_a_link")
    os.symlink(outside, link_only, target_is_directory=True)
    ok, _ = cp.delete_path_resilient(link_only)
    check("удаление самой ссылки удаляет только ссылку", ok and not os.path.lexists(link_only))
    check("цель после удаления ссылки цела", os.path.exists(os.path.join(outside, "precious.txt")))

    cleanme = os.path.join(ROOT, "cleanme")
    mk(os.path.join(cleanme, "a.tmp"), 10)
    os.symlink(outside, os.path.join(cleanme, "evil_link"), target_is_directory=True)
    errors = cp.clear_folder_contents(cleanme)
    check("clear_folder_contents: содержимое очищено", errors == 0 and os.listdir(cleanme) == [])
    check("clear_folder_contents: цель вложенной ссылки не тронута",
          os.path.exists(os.path.join(outside, "precious.txt")))

    redirected = os.path.join(ROOT, "redirected_temp")
    os.symlink(outside, redirected, target_is_directory=True)
    check("clear_folder_contents отказывается чистить папку-ссылку",
          cp.clear_folder_contents(redirected) == 1 and os.path.exists(os.path.join(outside, "precious.txt")))

    cp.clear_size_cache()
    sized = os.path.join(ROOT, "sized")
    mk(os.path.join(sized, "own.bin"), 700)
    os.symlink(outside, os.path.join(sized, "lnk"), target_is_directory=True)
    check("get_size не считает содержимое вложенных ссылок (нет двойного счёта)", cp.get_size(sized) == 700)
else:
    print("[SKIP] symlink недоступны в этом окружении (на Windows нужен режим разработчика)")

print("\n=== 4. Обход защиты через ссылку: реальный путь в Windows ===")
fake_dir = os.path.join(ROOT, "Downloads", "win", "System32")
mk(os.path.join(fake_dir, "sentinel.dll"), 10)
orig_realpath = os.path.realpath
os.path.realpath = lambda p, *a, **k: r"C:\Windows\System32" if os.path.normpath(p) == os.path.normpath(fake_dir) else orig_realpath(p, *a, **k)
try:
    check("is_protected_for_delete видит реальный путь в C:\\Windows", cp.is_protected_for_delete(fake_dir))
    raised = False
    try:
        cp.delete_path_resilient(fake_dir)
    except cp.ProtectedPathError:
        raised = True
    check("delete_path_resilient бросает ProtectedPathError", raised)
    check("ничего не удалено", os.path.exists(os.path.join(fake_dir, "sentinel.dll")))
finally:
    os.path.realpath = orig_realpath

print("\n=== 5. «Выйти из папки» в корне диска остаётся на месте ===")
check("родитель C:\\ — None (раньше уводило в «C:» = текущий каталог)", cp._parent_path("C:\\", ntpath) is None)
check("родитель C:\\Users — C:\\", cp._parent_path("C:\\Users", ntpath) == "C:\\")
check("родитель D:\\Games\\X — D:\\Games", cp._parent_path("D:\\Games\\X", ntpath) == "D:\\Games")

print("\n=== 6. Реестр: ошибки доступа и повреждённые записи не роняют список ===")


class FakeKey:
    def __init__(self, name, values=None, children=None):
        self.name, self.values, self.children = name, values or {}, children or []


class FakeWinreg:
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"

    def __init__(self):
        good = FakeKey("good", {"DisplayName": "Good App", "InstallLocation": '"C:\\Apps\\Good\\"',
                                "UninstallString": "good.exe /uninstall",
                                "QuietUninstallString": "good.exe /S"})
        bad_type = FakeKey("bad_type", {"DisplayName": 12345})
        broken_loc = FakeKey("broken_loc", {"DisplayName": "Broken", "InstallLocation": "C:\\",
                                            "UninstallString": "broken.exe"})
        component = FakeKey("comp", {"DisplayName": "Hidden", "SystemComponent": 1})
        self.trees = {"HKLM": FakeKey("root", children=[good, bad_type, "EXPLODE", broken_loc, component])}

    def OpenKey(self, parent, sub):
        if parent == "HKCU":
            raise PermissionError("Отказано в доступе")
        if parent in self.trees:
            if "WOW6432Node" in sub:
                raise FileNotFoundError(sub)
            return self.trees[parent]
        for c in parent.children:
            if isinstance(c, FakeKey) and c.name == sub:
                return c
        raise OSError("повреждённая запись")

    def QueryInfoKey(self, key):
        return (len(key.children), 0, 0)

    def EnumKey(self, key, i):
        c = key.children[i]
        if c == "EXPLODE":
            raise OSError("битый подключ")
        return c.name

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 1

    def CloseKey(self, key):
        pass


saved = cp.winreg
cp.winreg = FakeWinreg()
try:
    progs = cp.get_installed_programs()
    check("список получен, несмотря на PermissionError/битые записи", isinstance(progs, dict))
    check("корректная программа найдена", "Good App" in progs)
    check("кавычки в InstallLocation убраны", progs.get("Good App", {}).get("path") == "C:\\Apps\\Good\\")
    check("предпочтён обычный деинсталлятор, а не «тихий»",
          progs.get("Good App", {}).get("uninstall") == "good.exe /uninstall")
    check("запись с DisplayName не-строкой пропущена", 12345 not in progs)
    check("SystemComponent скрыт", "Hidden" not in progs)
finally:
    cp.winreg = saved

print("\n=== 7. Битый InstallLocation не считается папкой программы ===")
check("C:\\ — небезопасно", cp._is_unsafe_install_location("C:\\", set()))
check("C:\\Windows\\System32 — небезопасно", cp._is_unsafe_install_location("C:\\Windows\\System32", set()))
pf_keys = {cp._cache_key("C:\\Program Files")}
check("C:\\Program Files — небезопасно", cp._is_unsafe_install_location("C:\\Program Files", pf_keys))
check("C:\\Program Files\\Foo — нормально", not cp._is_unsafe_install_location("C:\\Program Files\\Foo", pf_keys))

base = os.path.join(ROOT, "FakeProgramFiles")
for name in ("GameA", "GameB"):
    mk(os.path.join(base, name, "x.bin"), 10)
orig = (cp._default_scan_folders, cp._user_appdata_folders, cp.get_installed_programs)
cp._default_scan_folders = lambda: [base]
cp._user_appdata_folders = lambda: []
cp.get_installed_programs = lambda: {"Broken": {"name": "Broken", "path": base, "uninstall": "u.exe"}}
try:
    cands = cp._collect_app_candidates()
    names = {c[0]: c for c in cands}
    check("папки внутри «Program Files» не пропали из-за битой записи",
          "GameA" in names and "GameB" in names)
    check("программа с битым путём осталась без пути (не считает весь контейнер)",
          names.get("Broken", (None, "x"))[1] is None)
finally:
    cp._default_scan_folders, cp._user_appdata_folders, cp.get_installed_programs = orig

print("\n=== 8. Краевые случаи подсчёта размера ===")
uni = os.path.join(ROOT, "Юникод папка ✓", "Игры (копия)")
mk(os.path.join(uni, "сохранение №1.dat"), 1234)
check("кириллица и спецсимволы в путях", cp.get_size(os.path.join(ROOT, "Юникод папка ✓")) == 1234)
deep = ROOT
for i in range(40):
    deep = os.path.join(deep, f"очень_длинное_имя_папки_{i:02d}")
# Сам тест создаёт путь через префикс \\?\: без него Windows с выключенным
# LongPathsEnabled не даёт создать путь длиннее 260 символов (а программа
# работает с такими путями сама, через _fs()).
long_ok = True
try:
    deep_fs = ("\\\\?\\" + os.path.abspath(deep)) if os.name == "nt" else deep
    os.makedirs(deep_fs, exist_ok=True)
    with open(os.path.join(deep_fs, "f.bin"), "wb") as f:
        f.truncate(77)
except OSError as e:
    long_ok = False
    print(f"[SKIP] путь длиной {len(deep)} символов: файловая система не дала его создать ({e})")
if long_ok:
    check(f"путь длиной {len(deep)} символов считается без ошибок",
          cp.get_size(os.path.join(ROOT, "очень_длинное_имя_папки_00")) == 77)
empty = os.path.join(ROOT, "empty_dir")
os.makedirs(empty)
check("пустая папка = 0", cp.get_size(empty) == 0)
check("несуществующий путь = 0 без исключения", cp.get_size(os.path.join(ROOT, "nope")) == 0)
gone = os.path.join(ROOT, "gone.bin")
mk(gone, 10)
os.remove(gone)
check("исчезнувший файл = 0 без исключения", cp.get_size(gone) == 0)
raised = False
try:
    cp._list_directory(os.path.join(ROOT, "nope"))
except OSError:
    raised = True
check("_list_directory на отсутствующей папке бросает OSError (UI покажет состояние ошибки)", raised)

denied = os.path.join(ROOT, "denied_parent")
mk(os.path.join(denied, "ok", "a.bin"), 100)
mk(os.path.join(denied, "locked", "b.bin"), 100)
real_scandir = os.scandir


def fake_scandir(p):
    if os.path.basename(str(p)) == "locked":
        raise PermissionError("Отказано в доступе")
    return real_scandir(p)


cp.clear_size_cache()
cp.os.scandir = fake_scandir
try:
    check("PermissionError на подпапке не роняет подсчёт (учтено доступное)", cp.get_size(denied) == 100)
finally:
    cp.os.scandir = real_scandir


# ======================= GUI =======================
def gui_available():
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


if not gui_available():
    print("\n[SKIP] GUI-часть: нет дисплея")
else:
    import tkinter as tk
    from tkinter import messagebox

    os.environ["USERPROFILE"] = os.path.join(ROOT, "profile")
    for name in ("Downloads", "Desktop", "Documents", "Videos"):
        for j in range(3):
            mk(os.path.join(ROOT, "profile", name, f"sub{j}", "f.bin"), 4096)
    gcp = load_module("cleaner_pro_gui")

    callback_errors = []
    offthread_tk = []
    main_thread = threading.main_thread()

    def guard(cls, meth):
        orig_m = getattr(cls, meth)

        def wrapper(self, *a, **k):
            if threading.current_thread() is not main_thread:
                offthread_tk.append(f"{cls.__name__}.{meth}")
            return orig_m(self, *a, **k)
        setattr(cls, meth, wrapper)

    for cls, meth in ((tk.Misc, "after"), (tk.Misc, "configure"), (tk.Variable, "set"),
                      (tk.ttk.Treeview, "insert"), (tk.ttk.Treeview, "set"), (tk.ttk.Treeview, "item")):
        guard(cls, meth)

    def new_app():
        app = gcp.CleanerProApp()
        app.report_callback_exception = lambda *exc: callback_errors.append(exc)
        return app

    def pump(app, seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            time.sleep(0.01)

    print("\n=== 9. Запуск и масштаб ===")
    app = new_app()
    pump(app, 0.5)
    th = app.theme
    print(f"DPI-масштаб окружения: {th.scale:.2f} (px(36) = {th.px(36)})")
    check("окно запустилось и все три раздела созданы", set(app.pages) == {"disk", "apps", "cleanup"})
    rh = int(tk.ttk.Style(app).lookup("Cp.Treeview", "rowheight"))
    check(f"высота строк масштабируется с DPI ({rh} == px(36))", rh == th.px(36))
    disk = app.pages["disk"]
    pump(app, 1.0)
    check("стартовая папка показана, статус не «завис» на загрузке",
          len(disk.tree.get_children()) == 4 and "объект" in disk.status_var.get())

    print("\n=== 10. Быстрое переключение разделов во время сканирования ===")
    for i in range(60):
        app.show_page(("disk", "apps", "cleanup")[i % 3])
        app.update()
    app.show_page("disk")
    pump(app, 1.5)
    check("после 60 переключений нет ошибок в обработчиках", not callback_errors)
    check("активен последний выбранный раздел", app.nav.active == "disk")

    print("\n=== 11. Быстрые переходы между папками ===")
    folders = [os.path.join(ROOT, "profile", n) for n in ("Downloads", "Desktop", "Documents", "Videos")]
    for i in range(30):
        disk.navigate_to(folders[i % 4])
        app.update()
    final = folders[29 % 4]
    pump(app, 1.0)
    names = sorted(disk._info[i]["name"] for i in disk.tree.get_children())
    check("текущая папка — последняя выбранная", disk.current_path == final)
    check("в таблице строки именно последней папки", names == ["sub0", "sub1", "sub2"])
    check("сканирование завершилось (статус не завис)", disk.status_var.get().startswith("3 объекта"))
    check("переходы «Назад» не накопили дубликатов текущей папки",
          not disk.back_stack or disk.back_stack[-1] != disk.current_path)

    print("\n=== 12. Пустая папка и папка без доступа ===")
    disk.navigate_to(os.path.join(ROOT, "empty_dir"))
    pump(app, 0.3)
    check("пустая папка: понятное состояние", disk.status_var.get() == "Папка пуста"
          and disk.card.empty.winfo_ismapped())
    orig_list = gcp._list_directory

    def denied_list(p):
        raise PermissionError("Отказано в доступе")

    gcp._list_directory = denied_list
    disk.navigate_to(os.path.join(ROOT, "profile"))
    pump(app, 0.3)
    gcp._list_directory = orig_list
    check("нет доступа: понятное сообщение вместо пустоты", disk.status_var.get() == "Нет доступа к этой папке")

    print("\n=== 13. Повторное нажатие «Удалить» не запускает удаление дважды ===")
    target_dir = os.path.join(ROOT, "profile", "ToDelete")
    mk(os.path.join(target_dir, "a.bin"), 10)
    disk.navigate_to(os.path.join(ROOT, "profile"))
    pump(app, 0.5)
    row = next(i for i in disk.tree.get_children() if disk._info[i]["name"] == "ToDelete")
    disk.tree.selection_set(row)
    asks = {"n": 0}
    orig_ask = messagebox.askyesno
    messagebox.askyesno = lambda *a, **k: asks.__setitem__("n", asks["n"] + 1) or True
    try:
        disk.delete_selected()
        disk.delete_selected()
        disk.delete_selected()
        pump(app, 0.8)
    finally:
        messagebox.askyesno = orig_ask
    check("подтверждение показано один раз", asks["n"] == 1)
    check("папка удалена", not os.path.exists(target_dir))
    check("после удаления список обновлён", all(disk._info[i]["name"] != "ToDelete" for i in disk.tree.get_children()))

    print("\n=== 14. Защита в интерфейсе: системная папка не удаляется ===")
    calls_del = {"n": 0}
    orig_del = gcp.delete_path_resilient
    gcp.delete_path_resilient = lambda p: calls_del.__setitem__("n", calls_del["n"] + 1) or (True, [])
    shown = []
    orig_err = messagebox.showerror
    messagebox.showerror = lambda title, msg, *a, **k: shown.append(msg)
    try:
        iid = disk.tree.insert("", "end", text="  Windows", values=("1 ГБ", "Папка"))
        disk.row_by_path[iid] = (r"C:\Windows", True)
        disk._info[iid] = {"name": "Windows", "kind": "dir", "size": 1, "type_label": "Папка"}
        disk.tree.selection_set(iid)
        disk.delete_selected()
    finally:
        gcp.delete_path_resilient = orig_del
        messagebox.showerror = orig_err
    check("показано сообщение о защите", len(shown) == 1 and "систем" in shown[0])
    check("функция удаления не вызывалась", calls_del["n"] == 0)

    print("\n=== 15. Поиск в «Программах» работает во время подсчёта ===")
    apps = app.pages["apps"]
    orig_collect = gcp._collect_app_candidates
    lib = os.path.join(ROOT, "Library")
    for n in ("Alpha Game", "Beta Tool", "Gamma Game"):
        mk(os.path.join(lib, n, "f.bin"), 10)
    gcp._collect_app_candidates = lambda extra=(): [(n, os.path.join(lib, n), None, "folder")
                                                   for n in ("Alpha Game", "Beta Tool", "Gamma Game")]
    try:
        apps.search_var.set("game")
        apps.scan_all(force=True)
        pump(app, 0.05)
        shown_now = sorted(apps.row_meta[i]["name"] for i in apps.tree.get_children())
        check("фильтр применён сразу, ещё до конца подсчёта", shown_now == ["Alpha Game", "Gamma Game"])
        pump(app, 0.6)
        check("после завершения фильтр сохранился",
              sorted(apps.row_meta[i]["name"] for i in apps.tree.get_children()) == ["Alpha Game", "Gamma Game"])
        apps.search_var.set("")
    finally:
        gcp._collect_app_candidates = orig_collect

    print("\n=== 16. Быстрая очистка: защита от повторного запуска ===")
    cleanup = app.pages["cleanup"]
    # Как в любом реальном профиле Windows: есть %TEMP% с файлами. Без этого во
    # «Быстрой очистке» нет ни одной существующей категории, и (начиная с RC)
    # программа честно сообщает «нечего очищать» вместо окна подтверждения.
    mk(os.path.join(ROOT, "profile", "AppData", "Local", "Temp", "old.tmp"), 2048)
    app.show_page("cleanup")
    cleanup.scan_all(force=True)
    pump(app, 0.5)
    asked = {"n": 0}
    messagebox.askyesno = lambda *a, **k: asked.__setitem__("n", asked["n"] + 1) or False
    try:
        cleanup._cleaning = True   # очистка «уже идёт»
        cleanup.clean_selected()
        check("при идущей очистке повторный запуск игнорируется", asked["n"] == 0)
        cleanup._cleaning = False
        cleanup.clean_selected()   # ответ «Нет» — ничего не удаляется
        check("без идущей очистки показывается подтверждение", asked["n"] == 1)
    finally:
        messagebox.askyesno = orig_ask
    check("сумма к освобождению отображается", cleanup.total_var.get() != "")

    print("\n=== 17. Изменение размера окна ===")
    for w, h in ((860, 560), (1920, 1080), (1000, 640), (2560, 1400)):
        app.geometry(f"{th.px(w)}x{th.px(h)}")
        pump(app, 0.15)
    mapped = all(b.winfo_ismapped() for b in (disk.btn_delete, disk.btn_reveal))
    app.show_page("cleanup")
    pump(app, 0.2)
    check("кнопки действий видимы на всех размерах", mapped and cleanup.btn_clean.winfo_ismapped())
    check("нет ошибок при изменении размеров", not callback_errors)

    print("\n=== 18. Ни один фоновый поток не трогал Tk напрямую ===")
    check(f"вызовов Tk из фоновых потоков: {len(offthread_tk)}", not offthread_tk)
    check(f"исключений в обработчиках интерфейса: {len(callback_errors)}", not callback_errors)
    app.destroy()

    print("\n=== 19. Закрытие окна во время фонового сканирования ===")
    big = os.path.join(ROOT, "big_profile")
    for i in range(8):
        for j in range(25):
            mk(os.path.join(big, f"dir{i}", f"s{j}", "f.bin"), 100)
    child = r'''
import os, sys, time, threading
os.environ["USERPROFILE"] = sys.argv[2]
from importlib.machinery import SourceFileLoader
import importlib.util
loader = SourceFileLoader("cp", sys.argv[1]); spec = importlib.util.spec_from_loader("cp", loader)
m = importlib.util.module_from_spec(spec); loader.exec_module(m)
errors = []
threading.excepthook = lambda a: errors.append(repr(a.exc_value))
app = m.CleanerProApp()
app.after(30, lambda: (app.show_page("apps"), app.show_page("cleanup")))
app.after(60, app.on_close)
app.mainloop()
time.sleep(1.0)
print("THREAD_ERRORS", len(errors))
'''
    r = subprocess.run([sys.executable, "-c", child, TARGET_FILE, big], capture_output=True, text=True, timeout=60)
    bad = [s for s in ("Traceback", "invalid command name", "Tcl_AsyncDelete", "main thread is not in main loop")
           if s in r.stderr]
    check("процесс завершился нормально", r.returncode == 0)
    check("нет ошибок Tcl/Tk и трейсбеков при закрытии во время сканирования" + (f" (найдено: {bad})" if bad else ""),
          not bad)
    check("нет исключений в фоновых потоках", "THREAD_ERRORS 0" in r.stdout)

shutil.rmtree(("\\\\?\\" + os.path.abspath(ROOT)) if os.name == "nt" else ROOT, ignore_errors=True)
print("\n" + "=" * 60)
print(f"Всего проверок: {total}. Провалено: {len(failures)}.")
if failures:
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ВСЕ РЕГРЕССИОННЫЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО")
sys.exit(0)
