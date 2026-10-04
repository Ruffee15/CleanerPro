"""
Cleaner Pro v1.0 Free — анализ диска, удаление программ и безопасная очистка.

Вкладки:
1. «Диск» — проваливаешься по папкам, видишь размеры, удаляешь ненужное.
2. «Программы» — установленные программы и игровые папки по размеру.
3. «Быстрая очистка» — временные файлы и кэш из заранее заданного списка.

Архитектура потоков: вся тяжёлая работа идёт в фоновых потоках, но
ни один фоновый поток НИКОГДА не трогает виджеты Tk напрямую — результаты
передаются в главный поток через очередь (CleanerProApp.ui_call).

Запуск: двойной клик по .pyw (нужен Python) или соберите .exe (build.bat).
"""

import ntpath
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
try:
    import winreg
except ImportError:
    # winreg есть только на Windows. Программа работает только на Windows,
    # но этот guard позволяет безопасно ИМПОРТИРОВАТЬ файл в тестах.
    winreg = None
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

APP_NAME = "Cleaner Pro"
APP_VERSION = "1.0"
APP_EDITION = "Free"

USER_PROFILE = os.environ.get("USERPROFILE", r"C:\Users")
LOCAL_APPDATA = os.environ.get("LOCALAPPDATA", "")
SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:")  # диск, на котором реально стоит Windows
WINDIR = os.environ.get("SystemRoot", os.path.join(SYSTEM_DRIVE, "Windows"))

IS_WINDOWS = os.name == "nt"

# Сколько параллельных подсчётов размера допускается ВСЕГО. Разделено на
# интерактивный пул (вкладка «Диск» — то, что пользователь ждёт прямо сейчас)
# и фоновый (Программы / Быстрая очистка), чтобы фоновое сканирование не
# занимало все потоки и не забивало HDD бесполезными параллельными чтениями.
MAX_WORKERS = 6
DISK_WORKERS = 4
BACKGROUND_WORKERS = 2


# ---------- Диски ----------

DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = 2, 3, 4, 5, 6


def get_available_drives():
    """Список дисков (C:\\, D:\\ …). На Windows — через GetLogicalDrives():
    это битовая маска из ядра, без обращения к самим дискам, поэтому
    отключённый сетевой диск или пустой картридер не подвешивают запуск
    и не вызывают системное окно «Нет диска»."""
    if not IS_WINDOWS:
        return []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]
    except Exception:
        return [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]


def get_drive_type(drive):
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive))
    except Exception:
        return None


def _suppress_drive_error_dialogs():
    """Без этого обращение к пустому картридеру/дисководу показывает
    системное модальное окно «Вставьте диск»."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)  # SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX
    except Exception:
        pass


def _enable_dpi_awareness():
    """Без этого Windows растягивает окно Tk картинкой на 125–200% и всё
    выглядит мыльным. System-DPI-aware (1), а не per-monitor (2): Tk 8.6 не
    обрабатывает смену DPI при переносе окна между мониторами."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# Папки-библиотеки игровых лаунчеров: то, что лежит НЕПОСРЕДСТВЕННО внутри них,
# это уже сами игры (не нужно проваливаться дальше — там только служебные файлы движка)
GAME_LIBRARY_FOLDER_NAMES = {
    "common", "epic games", "gog games", "gog galaxy games", "riot games",
    "battle.net", "origin games", "ea games", "ubisoft game launcher", "games",
}


def is_game_library_folder(path):
    """True, если это папка типа steamapps\\common — то, что внутри неё, уже сами игры."""
    return os.path.basename(path.rstrip("\\/")).lower() in GAME_LIBRARY_FOLDER_NAMES


# ---------- Общие утилиты ----------

def format_size(num_bytes):
    """1536 -> «1,5 КБ». Байты — целым числом («0 Б», а не «0.0 Б»),
    десятичная запятая — как в русской Windows."""
    if num_bytes is None:
        return "..."
    n = float(num_bytes)
    if n < 1024:
        return f"{int(n)} Б"
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        n /= 1024
        if n < 1023.95:
            return f"{n:.1f} {unit}".replace(".", ",")
    return f"{n / 1024:.1f} ПБ".replace(".", ",")


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _fs(path):
    """Путь для системного вызова. На Windows для путей длиннее ~240 символов
    добавляет префикс \\\\?\\ — иначе без включённых LongPaths любые операции
    с такими путями падают. Логические пути (в кэше, в UI) остаются без
    префикса: дочерние пути всегда строятся через os.path.join(path, name)."""
    if not IS_WINDOWS or not path or path.startswith("\\\\?\\"):
        return path
    p = os.path.abspath(path)
    if len(p) < 240:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _parent_path(path, pathmod=os.path):
    """Родительская папка или None, если мы уже в корне диска.
    (Раньше на C:\\ «Выйти из папки» уводило в «C:» — текущий каталог диска.)"""
    if not path:
        return None
    norm = pathmod.normpath(path)
    parent = pathmod.dirname(norm)
    if not parent or parent == norm:
        return None
    return parent


# ---------- Ссылки: symlink / junction ----------
# NTFS junction (точка подключения) на Windows НЕ считается symlink'ом в
# os.path.islink()/DirEntry.is_symlink() до Python 3.12. Поэтому ссылки
# определяются ещё и по тегу reparse point. ВАЖНО: проверяем именно теги
# symlink/mount point, а не любой reparse point — файлы и папки OneDrive
# «по требованию» тоже являются reparse point'ами, но это обычные данные.

_LINK_REPARSE_TAGS = {0xA0000003, 0xA000000C}  # IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK


def _is_link_tag(tag):
    """Ссылка на другое имя: symlink, junction и любой другой reparse point
    с битом «name surrogate» (WSL-symlink, …). Файлы OneDrive «по запросу»
    и дедупликация этого бита не имеют — это обычные данные."""
    return bool(tag) and (tag in _LINK_REPARSE_TAGS or bool(tag & 0x20000000))


def _is_link_entry(entry):
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            pass
    if IS_WINDOWS:
        try:
            st = entry.stat(follow_symlinks=False)
            if _is_link_tag(getattr(st, "st_reparse_tag", 0)):
                return True
        except OSError:
            pass
    return False


def _is_link_path(path):
    try:
        st = os.lstat(_fs(path))
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return IS_WINDOWS and _is_link_tag(getattr(st, "st_reparse_tag", 0))


# ---------- Кэш размеров папок (в рамках текущего запуска программы) ----------
# Ключ — нормализованный путь, значение — размер в байтах. Заполняется на
# КАЖДОМ уровне рекурсии get_size(), поэтому вкладки переиспользуют
# посчитанное друг у друга.
#
# _CACHE_GEN — «поколение» кэша. Каждая инвалидация увеличивает его.
# get_size() запоминает поколение при старте и пишет в кэш ТОЛЬКО если оно
# не изменилось — так подсчёт, начатый ДО удаления, не может записать
# устаревший размер ПОСЛЕ invalidate_size_cache().
_SIZE_CACHE = {}
_SIZE_CACHE_LOCK = threading.Lock()
_CACHE_GEN = 0


def _cache_key(path):
    try:
        if path.startswith("\\\\?\\"):
            path = path[4:]
        return ntpath.normcase(ntpath.normpath(path))
    except Exception:
        return path


def _cache_get(path):
    with _SIZE_CACHE_LOCK:
        return _SIZE_CACHE.get(_cache_key(path))


def invalidate_size_cache(path):
    """Вызывать после любого удаления/очистки: убирает сам путь, всё, что было
    закэшировано ВНУТРИ него, и кэш всех предков (их сумма устарела)."""
    global _CACHE_GEN
    key = _cache_key(path)
    with _SIZE_CACHE_LOCK:
        _CACHE_GEN += 1
        to_remove = [k for k in _SIZE_CACHE if k == key or k.startswith(key + ntpath.sep)]
        for k in to_remove:
            del _SIZE_CACHE[k]
        cur = key
        while True:
            parent = ntpath.dirname(cur)
            if not parent or parent == cur:
                break
            _SIZE_CACHE.pop(parent, None)
            cur = parent


def clear_size_cache():
    """Полный сброс кэша."""
    global _CACHE_GEN
    with _SIZE_CACHE_LOCK:
        _CACHE_GEN += 1
        _SIZE_CACHE.clear()


def _dir_node_id(entry, root_dev):
    """(устройство, inode) папки для защиты от зацикливания.

    ВАЖНО: на Windows os.DirEntry.stat() ВСЕГДА возвращает st_ino = 0 и
    st_dev = 0 (так задокументировано в Python). Если использовать их
    напрямую, у всех подпапок один и тот же id (0, 0): первая подпапка
    считается, а все остальные пропускаются как «уже посещённые» — размеры
    папок на Windows получались сильно заниженными. Поэтому нулевые значения
    считаются «неизвестными»: inode берётся через entry.inode() (на Windows
    это настоящий индекс файла NTFS), устройство — от корня подсчёта
    (через ссылки и точки монтирования подсчёт не переходит, так что том
    внутри одного подсчёта не меняется). Если и так inode неизвестен
    (некоторые сетевые/FAT-тома) — возвращает None: такую папку просто не
    дедуплицируем; зациклиться без ссылок нельзя, а глубина ограничена."""
    try:
        st = entry.stat(follow_symlinks=False)
        dev, ino = st.st_dev, st.st_ino
    except OSError:
        dev, ino = 0, 0
    if not ino:
        try:
            ino = entry.inode()
        except OSError:
            ino = 0
    if not ino:
        return None
    return (dev or root_dev, ino)


def get_size(path, _visited=None, _depth=0, cancel_check=None, use_cache=True,
             force_refresh=False, _gen=None, _root_dev=0):
    """Размер папки (или файла) в байтах.

    - Защита от зацикливания через (устройство, inode) — см. _dir_node_id().
    - Вложенные symlink'и и junction'ы НЕ учитываются (как в свойствах папки
      в Проводнике), иначе файлы цели считались бы дважды.
    - cancel_check() -> True прерывает подсчёт. Прерванный (частичный)
      результат в кэш НЕ записывается — иначе при возврате в папку был бы
      показан заниженный размер.
    - force_refresh=True — не читать кэш (кнопка «Обновить»), но записать
      свежий результат.
    """
    if _visited is None:
        _visited = set()
        with _SIZE_CACHE_LOCK:
            _gen = _CACHE_GEN
        try:
            if os.path.isfile(_fs(path)):
                return os.path.getsize(_fs(path))
        except OSError:
            return 0
        try:
            st = os.stat(_fs(path), follow_symlinks=False)  # os.stat даёт настоящие значения и на Windows
            _root_dev = st.st_dev
            if st.st_ino:
                _visited.add((st.st_dev, st.st_ino))
        except OSError:
            pass

    if cancel_check is not None and cancel_check():
        return 0

    if _depth > 60:
        return 0

    if use_cache and not force_refresh:
        with _SIZE_CACHE_LOCK:
            cached = _SIZE_CACHE.get(_cache_key(path))
        if cached is not None:
            return cached

    total = 0
    cancelled = False
    try:
        with os.scandir(_fs(path)) as it:
            for entry in it:
                try:
                    if _is_link_entry(entry):
                        continue
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        node_id = _dir_node_id(entry, _root_dev)
                        if node_id is not None:
                            if node_id in _visited:
                                continue
                            _visited.add(node_id)
                        total += get_size(os.path.join(path, entry.name), _visited, _depth + 1,
                                          cancel_check, use_cache, force_refresh, _gen, _root_dev)
                        if cancel_check is not None and cancel_check():
                            cancelled = True
                            break
                except OSError:
                    continue
    except OSError:
        pass

    if cancelled or (cancel_check is not None and cancel_check()):
        return total  # частичный результат — НЕ кэшируем

    if use_cache:
        with _SIZE_CACHE_LOCK:
            if _gen is None or _gen == _CACHE_GEN:
                _SIZE_CACHE[_cache_key(path)] = total

    return total


def _list_directory(path):
    """Содержимое одной папки для вкладки «Диск»: (имя, путь, тип, размер).
    Размер файлов берётся сразу (stat уже есть после scandir), папки
    считаются отдельно. Бросает OSError, если саму папку открыть нельзя."""
    out = []
    with os.scandir(_fs(path)) as it:
        for entry in it:
            child = os.path.join(path, entry.name)
            try:
                if _is_link_entry(entry):
                    kind, size = "link", None
                elif entry.is_dir(follow_symlinks=False):
                    kind, size = "dir", None
                else:
                    kind, size = "file", entry.stat(follow_symlinks=False).st_size
            except OSError:
                kind, size = "file", None
            out.append((entry.name, child, kind, size))
    return out


def get_recycle_bin_size():
    if not IS_WINDOWS:
        return None
    try:
        import ctypes

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong),
                        ("i64NumItems", ctypes.c_longlong)]

        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
            return None
        return int(info.i64Size)
    except Exception:
        return None


# ---------- Защита от опасного удаления ----------
# Эти папки на корне диска блокируются ПОЛНОСТЬЮ, вместе со всем содержимым.
FULLY_BLOCKED_SUBTREE_NAMES = {
    "windows", "programdata", "system volume information", "$recycle.bin",
    "recovery", "perflogs", "boot", "msocache", "config.msi",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
}

# А эти — только КАК КОНТЕЙНЕР ЦЕЛИКОМ; то, что лежит ВНУТРИ, удалять можно.
BLOCKED_CONTAINER_ONLY_NAMES = {
    "program files", "program files (x86)",
}


class ProtectedPathError(Exception):
    """Попытка удалить защищённую системную папку."""
    def __init__(self, path):
        self.path = path
        super().__init__(f"Удаление защищённого пути запрещено: {path}")


class RedirectedPathError(Exception):
    """Папка «Быстрой очистки» на самом деле находится не там, где задано в
    списке категорий (путь перенаправлен ссылкой). Очистка отменена."""
    def __init__(self, path, real=None):
        self.path, self.real = path, real
        super().__init__(f"Путь перенаправлен: {path} → {real or 'неизвестно'}")


class PathChangedError(Exception):
    """Объект по этому пути изменился после подтверждения (подменён,
    пересоздан или стал ссылкой) — удаление отменено."""
    def __init__(self, path):
        self.path = path
        super().__init__(f"Объект изменился после подтверждения: {path}")


def _is_drive_root(norm_path):
    return bool(re.fullmatch(r"[A-Za-z]:\\?", norm_path))


def _canonical_path(path):
    """Каноническая форма абсолютного пути Windows для сравнения в защите,
    или None, если путь не поддерживается (тогда он считается защищённым).

    - «/» → «\\», «..» схлопывается, регистр не важен;
    - \\\\?\\C:\\…, \\\\.\\C:\\… и \\\\?\\UNC\\… сводятся к обычной форме;
      прочие пространства имён устройств (\\\\?\\Volume{…}, \\\\.\\PhysicalDrive0,
      GLOBALROOT) не поддерживаются;
    - относительные пути, «C:папка» (относительно текущей папки диска),
      пустые значения и альтернативные потоки NTFS («имя:поток») отклоняются;
    - точки и пробелы в конце имени отбрасываются — Windows считает
      «C:\\Windows.» и «C:\\Windows» одним и тем же путём."""
    if not isinstance(path, str):
        return None
    p = path.strip().replace("/", "\\")
    if not p or "\x00" in p:
        return None
    up = p.upper()
    if up.startswith("\\\\?\\UNC\\") or up.startswith("\\\\.\\UNC\\"):
        p = "\\\\" + p[8:]
    elif up.startswith("\\\\?\\") or up.startswith("\\\\.\\") or up.startswith("\\??\\"):
        rest = p[4:]
        if not re.match(r"[A-Za-z]:(\\|$)", rest):
            return None
        p = rest
    try:
        p = ntpath.normpath(p)
    except Exception:
        return None
    drive, rest = ntpath.splitdrive(p)
    if re.fullmatch(r"[A-Za-z]:", drive):
        if not rest.startswith("\\"):
            return None
    elif drive.startswith("\\\\"):
        server_share = [x for x in drive[2:].split("\\") if x]
        if len(server_share) != 2 or server_share[0] in (".", "?"):
            return None
    else:
        return None
    parts = []
    for comp in rest.split("\\"):
        if not comp:
            continue
        if ":" in comp:
            return None
        comp = comp.rstrip(". ")
        if not comp:
            return None
        parts.append(comp)
    return ntpath.normcase("\\".join([drive.rstrip("\\")] + parts) if parts else drive.rstrip("\\") + "\\")


def _canon_set(paths):
    out = set()
    for p in paths:
        c = _canonical_path(p) if p else None
        if c:
            out.add(c.rstrip("\\") if not _is_drive_root(c) else c)
    return out


def _windows_dirs_from_api():
    """Папка Windows по данным самой системы (не только из переменных
    окружения, которые можно подменить при запуске)."""
    dirs = []
    if not IS_WINDOWS:
        return dirs
    try:
        import ctypes
        for fn in ("GetWindowsDirectoryW", "GetSystemWindowsDirectoryW"):
            buf = ctypes.create_unicode_buffer(520)
            if getattr(ctypes.windll.kernel32, fn)(buf, 520):
                dirs.append(buf.value)
    except Exception:
        pass
    return dirs


def _profiles_directory():
    """Папка с профилями всех пользователей (обычно C:\\Users, но её можно
    перенести). Берётся из реестра ProfileList."""
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList")
        try:
            value, _ = winreg.QueryValueEx(key, "ProfilesDirectory")
        finally:
            winreg.CloseKey(key)
        return os.path.expandvars(value) if isinstance(value, str) else None
    except OSError:
        return None


# Неизменные на время работы системные корни. Вычисляются один раз.
_SYSTEM_SUBTREES = _canon_set([WINDIR, os.environ.get("windir")] + _windows_dirs_from_api()
                              + [os.environ.get("ProgramData"), os.environ.get("ALLUSERSPROFILE")])
_PROFILES_ROOTS = _canon_set([_profiles_directory(), os.path.join(SYSTEM_DRIVE + "\\", "Users")])
_SYSTEM_CONTAINERS = _canon_set([
    os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramW6432"),
    os.environ.get("CommonProgramFiles"), os.environ.get("CommonProgramFiles(x86)"),
    os.environ.get("CommonProgramW6432"), os.environ.get("PUBLIC"),
]) | _PROFILES_ROOTS


def _profile_containers():
    """Профиль текущего пользователя и его служебные контейнеры AppData.
    Считаются при каждой проверке: USER_PROFILE может меняться (тесты)."""
    if not USER_PROFILE:
        return set()
    return _canon_set([USER_PROFILE] + [os.path.join(USER_PROFILE, *sub) for sub in (
        ("AppData",), ("AppData", "Local"), ("AppData", "LocalLow"), ("AppData", "Roaming"))])


def _is_under(path, root):
    return path == root or path.startswith(root + "\\")


def is_protected_path(path):
    """True, если путь нельзя удалять через эту программу:
    - неподдерживаемый, относительный или пустой путь;
    - корень диска или сетевого ресурса (\\\\сервер\\папка);
    - папка Windows и ProgramData со всем содержимым — по имени на любом
      диске и по фактическому расположению (Windows на D:, нестандартное имя);
    - Program Files, Program Files (x86), Common Files, Users (и перенесённая
      папка профилей), профиль любого пользователя, AppData текущего
      профиля — только сами контейнеры, содержимое удалять можно;
    - любая папка, ВНУТРИ которой лежит что-то из перечисленного
      (удаление предка удалило бы и системную папку).
    Использует только строки (ntpath) — для реального расположения объекта
    см. is_protected_for_delete() и delete_path_resilient()."""
    norm = _canonical_path(path)
    if norm is None:
        return True
    if _is_drive_root(norm):
        return True
    drive, rest = ntpath.splitdrive(norm)
    rest_parts = [p for p in rest.split("\\") if p]
    if not rest_parts:
        return True          # \\сервер\папка — корень сетевого ресурса

    first = rest_parts[0]
    if first in FULLY_BLOCKED_SUBTREE_NAMES:
        return True
    if first in BLOCKED_CONTAINER_ONLY_NAMES and len(rest_parts) == 1:
        return True
    if len(rest_parts) == 1 and first == "users":
        return True

    for root in _SYSTEM_SUBTREES:
        if _is_under(norm, root):
            return True
    containers = _SYSTEM_CONTAINERS | _PROFILES_ROOTS | _profile_containers()
    for root in containers | _SYSTEM_SUBTREES:
        if _is_under(root, norm):          # norm — сам контейнер или его предок
            return True
    parent = ntpath.dirname(norm)
    if parent in _PROFILES_ROOTS:          # профиль любого пользователя целиком
        return True
    return False


