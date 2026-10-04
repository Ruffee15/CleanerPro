"""Регрессионные тесты аудита безопасности Cleaner Pro (v1.0 Free, v4).

Каждый раздел проверяет конкретное исправление. Всё, что удаляется, создаётся
здесь же во временной папке. Реальные файлы пользователя, C:\\Windows\\Temp и
Корзина не трогаются; деинсталляторы не запускаются (кроме самого Python,
который лишь печатает свои аргументы).
"""
import os
import sys
import stat
import shutil
import tempfile
import subprocess
import importlib.util
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, "cleaner_pro.pyw")
IS_WIN = os.name == "nt"

passed = failed = skipped = 0
failures = []


def check(name, ok):
    global passed, failed
    if ok:
        passed += 1
        print(f"[OK] {name}")
    else:
        failed += 1
        failures.append(name)
        print(f"[FAIL] {name}")


def skip(name, why):
    global skipped
    skipped += 1
    print(f"[SKIP] {name}: {why}")


ROOT = tempfile.mkdtemp(prefix="cp_audit_")
PROFILE = os.path.join(ROOT, "profile")
os.makedirs(PROFILE)
os.environ["USERPROFILE"] = PROFILE
os.environ["LOCALAPPDATA"] = os.path.join(PROFILE, "AppData", "Local")
loader = SourceFileLoader("cp_audit", TARGET_FILE)
spec = importlib.util.spec_from_loader("cp_audit", loader)
cp = importlib.util.module_from_spec(spec)
loader.exec_module(cp)


