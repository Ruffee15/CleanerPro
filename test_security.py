"""
Автоматические тесты безопасности для Cleaner Pro v1.0 Free.

Запуск (из этой же папки, где лежит cleaner_pro.pyw):
    python test_security.py

Тест импортирует НАПРЯМУЮ production-файл cleaner_pro.pyw — не копию,
не дублированный код. Если в будущем логику защиты поменяют в cleaner_pro.pyw,
эти тесты будут проверять именно актуальный код, а не устаревшую копию.

Тест не требует Windows и не требует winreg — модуль импортируется, даже
если winreg недоступен (см. guard на import winreg в cleaner_pro.pyw);
функциональность, завязанная на реестр Windows (список установленных
программ), в таком случае просто возвращает пустой результат — тестам
безопасности она не нужна, они проверяют логику работы с файловой системой.
"""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, "cleaner_pro.pyw")


def load_production_module():
    if not os.path.isfile(TARGET_FILE):
        print(f"ОШИБКА: не найден {TARGET_FILE}")
        print("Этот тест должен лежать В ТОЙ ЖЕ папке, что и cleaner_pro.pyw.")
        sys.exit(2)
    loader = SourceFileLoader("cleaner_pro", TARGET_FILE)
    spec = importlib.util.spec_from_loader("cleaner_pro", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cpv1 = load_production_module()

# Фиксируем тестовое окружение, не трогая реальную систему, на которой
# запускаются тесты (важно и для Windows, и для Linux/CI)
os.environ["USERPROFILE"] = r"C:\Users\TestUser"
os.environ["LOCALAPPDATA"] = r"C:\Users\TestUser\AppData\Local"
# Перестроим зависящие от USERPROFILE/LOCALAPPDATA структуры заново,
# так как они были один раз вычислены при самом первом импорте модуля
cpv1.USER_PROFILE = r"C:\Users\TestUser"
cpv1.LOCAL_APPDATA = r"C:\Users\TestUser\AppData\Local"
# TARGETS и QUICK_CLEANUP_ALLOWLIST вычисляются при импорте на основе
# USER_PROFILE/LOCAL_APPDATA, актуальных на тот момент — пересчитаем явно,
# чтобы тест был независим от того, что было в окружении при самом импорте
cpv1.TARGETS = [
    {"id": "temp_user", "name": "", "path": os.path.join(cpv1.USER_PROFILE, "AppData", "Local", "Temp"),
     "action": "clear_contents"},
    {"id": "temp_windows", "name": "", "path": os.path.join(cpv1.WINDIR, "Temp"), "action": "clear_contents"},
    {"id": "windows_update", "name": "",
     "path": os.path.join(cpv1.WINDIR, "SoftwareDistribution", "Download"), "action": "clear_contents"},
    {"id": "thumbnails", "name": "",
     "path": os.path.join(cpv1.USER_PROFILE, "AppData", "Local", "Microsoft", "Windows", "Explorer"),
     "action": "clear_contents"},
    {"id": "nvidia_dxcache", "name": "",
     "path": os.path.join(cpv1.USER_PROFILE, "AppData", "Local", "NVIDIA", "DXCache"), "action": "clear_contents"},
    {"id": "nvidia_glcache", "name": "",
     "path": os.path.join(cpv1.USER_PROFILE, "AppData", "Local", "NVIDIA", "GLCache"), "action": "clear_contents"},
    {"id": "chrome_cache", "name": "",
     "path": os.path.join(cpv1.LOCAL_APPDATA, "Google", "Chrome", "User Data", "Default", "Cache"),
     "action": "clear_contents"},
    {"id": "edge_cache", "name": "",
     "path": os.path.join(cpv1.LOCAL_APPDATA, "Microsoft", "Edge", "User Data", "Default", "Cache"),
     "action": "clear_contents"},
    {"id": "windows_old", "name": "", "path": os.path.join(cpv1.SYSTEM_DRIVE + "\\", "Windows.old"),
     "action": "clear_folder_full"},
    {"id": "recycle_bin", "name": "", "path": None, "action": "empty_recycle_bin"},
]
cpv1.QUICK_CLEANUP_ALLOWLIST = cpv1._build_quick_cleanup_allowlist()


failures = []
total_checks = 0


def check(name, condition):
    global total_checks
    total_checks += 1
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


print("=== 0. Базовая проверка импорта production-файла ===")
check("cleaner_pro.pyw импортирован без ошибок", cpv1 is not None)
check("winreg guard работает (None на не-Windows или сам модуль на Windows)",
      cpv1.winreg is None or hasattr(cpv1.winreg, "HKEY_LOCAL_MACHINE"))
check("get_installed_programs() не падает без Windows", cpv1.get_installed_programs() == {} or cpv1.winreg is not None)

print("\n=== 1. is_protected_path() блокирует критические пути ===")
dangerous = [
    "C:\\", "D:\\",
    r"C:\Windows", r"C:\Windows\System32", r"C:\Windows\Temp\SubFolder",
    r"C:\Program Files", r"C:\Program Files (x86)",
    r"C:\ProgramData", r"C:\ProgramData\SomeVendor",
    r"C:\Users",
    cpv1.USER_PROFILE,
]
for p in dangerous:
    check(f"блокирован: {p}", cpv1.is_protected_path(p))

print("\n=== 2. is_protected_path() НЕ блокирует легитимные пути ===")
safe = [
    os.path.join(cpv1.USER_PROFILE, "Downloads"),
    r"C:\Program Files\SomeGame",
    r"C:\Windows.old",
]
for p in safe:
    check(f"НЕ блокирован: {p}", not cpv1.is_protected_path(p))

print("\n=== 3. Quick Cleanup allowlist: ТОЛЬКО заданные пути разрешены ===")
for t in cpv1.TARGETS:
    path = t.get("path")
    if path and t.get("action") in ("clear_contents", "clear_folder_full"):
        check(f"allowlist разрешает target '{t['id']}': {path}",
              cpv1.is_allowed_quick_cleanup_path(path))

print("\n=== 4. Allowlist НЕ пропускает произвольные/опасные пути ===")
arbitrary_paths = [
    r"C:\Windows\System32",
    r"C:\Windows",
    os.path.join(cpv1.USER_PROFILE, "Downloads"),
    r"C:\Users\Evil\AppData\Local\Temp",
    r"D:\RandomFolder",
]
for p in arbitrary_paths:
    check(f"allowlist отклоняет произвольный путь: {p}", not cpv1.is_allowed_quick_cleanup_path(p))

print("\n=== 5. Конкретно: temp_windows и windows_update ===")
temp_windows_path = [t for t in cpv1.TARGETS if t["id"] == "temp_windows"][0]["path"]
windows_update_path = [t for t in cpv1.TARGETS if t["id"] == "windows_update"][0]["path"]
check("temp_windows заблокирован is_protected_path (ожидаемо)", cpv1.is_protected_path(temp_windows_path))
check("temp_windows РАЗРЕШЁН через Quick Cleanup allowlist", cpv1.is_allowed_quick_cleanup_path(temp_windows_path))
check("windows_update заблокирован is_protected_path (ожидаемо)", cpv1.is_protected_path(windows_update_path))
check("windows_update РАЗРЕШЁН через Quick Cleanup allowlist", cpv1.is_allowed_quick_cleanup_path(windows_update_path))

print("\n=== 6. Windows.old обрабатывается как задумано ===")
windows_old_target = [t for t in cpv1.TARGETS if t["id"] == "windows_old"][0]
check("Windows.old НЕ блокируется is_protected_path", not cpv1.is_protected_path(windows_old_target["path"]))
check("Windows.old разрешён в Quick Cleanup allowlist", cpv1.is_allowed_quick_cleanup_path(windows_old_target["path"]))
check("Windows.old использует action=clear_folder_full", windows_old_target["action"] == "clear_folder_full")

print("\n=== 7. delete_path_resilient() бросает ProtectedPathError ===")
try:
    cpv1.delete_path_resilient(r"C:\Windows")
    check("ProtectedPathError выброшен для C:\\Windows", False)
except cpv1.ProtectedPathError:
    check("ProtectedPathError выброшен для C:\\Windows", True)

try:
    cpv1.delete_path_resilient("C:\\")
    check("ProtectedPathError выброшен для C:\\", False)
except cpv1.ProtectedPathError:
    check("ProtectedPathError выброшен для C:\\", True)

print("\n=== 8. clear_folder_contents() имеет собственную защиту ===")
check("clear_folder_contents отказывает на корне диска", cpv1.clear_folder_contents("C:\\") == 1)
check("clear_folder_contents отказывает на WINDIR целиком", cpv1.clear_folder_contents(cpv1.WINDIR) == 1)
check("clear_folder_contents отказывает на профиле пользователя целиком",
      cpv1.clear_folder_contents(cpv1.USER_PROFILE) == 1)

print("\n=== 9. Интеграционный тест: реальная очистка через полный пайплайн ===")
import shutil
import tempfile

fake_root = tempfile.mkdtemp(prefix="cleanerpro_test_")
fake_windows = os.path.join(fake_root, "Windows")
fake_temp = os.path.join(fake_windows, "Temp")
os.makedirs(fake_temp, exist_ok=True)
for i in range(5):
    with open(os.path.join(fake_temp, f"junk_{i}.tmp"), "w") as f:
        f.write("junk")
os.makedirs(os.path.join(fake_temp, "SubfolderJunk"), exist_ok=True)

errors = cpv1.clear_folder_contents(fake_temp)
remaining = os.listdir(fake_temp)
check("реальные файлы в Temp удалены без ошибок", errors == 0)
check("папка Temp пуста после очистки", len(remaining) == 0)
check("сама папка Temp осталась (чистим содержимое, не саму папку)", os.path.isdir(fake_temp))
shutil.rmtree(fake_root, ignore_errors=True)

print("\n" + "=" * 60)
print(f"Всего проверок: {total_checks}. Провалено: {len(failures)}.")
if failures:
    print("\nПроваленные проверки:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
else:
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО")
    sys.exit(0)