# ---------- Win32: операции через дескриптор (защита от подмены пути) ----------
# Проверка «путь не ссылка» и последующее удаление по ТОЙ ЖЕ строке пути —
# это гонка (TOCTOU): между проверкой и удалением папку можно заменить на
# junction, и удаление уйдёт в его цель. Поэтому на Windows удаление идёт
# через дескрипторы:
#  * каждый объект открывается с FILE_FLAG_OPEN_REPARSE_POINT (ссылка
#    открывается сама, а не её цель) и проверяется по ОТКРЫТОМУ объекту;
#  * папки открываются без FILE_SHARE_DELETE: пока дескриптор открыт, папку
#    (и любую папку выше неё) нельзя переименовать, удалить или заменить
#    ссылкой — путь до неё «закреплён»;
#  * удаление выполняется через тот же дескриптор (FileDispositionInfo),
#    а не повторным открытием по имени;
#  * реальное расположение берётся из дескриптора (GetFinalPathNameByHandle)
#    и снова проверяется защитой.

_NAME_SURROGATE_BIT = 0x20000000   # IsReparseTagNameSurrogate: ссылка на другое имя

_W_DELETE = 0x00010000
_W_FILE_LIST_DIRECTORY = 0x0001
_W_FILE_READ_ATTRIBUTES = 0x0080
_W_FILE_WRITE_ATTRIBUTES = 0x0100
_W_SYNCHRONIZE = 0x00100000
_W_SHARE_RW = 0x1 | 0x2
_W_SHARE_ALL = 0x1 | 0x2 | 0x4
_W_OPEN_EXISTING = 3
_W_FLAGS_NOFOLLOW = 0x02000000 | 0x00200000   # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
_W_ATTR_READONLY = 0x01
_W_ATTR_DIRECTORY = 0x10
_W_ATTR_REPARSE = 0x400
_W_ATTR_NORMAL = 0x80

_win = None


def _win32():
    """Ленивая загрузка ctypes-обвязки (на не-Windows — None)."""
    global _win
    if _win is not None or not IS_WINDOWS:
        return _win or None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.LPVOID]

    class AttrTag(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class Basic(ctypes.Structure):
        _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                    ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                    ("FileAttributes", wintypes.DWORD)]

    class Disp(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    class DispEx(ctypes.Structure):
        _fields_ = [("Flags", wintypes.ULONG)]

    class ByHandle(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]

    _win = {"ct": ctypes, "k32": k32, "AttrTag": AttrTag, "Basic": Basic, "Disp": Disp,
            "DispEx": DispEx, "ByHandle": ByHandle, "invalid": wintypes.HANDLE(-1).value}
    return _win


def _ext_path(path):
    """Точный путь для CreateFileW с префиксом \\\\?\\ — без нормализации Win32
    (которая молча отрезает точки/пробелы в конце имени и открыла бы ДРУГОЙ
    объект). Принимает только абсолютные пути диска или UNC."""
    p = path.replace("/", "\\")
    if p.startswith("\\\\?\\"):
        return p
    p = ntpath.normpath(p)
    if re.match(r"[A-Za-z]:\\", p):
        return "\\\\?\\" + p
    if p.startswith("\\\\") and not p.startswith("\\\\.\\"):
        return "\\\\?\\UNC\\" + p[2:]
    raise ValueError(f"неподдерживаемый путь: {path}")


class _Handle:
    def __init__(self, h):
        self.h = h

    def close(self):
        if self.h:
            _win32()["k32"].CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _w_error():
    w = _win32()
    err = w["ct"].get_last_error()
    e = w["ct"].WinError(err)
    return e


def _w_open(ext_path, access, share=_W_SHARE_RW):
    """Открыть объект БЕЗ следования по ссылке. Папка, открытая без
    FILE_SHARE_DELETE, закреплена, пока открыт дескриптор."""
    w = _win32()
    h = w["k32"].CreateFileW(ext_path, access, share, None, _W_OPEN_EXISTING, _W_FLAGS_NOFOLLOW, None)
    if h is None or h == w["invalid"]:
        raise _w_error()
    return _Handle(h)


def _w_attr_tag(handle):
    w = _win32()
    info = w["AttrTag"]()
    if not w["k32"].GetFileInformationByHandleEx(handle.h, 9, w["ct"].byref(info), w["ct"].sizeof(info)):
        raise _w_error()
    return info.FileAttributes, info.ReparseTag


def _w_identity(handle):
    """(серийный номер тома, индекс файла) — однозначно определяет объект."""
    w = _win32()
    info = w["ByHandle"]()
    if not w["k32"].GetFileInformationByHandle(handle.h, w["ct"].byref(info)):
        raise _w_error()
    return info.dwVolumeSerialNumber, (info.nFileIndexHigh << 32) | info.nFileIndexLow


def _w_final_path(handle):
    """Реальный путь открытого объекта (C:\\... или \\\\server\\share\\...) или None."""
    w = _win32()
    buf = w["ct"].create_unicode_buffer(1024)
    n = w["k32"].GetFinalPathNameByHandleW(handle.h, buf, 1024, 0)
    if n > 1024:
        buf = w["ct"].create_unicode_buffer(n + 1)
        n = w["k32"].GetFinalPathNameByHandleW(handle.h, buf, n + 1, 0)
    if not n:
        return None
    p = buf.value
    if p.upper().startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def _w_is_link(attrs, tag):
    return bool(attrs & _W_ATTR_REPARSE) and bool(tag & _NAME_SURROGATE_BIT)


def _w_delete_handle(handle, attrs):
    """Пометить ОТКРЫТЫЙ объект на удаление. Для ссылки (открытой с
    OPEN_REPARSE_POINT) удаляется сама ссылка, не цель."""
    w = _win32()
    ct, k32 = w["ct"], w["k32"]
    ex = w["DispEx"](0x1 | 0x2 | 0x10)   # DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE
    if k32.SetFileInformationByHandle(handle.h, 21, ct.byref(ex), ct.sizeof(ex)):
        return True
    # Старые Windows / FAT: обычная семантика; «только чтение» снимаем на том же объекте
    if attrs & _W_ATTR_READONLY:
        basic = w["Basic"](0, 0, 0, 0, (attrs & ~_W_ATTR_READONLY) or _W_ATTR_NORMAL)
        k32.SetFileInformationByHandle(handle.h, 0, ct.byref(basic), ct.sizeof(basic))
    disp = w["Disp"](1)
    return bool(k32.SetFileInformationByHandle(handle.h, 4, ct.byref(disp), ct.sizeof(disp)))


_DEL_ACCESS = (_W_DELETE | _W_FILE_READ_ATTRIBUTES | _W_FILE_WRITE_ATTRIBUTES
               | _W_FILE_LIST_DIRECTORY | _W_SYNCHRONIZE)

# После того как корень открыт и проверен, НИ ОДНА операция больше не
# разбирает строковый путь: дети перечисляются через дескриптор папки и
# открываются ОТНОСИТЕЛЬНО него (NtCreateFile с RootDirectory). Поэтому
# изменение любого компонента выше (переименование, подмена, перенацеливание
# существующего junction) не может увести удаление в другое место: мы просто
# больше не проходим по этой цепочке. Закрепление папок (без FILE_SHARE_DELETE)
# остаётся дополнительным рубежом, но защита на нём не основана.

_win_rel = None


def _win32_rel():
    """NtCreateFile / перечисление через дескриптор (ленивая загрузка)."""
    global _win_rel
    if _win_rel is not None:
        return _win_rel
    w = _win32()
    ct = w["ct"]
    from ctypes import wintypes

    class UNICODE_STRING(ct.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ct.c_void_p)]

    class OBJECT_ATTRIBUTES(ct.Structure):
        _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                    ("ObjectName", ct.POINTER(UNICODE_STRING)), ("Attributes", wintypes.ULONG),
                    ("SecurityDescriptor", ct.c_void_p), ("SecurityQualityOfService", ct.c_void_p)]

    class IO_STATUS_BLOCK(ct.Structure):
        _fields_ = [("Status", ct.c_void_p), ("Information", ct.c_void_p)]

    class FULL_DIR_INFO(ct.Structure):
        _fields_ = [("NextEntryOffset", wintypes.DWORD), ("FileIndex", wintypes.DWORD),
                    ("CreationTime", ct.c_longlong), ("LastAccessTime", ct.c_longlong),
                    ("LastWriteTime", ct.c_longlong), ("ChangeTime", ct.c_longlong),
                    ("EndOfFile", ct.c_longlong), ("AllocationSize", ct.c_longlong),
                    ("FileAttributes", wintypes.DWORD), ("FileNameLength", wintypes.DWORD),
                    ("EaSize", wintypes.DWORD), ("FileName", ct.c_wchar * 1)]

    nt = ct.WinDLL("ntdll")
    nt.NtCreateFile.restype = ct.c_long
    nt.NtCreateFile.argtypes = [ct.POINTER(wintypes.HANDLE), wintypes.ULONG, ct.POINTER(OBJECT_ATTRIBUTES),
                                ct.POINTER(IO_STATUS_BLOCK), ct.c_void_p, wintypes.ULONG, wintypes.ULONG,
                                wintypes.ULONG, wintypes.ULONG, ct.c_void_p, wintypes.ULONG]
    nt.RtlNtStatusToDosError.restype = wintypes.ULONG
    nt.RtlNtStatusToDosError.argtypes = [ct.c_long]
    _win_rel = {"nt": nt, "US": UNICODE_STRING, "OA": OBJECT_ATTRIBUTES, "IOSB": IO_STATUS_BLOCK,
                "FDI": FULL_DIR_INFO, "name_off": FULL_DIR_INFO.FileName.offset}
    return _win_rel


def _w_open_rel(parent, name, access, share=_W_SHARE_RW):
    """Открыть объект `name` ВНУТРИ уже открытой папки `parent` — без
    повторного разбора пути и без следования по ссылке."""
    if not name or name in (".", "..") or "\\" in name or "/" in name or "\x00" in name:
        raise OSError(f"недопустимое имя: {name!r}")
    w, r = _win32(), _win32_rel()
    ct = w["ct"]
    from ctypes import wintypes
    buf = ct.create_unicode_buffer(name)
    us = r["US"](len(name) * 2, len(name) * 2, ct.addressof(buf))
    oa = r["OA"](ct.sizeof(r["OA"]), parent.h, ct.pointer(us), 0x40, None, None)   # OBJ_CASE_INSENSITIVE
    iosb = r["IOSB"]()
    h = wintypes.HANDLE()
    # FILE_OPEN; FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_FOR_BACKUP_INTENT
    status = r["nt"].NtCreateFile(ct.byref(h), access | _W_SYNCHRONIZE, ct.byref(oa), ct.byref(iosb),
                                  None, 0, share, 1, 0x00200000 | 0x20 | 0x4000, None, 0)
    if status < 0:
        raise ct.WinError(r["nt"].RtlNtStatusToDosError(status))
    return _Handle(h.value)