def mk(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    return r.returncode == 0 and os.path.lexists(link)


def fresh(name):
    d = os.path.join(ROOT, name)
    os.makedirs(d)
    return d


# ----------------------------------------------------------------------
print("=== 1. Защита путей: формы путей, которые раньше проходили проверку ===")
up = cp.USER_PROFILE
must_block = {
    r"\\server\share": "корень сетевого ресурса",
    "\\\\server\\share\\": "корень сетевого ресурса со слэшем",
    "\\\\?\\C:\\": "корень диска с префиксом \\\\?\\",
    "\\\\.\\C:\\": "корень диска с префиксом \\\\.\\",
    "\\\\?\\UNC\\server\\share": "корень ресурса в форме \\\\?\\UNC",
    "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\x": "путь тома без буквы",
    "\\\\.\\PhysicalDrive0": "устройство",
    ".": "относительный путь «.»",
    "..": "относительный путь «..»",
    "Windows": "относительный путь",
    "C:folder": "путь относительно текущей папки диска",
    "": "пустой путь",
    r"C:\Windows.": "точка в конце имени (= C:\\Windows)",
    "C:\\WINDOWS \\System32": "пробел в конце имени",
    r"C:\Windows::$INDEX_ALLOCATION": "альтернативный поток NTFS",
    r"C:\Temp\..\Windows": "«..» внутри пути",
    "c:/windows/system32": "прямые слэши",
    r"C:\Program Files\Common Files": "контейнер Common Files",
    r"C:\Users\Public": "общий профиль",
    r"C:\Users\Default": "профиль по умолчанию",
    r"C:\Users\SomeoneElse": "профиль другого пользователя",
    os.path.join(up, "AppData"): "AppData текущего профиля",
    os.path.join(up, "AppData", "Local"): "AppData\\Local",
    os.path.join(up, "AppData", "Roaming"): "AppData\\Roaming",
    ROOT: "папка, внутри которой лежит профиль",
    r"D:\Windows": "папка Windows на другом диске (по имени)",
}
for p, why in must_block.items():
    check(f"блокируется: {why} ({p!r})", cp.is_protected_path(p))
check("блокируется: None", cp.is_protected_path(None))
for p in cp._windows_dirs_from_api():
    check(f"папка Windows по данным системы защищена: {p}", cp.is_protected_path(os.path.join(p, "x")))

must_allow = [r"C:\Program Files\SomeGame", r"C:\Windows.old", os.path.join(up, "Downloads", "a.zip"),
              r"D:\Games\Old", r"\\server\share\folder", os.path.join(up, "AppData", "Local", "SomeApp"),
              r"C:\Users\SomeoneElse\Downloads\x"]
for p in must_allow:
    check(f"по-прежнему можно удалить: {p}", not cp.is_protected_path(p))

# Windows с нестандартным именем/диском — по фактическому расположению
saved = cp._SYSTEM_SUBTREES
cp._SYSTEM_SUBTREES = saved | {cp._canonical_path(r"E:\WinNT")}
check("Windows в E:\\WinNT: сама папка защищена", cp.is_protected_path(r"E:\WinNT"))
check("Windows в E:\\WinNT: содержимое защищено", cp.is_protected_path(r"E:\WinNT\System32\drivers"))
check("Windows в E:\\WinNT: корень диска E: защищён", cp.is_protected_path("E:\\"))
cp._SYSTEM_SUBTREES = saved
saved_p = cp._PROFILES_ROOTS
cp._PROFILES_ROOTS = saved_p | {cp._canonical_path(r"D:\Profiles")}
check("перенесённая папка профилей D:\\Profiles защищена", cp.is_protected_path(r"D:\Profiles"))
check("профиль внутри D:\\Profiles защищён целиком", cp.is_protected_path(r"D:\Profiles\anna"))
check("файлы внутри такого профиля удалять можно", not cp.is_protected_path(r"D:\Profiles\anna\Downloads\x"))
cp._PROFILES_ROOTS = saved_p

# ----------------------------------------------------------------------
print("\n=== 2. Удаление: ссылки, гонки с подменой, частичное удаление ===")
if not IS_WIN:
    skip("разделы 2–3", "проверки дескрипторов Windows (CreateFileW) — только на Windows")
else:
    outside = fresh("outside")
    mk(os.path.join(outside, "precious.txt"), b"keep")
    precious = os.path.join(outside, "precious.txt")

    # 2.1 Вложенный junction: удаляется только ссылка
    t = fresh("t_nested")
    mk(os.path.join(t, "a", "junk.txt"))
    if junction(os.path.join(t, "a", "j"), outside):
        ok, fl = cp.delete_path_resilient(t)
        check("вложенный junction: папка удалена", ok and not os.path.lexists(t))
        check("вложенный junction: цель не тронута", os.path.exists(precious))
    else:
        skip("вложенный junction", "mklink /J не сработал")

    # 2.2 Гонка: подпапку подменяют на junction ПОСЛЕ перечисления, ДО захода внутрь
    t = fresh("t_race_child")
    victim = os.path.join(t, "sub")
    mk(os.path.join(victim, "f.txt"))
    real_scandir = os.scandir
    swapped = {"done": False}

    class _Swap:
        """Обёртка над scandir: после чтения списка подменяет sub на junction."""
        def __init__(self, it):
            self.it = it

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.it.close()

        def __iter__(self):
            entries = list(self.it)
            if not swapped["done"]:
                swapped["done"] = True
                shutil.rmtree(victim)
                junction(victim, outside)
            return iter(entries)

    def racing_scandir(path):
        it = real_scandir(path)
        return _Swap(it) if not swapped["done"] and os.path.normcase(str(path)).endswith(
            os.path.normcase(t)) else it

    # Код удаления перечисляет папку через дескриптор (_w_list_names), а не
    # os.scandir: подмену делаем сразу после перечисления содержимого.
    real_list_names = cp._w_list_names

    def racing_list(h):
        names = real_list_names(h)
        if not swapped["done"] and os.path.basename(victim) in names:
            swapped["done"] = True
            shutil.rmtree(victim)
            junction(victim, outside)
        return names

    cp._w_list_names = racing_list
    try:
        cp.delete_path_resilient(t)
    finally:
        cp._w_list_names = real_list_names
    check("гонка (подпапка → junction): подмена произошла", swapped["done"])
    check("гонка (подпапка → junction): файлы цели НЕ удалены", os.path.exists(precious))
    check("гонка (подпапка → junction): папка удалена", not os.path.lexists(t))

    # 2.3 Подмена объекта между подтверждением и удалением
    t = fresh("t_changed")
    mk(os.path.join(t, "f.txt"))
    ident = cp.path_identity(t)
    shutil.rmtree(t)
    junction(t, outside)
    raised = False
    try:
        cp.delete_path_resilient(t, expected_identity=ident)
    except cp.PathChangedError:
        raised = True
    check("папку заменили на junction после подтверждения → отказ (PathChangedError)", raised)
    check("…цель junction не тронута", os.path.exists(precious))
    os.rmdir(t)

    t = fresh("t_recreated")
    ident = cp.path_identity(t)
    os.rmdir(t)
    os.makedirs(t)                      # то же имя, другой объект
    raised = False
    try:
        cp.delete_path_resilient(t, expected_identity=ident)
    except cp.PathChangedError:
        raised = True
    check("папку пересоздали после подтверждения → отказ", raised and os.path.isdir(t))

    t = fresh("t_vanished")
    ident = cp.path_identity(t)
    os.rmdir(t)
    raised = False
    try:
        cp.delete_path_resilient(t, expected_identity=ident)
    except cp.PathChangedError:
        raised = True
    check("объект исчез после подтверждения → сообщение «изменился», без исключений иного рода", raised)
    check("несуществующий путь без отпечатка — «уже удалено», без ошибки",
          cp.delete_path_resilient(t) == (True, []))

    # 2.4 Путь проходит через junction в защищённое место: проверка по дескриптору
    win_dirs = cp._windows_dirs_from_api()
    via = os.path.join(ROOT, "via_link")
    if win_dirs and junction(via, win_dirs[0]):
        sub = os.path.join(via, "Temp")
        raised = False
        try:
            cp.delete_path_resilient(sub)
        except cp.ProtectedPathError:
            raised = True
        check("путь через junction в папку Windows → ProtectedPathError (реальное расположение)", raised)
        check("…папка Windows\\Temp на месте", os.path.isdir(os.path.join(win_dirs[0], "Temp")))
        os.rmdir(via)
    else:
        skip("junction в папку Windows", "не удалось создать")

    # 2.5 Только чтение, длинный путь, имя с точкой в конце
    t = fresh("t_special")
    ro = os.path.join(t, "ro.txt")
    mk(ro)
    os.chmod(ro, stat.S_IREAD)
    P = "\\\\?\\"
    long_dir = os.path.join(t, *(["d" * 40] * 7))
    os.makedirs(P + long_dir)
    with open(P + os.path.join(long_dir, "deep.txt"), "wb") as f:
        f.write(b"z")
    os.makedirs(P + os.path.join(t, "dotname."))
    ok, fl = cp.delete_path_resilient(t)
    check(f"файл «только чтение», путь {len(long_dir)}+ символов и имя «dotname.» удалены", ok and not fl
          and not os.path.lexists(t))

    # 2.6 Заблокированный файл — частичное удаление честно сообщается
    t = fresh("t_locked")
    keep = os.path.join(t, "busy.txt")
    mk(keep)
    mk(os.path.join(t, "free.txt"))
    w = cp._win32()
    h = w["k32"].CreateFileW(keep, 0x80000000, 0x1, None, 3, 0, None)   # чтение, без FILE_SHARE_DELETE
    try:
        ok, fl = cp.delete_path_resilient(t)
    finally:
        w["k32"].CloseHandle(h)
    check("занятый файл: результат «удалено не всё»", ok is False and any("busy.txt" in x for x in fl))
    check("занятый файл: остальное удалено, занятый — на месте",
          not os.path.exists(os.path.join(t, "free.txt")) and os.path.exists(keep))

    # 2.7 Закрепление: пока идёт удаление, папку и её родителя нельзя переименовать
    t = fresh("t_pin")
    os.makedirs(os.path.join(t, "inner"))
    with cp._w_open(cp._ext_path(os.path.join(t, "inner")), cp._DEL_ACCESS):
        blocked = []
        for src, dst in ((os.path.join(t, "inner"), os.path.join(t, "inner2")), (t, t + "_moved")):
            try:
                os.rename(src, dst)
                blocked.append(False)
            except OSError:
                blocked.append(True)
    check("открытую при удалении папку нельзя подменить (переименовать)", blocked[0])
    check("…и её родителя тоже", blocked[1])

    # 2.8 Удаление самой ссылки
    lnk = os.path.join(ROOT, "just_link")
    if junction(lnk, outside):
        ident = cp.path_identity(lnk)
        ok, _ = cp.delete_path_resilient(lnk, expected_identity=ident)
        check("junction как цель: удалена только ссылка", ok and not os.path.lexists(lnk)
              and os.path.exists(precious))

    # ------------------------------------------------------------------
    print("\n=== 3. Быстрая очистка: только своя папка, ссылки не выводят наружу ===")
    temp_like = fresh("cat_temp")
    mk(os.path.join(temp_like, "a.tmp"))
    mk(os.path.join(temp_like, "sub", "b.tmp"))
    junction(os.path.join(temp_like, "evil"), outside)
    errors = cp.clear_folder_contents(temp_like)
    check("очистка: содержимое удалено, папка категории осталась",
          errors == 0 and os.path.isdir(temp_like) and os.listdir(temp_like) == [])
    check("очистка: цель вложенного junction не тронута", os.path.exists(precious))

    redirected = os.path.join(ROOT, "cat_redirected")
    junction(redirected, outside)
    check("очистка: папка категории-ссылка — отказ", cp.clear_folder_contents(redirected) == 1
          and os.path.exists(precious))

    # Гонка в очистке: подпапку подменяют на junction после перечисления
    temp_like = fresh("cat_race")
    victim = os.path.join(temp_like, "sub")
    mk(os.path.join(victim, "f.tmp"))
    swapped["done"] = False
    t = temp_like
    cp._w_list_names = racing_list
    try:
        cp.clear_folder_contents(temp_like)
    finally:
        cp._w_list_names = real_list_names
    check("очистка, гонка (подпапка → junction): подмена произошла", swapped["done"])
    check("очистка, гонка: файлы цели НЕ удалены", os.path.exists(precious))

    check("очистка отказывает на папке Windows целиком",
          all(cp.clear_folder_contents(d) == 1 for d in cp._windows_dirs_from_api()))
    check("очистка отказывает на профиле и AppData\\Local",
          cp.clear_folder_contents(cp.USER_PROFILE) == 1
          and cp.clear_folder_contents(os.path.join(cp.USER_PROFILE, "AppData", "Local")) == 1)

check("allowlist очистки не принимает произвольный путь",
      not cp.is_allowed_quick_cleanup_path(os.path.join(ROOT, "cat_temp")))
check("allowlist очистки не принимает родителя категории",
      not cp.is_allowed_quick_cleanup_path(os.path.dirname(cp.TARGETS[0]["path"])))
src = open(TARGET_FILE, encoding="utf-8").read()
check("Windows.old и Корзина не отмечены по умолчанию (OPT_IN)",
      "OPT_IN = {\"windows_old\", \"recycle_bin\"}" in src
      and 't["id"] not in self.OPT_IN for t in TARGETS' in src)
check("у Windows.old и Корзины есть явное предупреждение",
      all(t.get("note") for t in cp.TARGETS if t["id"] in ("windows_old", "recycle_bin")))

# ----------------------------------------------------------------------
print("\n=== 4. Удаление программ: разбор команды без оболочки ===")
apps = fresh("Program Files X")
inst = os.path.join(apps, "My App", "unins000.exe")
mk(inst)
other = os.path.join(apps, "Other App", "uninstall.exe")
mk(other)


def parse(cmd):
    try:
        return cp.parse_uninstall_command(cmd)
    except cp.UninstallCommandError as e:
        return e


r = parse(f'"{inst}" /SILENT')
check("в кавычках: exe и аргументы", not isinstance(r, Exception) and r[0] == inst and r[1] == "/SILENT")
r = parse(f'{inst} /x "a b"')
check("без кавычек, путь с пробелами: найден единственный exe", not isinstance(r, Exception)
      and r[0] == inst and r[1] == '/x "a b"')
r = parse(f'"{inst}" _?={os.path.dirname(inst)}')
check("ключ NSIS _?=путь_с_пробелами передаётся как есть (без split)",
      not isinstance(r, Exception) and r[1] == f"_?={os.path.dirname(inst)}")
r = parse("MsiExec.exe /X{11111111-2222-3333-4444-555555555555}")
check("MsiExec без пути → только System32\\msiexec.exe", not isinstance(r, Exception)
      and os.path.normcase(r[0]) == os.path.normcase(os.path.join(cp._system32_dir(), "msiexec.exe"))
      and r[1].startswith("/X{"))
os.environ["CP_AUDIT_DIR"] = os.path.dirname(inst)
r = parse('"%CP_AUDIT_DIR%\\unins000.exe" /S')
check("переменные окружения раскрываются программой, а не cmd.exe", not isinstance(r, Exception) and r[0] == inst)
r = parse(f'"{inst}" /S & del C:\\x')
check("«& del …» остаётся аргументом деинсталлятора, а не командой", not isinstance(r, Exception)
      and r[1] == "/S & del C:\\x")
for bad, why in [("", "пустая строка"), ('"C:\\nope\\un.exe" /S', "файл не существует"),
                 ("unins000.exe /S", "имя без пути (поиск в PATH/текущей папке)"),
                 ('"' + inst, "незакрытая кавычка"), (f'"{inst}"x', "мусор после кавычки"),
                 ("notepad.exe", "посторонняя программа без пути"),
                 (f'"{os.path.join(apps, "My App", "readme.txt")}"', "не .exe"),
                 ('"\\\\server\\share\\un.exe"', "сетевой путь"),
                 ("%NO_SUCH_VAR_CP%\\un.exe", "нераскрытая переменная"),
                 ("..\\un.exe", "относительный путь")]:
    check(f"отказ: {why}", isinstance(parse(bad), cp.UninstallCommandError))
amb_dir = fresh("amb")
mk(os.path.join(amb_dir, "a.exe"))
mk(os.path.join(amb_dir, "a.exe b.exe"))
check("отказ: неоднозначный путь без кавычек (два подходящих .exe)",
      isinstance(parse(os.path.join(amb_dir, "a.exe b.exe") + " /S"), cp.UninstallCommandError))

calls = []
cp.launch_uninstaller(f'"{inst}" /S', _popen=lambda *a, **k: calls.append((a, k)))
check("запуск: shell=False, exe передан явно, рабочая папка — папка деинсталлятора",
      calls and calls[0][1].get("shell") is False and calls[0][1].get("executable") == inst
      and calls[0][1].get("cwd") == os.path.dirname(inst) and calls[0][0][0] == f'"{inst}" /S')


def need_admin(*a, **k):
    e = OSError("elevation required")
    e.winerror = 740
    raise e


shell_calls = []
cp.launch_uninstaller(f'"{inst}" /S', _popen=need_admin, _shell_execute=lambda exe, args: shell_calls.append((exe, args)))
check("деинсталлятор требует прав (740) → ShellExecute «open» тем же exe (UAC по манифесту), без runas",
      shell_calls == [(inst, "/S")] and '"runas"' not in src)
check("в коде нет shell=True", "shell=True" not in src)
raised = False
try:
    cp.launch_uninstaller("notepad.exe", _popen=lambda *a, **k: calls.append("RAN"))
except cp.UninstallCommandError:
    raised = True
check("повреждённая команда — ничего не запущено", raised and "RAN" not in calls)

if IS_WIN:
    # Настоящий CreateProcess: Python печатает свои аргументы. & и кавычки не
    # должны выполниться как команды оболочки.
    marker = os.path.join(ROOT, "SHELL_RAN.txt")
    cmd = (f'"{sys.executable}" -c "import sys; print(sys.argv[1:])" '
           f'a&echo x>"{marker}" "_?=C:\\Program Files\\X"')
    out = {}

    def run_capture(cmdline, **kw):
        out["r"] = subprocess.run(cmdline, capture_output=True, text=True, **kw)
    cp.launch_uninstaller(cmd, _popen=run_capture)
    printed = out["r"].stdout.strip()
    check(f"реальный запуск: «&» не выполнен оболочкой ({printed})",
          not os.path.exists(marker) and "a&echo" in printed and "_?=C:\\\\Program Files\\\\X" in printed)
else:
    skip("реальный запуск деинсталлятора", "только Windows")

check("в «Программах» нет удаления папок (только деинсталлятор/Проводник)",
      "delete_path_resilient" not in src[src.index("class AppsTab"):src.index("class CleanupRow")])

# ----------------------------------------------------------------------
print("\n=== 5. Проводник запускается по полному пути ===")
if IS_WIN:
    exe = cp._explorer_exe()
    check(f"explorer.exe — полный путь в папке Windows ({exe})",
          bool(exe) and os.path.isabs(exe) and os.path.isfile(exe))
else:
    skip("explorer.exe", "только Windows")

# ----------------------------------------------------------------------
print("\n=== 6. Перенаправление родителя категории и подмена цепочки во время работы ===")
if not IS_WIN:
    skip("раздел 6", "junction и дескрипторы — только Windows")
else:
    # 6.1 Junction В РОДИТЕЛЕ настоящей категории из allowlist (кэш Chrome).
    #     Конечная Cache — обычная папка (не ссылка) вне разрешённого кэша.
    chrome = next(t for t in cp.TARGETS if t["id"] == "chrome_cache")["path"]
    local = os.environ["LOCALAPPDATA"]
    google_link = os.path.join(local, "Google")
    elsewhere = fresh("elsewhere_google")
    foreign_cache = os.path.join(elsewhere, "Chrome", "User Data", "Default", "Cache")
    control = os.path.join(foreign_cache, "CONTROL_do_not_delete.txt")
    mk(control, b"keep")
    os.makedirs(local, exist_ok=True)
    if junction(google_link, elsewhere):
        check("6.1 условия: путь категории есть в allowlist", cp.is_allowed_quick_cleanup_path(chrome))
        check("6.1 условия: сама папка Cache — НЕ ссылка (обход _is_link_path)", not cp._is_link_path(chrome))
        check("6.1 условия: realpath уводит за пределы категории",
              os.path.normcase(os.path.realpath(chrome)) == os.path.normcase(foreign_cache))
        raised = None
        try:
            cp.clear_quick_cleanup_category(chrome)
        except cp.RedirectedPathError as e:
            raised = e
        check("6.1 очистка категории с junction в родителе → отказ (RedirectedPathError)", raised is not None)
        check("6.1 контрольный файл за пределами кэша сохранён", os.path.exists(control))
        check("6.1 общая clear_folder_contents тоже отказывает (1) и ничего не удаляет",
              cp.clear_folder_contents(chrome) == 1 and os.path.exists(control))
        check("6.1 allowlist не расширился до реальной цели",
              not cp.is_allowed_quick_cleanup_path(foreign_cache))
        os.rmdir(google_link)
    else:
        skip("6.1", "mklink /J не сработал")

    # 6.2 То же для полного удаления категории (Windows.old): строгий режим
    sysdrive = os.path.join(ROOT, "fake_sysdrive")
    real_drive = fresh("real_drive_elsewhere")
    old_ctrl = os.path.join(real_drive, "Windows.old", "CONTROL.txt")
    mk(old_ctrl, b"keep")
    if junction(sysdrive, real_drive):
        logical_old = os.path.join(sysdrive, "Windows.old")
        saved_allow = cp.QUICK_CLEANUP_ALLOWLIST
        cp.QUICK_CLEANUP_ALLOWLIST = saved_allow | {os.path.normcase(os.path.normpath(logical_old))}
        try:
            raised = None
            try:
                cp.delete_path_resilient(logical_old, strict_allowlist=True)
            except cp.RedirectedPathError as e:
                raised = e
            check("6.2 Windows.old через junction в родителе → отказ", raised is not None)
            check("6.2 контрольный файл сохранён", os.path.exists(old_ctrl))
        finally:
            cp.QUICK_CLEANUP_ALLOWLIST = saved_allow
        not_allowed = None
        try:
            cp.delete_path_resilient(os.path.join(real_drive, "Windows.old"), strict_allowlist=True)
        except cp.ProtectedPathError as e:
            not_allowed = e
        check("6.2 строгий режим: путь вне allowlist → отказ", not_allowed is not None and os.path.exists(old_ctrl))
        os.rmdir(sysdrive)

    # 6.3 Перенацеливание junction-родителя ВО ВРЕМЯ удаления (после проверки корня).
    #     Закрепление здесь не помогает: сам junction не заблокирован нашим дескриптором.
    o1, o2 = fresh("chain_o1"), fresh("chain_o2")
    mk(os.path.join(o1, "target", "sub", "f.txt"))
    precious2 = os.path.join(o2, "target", "sub", "PRECIOUS.txt")
    mk(precious2, b"keep")
    via = os.path.join(ROOT, "chain_via")
    if junction(via, o1):
        state = {"retargeted": False}
        real_list = cp._w_list_names

        def retarget_then_list(h):
            if not state["retargeted"]:
                state["retargeted"] = True
                os.rmdir(via)                      # junction удаляется как объект-ссылка
                junction(via, o2)                  # и создаётся заново на другую папку
            return real_list(h)

        cp._w_list_names = retarget_then_list
        try:
            ok, fl = cp.delete_path_resilient(os.path.join(via, "target"))
        finally:
            cp._w_list_names = real_list
        check("6.3 условия: junction-родитель действительно перенацелен во время удаления",
              state["retargeted"] and os.path.normcase(os.path.realpath(via)) == os.path.normcase(o2))
        check("6.3 файлы новой цели junction НЕ удалены", os.path.exists(precious2))
        check("6.3 удалено именно то, что было открыто и проверено (o1\\target)",
              not os.path.exists(os.path.join(o1, "target")))
        os.rmdir(via)
    else:
        skip("6.3", "mklink /J не сработал")

    # 6.4 Попытка заменить настоящего родителя категории во время очистки.
    #     Блокировка переименования — не доказательство; проверяем результат.
    cat_parent = fresh("cat_parent")
    cat = os.path.join(cat_parent, "Cache")
    mk(os.path.join(cat, "junk", "a.tmp"))
    decoy = fresh("decoy")
    decoy_file = os.path.join(decoy, "Cache", "junk", "DECOY.txt")
    mk(decoy_file, b"keep")
    state = {"tried": False, "renamed": False}
    real_list = cp._w_list_names

    def swap_parent_then_list(h):
        if not state["tried"]:
            state["tried"] = True
            try:
                os.rename(cat_parent, cat_parent + "_moved")
                state["renamed"] = True
                junction(cat_parent, decoy)
            except OSError:
                pass
        return real_list(h)

    cp._w_list_names = swap_parent_then_list
    try:
        cp.clear_folder_contents(cat)
    finally:
        cp._w_list_names = real_list
    print(f"   (переименование родителя во время очистки {'УДАЛОСЬ' if state['renamed'] else 'заблокировано Windows'})")
    check("6.4 при попытке подменить родителя категории посторонние файлы целы", os.path.exists(decoy_file))
    if state["renamed"]:
        os.rmdir(cat_parent)
        os.rename(cat_parent + "_moved", cat_parent)

shutil.rmtree(("\\\\?\\" + ROOT) if IS_WIN else ROOT, ignore_errors=True)
print("\n" + "=" * 60)
print(f"Всего проверок: {passed + failed}. Провалено: {failed}. Пропущено: {skipped}.")
if failed:
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ АУДИТА ПРОЙДЕНЫ" + (" (с пропусками — см. [SKIP])" if skipped else ""))