def _w_list_names(dir_handle):
    """Имена в открытой папке — через сам дескриптор (без пути)."""
    w, r = _win32(), _win32_rel()
    ct, k32 = w["ct"], w["k32"]
    buf = ct.create_string_buffer(64 * 1024)
    names = []
    info_class = 15                               # FileFullDirectoryRestartInfo
    while True:
        if not k32.GetFileInformationByHandleEx(dir_handle.h, info_class, buf, len(buf)):
            err = ct.get_last_error()
            if err == 18:                         # ERROR_NO_MORE_FILES
                return names
            raise ct.WinError(err)
        info_class = 14                           # FileFullDirectoryInfo
        off = 0
        base = ct.addressof(buf)
        while True:
            info = r["FDI"].from_buffer(buf, off)
            name = ct.wstring_at(base + off + r["name_off"], info.FileNameLength // 2)
            if name not in (".", ".."):
                names.append(name)
            if not info.NextEntryOffset:
                break
            off += info.NextEntryOffset


def _w_delete_children(dir_handle, logical_dir, failed, depth):
    """Удаляет содержимое ОТКРЫТОЙ папки. Пути — только для сообщений."""
    if depth > 250:
        failed.append(logical_dir)
        return
    try:
        names = _w_list_names(dir_handle)
    except OSError:
        failed.append(logical_dir)
        return
    for name in names:
        _w_delete_entry(dir_handle, name, os.path.join(logical_dir, name), failed, depth + 1)


def _w_delete_entry(parent, name, logical, failed, depth):
    try:
        handle = _w_open_rel(parent, name, _DEL_ACCESS)
    except FileNotFoundError:
        return
    except OSError:
        failed.append(logical)
        return
    with handle:
        try:
            attrs, tag = _w_attr_tag(handle)
        except OSError:
            failed.append(logical)
            return
        before = len(failed)
        if attrs & _W_ATTR_DIRECTORY and not _w_is_link(attrs, tag):
            _w_delete_children(handle, logical, failed, depth)
            if len(failed) > before:
                return              # внутри что-то осталось — папку не удалить
        if not _w_delete_handle(handle, attrs):
            failed.append(logical)


def _w_long_path(path):
    """Длинная форма пути (8.3-имена раскрыты) — БЕЗ разрешения ссылок."""
    try:
        w = _win32()
        ct = w["ct"]
        ext = _ext_path(path)
        n = ct.windll.kernel32.GetLongPathNameW(ext, None, 0)
        if not n:
            return None
        buf = ct.create_unicode_buffer(n + 1)
        if not ct.windll.kernel32.GetLongPathNameW(ext, buf, n + 1):
            return None
        out = buf.value
        if out.upper().startswith("\\\\?\\UNC\\"):
            return "\\\\" + out[8:]
        return out[4:] if out.startswith("\\\\?\\") else out
    except (OSError, ValueError, AttributeError):
        return None


def _w_same_location(final, logical):
    """Реальный путь открытого объекта совпадает с запрошенным: ни один
    компонент пути (включая родителей) не перенаправлен ссылкой,
    подключённым томом или SUBST."""
    want = _canonical_path(_w_long_path(logical) or logical)
    got = _canonical_path(final) if final else None
    return want is not None and want == got


def _allowlist_canonical():
    return {_canonical_path(p) for p in QUICK_CLEANUP_ALLOWLIST if _canonical_path(p)}


def is_protected_for_delete(path):
    """Проверка перед удалением с вкладки «Диск»: сам путь И его реальное
    расположение. Закрывает обход защиты через ссылку: junction
    «Downloads\\win» → C:\\Windows, внутри него «System32» — логический путь
    выглядит безопасным, а реальный указывает в Windows.
    Если удаляется САМА ссылка — достаточно проверить её путь: цель не
    затрагивается (см. delete_path_resilient).
    ВАЖНО: это проверка по строке пути, она не защищает от подмены папки
    между проверкой и удалением. От этого защищает delete_path_resilient()
    на Windows (работа через дескрипторы)."""
    if is_protected_path(path):
        return True
    if _is_link_path(path):
        return False
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return True
    return is_protected_path(real)


def path_identity(path):
    """Отпечаток объекта для сверки «подтвердили то же, что удаляем»:
    (том, индекс файла, это_ссылка, реальный_путь) или None.
    Берётся из открытого без следования по ссылке объекта."""
    if not IS_WINDOWS or _win32() is None:
        return None
    try:
        with _w_open(_ext_path(path), _W_FILE_READ_ATTRIBUTES | _W_SYNCHRONIZE, _W_SHARE_ALL) as h:
            attrs, tag = _w_attr_tag(h)
            vol, index = _w_identity(h)
            link = _w_is_link(attrs, tag)
            return (vol, index, link, None if link else _w_final_path(h))
    except (OSError, ValueError):
        return None


def _remove_link(path):
    """Удаляет саму ссылку (symlink/junction), не трогая то, на что она указывает."""
    try:
        os.unlink(_fs(path))
        return True
    except OSError:
        pass
    try:
        os.rmdir(_fs(path))
        return True
    except OSError:
        return False


def _remove_file(path):
    try:
        os.remove(_fs(path))
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        # Частая причина «Удалено не всё» — атрибут «Только чтение».
        # Ссылку не трогаем: chmod пошёл бы в её цель.
        if _is_link_path(path):
            return False
        try:
            os.chmod(_fs(path), stat.S_IWRITE)
            os.remove(_fs(path))
            return True
        except OSError:
            return False
    except OSError:
        return False


def _rmtree_no_follow(path, failed, _depth=0):
    """Рекурсивное удаление содержимого БЕЗ захода внутрь ссылок.
    Используется только не на Windows (там — _w_delete_children: по строке
    пути нельзя исключить подмену папки ссылкой между проверкой и удалением)."""
    if _depth > 250:
        failed.append(path)
        return
    try:
        with os.scandir(_fs(path)) as it:
            entries = list(it)
    except FileNotFoundError:
        return
    except OSError:
        failed.append(path)
        return
    for entry in entries:
        child = os.path.join(path, entry.name)
        if _is_link_entry(entry):
            if not _remove_link(child):
                failed.append(child)
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir:
            _rmtree_no_follow(child, failed, _depth + 1)
            try:
                os.rmdir(_fs(child))
            except OSError:
                pass
        elif not _remove_file(child):
            failed.append(child)


def _delete_path_legacy(path):
    """Удаление по строке пути (не Windows). Подмена папки ссылкой во время
    удаления здесь не исключена — см. остаточный риск в README."""
    failed = []
    try:
        if not os.path.lexists(_fs(path)):
            return True, failed
    except (OSError, ValueError):
        return False, [path]
    if _is_link_path(path):
        ok = _remove_link(path)
        return ok, ([] if ok else [path])
    if is_protected_for_delete(path):
        raise ProtectedPathError(path)
    if os.path.isfile(_fs(path)):
        ok = _remove_file(path)
        return ok, ([] if ok else [path])
    _rmtree_no_follow(path, failed)
    try:
        os.rmdir(_fs(path))
    except OSError:
        pass
    return not os.path.lexists(_fs(path)), failed


def delete_path_resilient(path, expected_identity=None, strict_allowlist=False):
    """Удаляет файл/папку, продолжая при ошибках на отдельных файлах.
    Возвращает (удалено_полностью, [пути_которые_не_удалились]).

    Бросает ProtectedPathError для защищённых путей — проверка здесь, на
    самом низком уровне, независимо от того, откуда вызвали удаление, и
    ПО РЕАЛЬНОМУ расположению открытого объекта.
    Бросает PathChangedError, если передан expected_identity (снят при
    подтверждении) и объект по пути уже другой.
    Никогда не заходит внутрь symlink/junction: если удаляется ссылка —
    удаляется только сама ссылка.
    strict_allowlist=True («Быстрая очистка»): путь должен быть в allowlist,
    а реальное расположение — совпадать с ним без перенаправлений, иначе
    RedirectedPathError."""
    if is_protected_path(path):
        raise ProtectedPathError(path)
    if strict_allowlist and not is_allowed_quick_cleanup_path(path):
        raise ProtectedPathError(path)
    if not IS_WINDOWS or _win32() is None:
        if strict_allowlist and _cache_key(os.path.realpath(path)) != _cache_key(path):
            raise RedirectedPathError(path, os.path.realpath(path))
        return _delete_path_legacy(path)
    try:
        ext = _ext_path(path)
    except ValueError:
        raise ProtectedPathError(path)

    try:
        handle = _w_open(ext, _DEL_ACCESS)
    except FileNotFoundError:
        if expected_identity is not None:
            raise PathChangedError(path)
        return True, []
    except OSError:
        # Нет прав на удаление / объект занят. Вердикт защиты не должен
        # зависеть от прав: смотрим реальное расположение без права удаления.
        ident = path_identity(path)
        if ident is not None and not ident[2] and (ident[3] is None or is_protected_path(ident[3])):
            raise ProtectedPathError(path)
        return False, [path]

    with handle:                       # пока открыт — путь до объекта закреплён
        try:
            attrs, tag = _w_attr_tag(handle)
            vol, index = _w_identity(handle)
        except OSError:
            return False, [path]
        link = _w_is_link(attrs, tag)
        final = None if link else _w_final_path(handle)
        if expected_identity is not None and expected_identity != (vol, index, link, final):
            raise PathChangedError(path)
        if strict_allowlist and (link or not _w_same_location(final, path)
                                 or _canonical_path(final) not in _allowlist_canonical()):
            raise RedirectedPathError(path, final)
        if link:
            ok = _w_delete_handle(handle, attrs)
            return ok, ([] if ok else [path])
        # Реальное расположение открытого объекта — не строка пути
        if final is None or is_protected_path(final):
            raise ProtectedPathError(path)
        # Прежняя проверка по строке (realpath) — дополнительный рубеж
        if is_protected_for_delete(path):
            raise ProtectedPathError(path)
        failed = []
        if attrs & _W_ATTR_DIRECTORY:
            _w_delete_children(handle, path, failed, 0)
            if failed:
                return False, failed
        ok = _w_delete_handle(handle, attrs)
    if not ok:
        return False, [path]
    return not os.path.lexists(ext), failed


# Корни критических контейнеров, у которых НИКОГДА нельзя зачищать всё
# содержимое целиком — даже если clear_folder_contents() вызовут откуда-то ещё.
def _never_clear_contents_of():
    program_data = os.environ.get("ProgramData", os.path.join(SYSTEM_DRIVE + "\\", "ProgramData"))
    program_files = os.environ.get("ProgramFiles", os.path.join(SYSTEM_DRIVE + "\\", "Program Files"))
    program_files_x86 = os.environ.get("ProgramFiles(x86)", os.path.join(SYSTEM_DRIVE + "\\", "Program Files (x86)"))
    users_root = os.path.join(SYSTEM_DRIVE + "\\", "Users")
    roots = [WINDIR, program_data, program_files, program_files_x86, users_root, USER_PROFILE]
    return {ntpath.normcase(ntpath.normpath(p)) for p in roots if p}


_NEVER_CLEAR_CONTENTS_OF = _never_clear_contents_of()


def _never_clear(norm):
    """norm — путь в форме _cache_key / _canonical_path."""
    if _is_drive_root(norm) or norm in _NEVER_CLEAR_CONTENTS_OF:
        return True
    canon = _canonical_path(norm)
    if canon is None or _is_drive_root(canon):
        return True
    if canon in (_SYSTEM_CONTAINERS | _PROFILES_ROOTS | _profile_containers()):
        return True
    # сама папка Windows/ProgramData или папка, внутри которой они лежат
    return any(_is_under(root, canon) for root in _SYSTEM_SUBTREES)


def clear_quick_cleanup_category(path):
    """Очистка категории «Быстрой очистки». Разрешено, только если:
    путь — точно из allowlist; реальная папка (по открытому дескриптору)
    находится ровно по этому пути — никакой компонент, включая родителей,
    не перенаправлен; реальный путь тоже есть в allowlist. Allowlist НЕ
    расширяется через realpath. Иначе — ProtectedPathError /
    RedirectedPathError, и ничего не удаляется."""
    if not is_allowed_quick_cleanup_path(path):
        raise ProtectedPathError(path)
    return clear_folder_contents(path, _strict_allowlist=True)


def clear_folder_contents(path, _strict_allowlist=False):
    """Удаляет СОДЕРЖИМОЕ папки (саму папку оставляет). Для «Быстрой очистки»
    используйте clear_quick_cleanup_category().
    Возвращает число элементов, которые удалить не удалось (1 — отказ).
    Папка, путь к которой перенаправлен ссылкой (в т.ч. в одном из
    родителей), не очищается.
    Доп. защита здесь — подстраховка на случай ошибки вызывающего кода."""
    try:
        norm = ntpath.normcase(ntpath.normpath(path))
    except Exception:
        return 1

    if _never_clear(norm):
        return 1
    try:
        if norm == ntpath.normcase(ntpath.normpath(USER_PROFILE)):
            return 1
    except Exception:
        return 1

    # Если сама папка — ссылка (например, %TEMP% перенаправлен junction'ом),
    # отказываемся: зачистка ушла бы в произвольное место.
    if _is_link_path(path):
        return 1
    try:
        real = _cache_key(os.path.realpath(path))
        if _never_clear(real):
            return 1
    except (OSError, ValueError):
        return 1

    if IS_WINDOWS and _win32() is not None:
        return _clear_folder_contents_win(path, _strict_allowlist)

    if _cache_key(os.path.realpath(path)) != _cache_key(path):
        if _strict_allowlist:
            raise RedirectedPathError(path, os.path.realpath(path))
        return 1
    errors = 0
    try:
        with os.scandir(_fs(path)) as it:
            entries = list(it)
    except OSError:
        return 1
    for entry in entries:
        child = os.path.join(path, entry.name)
        if _is_link_entry(entry):
            if not _remove_link(child):
                errors += 1
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir:
            failed = []
            _rmtree_no_follow(child, failed)
            try:
                os.rmdir(_fs(child))
            except OSError:
                pass
            if failed or os.path.lexists(_fs(child)):
                errors += 1
        elif not _remove_file(child):
            errors += 1
    return errors


def _clear_folder_contents_win(path, strict_allowlist=False):
    """Очистка через дескрипторы. Строка пути разбирается ОДИН раз — при
    открытии папки категории; дальше её реальное расположение проверяется
    по дескриптору, а дети перечисляются и открываются относительно него."""
    def refuse(real=None):
        if strict_allowlist:
            raise RedirectedPathError(path, real)
        return 1

    try:
        ext = _ext_path(path)
        handle = _w_open(ext, _W_FILE_READ_ATTRIBUTES | _W_FILE_LIST_DIRECTORY | _W_SYNCHRONIZE)
    except (OSError, ValueError):
        return 1
    with handle:
        try:
            attrs, tag = _w_attr_tag(handle)
        except OSError:
            return 1
        if _w_is_link(attrs, tag):
            return refuse()
        if not attrs & _W_ATTR_DIRECTORY:
            return 1
        final = _w_final_path(handle)
        if final is None or _never_clear(_cache_key(final)):
            return 1
        if not _w_same_location(final, path):
            return refuse(final)
        if strict_allowlist and _canonical_path(final) not in _allowlist_canonical():
            return refuse(final)
        try:
            names = _w_list_names(handle)
        except OSError:
            return 1
        errors = 0
        for name in names:
            failed = []
            _w_delete_entry(handle, name, os.path.join(path, name), failed, 1)
            if failed:
                errors += 1
        return errors


def _explorer_exe():
    """Полный путь к explorer.exe: запуск просто «explorer» искал бы файл
    сначала в папке программы и в ТЕКУЩЕЙ папке (подмена исполняемого файла)."""
    for base in _windows_dirs_from_api() + [WINDIR]:
        exe = os.path.join(base, "explorer.exe")
        if os.path.isfile(exe):
            return exe
    return None


def _reveal_in_explorer(path, select=False):
    """Показать в Проводнике. Для файла — открыть папку с выделенным файлом.
    Папка открывается только если это действительно папка (не ссылка на
    файл и не подменённый исполняемый файл): иначе — «показать в папке»,
    ничего не запуская."""
    if not IS_WINDOWS:
        return False
    exe = _explorer_exe()
    if not exe:
        return False
    try:
        if not select and os.path.isdir(path) and not _is_link_path(path):
            subprocess.Popen([exe, os.path.normpath(path)])
        else:
            subprocess.Popen(f'"{exe}" /select,"{os.path.normpath(path)}"')
        return True
    except OSError:
        return False


# ---------- Деинсталляция ----------

class UninstallCommand(str):
    """Строка удаления из реестра. quiet=True — это QuietUninstallString:
    программа удалится без собственных окон и вопросов."""
    quiet = False


class UninstallCommandError(ValueError):
    """Строка удаления повреждена или неоднозначна — ничего не запускаем."""


# Системные программы, которые деинсталляторы вызывают по короткому имени.
# Имя без пути ищется ТОЛЬКО в System32, не в PATH и не в текущей папке.
_UNINSTALL_SYSTEM_TOOLS = {"msiexec": "msiexec.exe", "msiexec.exe": "msiexec.exe",
                           "rundll32": "rundll32.exe", "rundll32.exe": "rundll32.exe"}


def _system32_dir():
    if IS_WINDOWS:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(520)
            if ctypes.windll.kernel32.GetSystemDirectoryW(buf, 520):
                return buf.value
        except Exception:
            pass
    return os.path.join(WINDIR, "System32")


def parse_uninstall_command(command):
    """Разбирает UninstallString БЕЗ командной оболочки.
    Возвращает (exe, аргументы, командная_строка).

    Поддерживается:  "C:\\Path\\uninst.exe" /args;  C:\\Path With Spaces\\uninst.exe /args
    (если однозначно находится ровно один существующий .exe);  MsiExec.exe /X{GUID};
    RunDll32 …;  %ProgramFiles%\\… (переменные раскрываются здесь, а не cmd.exe).
    Аргументы передаются деинсталлятору как есть — без наивного split(),
    который сломал бы кавычки и ключи вида _?=C:\\Program Files\\App."""
    if not command or not isinstance(command, str):
        raise UninstallCommandError("строка удаления пуста")
    s = command.replace("\x00", "").strip()
    s = os.path.expandvars(s)
    if re.search(r"%[^%\s\\]+%", s):
        raise UninstallCommandError("в строке осталась нераскрытая переменная окружения")
    if not s:
        raise UninstallCommandError("строка удаления пуста")

    if s.startswith('"'):
        end = s.find('"', 1)
        if end < 0:
            raise UninstallCommandError("незакрытая кавычка")
        exe, rest = s[1:end].strip(), s[end + 1:]
        if rest and not rest[0].isspace():
            raise UninstallCommandError("текст сразу после закрывающей кавычки")
        args = rest.strip()
        if exe.lower() in _UNINSTALL_SYSTEM_TOOLS:
            exe = os.path.join(_system32_dir(), _UNINSTALL_SYSTEM_TOOLS[exe.lower()])
    else:
        first, _, rest = s.partition(" ")
        if "\\" not in first and "/" not in first and ":" not in first:
            tool = _UNINSTALL_SYSTEM_TOOLS.get(first.lower())
            if not tool:
                raise UninstallCommandError(f"программа «{first}» указана без пути")
            exe, args = os.path.join(_system32_dir(), tool), rest.strip()
        else:
            # Путь без кавычек может содержать пробелы. Берём только варианты,
            # оканчивающиеся на .exe на границе слова и реально существующие;
            # если таких не ровно один — команда неоднозначна.
            found = []
            for m in re.finditer(r"(?=\s)|$", s):
                prefix = s[:m.start()]
                if prefix.lower().endswith(".exe") and os.path.isfile(prefix):
                    found.append(prefix)
            found = list(dict.fromkeys(found))
            if len(found) != 1:
                raise UninstallCommandError("путь к деинсталлятору не найден или неоднозначен"
                                            if not found else "несколько возможных исполняемых файлов")
            exe = found[0]
            args = s[len(exe):].strip()

    if not re.match(r"[A-Za-z]:\\", exe):
        raise UninstallCommandError("деинсталлятор должен быть указан полным локальным путём")
    if not exe.lower().endswith(".exe"):
        raise UninstallCommandError("деинсталлятор — не .exe")
    if not os.path.isfile(exe):
        raise UninstallCommandError("файл деинсталлятора не найден")
    cmdline = f'"{exe}"' + (f" {args}" if args else "")
    return exe, args, cmdline


def launch_uninstaller(command, _popen=None, _shell_execute=None):
    """Запускает деинсталлятор без cmd.exe: путь к .exe передаётся явно
    (lpApplicationName), поэтому символы & | ^ % в строке не выполняются
    оболочкой и короткий путь «C:\\Program.exe» не может быть подставлен.
    Если деинсталлятор по своему манифесту требует прав администратора
    (ошибка 740), он запускается через ShellExecute с обычным действием
    «open» — Windows сама покажет запрос UAC, как при запуске из Проводника.
    Программа НЕ запрашивает повышение прав сама (verb «runas» не используется)."""
    exe, args, cmdline = parse_uninstall_command(command)
    popen = _popen or subprocess.Popen
    try:
        popen(cmdline, executable=exe, cwd=os.path.dirname(exe), shell=False)
        return exe, args
    except OSError as e:
        if getattr(e, "winerror", None) != 740:
            raise
    run = _shell_execute or _shell_execute_open
    run(exe, args)
    return exe, args


def _shell_execute_open(exe, args):
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.shell32.ShellExecuteW
    fn.restype = wintypes.HINSTANCE
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                   wintypes.LPCWSTR, ctypes.c_int]
    res = fn(None, None, exe, args or None, os.path.dirname(exe), 1)
    if (res or 0) <= 32:
        raise OSError(f"ShellExecute вернул ошибку {res}")


# ---------- Программы и игры: реестр ----------

def _build_uninstall_paths():
    """Построено лениво, чтобы импорт файла не требовал winreg."""
    if winreg is None:
        return []
    return [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]


def _default_scan_folders():
    """Program Files / Program Files (x86) / Games на ЛОКАЛЬНЫХ несъёмных дисках.
    Сетевые и съёмные диски не проверяются: отключённый сетевой диск может
    отвечать десятки секунд."""
    folders = []
    for drive in get_available_drives():
        if get_drive_type(drive) not in (DRIVE_FIXED, DRIVE_RAMDISK):
            continue
        for sub in ("Program Files", "Program Files (x86)", "Games"):
            path = os.path.join(drive, sub)
            try:
                if os.path.isdir(path):
                    folders.append(path)
            except OSError:
                pass
    return folders


def _read_value(key, value_name):
    try:
        value, _ = winreg.QueryValueEx(key, value_name)
        return value
    except OSError:
        return None


def _clean_reg_str(value):
    if not isinstance(value, str):
        return None
    value = value.replace("\x00", "").strip()
    return value or None


def _clean_reg_path(value):
    s = _clean_reg_str(value)
    if not s:
        return None
    s = s.strip('"').strip()
    try:
        s = os.path.expandvars(s)
    except Exception:
        pass
    return s or None


def get_installed_programs():
    """Установленные программы из реестра. Устойчиво к PermissionError,
    повреждённым записям и значениям неожиданного типа: проблемная запись
    пропускается, а не роняет весь список."""
    if winreg is None:
        return {}
    programs = {}
    for hive, path in _build_uninstall_paths():
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            try:
                count = winreg.QueryInfoKey(key)[0]
            except OSError:
                continue
            for i in range(count):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                except OSError:
                    continue
                try:
                    name = _clean_reg_str(_read_value(subkey, "DisplayName"))
                    if not name or _read_value(subkey, "SystemComponent") == 1:
                        continue
                    location = _clean_reg_path(_read_value(subkey, "InstallLocation"))
                    # Обычный (интерактивный) деинсталлятор производителя в
                    # приоритете над «тихим»: тихий удаляет сразу, без окон
                    # и возможности передумать.
                    uninstall = _clean_reg_str(_read_value(subkey, "UninstallString"))
                    quiet = False
                    if not uninstall:
                        uninstall = _clean_reg_str(_read_value(subkey, "QuietUninstallString"))
                        quiet = bool(uninstall)
                    if uninstall:
                        uninstall = UninstallCommand(uninstall)
                        uninstall.quiet = quiet
                    existing = programs.get(name)
                    if existing and (existing["path"] or not location):
                        continue
                    programs[name] = {"name": name, "path": location, "uninstall": uninstall}
                except Exception:
                    continue
                finally:
                    try:
                        winreg.CloseKey(subkey)
                    except Exception:
                        pass
        finally:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    return programs


def _user_appdata_folders():
    folders = []
    for sub in (r"AppData\Local", r"AppData\Roaming"):
        p = os.path.join(USER_PROFILE, sub)
        if os.path.isdir(p):
            folders.append(p)
    return folders


def _generic_container_keys(scan_folders=()):
    """Папки-контейнеры, которые не могут быть «папкой одной программы»."""
    env = os.environ.get
    extra = [
        env("ProgramData"), env("ProgramFiles"), env("ProgramFiles(x86)"), env("ProgramW6432"),
        env("CommonProgramFiles"), env("CommonProgramFiles(x86)"), env("CommonProgramW6432"),
        os.path.join(SYSTEM_DRIVE + "\\", "Users"), USER_PROFILE,
        os.path.join(USER_PROFILE, "AppData"), os.path.join(USER_PROFILE, "AppData", "Local"),
        os.path.join(USER_PROFILE, "AppData", "Roaming"), os.path.join(USER_PROFILE, "AppData", "LocalLow"),
        os.path.join(USER_PROFILE, "AppData", "Local", "Programs"), LOCAL_APPDATA,
        os.path.join(USER_PROFILE, "Desktop"), os.path.join(USER_PROFILE, "Documents"),
        os.path.join(USER_PROFILE, "Downloads"),
    ]
    keys = {_cache_key(p) for p in scan_folders if p}
    keys |= {_cache_key(p) for p in extra if p}
    for p in list(scan_folders):
        keys.add(_cache_key(os.path.join(p, "Common Files")))
    return keys


def _is_unsafe_install_location(path, container_keys=None):
    """Повреждённые записи реестра иногда указывают InstallLocation = «C:\\»,
    «C:\\Program Files» или папку Windows. Такой путь нельзя считать папкой
    программы: пришлось бы пересчитывать весь диск, а все реальные программы
    внутри Program Files пропали бы из списка как «уже учтённые»."""
    if not path:
        return True
    norm = _cache_key(path)
    if _is_drive_root(norm):
        return True
    _, rest = ntpath.splitdrive(norm)
    parts = [p for p in rest.split("\\") if p]
    if parts and parts[0] in ("windows", "$recycle.bin", "system volume information"):
        return True
    keys = container_keys if container_keys is not None else _generic_container_keys()
    return norm in keys


def _collect_app_candidates(extra_folders=()):
    """(название, путь или None, строка удаления или None, вид "app"|"folder")."""
    registry_programs = get_installed_programs()
    scan_folders = _default_scan_folders() + _user_appdata_folders() + list(extra_folders)
    containers = _generic_container_keys(scan_folders)

    candidates = []
    matched = set()
    for prog in registry_programs.values():
        path = prog.get("path")
        if path:
            try:
                if _is_unsafe_install_location(path, containers) or not os.path.isdir(path):
                    path = None
            except OSError:
                path = None
        if path:
            candidates.append((prog["name"], path, prog.get("uninstall"), "app"))
            matched.add(_cache_key(path))
        elif prog.get("uninstall"):
            candidates.append((prog["name"], None, prog.get("uninstall"), "app"))

    seen_bases = set()
    for base in scan_folders:
        base_key = _cache_key(base)
        if base_key in seen_bases:
            continue
        seen_bases.add(base_key)
        try:
            with os.scandir(_fs(base)) as it:
                subfolders = [e.name for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for name in subfolders:
            child = os.path.join(base, name)
            key = _cache_key(child)
            if key in matched or any(key.startswith(m + "\\") for m in matched):
                continue
            candidates.append((name, child, None, "folder"))
    return candidates


# ---------- Быстрая очистка: категории ----------

TARGETS = [
    {"id": "temp_user", "name": "Временные файлы пользователя (%TEMP%)",
     "path": os.path.join(USER_PROFILE, "AppData", "Local", "Temp") if USER_PROFILE else None,
     "action": "clear_contents"},
    {"id": "temp_windows", "name": "Временные файлы Windows",
     "path": os.path.join(WINDIR, "Temp"), "action": "clear_contents"},
    {"id": "windows_update", "name": "Кэш загрузок Windows Update",
     "path": os.path.join(WINDIR, "SoftwareDistribution", "Download"), "action": "clear_contents"},
    {"id": "thumbnails", "name": "Кэш миниатюр",
     "path": os.path.join(USER_PROFILE, "AppData", "Local", "Microsoft", "Windows", "Explorer") if USER_PROFILE else None,
     "action": "clear_contents"},
    {"id": "nvidia_dxcache", "name": "Кэш шейдеров NVIDIA (DXCache)",
     "path": os.path.join(USER_PROFILE, "AppData", "Local", "NVIDIA", "DXCache") if USER_PROFILE else None,
     "action": "clear_contents"},
    {"id": "nvidia_glcache", "name": "Кэш шейдеров NVIDIA (GLCache)",
     "path": os.path.join(USER_PROFILE, "AppData", "Local", "NVIDIA", "GLCache") if USER_PROFILE else None,
     "action": "clear_contents"},
    {"id": "chrome_cache", "name": "Кэш Google Chrome",
     "path": os.path.join(LOCAL_APPDATA, "Google", "Chrome", "User Data", "Default", "Cache") if LOCAL_APPDATA else None,
     "action": "clear_contents"},
    {"id": "edge_cache", "name": "Кэш Microsoft Edge",
     "path": os.path.join(LOCAL_APPDATA, "Microsoft", "Edge", "User Data", "Default", "Cache") if LOCAL_APPDATA else None,
     "action": "clear_contents"},
    {"id": "windows_old", "name": "Windows.old (старая версия Windows)",
     "path": os.path.join(SYSTEM_DRIVE + "\\", "Windows.old"), "action": "clear_folder_full",
     "note": "После удаления нельзя будет откатиться на предыдущую версию Windows через "
             "стандартный откат — только если у тебя уже всё устраивает в текущей версии."},
    {"id": "recycle_bin", "name": "Корзина", "path": None, "action": "empty_recycle_bin",
     "note": "Безвозвратно удаляет всё, что сейчас лежит в корзине."},
]

# Пояснения только для интерфейса (TARGETS — часть механизма безопасности,
# его структура намеренно не меняется).
CLEANUP_DESCRIPTIONS = {
    "temp_user": "Временные файлы программ в вашем профиле. Программы создают их заново; занятые файлы пропускаются.",
    "temp_windows": "Временные файлы самой Windows. Файлы, которые сейчас используются, пропускаются.",
    "windows_update": "Уже скачанные установочные файлы обновлений. При необходимости Windows скачает их снова.",
    "thumbnails": "Миниатюры и значки Проводника. Создаются заново; первое открытие папок может быть чуть медленнее.",
    "nvidia_dxcache": "Кэш шейдеров видеокарты. Игры соберут его заново — первый запуск может быть дольше.",
    "nvidia_glcache": "Кэш OpenGL-шейдеров видеокарты. Создаётся заново автоматически.",
    "chrome_cache": "Кэш страниц и картинок. Пароли, история и вкладки не затрагиваются. Лучше закрыть Chrome.",
    "edge_cache": "Кэш страниц и картинок. Пароли, история и вкладки не затрагиваются. Лучше закрыть Edge.",
    "windows_old": "Копия предыдущей версии Windows, остающаяся после крупного обновления.",
    "recycle_bin": "Файлы, которые вы ранее удалили в корзину.",
}

# ---------- Allowlist для Quick Cleanup ----------
# Архитектура защиты сознательно разделена на два независимых механизма:
#
# 1. is_protected_path() / delete_path_resilient() — для вкладки "Диск", где
#    путь выбирает ПОЛЬЗОВАТЕЛЬ, проваливаясь по произвольным папкам. Там
#    действует блок-лист: запрещено всё опасное (весь Windows\*, корень диска
#    и т.д.), разрешено всё остальное.
#
# 2. QUICK_CLEANUP_ALLOWLIST / is_allowed_quick_cleanup_path() — для Quick
#    Cleanup, где пути НЕ выбирает пользователь, а жёстко заданы разработчиком
#    в TARGETS выше. Здесь действует allowlist: разрешены ТОЛЬКО эти конкретные
#    пути, точным совпадением, и ничего больше. Даже если какой-то путь из
#    TARGETS случайно совпадёт с тем, что in_protected_path() считает общей
#    "запрещённой зоной" (как Windows\Temp) — для Quick Cleanup это ОК, потому
#    что это не свободный ввод пользователя, а конкретная, проверенная заранее
#    запись в заранее одобренном списке.
#
# Этот allowlist невозможно обойти пользовательским вводом — он строится
# ТОЛЬКО из путей внутри TARGETS, захардкоженных выше в этом же файле.
def _build_quick_cleanup_allowlist():
    import ntpath
    allowed = set()
    for t in TARGETS:
        path = t.get("path")
        if path and t.get("action") in ("clear_contents", "clear_folder_full"):
            allowed.add(ntpath.normcase(ntpath.normpath(path)))
    return allowed


QUICK_CLEANUP_ALLOWLIST = _build_quick_cleanup_allowlist()


def is_allowed_quick_cleanup_path(path):
    """True, только если путь ТОЧНО совпадает с одним из предопределённых
    путей Quick Cleanup из TARGETS. Никакой пользовательский путь (например,
    из "Выбрать папку" на вкладке "Диск") никогда не попадёт в этот список,
    потому что список строится статически из TARGETS при запуске программы,
    а не принимает пути во время выполнения."""
    import ntpath
    if not path:
        return False
    try:
        norm = ntpath.normcase(ntpath.normpath(path))
    except Exception:
        return False
    return norm in QUICK_CLEANUP_ALLOWLIST




# ======================================================================
#                               ИНТЕРФЕЙС
# ======================================================================

# Собственное увеличение интерфейса поверх масштаба Windows. Системный DPI
# не трогаем: на больших экранах просто рисуем всё на ~22% крупнее, а на
# маленьких «логических» экранах (1920×1080 при 125–150%) увеличение
# плавно уменьшается до 0, чтобы окно и всё содержимое помещались.
UI_BOOST_MAX = 1.22
_UI_DESIGN_W, _UI_DESIGN_H = 1120, 740      # базовый размер окна в логических px


def _ui_boost(screen_w, screen_h, dpi_scale):
    """Множитель интерфейса для экрана screen_w×screen_h (физические px)
    при масштабе Windows dpi_scale (1.0 = 100%)."""
    try:
        logical_w = screen_w / dpi_scale
        logical_h = screen_h / dpi_scale
        fit = min(logical_w * 0.9 / _UI_DESIGN_W, logical_h * 0.88 / _UI_DESIGN_H)
    except (TypeError, ZeroDivisionError):
        return 1.0
    return round(max(1.0, min(UI_BOOST_MAX, fit)), 3)


# Микроанимации: короткие, через after(), без потоков и без sleep().
# Все переходы идут от ОДНОГО общего таймера кадров (_Animator): сколько бы
# элементов ни анимировалось, за кадр выполняется один callback, и пока
# анимаций нет, таймера нет вообще. Анимируются только дешёвые вещи
# (цвет элемента Canvas, кадр готовой картинки, цвет Label); стили ttk не
# анимируются: их смена перерисовывает ВСЕ ttk-виджеты, включая таблицу
# на тысячи строк (~7 мс на кадр).
ANIM_MS = 130
ANIM_FRAME_MS = 16


def _hex_rgb(c):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _blend(c1, c2, p):
    if p <= 0:
        return c1
    if p >= 1:
        return c2
    a, b = _hex_rgb(c1), _hex_rgb(c2)
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * p)) for x, y in zip(a, b))


def _ease_out(p):
    return 1 - (1 - p) * (1 - p)


def _lerp(a, b, p):
    """Цвет "#rrggbb" или число."""
    if isinstance(a, str):
        return _blend(a, b, p)
    return a + (b - a) * p


class _Animator:
    """Общие часы анимаций одного окна."""

    def __init__(self, root):
        self.root = root
        self.active = []
        self._job = None

    @staticmethod
    def of(widget):
        root = widget._root()
        anim = getattr(root, "_cp_animator", None)
        if anim is None:
            anim = root._cp_animator = _Animator(root)
        return anim

    def add(self, tween):
        if tween not in self.active:
            self.active.append(tween)
        if self._job is None:
            self._job = self.root.after(ANIM_FRAME_MS, self._step)

    def remove(self, tween):
        if tween in self.active:
            self.active.remove(tween)
        if not self.active and self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None

    def _step(self):
        self._job = None
        now = time.perf_counter()
        finished = []
        for tween in list(self.active):
            try:
                if tween._advance(now):
                    finished.append(tween)
            except tk.TclError:          # виджет уже уничтожен
                tween._anim = None
                tween._done = None
                finished.append(tween)
        for tween in finished:
            if tween in self.active:
                self.active.remove(tween)
        for tween in finished:
            tween._finish()
        if self.active and self._job is None:
            self._job = self.root.after(ANIM_FRAME_MS, self._step)


class _Tween:
    """Переход набора значений (цвета "#rrggbb" или числа) от текущих к
    целевым. Новый переход сразу отменяет незавершённый (быстрые наведения
    не копятся), при уничтожении виджета переход тоже отменяется."""

    def __init__(self, widget, values, apply):
        self.w = widget
        self.cur = tuple(values)
        self.apply = apply
        self._anim = None
        self._from = self._to = self.cur
        self._t0 = 0.0
        self._dur = ANIM_MS
        self._done = None
        widget.bind("<Destroy>", lambda e: self.cancel() if e.widget is widget else None, add="+")

    @property
    def running(self):
        return self._anim is not None

    def cancel(self):
        self._done = None
        if self._anim is not None:
            self._anim.remove(self)
            self._anim = None

    def set(self, values):
        """Мгновенно, без анимации."""
        self.cancel()
        self.cur = self._to = tuple(values)
        self.apply(self.cur)

    def to(self, values, duration=ANIM_MS, done=None):
        """done() вызывается один раз, если переход дошёл до конца."""
        values = tuple(values)
        if values == self._to and (self.running or values == self.cur):
            if done is not None and not self.running:
                done()
            elif done is not None:
                self._done = done
            return
        self.cancel()
        self._done = done
        self._from, self._to = self.cur, values
        self._dur = max(1, duration)
        self._t0 = time.perf_counter()
        self._anim = _Animator.of(self.w)
        self._anim.add(self)

    def _advance(self, now):
        p = min(1.0, (now - self._t0) * 1000.0 / self._dur)
        if p >= 1:
            self.cur = self._to
        else:
            e = _ease_out(p)
            self.cur = tuple(_lerp(a, b, e) for a, b in zip(self._from, self._to))
        self.apply(self.cur)
        return p >= 1

    def _finish(self):
        self._anim = None
        done, self._done = self._done, None
        if done is not None:
            done()


class Theme:
    """Цвета, шрифты и масштаб. px() переводит «логические» пиксели (как при
    100% масштабе Windows) в реальные, чтобы отступы и высоты строк
    масштабировались на 125/150/175/200% вместе со шрифтами. Сверху к этому
    добавляется ui_boost (см. _ui_boost) — одинаково для px() и шрифтов,
    поэтому интерфейс растёт пропорционально."""

    BG = "#14161b"
    SURFACE = "#1a1d23"
    SURFACE_2 = "#22262e"
    SURFACE_3 = "#2b3039"
    SURFACE_4 = "#343a45"
    BORDER = "#262a32"
    TEXT = "#e8eaef"
    TEXT_2 = "#a4aab6"
    TEXT_3 = "#6f7684"
    ACCENT = "#4c8dff"
    ACCENT_HOVER = "#669dff"
    ACCENT_PRESS = "#3a76e0"
    ACCENT_SOFT = "#1d2c47"
    DANGER = "#e5484d"
    DANGER_HOVER = "#ee5d61"
    DANGER_PRESS = "#c63c40"
    DANGER_SOFT = "#33191b"
    DANGER_TEXT = "#ff8f92"
    WARN = "#d9a441"
    SELECT = "#22355a"
    ROW_HOVER = "#20242c"

    def __init__(self, root):
        self.root = root
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        self.dpi_scale = max(1.0, dpi / 96.0)
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        self.ui_boost = _ui_boost(sw, sh, self.dpi_scale)
        self.scale = self.dpi_scale * self.ui_boost

        families = set(tkfont.families(root))
        base = "Segoe UI" if "Segoe UI" in families else tkfont.nametofont("TkDefaultFont").actual("family")
        boost = self.ui_boost
        # Размер в пунктах Tk сам масштабирует под DPI — домножаем только на ui_boost
        pt = lambda size: max(1, int(round(size * boost)))
        if "Segoe UI Semibold" in families:
            semi = lambda size: ("Segoe UI Semibold", pt(size))
        else:
            semi = lambda size: (base, pt(size), "bold")
        reg = lambda size: (base, pt(size))

        self.f_title = semi(18)
        self.f_nav = reg(11)
        self.f_nav_active = semi(11)
        self.f_body = reg(11)
        self.f_body_semi = semi(11)
        self.f_small = reg(10)
        self.f_small_semi = semi(10)
        self.f_caption = semi(9)
        self.f_button = semi(10)
        self.f_icon = reg(14)
        self.f_big = semi(15)
        # Отдельные роли, которые на 1440p были особенно мелкими
        self.f_crumb = reg(12)
        self.f_crumb_last = semi(12)
        self.f_status = reg(10.5)
        self.f_row_title = semi(11.5)
        self.f_row_desc = reg(10.5)
        self.f_row_size = semi(11.5)
        self.f_total = semi(18)
        self.f_button_lg = semi(11)

        self._font_cache = {}

    def px(self, n):
        return int(round(n * self.scale))

    def font(self, spec):
        f = self._font_cache.get(spec)
        if f is None:
            f = tkfont.Font(root=self.root, font=spec)
            self._font_cache[spec] = f
        return f

    def apply_ttk(self):
        px = self.px
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(bg=self.BG)

        style.configure(".", background=self.BG, foreground=self.TEXT, font=self.f_body,
                        borderwidth=0, focuscolor=self.BG)

        # Таблица без рамки, без зебры, с нормальной высотой строк
        style.layout("Cp.Treeview", [("Cp.Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.indicator", {"side": "left", "sticky": ""}),
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""}),
        ]})])
        style.configure("Cp.Treeview", background=self.SURFACE, fieldbackground=self.SURFACE,
                        foreground=self.TEXT, rowheight=px(36), borderwidth=0, relief="flat",
                        font=self.f_body)
        style.map("Cp.Treeview",
                  background=[("selected", self.SELECT)],
                  foreground=[("selected", self.TEXT)])
        style.configure("Cp.Treeview.Heading", background=self.SURFACE, foreground=self.TEXT_3,
                        font=self.f_caption, relief="flat", borderwidth=0,
                        padding=(px(12), px(10)),
                        bordercolor=self.SURFACE, lightcolor=self.SURFACE, darkcolor=self.SURFACE)
        style.map("Cp.Treeview.Heading",
                  background=[("active", self.SURFACE), ("pressed", self.SURFACE)],
                  foreground=[("active", self.TEXT_2)])

        # Тонкий скроллбар без стрелок
        style.layout("Cp.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Cp.Vertical.TScrollbar", troughcolor=self.SURFACE, background=self.SURFACE_3,
                        bordercolor=self.SURFACE, lightcolor=self.SURFACE_3, darkcolor=self.SURFACE_3,
                        arrowsize=px(10), gripcount=0, relief="flat", borderwidth=0)
        style.map("Cp.Vertical.TScrollbar",
                  background=[("pressed", "#4a5160"), ("active", self.SURFACE_4)],
                  lightcolor=[("pressed", "#4a5160"), ("active", self.SURFACE_4)],
                  darkcolor=[("pressed", "#4a5160"), ("active", self.SURFACE_4)])

        # Тонкая полоса прогресса
        style.configure("Cp.Horizontal.TProgressbar", troughcolor=self.SURFACE, background=self.ACCENT,
                        bordercolor=self.SURFACE, lightcolor=self.ACCENT, darkcolor=self.ACCENT,
                        thickness=max(2, px(2.5)), borderwidth=0,
                        # В clam высоту полосы задаёт arrowsize (+ рамка элемента),
                        # а не thickness; ширина бегущего блока — sliderlength
                        arrowsize=max(1, px(1)), sliderlength=px(90))
        return style


def _rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _widget_bg(widget, default):
    try:
        return widget.cget("bg")
    except tk.TclError:
        return default


def _auto_wrap(label, margin=0):
    """Перенос строк поясняющего текста по фактической ширине, а не по
    фиксированной: на узком окне и при 150% текст не обрезается."""
    def _on(e):
        wl = max(e.width - margin, 120)
        if int(label.cget("wraplength") or 0) != wl:
            label.configure(wraplength=wl)
    label.bind("<Configure>", _on, add="+")


class Tooltip:
    def __init__(self, widget, theme, text):
        self.widget, self.t, self.text = widget, theme, text
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self.widget.after(550, self._show)

    def _cancel(self):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + self.t.px(6)
            self._tip = tk.Toplevel(self.widget)
            self._tip.wm_overrideredirect(True)
            self._tip.configure(bg=self.t.SURFACE_4)
            tk.Label(self._tip, text=self.text, bg=self.t.SURFACE_3, fg=self.t.TEXT,
                     font=self.t.f_small, padx=self.t.px(8), pady=self.t.px(4)).pack(padx=1, pady=1)
            self._tip.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            self._tip = None

    def _hide(self, _e=None):
        self._cancel()
        if self._tip:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class FlatButton(tk.Canvas):
    """Кнопка с мягкими скруглениями и состояниями hover / pressed / disabled /
    фокус с клавиатуры. Варианты: primary, secondary, ghost, danger (мягкий —
    красный текст на тёмном фоне), danger_solid, icon."""

    def __init__(self, master, theme, text="", command=None, variant="secondary",
                 tooltip=None, height=None, width=None, font=None, icon=None):
        self.t = theme
        self._text = text
        self._icon = icon              # имя векторной иконки (назад/вперёд/вверх/обновить)
        self._img_item = None
        self._command = command
        self._variant = variant
        self._font = font or (theme.f_icon if variant == "icon" else theme.f_button)
        self._enabled = True
        self._hover = self._pressed = self._focused = False
        h = height or theme.px(36)
        if variant == "icon":
            w = width or h
        else:
            w = width or (theme.font(self._font).measure(text) + theme.px(32))
        self._parent_bg = _widget_bg(master, theme.BG)
        super().__init__(master, width=w, height=h, bg=self._parent_bg, highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1)
        self._bg_item = self._focus_item = self._text_item = None
        self._tween = _Tween(self, self._colors(), self._apply_colors)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._set(focused=True))
        self.bind("<FocusOut>", lambda e: self._set(focused=False))
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        self.bind("<Configure>", lambda e: self._draw())
        if tooltip:
            Tooltip(self, theme, tooltip)
        self._draw()

    def _palette(self):
        t = self.t
        v = self._variant
        if not self._enabled:
            if v in ("ghost", "icon"):
                return None, t.TEXT_3
            return t.SURFACE_2, t.TEXT_3
        hp = "press" if self._pressed and self._hover else ("hover" if self._hover else "normal")
        table = {
            "primary": ({"normal": t.ACCENT, "hover": t.ACCENT_HOVER, "press": t.ACCENT_PRESS}, "#ffffff"),
            "secondary": ({"normal": t.SURFACE_2, "hover": t.SURFACE_3, "press": t.SURFACE_4}, t.TEXT),
            "ghost": ({"normal": None, "hover": t.SURFACE_2, "press": t.SURFACE_3}, t.TEXT_2),
            "icon": ({"normal": None, "hover": t.SURFACE_2, "press": t.SURFACE_3}, t.TEXT_2),
            "danger": ({"normal": t.DANGER_SOFT, "hover": t.DANGER, "press": t.DANGER_PRESS}, t.DANGER_TEXT),
            "danger_solid": ({"normal": t.DANGER, "hover": t.DANGER_HOVER, "press": t.DANGER_PRESS}, "#ffffff"),
        }
        bgs, fg = table.get(v, table["secondary"])
        bg = bgs[hp]
        if v == "danger" and hp != "normal":
            fg = "#ffffff"
        if v in ("ghost", "icon") and hp != "normal":
            fg = t.TEXT
        return bg, fg

    def _colors(self):
        """(фон, текст, рамка фокуса) — всегда настоящие цвета, «прозрачный»
        фон = фон родителя, чтобы переход между ними был плавным."""
        bg, fg = self._palette()
        bg = bg or self._parent_bg
        focus = self.t.ACCENT if (self._focused and self._enabled) else bg
        return bg, fg, focus

    def _apply_colors(self, colors):
        if self._bg_item is None:
            return
        bg, fg, focus = colors
        self.itemconfigure(self._bg_item, fill=bg)
        self.itemconfigure(self._focus_item, outline=focus)
        if self._text_item is not None:
            self.itemconfigure(self._text_item, fill=fg)

    def _icon_image(self):
        t = self.t
        if not self._enabled:
            color = "#4d5360"
        elif self._hover or self._pressed or self._focused:
            color = t.TEXT
        else:
            color = t.TEXT_2
        return t.iconset.nav(self._icon, color, t.px(20))

    def _draw(self):
        """Полная перерисовка геометрии (размер/текст). Смена состояний
        hover/pressed/disabled идёт через _tween и только меняет цвета."""
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self.cget("width")), int(self.cget("height"))
        bg, fg, focus = self._tween.cur
        r = self.t.px(8)
        self._bg_item = _rounded_rect(self, 1, 1, w - 1, h - 1, r, fill=bg, outline="")
        self._focus_item = _rounded_rect(self, 1, 1, w - 1, h - 1, r, fill="", outline=focus, width=1)
        if self._icon:
            self._text_item = None
            self._img_item = self.create_image(w // 2, h // 2, image=self._icon_image())
        else:
            self._text_item = self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _refresh(self, duration=ANIM_MS):
        if self._img_item is not None:
            # Цвет значка — готовая сглаженная картинка: меняется сразу
            self.itemconfigure(self._img_item, image=self._icon_image())
        self._tween.to(self._colors(), duration)

    def _set(self, **kw):
        if not self._enabled and ("hover" in kw or "pressed" in kw):
            kw.pop("pressed", None)
        for k, v in kw.items():
            setattr(self, "_" + k, v)
        # Нажатие — почти мгновенно, отпускание и hover — мягко
        self._refresh(70 if kw.get("pressed") else ANIM_MS)

    def _on_release(self, _e):
        fire = self._pressed and self._hover and self._enabled
        self._set(pressed=False)
        if fire:
            self.invoke()

    def invoke(self):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self._enabled:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self._refresh()

    @property
    def enabled(self):
        return self._enabled

    def set_text(self, text):
        self._text = text
        if self._variant != "icon":
            self.configure(width=self.t.font(self._font).measure(text) + self.t.px(32))
        self._draw()


class CheckBox(tk.Canvas):
    """Чекбокс из заранее отрисованных сглаженных картинок (6 кадров
    заполнения + неактивный). Состояние меняется мгновенно; анимация лишь
    переключает кадр у одного элемента Canvas — без перерисовки фигур."""

    def __init__(self, master, theme, checked=True, command=None):
        self.t = theme
        s = theme.px(22)
        super().__init__(master, width=s, height=s, bg=_widget_bg(master, theme.SURFACE),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.checked = checked
        self.command = command
        self.enabled = True
        self._frames = theme.iconset.checkbox_frames(s)
        self._off = theme.iconset.checkbox_disabled(s)
        self._shown = None
        self._item = self.create_image(s // 2, s // 2)
        # _p идёт 0 → 1 (снят → отмечен)
        self._p = 1.0 if checked else 0.0
        self._tween = _Tween(self, (self._p,), self._on_tween)
        self.bind("<Button-1>", self._toggle)
        self.draw()

    def _on_tween(self, values):
        self._p = values[0]
        self.draw()

    def _animate(self):
        self._tween.to((1.0 if self.checked else 0.0,), 110)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self.draw()

    def _toggle(self, _e=None):
        if not self.enabled:
            return "break"
        self.checked = not self.checked
        self._animate()
        if self.command:
            self.command(self.checked)
        return "break"

    def set(self, checked):
        self.checked = bool(checked)
        self._animate()

    def draw(self):
        if not self.enabled:
            img = self._off
        else:
            img = self._frames[int(round(self._p * (len(self._frames) - 1)))]
        if img is not self._shown:
            self._shown = img
            self.itemconfigure(self._item, image=img)


class Badge(tk.Canvas):
    def __init__(self, master, theme, text, fg=None, bg=None):
        self.t = theme
        f = theme.font(theme.f_caption)
        w = f.measure(text) + theme.px(18)
        h = theme.px(22)
        super().__init__(master, width=w, height=h, bg=_widget_bg(master, theme.BG),
                         highlightthickness=0, bd=0)
        _rounded_rect(self, 0, 0, w, h, h / 2, fill=bg or theme.ACCENT_SOFT, outline="")
        self.create_text(w / 2, h / 2, text=text, fill=fg or theme.ACCENT, font=theme.f_caption)


class NavBar(tk.Frame):
    """Навигация разделов: текст + тонкая линия под активным пунктом.
    Линия одна и мягко переезжает к выбранному пункту; цвет текста при
    наведении меняется плавно.

    У каждого пункта две заранее созданные метки в одной ячейке — обычная и
    полужирная; переключение лишь поднимает нужную наверх. Смена шрифта у
    метки в Tk запускает перекладку окна (~6 мс на каждое переключение
    вкладки), поднятие метки — нет."""

    def __init__(self, master, theme, items, on_select):
        super().__init__(master, bg=theme.BG)
        self.t = theme
        self.on_select = on_select
        self._items = {}        # key -> обычная метка
        self._bold = {}         # key -> полужирная метка (активный пункт)
        self._fg = {}
        self._order = []
        self.active = None
        self._padx = theme.px(14)
        self._ind_h = max(2, theme.px(2.5))
        f_reg, f_bold = theme.font(theme.f_nav), theme.font(theme.f_nav_active)
        for col, (key, label) in enumerate(items):
            bold = tk.Label(self, text=label, font=f_bold, bg=theme.BG, fg=theme.TEXT,
                            cursor="hand2", padx=self._padx, pady=theme.px(8))
            lbl = tk.Label(self, text=label, font=f_reg, bg=theme.BG, fg=theme.TEXT_2,
                           cursor="hand2", padx=self._padx, pady=theme.px(8))
            bold.grid(row=0, column=col)
            lbl.grid(row=0, column=col)
            for w in (bold, lbl):
                w.bind("<Button-1>", lambda e, k=key: self.on_select(k))
                w.bind("<Enter>", lambda e, k=key: self._hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._hover(k, False))
            self._items[key] = lbl
            self._bold[key] = bold
            self._order.append(key)
            self._fg[key] = _Tween(lbl, (theme.TEXT_2,), lambda v, l=lbl: l.configure(fg=v[0]))
        # Пустая строка под пунктами — место для линии-индикатора
        tk.Frame(self, bg=theme.BG, height=self._ind_h).grid(row=1, column=0, columnspan=len(items), sticky="ew")
        self._ind = tk.Frame(self, bg=theme.ACCENT, height=self._ind_h)
        self._slide = _Tween(self, (0.0, 0.0), self._place_indicator)

    def _hover(self, key, on):
        if key == self.active:
            return
        self._fg[key].to((self.t.TEXT if on else self.t.TEXT_2,), 120)

    def _indicator_target(self, key):
        # Колонка по ширине полужирной метки (она шире), обычная — по центру
        x = 0
        for k in self._order:
            w = self._bold[k].winfo_reqwidth()
            if k == key:
                return float(x + self._padx), float(max(1, w - 2 * self._padx))
            x += w
        return 0.0, 1.0

    def _place_indicator(self, v):
        x, w = v
        y = max(l.winfo_reqheight() for l in self._bold.values())
        self._ind.place(x=int(round(x)), y=y, width=max(1, int(round(w))), height=self._ind_h)

    def set_active(self, key):
        first = self.active is None or not self.winfo_ismapped()
        self.active = key
        for k, lbl in self._items.items():
            if k == key:
                self._bold[k].lift()
                self._fg[k].set((self.t.TEXT,))
            else:
                lbl.lift()
                if first:
                    self._fg[k].set((self.t.TEXT_2,))
                else:
                    self._fg[k].to((self.t.TEXT_2,), 120)
        target = self._indicator_target(key)
        if first:
            self._slide.set(target)
        else:
            self._slide.to(target, 170)


class SearchField(tk.Canvas):
    def __init__(self, master, theme, variable, placeholder="Поиск"):
        self.t = theme
        h = theme.px(40)
        super().__init__(master, height=h, bg=_widget_bg(master, theme.BG), highlightthickness=0, bd=0)
        self.var = variable
        self.placeholder = placeholder
        self._focused = False
        self.entry = tk.Entry(self, textvariable=variable, bd=0, relief="flat", bg=theme.SURFACE_2,
                              fg=theme.TEXT, insertbackground=theme.TEXT, font=theme.f_body,
                              highlightthickness=0, disabledbackground=theme.SURFACE_2)
        self._win = self.create_window(theme.px(40), h / 2, window=self.entry, anchor="w")
        self._ph = tk.Label(self, text=placeholder, bg=theme.SURFACE_2, fg=theme.TEXT_3,
                            font=theme.f_body, cursor="xterm")
        self._ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.entry.bind("<Escape>", lambda e: self.var.set(""))
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.bind("<Configure>", lambda e: self._draw())
        variable.trace_add("write", lambda *a: self._draw())
        # Рамка фокуса проявляется/гаснет плавно (цвета: рамка, иконка лупы)
        self._tween = _Tween(self, (theme.SURFACE_2, theme.TEXT_3), self._apply_colors)

    def _apply_colors(self, v):
        self.itemconfigure("bg", outline=v[0])
        self.itemconfigure("icon", outline=v[1])
        self.itemconfigure("icon_line", fill=v[1])

    def _set_focus(self, f):
        self._focused = f
        self._draw()
        self._tween.to((self.t.ACCENT, self.t.TEXT_2) if f else (self.t.SURFACE_2, self.t.TEXT_3))

    def _draw(self):
        self.delete("bg")
        self.delete("deco")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            return
        px = self.t.px
        border, col = self._tween.cur
        _rounded_rect(self, 1, 1, w - 1, h - 1, px(9), fill=self.t.SURFACE_2,
                      outline=border, width=max(1, px(1.2)), tags="bg")
        self.tag_lower("bg")
        cx, cy, r = px(21), h / 2 - px(1), px(5.5)
        self.create_oval(cx - r, cy - r, cx + r, cy + r, outline=col, width=max(1, px(1.6)),
                         tags=("deco", "icon"))
        self.create_line(cx + r * 0.7, cy + r * 0.7, cx + r * 1.6, cy + r * 1.6, fill=col,
                         width=max(1, px(1.6)), capstyle="round", tags=("deco", "icon_line"))
        self.itemconfigure(self._win, width=max(10, w - px(54)), height=h - px(12))
        if not self.var.get() and not self._focused:
            self._ph.place(x=px(40), y=h / 2, anchor="w")
            self._ph.lift()
        else:
            self._ph.place_forget()


class ScrollFrame(tk.Frame):
    """Вертикально прокручиваемый контейнер с тонким скроллбаром,
    который скрывается, если всё помещается."""

    def __init__(self, master, theme, bg):
        super().__init__(master, bg=bg)
        self.t = theme
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style="Cp.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", self._update)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update()

    def _update(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need:
            self.vsb.grid()
        else:
            self.vsb.grid_remove()
            self.canvas.yview_moveto(0)

    def _bind_wheel(self, on):
        if on:
            self.bind_all("<MouseWheel>", self._wheel)
            self.bind_all("<Button-4>", self._wheel)
            self.bind_all("<Button-5>", self._wheel)
        else:
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")

    def _wheel(self, e):
        if not self.vsb.winfo_ismapped():
            return
        if getattr(e, "num", None) == 4:
            step = -3
        elif getattr(e, "num", None) == 5:
            step = 3
        else:
            step = -int(e.delta / 120) * 3 if e.delta else 0
        self.canvas.yview_scroll(step, "units")


import base64  # noqa: E402 — нужны только интерфейсу (иконки)
import math  # noqa: E402
import struct  # noqa: E402
import zlib  # noqa: E402


# ---------- Векторные иконки ----------
# Иконки описаны как простые фигуры в сетке 24×24 (как SVG) и один раз
# растрируются со сглаживанием ровно в нужный размер под текущий DPI.
# Получается PNG с альфа-каналом (только stdlib: zlib + struct), который
# Tk 8.6 читает сам. Без Pillow, без файлов-ассетов и без обращения к
# диску / Windows Shell ради иконок: всё кэшируется и переиспользуется.

def _rgb01(c):
    r, g, b = _hex_rgb(c)
    return r / 255.0, g / 255.0, b / 255.0


def _seg_dist(px_, py_, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px_ - ax) * dx + (py_ - ay) * dy) / ll))
    ex, ey = px_ - ax - dx * t, py_ - ay - dy * t
    return math.sqrt(ex * ex + ey * ey)


def _rrect_sd(x, y, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    qx = abs(x - cx) - (x1 - x0) / 2 + r
    qy = abs(y - cy) - (y1 - y0) / 2 + r
    out = math.sqrt(max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2)
    return out + min(max(qx, qy), 0.0) - r


class _Raster:
    """Холст size×size, координаты фигур — в единицах сетки 24×24."""

    def __init__(self, size):
        self.n = size
        self.k = size / 24.0
        self.px = [0.0] * (size * size * 4)   # premultiplied RGBA

    def _paint(self, bbox, sd, color, alpha=1.0, clear=False):
        n, k = self.n, self.k
        x0 = max(0, int(math.floor(bbox[0] * k)) - 1)
        y0 = max(0, int(math.floor(bbox[1] * k)) - 1)
        x1 = min(n, int(math.ceil(bbox[2] * k)) + 1)
        y1 = min(n, int(math.ceil(bbox[3] * k)) + 1)
        r, g, b = _rgb01(color) if not clear else (0, 0, 0)
        buf = self.px
        for py_ in range(y0, y1):
            v = (py_ + 0.5) / k
            for px_ in range(x0, x1):
                cov = 0.5 - sd((px_ + 0.5) / k, v) * k
                if cov <= 0.0:
                    continue
                if cov > 1.0:
                    cov = 1.0
                a = cov * alpha
                i = (py_ * n + px_) * 4
                inv = 1.0 - a
                if clear:
                    buf[i] *= inv; buf[i + 1] *= inv; buf[i + 2] *= inv; buf[i + 3] *= inv
                else:
                    buf[i] = r * a + buf[i] * inv
                    buf[i + 1] = g * a + buf[i + 1] * inv
                    buf[i + 2] = b * a + buf[i + 2] * inv
                    buf[i + 3] = a + buf[i + 3] * inv

    # --- фигуры ---
    def rrect(self, x0, y0, x1, y1, r, color, alpha=1.0, clear=False):
        self._paint((x0, y0, x1, y1), lambda x, y: _rrect_sd(x, y, x0, y0, x1, y1, r), color, alpha, clear)

    def rrect_stroke(self, x0, y0, x1, y1, r, w, color, alpha=1.0):
        h = w / 2
        self._paint((x0 - h, y0 - h, x1 + h, y1 + h),
                    lambda x, y: abs(_rrect_sd(x, y, x0, y0, x1, y1, r)) - h, color, alpha)

    def circle(self, cx, cy, rad, color, alpha=1.0, clear=False):
        self._paint((cx - rad, cy - rad, cx + rad, cy + rad),
                    lambda x, y: math.hypot(x - cx, y - cy) - rad, color, alpha, clear)

    def line(self, pts, w, color, alpha=1.0):
        h = w / 2
        segs = list(zip(pts, pts[1:]))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]

        def sd(x, y):
            return min(_seg_dist(x, y, a[0], a[1], b[0], b[1]) for a, b in segs) - h
        self._paint((min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h), sd, color, alpha)

    def poly(self, pts, color, alpha=1.0, clear=False):
        edges = list(zip(pts, pts[1:] + pts[:1]))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]

        def sd(x, y):
            inside = False
            for (ax, ay), (bx, by) in edges:
                if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                    inside = not inside
            d = min(_seg_dist(x, y, ax, ay, bx, by) for (ax, ay), (bx, by) in edges)
            return -d if inside else d
        self._paint((min(xs), min(ys), max(xs), max(ys)), sd, color, alpha, clear)

    def arc(self, cx, cy, rad, a0, a1, w, color, alpha=1.0):
        """Дуга от угла a0 до a1 (градусы, по часовой стрелке, 0 = вправо)."""
        h = w / 2
        p0 = (cx + rad * math.cos(math.radians(a0)), cy + rad * math.sin(math.radians(a0)))
        p1 = (cx + rad * math.cos(math.radians(a1)), cy + rad * math.sin(math.radians(a1)))
        span = (a1 - a0) % 360

        def sd(x, y):
            ang = (math.degrees(math.atan2(y - cy, x - cx)) - a0) % 360
            if ang <= span:
                return abs(math.hypot(x - cx, y - cy) - rad) - h
            return min(math.hypot(x - p0[0], y - p0[1]), math.hypot(x - p1[0], y - p1[1])) - h
        self._paint((cx - rad - h, cy - rad - h, cx + rad + h, cy + rad + h), sd, color, alpha)

    def png(self):
        n = self.n
        rows = []
        buf = self.px
        for y in range(n):
            row = bytearray(b"\x00")
            for x in range(n):
                i = (y * n + x) * 4
                a = buf[i + 3]
                if a <= 0.0:
                    row += b"\x00\x00\x00\x00"
                    continue
                row += bytes((min(255, int(buf[i] / a * 255 + 0.5)), min(255, int(buf[i + 1] / a * 255 + 0.5)),
                              min(255, int(buf[i + 2] / a * 255 + 0.5)), min(255, int(a * 255 + 0.5))))
            rows.append(bytes(row))

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


# --- рисунки (сетка 24×24) ---
_PAGE = "#c9ced8"
_PAGE_FOLD = "#949cab"
_FOLDER_BACK = "#c48b2c"
_FOLDER = "#e9ad42"
_FOLDER_FRONT = "#f3be58"
_FOLDER_GLYPH = "#7a4f0e"


def _draw_folder(R, glyph=None):
    R.rrect(2, 4.5, 11, 9.5, 1.6, _FOLDER_BACK)
    R.rrect(2, 6.5, 22, 20, 2.2, _FOLDER)
    R.rrect(2, 8.5, 22, 20, 2.2, _FOLDER_FRONT)
    g = _FOLDER_GLYPH
    if glyph == "documents":
        R.line([(8, 12.5), (16, 12.5)], 1.5, g)
        R.line([(8, 15.5), (14, 15.5)], 1.5, g)
    elif glyph == "downloads":
        R.line([(12, 11), (12, 17)], 1.6, g)
        R.line([(9, 14.5), (12, 17.3), (15, 14.5)], 1.6, g)
    elif glyph == "pictures":
        R.poly([(7.5, 18), (10.8, 13.2), (13, 16), (14.6, 14.4), (17, 18)], g)
        R.circle(15.3, 11.8, 1.3, g)
    elif glyph == "music":
        R.line([(13.8, 10.8), (13.8, 16.4)], 1.4, g)
        R.line([(13.8, 10.8), (16.4, 11.8)], 1.4, g)
        R.circle(12.2, 16.6, 1.9, g)
    elif glyph == "videos":
        R.poly([(10, 11), (10, 18), (16, 14.5)], g)


def _draw_page(R, accent=None):
    R.poly([(5, 2), (14.5, 2), (19.5, 7), (19.5, 22), (5, 22)], _PAGE)
    R.poly([(14.5, 2), (19.5, 7), (14.5, 7)], _PAGE_FOLD)
    if accent:
        R.poly([(14.5, 2), (19.5, 7), (14.5, 7)], accent, alpha=0.9)


def _draw_link_badge(R):
    R.circle(6.5, 17.5, 5.6, "#000000", clear=True)
    R.circle(6.5, 17.5, 4.6, "#e7ebf2")
    R.line([(4.6, 19.4), (8.6, 15.4)], 1.5, "#20242c")
    R.line([(5.9, 15.2), (8.7, 15.2), (8.7, 18.0)], 1.5, "#20242c")


def _draw_icon(R, name):
    if name.startswith("folder"):
        _draw_folder(R, name.split(":", 1)[1] if ":" in name else None)
        return
    if name == "link":
        _draw_folder(R)
        _draw_link_badge(R)
        return
    if name == "link_file":
        _draw_page(R)
        _draw_link_badge(R)
        return
    if name == "app":
        R.rrect(2.5, 3.5, 21.5, 20.5, 3, "#4c8dff")
        R.rrect(2.5, 3.5, 21.5, 8, 3, "#7aaaff")
        R.rrect(2.5, 6, 21.5, 8, 0, "#7aaaff")
        R.rrect(6, 11, 18, 17.5, 1.5, "#dce8ff")
        return
    if name == "image":
        R.rrect(2.5, 4, 21.5, 20, 2.5, "#3b82d6")
        R.poly([(4.5, 18), (10, 10.5), (13.5, 15), (15.6, 12.8), (19.5, 18)], "#d9ecff")
        R.circle(16.2, 8.6, 1.8, "#ffd66b")
        return
    _draw_page(R, {"pdf": "#e5484d", "archive": "#d9a441", "audio": "#a46cf0", "video": "#e0567a",
                   "code": "#3fb68b", "system": "#7d8596"}.get(name))
    if name == "text":
        for y, x1 in ((10.5, 16.5), (13.5, 16.5), (16.5, 13.5)):
            R.line([(8, y), (x1, y)], 1.3, "#6b7383")
    elif name == "pdf":
        R.rrect(3, 12, 17, 19.5, 1.6, "#e5484d")
        R.line([(6, 15.8), (14, 15.8)], 1.4, "#ffffff", alpha=0.95)
    elif name == "archive":
        for y in (4, 7, 10, 13):
            R.rrect(10, y, 12.4, y + 1.6, 0.5, "#6b5a3a")
        R.rrect(9, 15, 13.4, 19.5, 1.2, "#d9a441")
        R.rrect(10.2, 17, 12.2, 18.2, 0.4, "#5b4a2a")
    elif name == "audio":
        R.line([(13.5, 10), (13.5, 16.6)], 1.5, "#a46cf0")
        R.line([(13.5, 10), (16.6, 11.2)], 1.5, "#a46cf0")
        R.circle(11.6, 16.8, 2.2, "#a46cf0")
    elif name == "video":
        R.rrect(7, 10.5, 17.5, 18.5, 1.8, "#e0567a")
        R.poly([(10.8, 12.4), (10.8, 16.6), (14.4, 14.5)], "#ffffff")
    elif name == "code":
        R.line([(10.2, 11), (7.6, 14.2), (10.2, 17.4)], 1.5, "#3fb68b")
        R.line([(14.3, 11), (16.9, 14.2), (14.3, 17.4)], 1.5, "#3fb68b")
    elif name == "system":
        cx, cy = 12.2, 14.5
        teeth = []
        for i in range(16):
            ang = math.radians(i * 22.5)
            rad = 5.0 if i % 2 == 0 else 3.6
            teeth.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        R.poly(teeth, "#7d8596")
        R.circle(cx, cy, 3.9, "#7d8596")
        R.circle(cx, cy, 1.6, "#000000", clear=True)


def _draw_nav(R, name, color):
    w = 2.0
    if name == "back":
        R.line([(19, 12), (5.5, 12)], w, color)
        R.line([(11, 6.5), (5.5, 12), (11, 17.5)], w, color)
    elif name == "forward":
        R.line([(5, 12), (18.5, 12)], w, color)
        R.line([(13, 6.5), (18.5, 12), (13, 17.5)], w, color)
    elif name == "up":
        R.line([(12, 19), (12, 5.5)], w, color)
        R.line([(6.5, 11), (12, 5.5), (17.5, 11)], w, color)
    elif name == "refresh":
        a0, a1 = 25.0, 300.0
        R.arc(12, 12, 7, a0, a1, w, color)
        ea = math.radians(a1)
        ex, ey = 12 + 7 * math.cos(ea), 12 + 7 * math.sin(ea)
        tx, ty = -math.sin(ea), math.cos(ea)          # касательная по ходу дуги
        nx, ny = math.cos(ea), math.sin(ea)           # нормаль наружу
        tip = (ex + tx * 3.2, ey + ty * 3.2)
        R.poly([tip, (ex + nx * 3.0 - tx * 0.6, ey + ny * 3.0 - ty * 0.6),
                (ex - nx * 3.0 - tx * 0.6, ey - ny * 3.0 - ty * 0.6)], color)


def _draw_checkbox(R, p, theme, enabled=True):
    if not enabled:
        R.rrect_stroke(3, 3, 21, 21, 4.5, 1.7, theme.SURFACE_4)
        return
    if p < 1.0:
        R.rrect_stroke(3, 3, 21, 21, 4.5, 1.7, _blend(theme.TEXT_3, theme.ACCENT, p))
    if p > 0.0:
        R.rrect(2.15, 2.15, 21.85, 21.85, 5.2, theme.ACCENT, alpha=p)
    if p > 0.3:
        R.line([(7, 12.4), (10.4, 15.8), (17.2, 8.6)], 2.3, "#ffffff", alpha=min(1.0, (p - 0.3) / 0.7))


# Известные папки — по имени (англ. и рус.), файлы — по расширению
_KNOWN_FOLDERS = {
    "documents": "documents", "документы": "documents", "my documents": "documents",
    "downloads": "downloads", "загрузки": "downloads",
    "pictures": "pictures", "изображения": "pictures", "картинки": "pictures", "photos": "pictures",
    "music": "music", "музыка": "music",
    "videos": "videos", "видео": "videos", "movies": "videos",
}
_EXT_KIND = {}
for _kind, _exts in {
    "app": "exe msi msix appx bat cmd com scr lnk",
    "text": "txt md log rtf doc docx odt csv ini cfg conf",
    "pdf": "pdf",
    "image": "png jpg jpeg gif bmp webp ico svg tif tiff heic psd raw",
    "archive": "zip rar 7z tar gz bz2 xz cab iso tgz zst",
    "audio": "mp3 wav flac ogg m4a aac wma opus mid",
    "video": "mp4 mkv avi mov wmv webm flv m4v mpg mpeg ts",
    "code": "py pyw js ts jsx tsx html htm css json xml yaml yml c cpp h hpp cs java go rs rb php sh ps1 sql lua kt swift",
    "system": "sys dll drv ocx cpl msc reg dat bin tmp db efi mui cat inf",
}.items():
    for _e in _exts.split():
        _EXT_KIND["." + _e] = _kind


class IconSet:
    """Все растровые иконки интерфейса. Каждая рисуется один раз на
    (имя, размер, цвет) и дальше берётся из кэша."""

    def __init__(self, theme):
        self.t = theme
        self._cache = {}
        self.list_size = theme.px(18)

    def _image(self, key, draw, size):
        img = self._cache.get(key)
        if img is None:
            R = _Raster(size)
            draw(R)
            img = tk.PhotoImage(master=self.t.root, data=base64.b64encode(R.png()).decode("ascii"))
            self._cache[key] = img
        return img

    # --- список файлов / программ ---
    def get(self, name):
        return self._image(("list", name), lambda R: _draw_icon(R, name), self.list_size)

    def __getitem__(self, kind):
        return self.get({"dir": "folder", "file": "file", "link": "link", "app": "app",
                         "folder": "folder"}.get(kind, kind))

    def for_entry(self, name, kind):
        """Иконка по имени и типу записи — без обращения к диску."""
        if kind == "dir":
            glyph = _KNOWN_FOLDERS.get(name.casefold())
            return self.get("folder:" + glyph if glyph else "folder")
        ext = os.path.splitext(name)[1].casefold()
        if kind == "link":
            return self.get("link_file" if ext in _EXT_KIND else "link")
        return self.get(_EXT_KIND.get(ext, "file"))

    # --- навигация ---
    def nav(self, name, color, size):
        return self._image(("nav", name, color, size), lambda R: _draw_nav(R, name, color), size)

    # --- чекбокс: кадры заполнения 0..1 ---
    CHECK_FRAMES = 6

    def checkbox_frames(self, size):
        n = self.CHECK_FRAMES
        return [self._image(("check", i, size), lambda R, p=i / (n - 1): _draw_checkbox(R, p, self.t), size)
                for i in range(n)]

    def checkbox_disabled(self, size):
        return self._image(("check_off", size), lambda R: _draw_checkbox(R, 0, self.t, enabled=False), size)


def _make_icons(theme):
    icons = IconSet(theme)
    theme.iconset = icons
    return icons


class FadingProgress(ttk.Progressbar):
    """Тонкая полоса «идёт работа». Появляется и исчезает сразу: плавное
    проявление требовало менять стиль ttk на каждом кадре, а это
    перерисовывает все ttk-виджеты (включая большую таблицу) — дёргалось."""

    def __init__(self, master, theme):
        self.t = theme
        super().__init__(master, mode="determinate", style="Cp.Horizontal.TProgressbar",
                         maximum=100, value=0)

    def start_busy(self):
        if str(self.cget("mode")) != "indeterminate":
            self.configure(mode="indeterminate")
            self.start(14)

    def stop_busy(self):
        self.stop()
        self.configure(mode="determinate", value=0)


class EmptyState(tk.Frame):
    """Сообщение поверх пустой таблицы: «Папка пуста», «Нет доступа» и т.п."""

    def __init__(self, master, theme):
        super().__init__(master, bg=theme.SURFACE)
        self.t = theme
        self.title = tk.Label(self, bg=theme.SURFACE, fg=theme.TEXT_2, font=theme.f_body_semi)
        self.sub = tk.Label(self, bg=theme.SURFACE, fg=theme.TEXT_3, font=theme.f_small,
                            wraplength=theme.px(460), justify="center")
        self.title.pack()
        self.sub.pack(pady=(theme.px(4), 0))

    def show(self, title, sub=""):
        self.title.configure(text=title)
        self.sub.configure(text=sub)
        self.place(relx=0.5, rely=0.45, anchor="center")
        self.lift()

    def hide(self):
        self.place_forget()


class TableCard(tk.Frame):
    """Карточка: тонкий прогресс сверху, таблица со стилизованным скроллбаром,
    hover строк, empty state."""

    def __init__(self, master, theme, columns, headings, widths):
        super().__init__(master, bg=theme.SURFACE)
        self.t = theme
        self.progress = FadingProgress(self, theme)
        self.progress.pack(fill="x")
        body = tk.Frame(self, bg=theme.SURFACE)
        body.pack(fill="both", expand=True, padx=(theme.px(6), 0), pady=(0, theme.px(6)))
        # height — лишь минимальная «просьба» (в строках); таблица всё равно
        # растягивается на всё место. Иначе при 150% на 1080p она забирала
        # высоту у нижней панели с кнопками.
        self.tree = ttk.Treeview(body, columns=columns, show="tree headings", selectmode="browse",
                                 style="Cp.Treeview", height=5)
        anchors = {"#0": "w", "size": "e"}
        for col, text in headings.items():
            self.tree.heading(col, text=text, anchor=anchors.get(col, "center"))
        self.tree.column("#0", width=theme.px(420), minwidth=theme.px(160), stretch=True)
        for col, w in widths.items():
            self.tree.column(col, width=theme.px(w), minwidth=theme.px(70), stretch=False,
                             anchor=anchors.get(col, "center"))
        self.vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview, style="Cp.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=self._on_yscroll)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns", padx=(theme.px(4), theme.px(4)))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("hover", background=theme.ROW_HOVER)
        self.tree.tag_configure("muted", foreground=theme.TEXT_3)
        self._hover_row = None
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda e: self._set_hover(None))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._set_hover(self._hover_row), add="+")
        # Выделение строки мгновенное: анимация цвета выделения меняла бы
        # стиль ttk и перерисовывала всю таблицу на каждом кадре.
        self.empty = EmptyState(self, theme)
        self._busy = False

    def _on_yscroll(self, first, last):
        self.vsb.set(first, last)
        # Скроллбар показывается только когда действительно есть что прокручивать
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.vsb.grid_remove()
        else:
            self.vsb.grid()

    def _on_motion(self, e):
        self._set_hover(self.tree.identify_row(e.y) or None)

    def _set_hover(self, row):
        prev = self._hover_row
        if prev and self.tree.exists(prev):
            tags = [t for t in self.tree.item(prev, "tags") if t != "hover"]
            self.tree.item(prev, tags=tags)
        self._hover_row = row
        if row and self.tree.exists(row) and row not in self.tree.selection():
            tags = list(self.tree.item(row, "tags"))
            if "hover" not in tags:
                tags.append("hover")
            self.tree.item(row, tags=tags)

    def clear(self):
        self._hover_row = None
        self.tree.delete(*self.tree.get_children())
        self.empty.hide()

    def set_busy(self, busy):
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self.progress.start_busy()
        else:
            self.progress.stop_busy()


class _SortMixin:
    """Сортировка таблицы кликом по заголовку. Неизвестные размеры всегда внизу."""

    _SORT_LABELS = {}

    def _init_sort(self, default=("size", True)):
        self._sort = default
        for col in self._SORT_LABELS:
            self.card.tree.heading(col, command=lambda c=col: self._on_sort_click(c))
        self._update_sort_headings()

    def _on_sort_click(self, col):
        key, rev = self._sort
        self._sort = (col, not rev) if key == col else (col, col == "size")
        self._update_sort_headings()
        self._apply_sort()

    def _update_sort_headings(self):
        key, rev = self._sort
        for col, label in self._SORT_LABELS.items():
            arrow = (" ↓" if rev else " ↑") if col == key else ""
            self.card.tree.heading(col, text=label + arrow)

    def _sort_key_func(self, info_of):
        key, rev = self._sort
        if key == "size":
            def k(iid):
                s = info_of(iid).get("size")
                return (s is None, -(s or 0) if rev else (s or 0))
            return k, False
        if key == "type":
            return (lambda iid: (info_of(iid).get("type_label", ""), info_of(iid)["name"].casefold())), rev
        return (lambda iid: info_of(iid)["name"].casefold()), rev


class DiskTab(tk.Frame, _SortMixin):
    _SORT_LABELS = {"#0": "Имя", "size": "Размер", "type": "Тип"}

    def __init__(self, parent, app, open_apps_tab=None):
        super().__init__(parent, bg=app.theme.BG)
        self.app = app
        self.t = app.theme
        self.icons = app.icons
        self.executor = app.disk_pool
        self.current_path = USER_PROFILE
        self.back_stack = []
        self.forward_stack = []
        self.scan_token = 0
        self.row_by_path = {}        # iid -> (путь, это_папка)
        self._info = {}              # iid -> {"name", "kind", "size", "type_label"}
        self._current_futures = []
        self._pending = 0
        self._done = 0
        self._closed = False
        self._deleting = False
        self.status_var = tk.StringVar(value="")
        self._build_ui()
        self._init_sort(("size", True))
        self.navigate_to(self.current_path, record_history=False)

    # ----- построение интерфейса -----
    def _build_ui(self):
        t, px = self.t, self.t.px
        top = tk.Frame(self, bg=t.BG)
        top.pack(fill="x")
        nav_h = px(38)
        self.btn_back = FlatButton(top, t, "", self.go_back, "icon", icon="back", height=nav_h,
                                   tooltip="Назад (Alt+←)")
        self.btn_fwd = FlatButton(top, t, "", self.go_forward, "icon", icon="forward", height=nav_h,
                                  tooltip="Вперёд (Alt+→)")
        self.btn_up = FlatButton(top, t, "", self.go_up, "icon", icon="up", height=nav_h,
                                 tooltip="Вверх — родительская папка (Backspace)")
        self.btn_refresh = FlatButton(top, t, "", self.refresh, "icon", icon="refresh", height=nav_h,
                                      tooltip="Обновить и пересчитать размеры (F5)")
        for b in (self.btn_back, self.btn_fwd, self.btn_up, self.btn_refresh):
            b.pack(side="left", padx=(0, px(3)))
        self.crumbs = tk.Frame(top, bg=t.BG)
        self.crumbs.pack(side="left", fill="x", expand=True, padx=(px(14), 0))

        places = tk.Frame(self, bg=t.BG)
        places.pack(fill="x", pady=(px(10), px(12)))
        tk.Label(places, text="Перейти:", bg=t.BG, fg=t.TEXT_3, font=t.f_small).pack(side="left", padx=(0, px(6)))
        chip_h = px(32)
        FlatButton(places, t, "Профиль", lambda: self.navigate_to(USER_PROFILE), "ghost",
                   height=chip_h, font=t.f_small_semi,
                   tooltip=USER_PROFILE).pack(side="left", padx=(0, px(2)))
        type_names = {DRIVE_REMOVABLE: "съёмный диск", DRIVE_FIXED: "локальный диск",
                      DRIVE_REMOTE: "сетевой диск", DRIVE_CDROM: "CD/DVD", DRIVE_RAMDISK: "RAM-диск"}
        for drive in get_available_drives():
            dtype = get_drive_type(drive)
            FlatButton(places, t, drive.rstrip("\\"), lambda d=drive: self.navigate_to(d), "ghost",
                       height=chip_h, font=t.f_small_semi,
                       tooltip=type_names.get(dtype, "диск")).pack(side="left", padx=(0, px(2)))
        FlatButton(places, t, "Выбрать папку…", self.choose_folder, "ghost", height=chip_h,
                   font=t.f_small_semi).pack(side="left", padx=(px(4), 0))

        self.card = TableCard(self, t, columns=("size", "type"),
                              headings={"#0": "Имя", "size": "Размер", "type": "Тип"},
                              widths={"size": 130, "type": 120})
        self.card.pack(fill="both", expand=True)
        self.tree = self.card.tree
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", self.on_double_click)
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<BackSpace>", lambda e: self.go_up())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_actions(), add="+")

        bottom = tk.Frame(self, bg=t.BG)
        bottom.pack(fill="x", pady=(px(14), 0))
        tk.Label(bottom, textvariable=self.status_var, bg=t.BG, fg=t.TEXT_2, font=t.f_status,
                 anchor="w").pack(side="left", fill="x", expand=True)
        act_h = px(38)
        self.btn_delete = FlatButton(bottom, t, "Удалить", self.delete_selected, "danger",
                                     tooltip="Удалить без корзины (Delete)", height=act_h,
                                     font=t.f_button_lg)
        self.btn_delete.pack(side="right")
        self.btn_reveal = FlatButton(bottom, t, "Показать в Проводнике", self.open_in_explorer, "secondary",
                                     height=act_h, font=t.f_button_lg)
        self.btn_reveal.pack(side="right", padx=(0, px(8)))
        # Подсказка о выбранном элементе — отдельно от строки статуса, чтобы
        # не затирать прогресс подсчёта размеров
        self.sel_hint = tk.Label(bottom, text="", bg=t.BG, fg=t.WARN, font=t.f_status, anchor="e")
        self.sel_hint.pack(side="right", padx=(0, px(12)))
        self._update_actions()

    def on_show(self):
        pass

    # ----- хлебные крошки -----
    def _render_breadcrumbs(self):
        t = self.t
        for w in self.crumbs.winfo_children():
            w.destroy()
        parts = []
        p = os.path.normpath(self.current_path)
        while True:
            head, tail = os.path.split(p)
            if tail:
                parts.append((tail, p))
                p = head
            else:
                if head:
                    parts.append((head.rstrip("\\/") or head, head))
                break
        parts.reverse()
        if len(parts) > 5:
            parts = parts[:1] + [("…", None)] + parts[-3:]
        for i, (label, target) in enumerate(parts):
            if i:
                tk.Label(self.crumbs, text="›", bg=t.BG, fg=t.TEXT_3, font=t.f_crumb).pack(side="left", padx=t.px(5))
            last = i == len(parts) - 1
            lbl = tk.Label(self.crumbs, text=label, bg=t.BG, font=t.f_crumb_last if last else t.f_crumb,
                           fg=t.TEXT if last else t.TEXT_2, cursor="arrow" if (last or not target) else "hand2")
            lbl.pack(side="left")
            if target and not last:
                lbl.bind("<Button-1>", lambda e, p=target: self.navigate_to(p))
                lbl.bind("<Enter>", lambda e, l=lbl: l.configure(fg=t.TEXT))
                lbl.bind("<Leave>", lambda e, l=lbl: l.configure(fg=t.TEXT_2))

    # ----- навигация и сканирование -----
    def _is_stale_factory(self, token):
        return lambda: token != self.scan_token or self._closed

    def _cancel_running(self):
        for fut in self._current_futures:
            fut.cancel()
        self._current_futures = []

    def navigate_to(self, path, record_history=True, force=False):
        if not path or self._closed:
            return
        path = os.path.normpath(path)
        if (record_history and self.current_path
                and os.path.normcase(path) != os.path.normcase(self.current_path)):
            self.back_stack.append(self.current_path)
            self.forward_stack.clear()

        # Старое сканирование больше не нужно: ещё не начатые задачи отменяем,
        # уже идущие остановятся на ближайшей проверке токена.
        self._cancel_running()
        self.scan_token += 1
        token = self.scan_token

        self.current_path = path
        self._render_breadcrumbs()
        self.card.clear()
        self.row_by_path = {}
        self._info = {}
        self._pending = self._done = 0
        self.status_var.set("Открываю папку…")
        self.card.set_busy(True)
        self._update_actions()

        state = {"handled": False}

        def list_worker():
            try:
                state["data"] = _list_directory(path)
            except OSError as e:
                state["error"] = e
            except Exception as e:  # неожиданные ошибки тоже должны дойти до UI
                state["error"] = e
            self.app.ui_call(self._on_listed, token, state, force)

        th = threading.Thread(target=list_worker, daemon=True, name="cp-list")
        th.start()
        # Обычная локальная папка читается за миллисекунды — тогда заполняем
        # таблицу сразу, без мигания пустого состояния. Медленный (сетевой)
        # путь не блокирует окно: дальше всё придёт асинхронно.
        th.join(0.12)
        if not th.is_alive():
            self._on_listed(token, state, force)

    def _on_listed(self, token, state, force):
        if state.get("handled") or token != self.scan_token or self._closed:
            return
        state["handled"] = True
        err = state.get("error")
        if err is not None:
            self.card.set_busy(False)
            if isinstance(err, PermissionError):
                title, sub = "Нет доступа к этой папке", "Windows не разрешает читать её содержимое."
            elif isinstance(err, FileNotFoundError):
                title, sub = "Папка не найдена", "Возможно, она была удалена или диск отключён."
            else:
                title, sub = "Не удалось открыть папку", str(err)
            self.card.empty.show(title, sub)
            self.status_var.set(title)
            return

        entries = state.get("data") or []
        if not entries:
            self.card.set_busy(False)
            self.card.empty.show("Папка пуста")
            self.status_var.set("Папка пуста")
            return

        is_stale = self._is_stale_factory(token)
        used_cache = False
        futures = []
        type_labels = {"dir": "Папка", "file": "Файл", "link": "Ссылка"}
        for name, child, kind, size in sorted(entries, key=lambda e: e[0].casefold()):
            if kind == "dir":
                cached = None if force else _cache_get(child)
                if cached is not None:
                    size, used_cache = cached, True
            label = type_labels[kind]
            if kind == "link":
                size_text = "—"
            elif size is None and kind == "dir":
                size_text = "…"
            else:
                size_text = format_size(size) if size is not None else "—"
            iid = self.tree.insert("", "end", text="  " + name, image=self.icons.for_entry(name, kind),
                                   values=(size_text, label))
            self.row_by_path[iid] = (child, kind == "dir")
            self._info[iid] = {"name": name, "kind": kind, "size": size, "type_label": label}
            if kind == "dir" and size is None:
                try:
                    fut = self.executor.submit(get_size, child, None, 0, is_stale, True, force)
                except RuntimeError:  # пул уже остановлен (окно закрывается)
                    return
                fut.add_done_callback(lambda f, iid=iid: self.app.ui_call(self._on_size_done, token, iid, f))
                futures.append(fut)

        self._current_futures = futures
        self._pending = len(futures)
        self._done = 0
        if not futures:
            self._finish(token, from_cache=used_cache)
        else:
            self.status_var.set(f"Считаю размеры • 0 из {self._pending}")
            self.after(20000, lambda: self._slow_hint(token))

    def _on_size_done(self, token, iid, fut):
        if token != self.scan_token or self._closed or fut.cancelled():
            return
        try:
            size = fut.result()
        except Exception:
            size = None
        info = self._info.get(iid)
        if info is None or not self.tree.exists(iid):
            return
        info["size"] = size
        self.tree.set(iid, "size", format_size(size) if size is not None else "—")
        self._done += 1
        if self._done >= self._pending:
            self._finish(token, from_cache=False)
        else:
            self.status_var.set(f"Считаю размеры • {self._done} из {self._pending}")

    def _slow_hint(self, token):
        if token == self.scan_token and not self._closed and self._done < self._pending:
            self.status_var.set(f"Считаю размеры • {self._done} из {self._pending} • "
                                "большие папки считаются дольше")

    def _finish(self, token, from_cache):
        if token != self.scan_token:
            return
        self.card.set_busy(False)
        self._current_futures = []
        self._apply_sort()
        count = len(self._info)
        total = sum(i["size"] for i in self._info.values() if i["kind"] != "link" and i["size"])
        text = f"{count} {plural(count, 'объект', 'объекта', 'объектов')} • {format_size(total)}"
        if from_cache:
            text += " • из кэша"
        self.status_var.set(text)

    def _apply_sort(self):
        func, rev = self._sort_key_func(lambda iid: self._info[iid])
        children = [iid for iid in self.tree.get_children() if iid in self._info]
        for i, iid in enumerate(sorted(children, key=func, reverse=rev)):
            self.tree.move(iid, "", i)

    # ----- действия -----
    def _selected(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in self._info:
            return None, None, None
        iid = sel[0]
        path, _ = self.row_by_path.get(iid, (None, None))
        return iid, path, self._info[iid]

    def _update_actions(self):
        self.btn_back.set_enabled(bool(self.back_stack))
        self.btn_fwd.set_enabled(bool(self.forward_stack))
        self.btn_up.set_enabled(_parent_path(self.current_path) is not None)
        iid, path, info = self._selected()
        protected = bool(path) and is_protected_path(path)
        self.btn_delete.set_enabled(bool(path) and not self._deleting and not protected)
        self.btn_reveal.set_enabled(True)
        self.sel_hint.configure(text="Системная папка — удаление недоступно" if protected else "")

    def refresh(self):
        self.navigate_to(self.current_path, record_history=False, force=True)

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.current_path)
        if folder:
            self.navigate_to(folder)

    def go_back(self):
        if not self.back_stack:
            return
        self.forward_stack.append(self.current_path)
        self.navigate_to(self.back_stack.pop(), record_history=False)

    def go_forward(self):
        if not self.forward_stack:
            return
        self.back_stack.append(self.current_path)
        self.navigate_to(self.forward_stack.pop(), record_history=False)

    def go_up(self):
        parent = _parent_path(self.current_path)
        if parent:
            self.navigate_to(parent)

    def on_double_click(self, event=None):
        iid, path, info = self._selected()
        if not path or info["kind"] == "file":
            return
        if is_game_library_folder(self.current_path) and info["kind"] == "dir":
            # Внутри steamapps\common и т.п. — это уже сами игры, заходить
            # внутрь бессмысленно (там только файлы движка).
            self.status_var.set(f"«{info['name']}» — папка игры целиком. "
                                "Её можно удалить кнопкой «Удалить».")
            return
        self.navigate_to(path)

    def open_in_explorer(self):
        iid, path, info = self._selected()
        target = path or self.current_path
        if not os.path.lexists(target):
            messagebox.showinfo("Не найдено", "Этот объект больше не существует.")
            return
        _reveal_in_explorer(target, select=bool(path and info["kind"] != "dir"))

    def delete_selected(self):
        if self._deleting:
            return  # защита от повторного нажатия во время удаления
        iid, path, info = self._selected()
        if not path:
            messagebox.showinfo("Ничего не выбрано", "Выберите файл или папку в списке.")
            return
        name = info["name"]

        # Проверяем защиту ДО диалога подтверждения
        if is_protected_for_delete(path):
            messagebox.showerror(
                "Удаление запрещено",
                f"«{name}» — это системная папка Windows, и её удаление через "
                "эту программу заблокировано намеренно, чтобы не сломать систему.\n\n"
                f"Путь: {path}\n\n"
                "Отдельные программы удаляются через вкладку «Программы» "
                "(официальным деинсталлятором), а не отсюда.",
            )
            return

        if not os.path.lexists(path):
            messagebox.showinfo("Уже удалено", f"«{name}» больше не существует. Список будет обновлён.")
            self.refresh()
            return

        # Отпечаток объекта на момент подтверждения: удалится только он.
        # Размер из кэша — лишь подпись в окне, а не разрешение на удаление.
        identity = path_identity(path)
        if IS_WINDOWS and identity is None:
            messagebox.showerror("Удаление невозможно",
                                 f"Не удалось открыть «{name}» для проверки. Возможно, нет доступа "
                                 "или объект занят.\n\nНичего не удалено.")
            return
        real_note = ""
        if identity and identity[2]:
            info_kind_link = True
        else:
            info_kind_link = info["kind"] == "link"
            real = identity[3] if identity else None
            if real and _canonical_path(real) != _canonical_path(path):
                if is_protected_path(real):
                    messagebox.showerror("Удаление запрещено",
                                         f"«{name}» на самом деле находится в защищённой папке:\n{real}\n\n"
                                         "Ничего не удалено.")
                    return
                real_note = (f"\n\nВнимание: путь проходит через ссылку. Фактически будет удалено:\n{real}")
        size = info.get("size")
        size_label = format_size(size) if size is not None else "размер не посчитан"
        link_note = ("\n\nЭто ссылка на другую папку. Будет удалена только сама ссылка — "
                     "файлы, на которые она указывает, не затрагиваются.") if info_kind_link else ""
        confirm = messagebox.askyesno(
            "Удалить насовсем?",
            f"Удалить «{name}» ({size_label})?\n\n"
            f"Полный путь: {path}{real_note}\n\n"
            "Это удаляет файлы напрямую, минуя корзину — отменить и восстановить "
            f"будет невозможно.{link_note}",
            icon="warning",
        )
        if not confirm:
            return

        self._deleting = True
        self._update_actions()
        self.card.set_busy(True)
        self.status_var.set(f"Удаление «{name}»…")

        def worker():
            try:
                if identity is not None:
                    result = ("ok",) + delete_path_resilient(path, expected_identity=identity)
                else:
                    result = ("ok",) + delete_path_resilient(path)
            except ProtectedPathError:
                result = ("protected",)
            except PathChangedError:
                result = ("changed",)
            except Exception as e:
                result = ("error", e)
            invalidate_size_cache(path)
            self.app.ui_call(self._on_deleted, name, result)

        threading.Thread(target=worker, daemon=True, name="cp-delete").start()

    def _on_deleted(self, name, result):
        self._deleting = False
        if self._closed:
            return
        self.navigate_to(self.current_path, record_history=False)
        kind = result[0]
        if kind == "protected":
            messagebox.showerror("Удаление запрещено", "Это защищённая системная папка — удаление отменено.")
        elif kind == "changed":
            messagebox.showerror("Удаление отменено",
                                 f"«{name}» изменился после подтверждения (был заменён, перемещён или "
                                 "стал ссылкой). Ничего не удалено — проверьте список и повторите.")
        elif kind == "error":
            messagebox.showerror("Ошибка удаления", f"Не удалось удалить «{name}»:\n{result[1]}")
        elif not result[1]:
            messagebox.showwarning(
                "Удалено не всё",
                f"Удалено частично. {len(result[2])} файл(ов) заняты другой программой "
                "или недоступны.\n\nЗакройте программы, которые могут их использовать, "
                "и попробуйте снова — или удалите после перезагрузки.")


class AppsTab(tk.Frame, _SortMixin):
    _SORT_LABELS = {"#0": "Название", "size": "Размер", "type": "Тип"}

    def __init__(self, parent, app, open_in_disk_tab=None):
        super().__init__(parent, bg=app.theme.BG)
        self.app = app
        self.t = app.theme
        self.icons = app.icons
        self.open_in_disk_tab = open_in_disk_tab
        self.executor = app.bg_pool
        self.extra_folders = []
        self.row_meta = {}
        self.sorted_order = []
        self.scan_token = 0
        self._current_futures = []
        self._pending = self._done = 0
        self._closed = False
        self._started = False
        self._recent_uninstall = {}
        self.status_var = tk.StringVar(value="")
        self.search_var = tk.StringVar()
        self._build_ui()
        self._init_sort(("size", True))

    def _build_ui(self):
        t, px = self.t, self.t.px
        top = tk.Frame(self, bg=t.BG)
        top.pack(fill="x")
        self.search = SearchField(top, t, self.search_var, "Поиск программ и игр")
        self.search.pack(side="left", fill="x", expand=True)
        self.search_var.trace_add("write", lambda *a: self.filter_rows())
        FlatButton(top, t, "", lambda: self.scan_all(force=True), "icon", icon="refresh",
                   tooltip="Обновить список и пересчитать размеры (F5)",
                   height=px(40)).pack(side="right", padx=(px(6), 0))
        FlatButton(top, t, "Добавить папку с играми", self.add_folder, "secondary",
                   height=px(40), font=t.f_button_lg).pack(side="right", padx=(px(8), 0))

        hint = tk.Label(self, text="Не видите игру из Steam, Epic или другого лаунчера? Добавьте папку его "
                                   "библиотеки (например, Steam\\steamapps\\common). Двойной клик открывает "
                                   "папку на вкладке «Диск».",
                        bg=t.BG, fg=t.TEXT_3, font=t.f_small, anchor="w", justify="left",
                        wraplength=px(900))
        hint.pack(fill="x", pady=(px(10), px(12)))
        _auto_wrap(hint)

        self.card = TableCard(self, t, columns=("size", "type"),
                              headings={"#0": "Название", "size": "Размер", "type": "Тип"},
                              widths={"size": 130, "type": 130})
        self.card.pack(fill="both", expand=True)
        self.tree = self.card.tree
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", self.on_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_actions(), add="+")

        bottom = tk.Frame(self, bg=t.BG)
        bottom.pack(fill="x", pady=(px(14), 0))
        tk.Label(bottom, textvariable=self.status_var, bg=t.BG, fg=t.TEXT_2, font=t.f_status,
                 anchor="w").pack(side="left", fill="x", expand=True)
        act_h = px(38)
        self.btn_uninstall = FlatButton(bottom, t, "Удалить…", self.uninstall_selected, "danger",
                                        tooltip="Запустить деинсталлятор программы", height=act_h,
                                        font=t.f_button_lg)
        self.btn_uninstall.pack(side="right")
        self.btn_reveal = FlatButton(bottom, t, "Открыть в Проводнике", self.open_selected_folder, "secondary",
                                     height=act_h, font=t.f_button_lg)
        self.btn_reveal.pack(side="right", padx=(0, px(8)))
        self.btn_disk = FlatButton(bottom, t, "Показать на диске", self.on_double_click, "secondary",
                                   height=act_h, font=t.f_button_lg)
        self.btn_disk.pack(side="right", padx=(0, px(8)))
        self._update_actions()

    def on_show(self):
        if not self._started:
            self.scan_all()

    def add_folder(self):
        folder = filedialog.askdirectory(title="Папка библиотеки игр (например, Steam\\steamapps\\common)")
        if not folder:
            return
        folder = os.path.normpath(folder)
        if _cache_key(folder) not in {_cache_key(f) for f in self.extra_folders}:
            self.extra_folders.append(folder)
        self.scan_all()

    def scan_all(self, force=False):
        if self._closed:
            return
        self._started = True
        for fut in self._current_futures:
            fut.cancel()
        self._current_futures = []
        self.scan_token += 1
        token = self.scan_token
        self.card.clear()
        self.row_meta = {}
        self.sorted_order = []
        self._pending = self._done = 0
        self.status_var.set("Ищу установленные программы…")
        self.card.set_busy(True)
        self._update_actions()
        extra = list(self.extra_folders)

        def worker():
            try:
                candidates, err = _collect_app_candidates(extra), None
            except Exception as e:
                candidates, err = [], e
            self.app.ui_call(self._on_candidates, token, candidates, force, err)

        threading.Thread(target=worker, daemon=True, name="cp-apps").start()

    def _on_candidates(self, token, candidates, force, err):
        if token != self.scan_token or self._closed:
            return
        if err is not None:
            self.card.set_busy(False)
            self.card.empty.show("Не удалось получить список программ", str(err))
            self.status_var.set("Ошибка чтения списка программ")
            return
        if not candidates:
            self.card.set_busy(False)
            self.card.empty.show("Программы не найдены")
            self.status_var.set("Программы не найдены")
            return

        is_stale = lambda: token != self.scan_token or self._closed
        futures = []
        used_cache = False
        # Несколько записей реестра часто указывают на ОДНУ папку (компоненты
        # NVIDIA, Visual C++ и т.п.). Раньше каждая запускала свой рекурсивный
        # подсчёт той же папки параллельно, а итог суммировал её несколько раз.
        to_scan = {}  # ключ пути -> (путь, [iid, ...])
        for name, path, uninstall, kind in candidates:
            label = "Программа" if kind == "app" else "Папка"
            size = None
            key = _cache_key(path) if path else None
            if path and not force:
                size = _cache_get(path)
                used_cache = used_cache or size is not None
            size_text = "—" if not path else (format_size(size) if size is not None else "…")
            iid = self.tree.insert("", "end", text="  " + name, image=self.icons[kind],
                                   values=(size_text, label))
            self.row_meta[iid] = {"name": name, "path": path, "uninstall": uninstall,
                                  "kind": kind, "size": size, "type_label": label, "key": key}
            if path and size is None:
                to_scan.setdefault(key, (path, []))[1].append(iid)

        for path, iids in to_scan.values():
            try:
                fut = self.executor.submit(get_size, path, None, 0, is_stale, True, force)
            except RuntimeError:  # пул уже остановлен (окно закрывается)
                return
            fut.add_done_callback(lambda f, iids=tuple(iids): self.app.ui_call(self._on_size_done, token, iids, f))
            futures.append(fut)

        self.sorted_order = list(self.row_meta)
        self.filter_rows()  # поиск работает и во время подсчёта размеров
        self._current_futures = futures
        self._pending = len(futures)
        self._done = 0
        if not futures:
            self._finish(token, used_cache)
        else:
            self.status_var.set(f"Считаю размеры • 0 из {self._pending}")

    def _on_size_done(self, token, iids, fut):
        if token != self.scan_token or self._closed or fut.cancelled():
            return
        try:
            size = fut.result()
        except Exception:
            size = None
        for iid in iids:
            meta = self.row_meta.get(iid)
            if meta is None or not self.tree.exists(iid):
                continue
            meta["size"] = size
            self.tree.set(iid, "size", format_size(size) if size is not None else "—")
        self._done += 1
        if self._done >= self._pending:
            self._finish(token, False)
        else:
            self.status_var.set(f"Считаю размеры • {self._done} из {self._pending}")

    def _unique_total(self):
        """Сумма без двойного счёта: каждая папка один раз, а вложенная в уже
        учтённую (запись «NVIDIA Corporation» и запись «…\\PhysX») не добавляется."""
        sizes = {}
        for m in self.row_meta.values():
            if m.get("size") and m.get("key"):
                sizes[m["key"]] = m["size"]
        kept = []
        for key in sorted(sizes, key=len):
            if not any(key.startswith(p + "\\") for p in kept):
                kept.append(key)
        return sum(sizes[k] for k in kept)

    def _finish(self, token, from_cache):
        if token != self.scan_token:
            return
        self.card.set_busy(False)
        self._current_futures = []
        self._apply_sort()
        n = len(self.row_meta)
        total = self._unique_total()
        text = f"{n} {plural(n, 'элемент', 'элемента', 'элементов')} • {format_size(total)}"
        if from_cache:
            text += " • из кэша"
        self.status_var.set(text)

    def _apply_sort(self):
        func, rev = self._sort_key_func(lambda iid: self.row_meta[iid])
        self.sorted_order = sorted(self.row_meta, key=func, reverse=rev)
        self.filter_rows()

    def filter_rows(self):
        query = self.search_var.get().lower().strip()
        order = self.sorted_order or list(self.row_meta)
        visible = 0
        for iid in order:
            if not self.tree.exists(iid):
                continue
            if not query or query in self.row_meta[iid]["name"].lower():
                self.tree.reattach(iid, "", "end")
                visible += 1
            else:
                self.tree.detach(iid)
        if self.row_meta and not visible:
            self.card.empty.show("Ничего не найдено", f"По запросу «{query}» нет программ и папок.")
        elif self.row_meta:
            self.card.empty.hide()

    def _get_selected(self):
        sel = self.tree.selection()
        return self.row_meta.get(sel[0]) if sel else None

    def _update_actions(self):
        item = self._get_selected()
        has_path = bool(item and item["path"])
        self.btn_disk.set_enabled(has_path)
        self.btn_reveal.set_enabled(has_path)
        self.btn_uninstall.set_enabled(bool(item and (item["uninstall"] or item["path"])))

    def on_double_click(self, event=None):
        item = self._get_selected()
        if not item or not item["path"]:
            return
        if self.open_in_disk_tab:
            self.open_in_disk_tab(item["path"])
        else:
            _reveal_in_explorer(item["path"])

    def open_selected_folder(self):
        item = self._get_selected()
        if not item or not item["path"]:
            messagebox.showinfo("Нет папки", "Для этого пункта неизвестна папка установки.")
            return
        _reveal_in_explorer(item["path"])

    def uninstall_selected(self):
        item = self._get_selected()
        if not item:
            messagebox.showinfo("Ничего не выбрано", "Выберите программу или папку в списке.")
            return
        last = self._recent_uninstall.get(item["name"])
        if last and time.monotonic() - last < 8:
            self.status_var.set(f"Деинсталлятор «{item['name']}» уже запущен")
            return

        if item["uninstall"]:
            try:
                exe, args, _ = parse_uninstall_command(item["uninstall"])
            except UninstallCommandError as e:
                msg = (f"Команда удаления «{item['name']}» в реестре повреждена или неоднозначна "
                       f"({e}). Ничего не запущено.\n\nКоманда: {item['uninstall']}")
                if item["path"]:
                    if messagebox.askyesno("Деинсталлятор не запущен",
                                           msg + "\n\nОткрыть папку программы, чтобы удалить вручную?"):
                        _reveal_in_explorer(item["path"])
                else:
                    messagebox.showerror("Деинсталлятор не запущен", msg)
                return
            quiet = getattr(item["uninstall"], "quiet", False)
            quiet_note = ("\n\nВНИМАНИЕ: у программы есть только «тихий» деинсталлятор — "
                          "она удалится сразу, без дополнительных окон и вопросов.") if quiet else ""
            confirm = messagebox.askyesno(
                "Подтверждение",
                f"Удалить «{item['name']}»?\n\nЗапустится штатный деинсталлятор программы:\n"
                f"{exe}" + (f"\nПараметры: {args}" if args else "") + quiet_note,
                icon="warning")
            if not confirm:
                return
            self._recent_uninstall[item["name"]] = time.monotonic()
            try:
                launch_uninstaller(item["uninstall"])
                self.status_var.set(f"Запущен деинсталлятор: {item['name']}. "
                                    "После удаления нажмите «Обновить», чтобы обновить список.")
            except (OSError, UninstallCommandError) as e:
                self._recent_uninstall.pop(item["name"], None)
                messagebox.showerror("Ошибка", f"Не удалось запустить деинсталлятор:\n{e}")
        elif item["path"]:
            confirm = messagebox.askyesno(
                "Деинсталлятор не найден",
                f"Для «{item['name']}» нет штатного деинсталлятора.\n\n"
                "Открыть папку, чтобы удалить вручную?")
            if confirm:
                _reveal_in_explorer(item["path"])
        else:
            messagebox.showinfo("Нет данных", "Не найдено ни деинсталлятора, ни папки.")


class CleanupRow(tk.Frame):
    """Одна категория очистки: чекбокс, название, пояснение, размер."""

    def __init__(self, master, theme, target, checked, on_toggle):
        super().__init__(master, bg=theme.SURFACE, cursor="hand2")
        self.t = theme
        self.target = target
        self.on_toggle = on_toggle
        px = theme.px
        self.check = CheckBox(self, theme, checked, command=lambda v: self.on_toggle(self.target["id"], v))
        self.check.grid(row=0, column=0, rowspan=3, sticky="n", padx=(px(18), px(16)), pady=(px(18), px(14)))
        self.title = tk.Label(self, text=target["name"], bg=theme.SURFACE, fg=theme.TEXT,
                              font=theme.f_row_title, anchor="w")
        self.title.grid(row=0, column=1, sticky="w", pady=(px(14), 0))
        desc = CLEANUP_DESCRIPTIONS.get(target["id"], "")
        self.desc = tk.Label(self, text=desc, bg=theme.SURFACE, fg=theme.TEXT_2, font=theme.f_row_desc,
                             anchor="w", justify="left", wraplength=px(640))
        self.desc.grid(row=1, column=1, sticky="w", pady=(px(3), px(15) if not target.get("note") else 0))
        self.note = None
        if target.get("note"):
            # Предупреждение: заметно (свой цвет и значок), но не кричит —
            # обычный, не жирный шрифт того же размера, что и описание
            self.note = tk.Label(self, text="⚠  " + target["note"], bg=theme.SURFACE, fg=theme.WARN,
                                 font=theme.f_row_desc, anchor="w", justify="left", wraplength=px(640))
            self.note.grid(row=2, column=1, sticky="w", pady=(px(5), px(15)))
        self.size = tk.Label(self, text="…", bg=theme.SURFACE, fg=theme.TEXT, font=theme.f_row_size, anchor="e")
        self.size.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(px(14), px(20)), pady=(px(14), 0))
        self.grid_columnconfigure(1, weight=1)
        # Плавная подсветка строки при наведении (6 виджетов, ~7 кадров)
        self._bg_tween = _Tween(self, (theme.SURFACE,), self._apply_bg)
        for w in (self, self.title, self.desc, self.size) + ((self.note,) if self.note else ()):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", self._leave)
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, e):
        wl = max(self.t.px(200), e.width - self.t.px(240))
        self.desc.configure(wraplength=wl)
        if self.note:
            self.note.configure(wraplength=wl)

    def _click(self, _e):
        self.check._toggle()

    def _leave(self, e):
        x, y = self.winfo_pointerxy()
        w = self.winfo_containing(x, y)
        while w is not None and w is not self:
            w = getattr(w, "master", None)
        if w is None:
            self._hover(False)

    def _apply_bg(self, v):
        bg = v[0]
        for w in (self, self.title, self.desc, self.size, self.check) + ((self.note,) if self.note else ()):
            w.configure(bg=bg)
        self.check.draw()

    def _hover(self, on):
        self._bg_tween.to((self.t.SURFACE_2 if on else self.t.SURFACE,), 100)

    def set_size(self, text, dim=False):
        self.size.configure(text=text, fg=self.t.TEXT_3 if dim else self.t.TEXT)

    def set_available(self, available):
        """Категории, которой нет на этом компьютере (нет папки Chrome и т.п.),
        нечего очищать: она приглушена и не участвует в выборе."""
        self.check.set_enabled(available)
        self.title.configure(fg=self.t.TEXT if available else self.t.TEXT_3)
        self.desc.configure(fg=self.t.TEXT_2 if available else self.t.TEXT_3)
        cursor = "hand2" if available else "arrow"
        for w in (self, self.title, self.desc, self.size):
            w.configure(cursor=cursor)


class CleanupTab(tk.Frame):
    OPT_IN = {"windows_old", "recycle_bin"}

    def __init__(self, parent, app):
        super().__init__(parent, bg=app.theme.BG)
        self.app = app
        self.t = app.theme
        self.executor = app.bg_pool
        # Необратимые категории (Windows.old, Корзина) по умолчанию НЕ отмечены
        self.checked = {t["id"]: t["id"] not in self.OPT_IN for t in TARGETS}
        self.sizes = {}            # id категории -> байты (None — неизвестно)
        self.available = {}        # id категории -> есть ли что очищать на этом ПК
        self.rows = {}             # id категории -> CleanupRow
        self.scan_token = 0
        self._current_futures = []
        self._pending = self._done = 0
        self._closed = False
        self._started = False
        self._cleaning = False
        self.status_var = tk.StringVar(value="")
        self.total_var = tk.StringVar(value="—")
        self._build_ui()

    def _build_ui(self):
        t, px = self.t, self.t.px
        intro = tk.Label(self, text="Временные файлы и кэш, которые Windows и программы создают заново. "
                                    "Документы, фото, загрузки и сохранения игр не затрагиваются.",
                         bg=t.BG, fg=t.TEXT_2, font=t.f_status, anchor="w", justify="left",
                         wraplength=px(900))
        intro.pack(fill="x", pady=(0, px(14)))
        _auto_wrap(intro)

        card = tk.Frame(self, bg=t.SURFACE)
        card.pack(fill="both", expand=True)
        self.progress = FadingProgress(card, t)
        self.progress.pack(fill="x")
        self.scroll = ScrollFrame(card, t, t.SURFACE)
        self.scroll.pack(fill="both", expand=True)
        for i, target in enumerate(TARGETS):
            if i:
                tk.Frame(self.scroll.inner, bg=t.BORDER, height=1).pack(fill="x", padx=px(18))
            row = CleanupRow(self.scroll.inner, t, target, self.checked[target["id"]], self._on_toggle)
            row.pack(fill="x")
            self.rows[target["id"]] = row

        bottom = tk.Frame(self, bg=t.BG)
        bottom.pack(fill="x", pady=(px(14), 0))
        left = tk.Frame(bottom, bg=t.BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Будет освобождено примерно", bg=t.BG, fg=t.TEXT_3,
                 font=t.f_status, anchor="w").pack(anchor="w")
        tk.Label(left, textvariable=self.total_var, bg=t.BG, fg=t.TEXT, font=t.f_total,
                 anchor="w").pack(anchor="w")
        tk.Label(left, textvariable=self.status_var, bg=t.BG, fg=t.TEXT_3, font=t.f_small,
                 anchor="w").pack(anchor="w")
        btn_h = px(42)
        self.btn_clean = FlatButton(bottom, t, "Очистить", self.clean_selected, "danger_solid",
                                    height=btn_h, font=t.f_button_lg,
                                    width=t.font(t.f_button_lg).measure("Очистить") + px(48))
        self.btn_clean.pack(side="right", anchor="s")
        self.btn_refresh = FlatButton(bottom, t, "", lambda: self.scan_all(force=True), "icon",
                                      icon="refresh", tooltip="Пересчитать размеры (F5)", height=btn_h)
        self.btn_refresh.pack(side="right", anchor="s", padx=(0, px(8)))
        FlatButton(bottom, t, "Снять всё", lambda: self._set_all_checked(False), "ghost",
                   height=btn_h, font=t.f_button_lg).pack(side="right", anchor="s", padx=(0, px(4)))
        FlatButton(bottom, t, "Выбрать всё", lambda: self._set_all_checked(True), "ghost",
                   height=btn_h, font=t.f_button_lg).pack(side="right", anchor="s", padx=(0, px(4)))

    def on_show(self):
        if not self._started:
            self.scan_all()

    def _on_toggle(self, tid, value):
        self.checked[tid] = value
        self._update_total()

    def _set_all_checked(self, value):
        for tid in self.checked:
            self.checked[tid] = value
            self.rows[tid].check.set(value)
        self._update_total()

    def _selected_targets(self):
        """Отмеченные И реально существующие на этом компьютере категории.
        Только они попадают в итог, в окно подтверждения и в очистку."""
        return [t for t in TARGETS
                if self.checked.get(t["id"]) and self.available.get(t["id"], True)]

    def _update_total(self):
        selected = self._selected_targets()
        known = [self.sizes.get(t["id"]) for t in selected]
        total = sum(s for s in known if s)
        if not selected:
            self.total_var.set("ничего не выбрано")
        else:
            self.total_var.set(format_size(total))
            self._unknown_hint = any(s is None for s in known) and self._pending == self._done
        self.btn_clean.set_enabled(bool(selected) and not self._cleaning)

    def scan_all(self, force=False):
        # Во время очистки пересчёт не запускается (F5/⟳): иначе цифры
        # прыгали бы посреди удаления. После очистки пересчёт делается сам.
        if self._closed or self._cleaning:
            return
        self._started = True
        for fut in self._current_futures:
            fut.cancel()
        self._current_futures = []
        self.scan_token += 1
        token = self.scan_token
        is_stale = lambda: token != self.scan_token or self._closed
        futures = []
        used_cache = False
        for target in TARGETS:
            tid = target["id"]
            row = self.rows[tid]
            if target["action"] == "empty_recycle_bin":
                self.available[tid] = IS_WINDOWS
                row.set_available(IS_WINDOWS)
                if not IS_WINDOWS:
                    self.sizes[tid] = 0
                    row.set_size("недоступно", dim=True)
                    continue
                fut = self.executor.submit(get_recycle_bin_size)
            elif not target["path"] or not os.path.isdir(target["path"]):
                self.sizes[tid] = 0
                self.available[tid] = False
                row.set_available(False)
                row.set_size("не найдено", dim=True)
                continue
            else:
                self.available[tid] = True
                row.set_available(True)
                cached = None if force else _cache_get(target["path"])
                if cached is not None:
                    self.sizes[tid] = cached
                    row.set_size(format_size(cached))
                    used_cache = True
                    continue
                fut = self.executor.submit(get_size, target["path"], None, 0, is_stale, True, force)
            self.sizes[tid] = None
            row.set_size("…", dim=True)
            fut.add_done_callback(lambda f, tid=tid: self.app.ui_call(self._on_size_done, token, tid, f))
            futures.append(fut)
        self._current_futures = futures
        self._pending, self._done = len(futures), 0
        self._update_total()
        if futures:
            self.progress.start_busy()
            self.status_var.set("Считаю размеры…")
        else:
            self._finish(token, used_cache)

    def _on_size_done(self, token, tid, fut):
        if token != self.scan_token or self._closed or fut.cancelled():
            return
        try:
            size = fut.result()
        except Exception:
            size = None
        self.sizes[tid] = size
        self.rows[tid].set_size(format_size(size) if size is not None else "—", dim=size is None)
        self._done += 1
        self._update_total()
        if self._done >= self._pending:
            self._finish(token, False)

    def _finish(self, token, from_cache):
        if token != self.scan_token:
            return
        self.progress.stop_busy()
        self._current_futures = []
        self._update_total()
        text = "Готово" + (" • из кэша" if from_cache else "")
        if getattr(self, "_unknown_hint", False):
            text += " • размер некоторых категорий узнать не удалось"
        self.status_var.set(text)

    def clean_selected(self):
        if self._cleaning:
            return  # защита от повторного запуска
        selected_targets = self._selected_targets()
        if not selected_targets:
            messagebox.showinfo("Ничего не выбрано", "Отметьте хотя бы одну категорию, которая есть на этом компьютере.")
            return

        lines = []
        for t in selected_targets:
            size = self.sizes.get(t["id"])
            size_text = format_size(size) if size else ""
            lines.append(f"• {t['name']}" + (f" ({size_text})" if size_text else ""))
            if t.get("note"):
                prefix = "НЕОБРАТИМО: " if t["id"] in self.OPT_IN else ""
                lines.append(f"   ⚠ {prefix}{t['note']}")
        confirm = messagebox.askyesno(
            "Подтверждение",
            "Очистить выбранные категории?\n\n" + "\n".join(lines) +
            "\n\nЭто удаляет файлы напрямую, минуя корзину — отменить будет нельзя.",
        )
        if not confirm:
            return

        self._cleaning = True
        self._update_total()
        self.btn_refresh.set_enabled(False)
        self.progress.start_busy()
        self.status_var.set("Очистка…")

        def worker():
            problems = []
            for target in selected_targets:
                try:
                    if target["action"] == "clear_contents" and target["path"] and os.path.isdir(target["path"]):
                        # Quick Cleanup — только allowlist: разрешено ровно то, что есть в TARGETS
                        if not is_allowed_quick_cleanup_path(target["path"]):
                            problems.append(f"{target['name']}: путь не в разрешённом списке Quick Cleanup — пропущено")
                            continue
                        errors = clear_quick_cleanup_category(target["path"])
                        invalidate_size_cache(target["path"])
                        if errors:
                            problems.append(f"{target['name']}: {errors} элемент(ов) не удалось удалить")
                    elif target["action"] == "clear_folder_full" and target["path"] and os.path.isdir(target["path"]):
                        if not is_allowed_quick_cleanup_path(target["path"]):
                            problems.append(f"{target['name']}: путь не в разрешённом списке Quick Cleanup — пропущено")
                            continue
                        fully_removed, failed = delete_path_resilient(target["path"], strict_allowlist=True)
                        invalidate_size_cache(target["path"])
                        if not fully_removed:
                            problems.append(f"{target['name']}: {len(failed)} файл(ов) не удалось удалить")
                    elif target["action"] == "empty_recycle_bin":
                        try:
                            import ctypes
                            flags = 0x1 | 0x2 | 0x4
                            ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
                        except Exception:
                            problems.append("Корзина: не удалось очистить")
                except RedirectedPathError as e:
                    problems.append(f"{target['name']}: папка на самом деле находится в другом месте "
                                    f"({e.real or 'неизвестно'}) — очистка отменена")
                except ProtectedPathError:
                    problems.append(f"{target['name']}: защищённый путь, пропущено")
                except Exception as e:
                    problems.append(f"{target['name']}: {e}")
            self.app.ui_call(self._on_cleaned, problems)

        threading.Thread(target=worker, daemon=True, name="cp-clean").start()

    def _on_cleaned(self, problems):
        self._cleaning = False
        self.btn_refresh.set_enabled(True)
        if self._closed:
            return
        self.scan_all()
        if problems:
            messagebox.showwarning(
                "Готово, но не всё",
                "Очистка завершена, но часть файлов удалить не получилось "
                "(скорее всего они заняты другой программой):\n\n" + "\n".join(problems))
        else:
            messagebox.showinfo("Готово", "Очистка завершена.")


# ---------- Главное окно ----------

class CleanerProApp(tk.Tk):
    def __init__(self):
        _enable_dpi_awareness()
        _suppress_drive_error_dialogs()
        super().__init__()
        self._ui_queue = queue.Queue()
        self._closing = False
        self.theme = Theme(self)
        self.theme.apply_ttk()
        self.icons = _make_icons(self.theme)
        self.title(f"{APP_NAME} — v{APP_VERSION} {APP_EDITION}")
        px = self.theme.px
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(px(_UI_DESIGN_W), int(sw * 0.9)), min(px(_UI_DESIGN_H), int(sh * 0.88))
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.minsize(min(px(860), w), min(px(560), h))

        self.disk_pool = ThreadPoolExecutor(max_workers=DISK_WORKERS, thread_name_prefix="cp-disk")
        self.bg_pool = ThreadPoolExecutor(max_workers=BACKGROUND_WORKERS, thread_name_prefix="cp-bg")
        self.pages = {}
        self._build_main_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(15, self._drain_ui_queue)

    # ----- потокобезопасная доставка результатов в главный поток -----
    def ui_call(self, fn, *args):
        """Можно вызывать из ЛЮБОГО потока. Функция выполнится в главном потоке Tk."""
        if not self._closing:
            self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        if self._closing:
            return
        deadline = time.perf_counter() + 0.012
        while time.perf_counter() < deadline:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                self.report_callback_exception(*sys.exc_info())
        try:
            self.after(15, self._drain_ui_queue)
        except tk.TclError:
            pass

    def _build_main_ui(self):
        t, px = self.theme, self.theme.px
        header = tk.Frame(self, bg=t.BG)
        header.pack(fill="x", padx=px(26), pady=(px(18), 0))
        tk.Label(header, text=APP_NAME, bg=t.BG, fg=t.TEXT, font=t.f_title).pack(side="left")
        Badge(header, t, APP_EDITION).pack(side="left", padx=(px(10), px(34)), pady=(px(3), 0))
        self.nav = NavBar(header, t, [("disk", "Диск"), ("apps", "Программы"), ("cleanup", "Быстрая очистка")],
                          self.show_page)
        self.nav.pack(side="left", anchor="s")
        tk.Frame(self, bg=t.BORDER, height=1).pack(fill="x", pady=(px(10), 0))

        container = tk.Frame(self, bg=t.BG)
        container.pack(fill="both", expand=True, padx=px(26), pady=(px(16), px(18)))
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        apps_tab = AppsTab(container, self, open_in_disk_tab=self.open_in_disk_tab)
        disk_tab = DiskTab(container, self)
        cleanup_tab = CleanupTab(container, self)
        self.pages = {"disk": disk_tab, "apps": apps_tab, "cleanup": cleanup_tab}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self.bind_all("<Control-Key-1>", lambda e: self.show_page("disk"))
        self.bind_all("<Control-Key-2>", lambda e: self.show_page("apps"))
        self.bind_all("<Control-Key-3>", lambda e: self.show_page("cleanup"))
        self.bind("<F5>", lambda e: self._refresh_current())
        self.bind("<Alt-Left>", lambda e: disk_tab.go_back() if self.nav.active == "disk" else None)
        self.bind("<Alt-Right>", lambda e: disk_tab.go_forward() if self.nav.active == "disk" else None)
        self.show_page("disk")

    def show_page(self, key):
        if self._closing or key not in self.pages:
            return
        self.nav.set_active(key)
        page = self.pages[key]
        page.tkraise()
        page.on_show()

    def open_in_disk_tab(self, path):
        self.show_page("disk")
        self.pages["disk"].navigate_to(path)

    def _refresh_current(self):
        page = self.pages.get(self.nav.active)
        if isinstance(page, DiskTab):
            page.refresh()
        elif page is not None:
            page.scan_all(force=True)

    def on_close(self):
        busy = []
        if getattr(self.pages.get("disk"), "_deleting", False):
            busy.append("удаление")
        if getattr(self.pages.get("cleanup"), "_cleaning", False):
            busy.append("очистка")
        if busy and not messagebox.askyesno(
                "Операция ещё выполняется",
                f"Сейчас идёт {' и '.join(busy)}. Если закрыть программу, операция прервётся "
                "и часть файлов останется на месте.\n\nЗакрыть всё равно?",
                icon="warning", default="no"):
            return
        self.destroy()

    def destroy(self):
        # Сначала сигнал всем фоновым задачам остановиться, потом окно.
        # Фоновые потоки не трогают Tk напрямую, поэтому после destroy()
        # им некуда «достучаться» — они просто тихо завершаются.
        self._closing = True
        for page in self.pages.values():
            page._closed = True
            for fut in getattr(page, "_current_futures", []):
                fut.cancel()
        for pool in (getattr(self, "disk_pool", None), getattr(self, "bg_pool", None)):
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
        # Отменяем все отложенные after-таймеры этого окна (очередь UI,
        # подсказки, «большие папки считаются дольше»), иначе они срабатывают
        # уже после уничтожения окна и пишут ошибки Tcl.
        try:
            for after_id in self.tk.splitlist(self.tk.call("after", "info")):
                try:
                    # Именно Tcl «after cancel», а не after_cancel(): тот ещё и
                    # удаляет Tcl-команду, которую потом пытается удалить сам
                    # виджет при уничтожении («can't delete Tcl command»).
                    self.tk.call("after", "cancel", after_id)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
        super().destroy()


def main():
    try:
        app = CleanerProApp()
    except Exception as e:
        # .exe собирается без консоли: без этого окна ошибка запуска выглядела
        # бы как «программа открылась и сразу закрылась».
        import traceback
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(f"{APP_NAME} — ошибка запуска",
                                 f"Не удалось запустить программу:\n\n{e}\n\n{traceback.format_exc()[-1500:]}")
            root.destroy()
        except Exception:
            pass
        raise
    app.mainloop()


if __name__ == "__main__":
    main()
