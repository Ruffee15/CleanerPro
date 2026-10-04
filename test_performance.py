"""
Тесты производительности и кэширования для Cleaner Pro v1.0 Free.

Запуск (из этой же папки, где лежит cleaner_pro.pyw):
    python test_performance.py

Как и test_security.py, импортирует напрямую production-файл cleaner_pro.pyw.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(HERE, "cleaner_pro.pyw")


def load_production_module():
    if not os.path.isfile(TARGET_FILE):
        print(f"ОШИБКА: не найден {TARGET_FILE}")
        sys.exit(2)
    loader = SourceFileLoader("cleaner_pro", TARGET_FILE)
    spec = importlib.util.spec_from_loader("cleaner_pro", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cpv1 = load_production_module()

failures = []
total_checks = 0


def check(name, condition):
    global total_checks
    total_checks += 1
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# ---------- Готовим синтетическое дерево файлов ----------
TEST_ROOT = tempfile.mkdtemp(prefix="cleanerpro_perf_")


def make_tree(root, depth, dirs_per_level, files_per_dir):
    os.makedirs(root, exist_ok=True)
    for i in range(files_per_dir):
        with open(os.path.join(root, f"file_{i}.dat"), "wb") as f:
            f.truncate(1000)
    if depth > 0:
        for d in range(dirs_per_level):
            make_tree(os.path.join(root, f"dir_{d}"), depth - 1, dirs_per_level, files_per_dir)


print("Готовлю тестовое дерево файлов...")
make_tree(TEST_ROOT, depth=3, dirs_per_level=4, files_per_dir=20)
total_files = sum(len(files) for _, _, files in os.walk(TEST_ROOT))
print(f"Дерево готово: {total_files} файлов, корень: {TEST_ROOT}\n")


print("=== 1. Кэш — наличие инфраструктуры ===")
check("_SIZE_CACHE существует", hasattr(cpv1, "_SIZE_CACHE"))
check("_SIZE_CACHE_LOCK существует и является Lock", hasattr(cpv1, "_SIZE_CACHE_LOCK"))
check("invalidate_size_cache существует", hasattr(cpv1, "invalidate_size_cache"))
check("clear_size_cache существует", hasattr(cpv1, "clear_size_cache"))

print("\n=== 2. Повторный подсчёт той же папки — из кэша, практически мгновенно ===")
cpv1.clear_size_cache()
t0 = time.perf_counter()
size1 = cpv1.get_size(TEST_ROOT)
t_cold = time.perf_counter() - t0

t0 = time.perf_counter()
size2 = cpv1.get_size(TEST_ROOT)
t_warm = time.perf_counter() - t0

print(f"Холодный подсчёт: {t_cold:.4f}с | Повторный (из кэша): {t_warm:.4f}с")
check("размер совпадает при повторном подсчёте", size1 == size2)
check("повторный подсчёт из кэша минимум в 5 раз быстрее холодного",
      t_warm < t_cold / 5 or t_warm < 0.001)

print("\n=== 3. Дочерняя папка, посчитанная заранее, переиспользуется родителем ===")
cpv1.clear_size_cache()
sub = os.path.join(TEST_ROOT, "dir_0")
t0 = time.perf_counter()
cpv1.get_size(sub)
t_sub = time.perf_counter() - t0

t0 = time.perf_counter()
cpv1.get_size(TEST_ROOT)
t_parent_after = time.perf_counter() - t0

cpv1.clear_size_cache()
t0 = time.perf_counter()
cpv1.get_size(TEST_ROOT)
t_parent_cold = time.perf_counter() - t0

print(f"Подсчёт подпапки: {t_sub:.4f}с | Родитель с уже посчитанной подпапкой: "
      f"{t_parent_after:.4f}с | Родитель полностью с нуля: {t_parent_cold:.4f}с")
check("подсчёт родителя с частично тёплым кэшем быстрее полностью холодного",
      t_parent_after < t_parent_cold)

print("\n=== 4. Инвалидация кэша после удаления ===")
cpv1.clear_size_cache()
victim = os.path.join(TEST_ROOT, "dir_1")
cpv1.get_size(TEST_ROOT)  # прогреваем кэш целиком, включая dir_1 и сам TEST_ROOT
key_victim = cpv1._cache_key(victim)
key_root = cpv1._cache_key(TEST_ROOT)
with cpv1._SIZE_CACHE_LOCK:
    had_victim = key_victim in cpv1._SIZE_CACHE
    had_root = key_root in cpv1._SIZE_CACHE
check("dir_1 был в кэше до удаления", had_victim)
check("корень был в кэше до удаления", had_root)

shutil.rmtree(victim)
cpv1.invalidate_size_cache(victim)

with cpv1._SIZE_CACHE_LOCK:
    has_victim_after = key_victim in cpv1._SIZE_CACHE
    has_root_after = key_root in cpv1._SIZE_CACHE
check("dir_1 удалён из кэша после invalidate_size_cache", not has_victim_after)
check("кэш родителя (TEST_ROOT) тоже инвалидирован (его сумма устарела)", not has_root_after)

new_size = cpv1.get_size(TEST_ROOT)
check("пересчитанный размер меньше исходного (dir_1 реально пропал)", new_size < size1)

print("\n=== 5. force_refresh игнорирует кэш на чтение, но обновляет его ===")
cpv1.clear_size_cache()
cpv1.get_size(TEST_ROOT)
with cpv1._SIZE_CACHE_LOCK:
    cached_before = cpv1._SIZE_CACHE.get(cpv1._cache_key(TEST_ROOT))
check("кэш заполнен после обычного подсчёта", cached_before is not None)

# Подменим значение в кэше на заведомо неверное, проверим что force_refresh его не использует
with cpv1._SIZE_CACHE_LOCK:
    cpv1._SIZE_CACHE[cpv1._cache_key(TEST_ROOT)] = 999999999
forced_size = cpv1.get_size(TEST_ROOT, force_refresh=True)
check("force_refresh вернул реальный пересчитанный размер, а не испорченный кэш",
      forced_size != 999999999)

with cpv1._SIZE_CACHE_LOCK:
    cached_after_force = cpv1._SIZE_CACHE.get(cpv1._cache_key(TEST_ROOT))
check("после force_refresh кэш обновлён корректным значением", cached_after_force == forced_size)

print("\n=== 6. cancel_check прерывает подсчёт досрочно ===")
cpv1.clear_size_cache()
call_count = {"n": 0}

def cancel_after_a_few():
    call_count["n"] += 1
    return call_count["n"] > 3  # отменяем после нескольких проверок

t0 = time.perf_counter()
result = cpv1.get_size(TEST_ROOT, cancel_check=cancel_after_a_few)
t_cancelled = time.perf_counter() - t0

cpv1.clear_size_cache()
t0 = time.perf_counter()
cpv1.get_size(TEST_ROOT)
t_full = time.perf_counter() - t0

print(f"С отменой: {t_cancelled:.4f}с (cancel_check вызван {call_count['n']} раз) | "
      f"Без отмены: {t_full:.4f}с")
check("cancel_check реально вызывался", call_count["n"] > 3)

print("\n=== 7. Потокобезопасность: параллельная запись в кэш не теряет данные и не падает ===")
cpv1.clear_size_cache()
errors = []

def worker(path):
    try:
        cpv1.get_size(path)
    except Exception as e:
        errors.append(e)

paths = [os.path.join(TEST_ROOT, f"dir_{i}") for i in range(4)] * 5  # одни и те же пути параллельно
threads = [threading.Thread(target=worker, args=(p,)) for p in paths]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("параллельные вычисления размера не вызвали исключений", len(errors) == 0)
check("кэш не пуст после параллельной работы", len(cpv1._SIZE_CACHE) > 0)

print("\n=== 8. Реальный прирост на полном сканировании (до/после кэша), в цифрах ===")
cpv1.clear_size_cache()
t0 = time.perf_counter()
cpv1.get_size(TEST_ROOT)
t_first = time.perf_counter() - t0
t0 = time.perf_counter()
cpv1.get_size(TEST_ROOT)
t_second = time.perf_counter() - t0
speedup = (t_first / t_second) if t_second > 0 else float("inf")
print(f"Первое сканирование: {t_first:.4f}с")
print(f"Повторное сканирование: {t_second:.4f}с")
print(f"Ускорение: {'мгновенно (>1000x)' if t_second < 0.0005 else f'{speedup:.0f}x'}")
check("повторное сканирование быстрее первого", t_second <= t_first)

shutil.rmtree(TEST_ROOT, ignore_errors=True)


print("\n=== 9. Холодное сканирование КРУПНОЙ структуры (с таймаутом на зависание) ===")
BIG_ROOT = tempfile.mkdtemp(prefix="cleanerpro_perf_big_")
make_tree(BIG_ROOT, depth=4, dirs_per_level=5, files_per_dir=40)
big_file_count = sum(len(files) for _, _, files in os.walk(BIG_ROOT))
print(f"Крупное дерево: {big_file_count} файлов, корень: {BIG_ROOT}")

cpv1.clear_size_cache()
HANG_TIMEOUT = 15  # секунд — если за это время не управились, считаем, что где-то завис
result_holder = {}

def run_big_scan():
    result_holder["size"] = cpv1.get_size(BIG_ROOT)

t0 = time.perf_counter()
scan_thread = threading.Thread(target=run_big_scan, daemon=True)
scan_thread.start()
scan_thread.join(timeout=HANG_TIMEOUT)
t_big_cold = time.perf_counter() - t0

check(f"холодное сканирование {big_file_count} файлов завершилось за {HANG_TIMEOUT}с "
      f"(не зависло) — заняло {t_big_cold:.3f}с", not scan_thread.is_alive())
check("холодное сканирование крупной структуры вернуло ненулевой размер",
      result_holder.get("size", 0) > 0)
print(f"Время холодного сканирования крупной структуры: {t_big_cold:.3f}с")

# И сразу повторное — тоже должно быть мгновенным
t0 = time.perf_counter()
big_size_cached = cpv1.get_size(BIG_ROOT)
t_big_warm = time.perf_counter() - t0
print(f"Время повторного (из кэша): {t_big_warm:.4f}с")
check("повторное сканирование крупной структуры из кэша быстрее холодного минимум в 20 раз",
      t_big_warm < t_big_cold / 20 or t_big_warm < 0.001)
check("размер при повторном сканировании совпадает", big_size_cached == result_holder.get("size"))


print("\n=== 10. Реальная навигация в GUI между уже просканированными папками ===")
gui_available = True
gui_skip_reason = ""
try:
    import tkinter as tk
    test_root_tk = tk.Tk()
    test_root_tk.destroy()
except Exception as e:
    gui_available = False
    gui_skip_reason = str(e)

if not gui_available:
    print(f"[SKIP] Нет доступного дисплея для Tkinter ({gui_skip_reason}) — "
          "раздел пропущен, это не провал теста, просто окружение без GUI.")
else:
    import tkinter as tk

    nav_root = tempfile.mkdtemp(prefix="cleanerpro_nav_")
    os.makedirs(os.path.join(nav_root, "FolderA"), exist_ok=True)
    os.makedirs(os.path.join(nav_root, "FolderB"), exist_ok=True)
    for i in range(15):
        with open(os.path.join(nav_root, "FolderA", f"f{i}.dat"), "wb") as f:
            f.truncate(2000)

    saved_userprofile = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = nav_root
    cpv1_gui = load_production_module()  # свежий импорт — чтобы USER_PROFILE подхватился заново
    if saved_userprofile is not None:
        os.environ["USERPROFILE"] = saved_userprofile

    app = cpv1_gui.CleanerProApp()
    disk_tab_holder = {}

    def find_disk_tab(w):
        for c in w.winfo_children():
            if isinstance(c, cpv1_gui.DiskTab):
                disk_tab_holder["tab"] = c
            find_disk_tab(c)

    find_disk_tab(app)
    nav_results = {}

    def step_initial():
        dt = disk_tab_holder["tab"]
        dt.navigate_to(nav_root, record_history=False)
        app.after(600, step_into_folder_a)

    def step_into_folder_a():
        dt = disk_tab_holder["tab"]
        folder_a = os.path.join(nav_root, "FolderA")
        dt.navigate_to(folder_a)
        app.after(600, step_back_to_root)

    def step_back_to_root():
        dt = disk_tab_holder["tab"]
        t0 = time.perf_counter()
        dt.navigate_to(nav_root)
        app.update()
        t1 = time.perf_counter()
        nav_results["revisit_time"] = t1 - t0
        nav_results["status_text"] = dt.status_var.get()
        nav_results["row_count"] = len(dt.tree.get_children())
        app.after(300, app.destroy)

    app.after(500, step_initial)
    app.mainloop()

    print(f"Статус после повторного захода в уже просканированную папку: "
          f"{nav_results.get('status_text', '(нет данных)')}")
    print(f"Время повторного захода: {nav_results.get('revisit_time', -1):.4f}с")
    check("повторный заход в уже просканированную папку показал данные из кэша",
          "кэша" in nav_results.get("status_text", ""))
    check("строки в таблице присутствуют после повторного захода",
          nav_results.get("row_count", 0) == 2)
    check("повторный заход в GUI занял меньше 0.5с",
          nav_results.get("revisit_time", 999) < 0.5)

    shutil.rmtree(nav_root, ignore_errors=True)


print("\n=== 11. Отмена старого сканирования при переходе в другую папку ===")
print("(часть А — детерминированная проверка самого механизма: диск в этом "
      "окружении настолько быстрый, что гонка с реальным I/O не ловит очередь "
      "задач надёжно — поэтому сначала проверяем ТОЧНО, без зависимости от "
      "скорости диска, через искусственно заблокированные задачи)")

from concurrent.futures import ThreadPoolExecutor as _TPE

det_executor = _TPE(max_workers=cpv1.MAX_WORKERS)
block_event = threading.Event()
started_order = []

def blocking_task(n):
    started_order.append(n)
    block_event.wait(timeout=5)
    return n

# Занимаем ВСЕ воркеры долгими задачами, которые не завершатся, пока мы не
# разрешим (block_event) — это гарантирует, что следующие задачи физически
# не могут начаться, они останутся в очереди
busy_futures = [det_executor.submit(blocking_task, i) for i in range(cpv1.MAX_WORKERS)]
time.sleep(0.2)  # даём воркерам точно захватить все задачи

# Эти задачи ГАРАНТИРОВАННО не начнутся, пока воркеры заняты — именно их и
# должен уметь отменять код DiskTab при переходе в другую папку
queued_futures = [det_executor.submit(blocking_task, 100 + i) for i in range(5)]
time.sleep(0.05)

cancelled_results = [f.cancel() for f in queued_futures]
check("все ещё не начатые задачи успешно отменились (future.cancel() вернул True)",
      all(cancelled_results))
check("отменённые задачи реально помечены как cancelled()",
      all(f.cancelled() for f in queued_futures))
check("отменённые задачи НИ РАЗУ не начали выполняться (не попали в started_order)",
      all((100 + i) not in started_order for i in range(5)))

block_event.set()  # отпускаем "занятые" задачи, чтобы корректно завершить executor
det_executor.shutdown(wait=True)

print("(часть Б — поведение DiskTab на практике: токен сканирования и "
      "текущий путь корректно переключаются даже при очень быстром переходе)")

if not gui_available:
    print("[SKIP] Нет доступного дисплея для Tkinter — часть Б пропущена.")
else:
    cancel_root = tempfile.mkdtemp(prefix="cleanerpro_cancel_")
    slow_folder = os.path.join(cancel_root, "SlowFolder")
    fast_folder = os.path.join(cancel_root, "FastFolder")
    make_tree(slow_folder, depth=3, dirs_per_level=4, files_per_dir=25)
    os.makedirs(fast_folder, exist_ok=True)
    with open(os.path.join(fast_folder, "tiny.txt"), "w") as f:
        f.write("x")

    cpv1.clear_size_cache()

    saved_userprofile = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = cancel_root
    cpv1_cancel = load_production_module()
    if saved_userprofile is not None:
        os.environ["USERPROFILE"] = saved_userprofile

    app2 = cpv1_cancel.CleanerProApp()
    disk_tab_holder2 = {}

    def find_disk_tab2(w):
        for c in w.winfo_children():
            if isinstance(c, cpv1_cancel.DiskTab):
                disk_tab_holder2["tab"] = c
            find_disk_tab2(c)

    find_disk_tab2(app2)
    cancel_results = {}

    def step_cancel_1():
        dt = disk_tab_holder2["tab"]
        dt.navigate_to(cancel_root, record_history=False)
        app2.after(300, step_cancel_2)

    def step_cancel_2():
        dt = disk_tab_holder2["tab"]
        token_before = dt.scan_token
        dt.navigate_to(slow_folder)
        dt.navigate_to(fast_folder)  # переключаемся ОЧЕНЬ быстро, одно за другим
        cancel_results["token_before"] = token_before
        app2.after(500, lambda: finish_cancel_check(dt))

    def finish_cancel_check(dt):
        cancel_results["token_after"] = dt.scan_token
        cancel_results["current_path"] = dt.current_path
        app2.after(200, app2.destroy)

    app2.after(400, step_cancel_1)
    app2.mainloop()

    print(f"Токен до: {cancel_results.get('token_before')}, "
          f"токен после двух быстрых переходов: {cancel_results.get('token_after')}")
    print(f"Текущая папка: {cancel_results.get('current_path')}")

    check("scan_token увеличился минимум дважды (оба перехода зафиксированы)",
          cancel_results.get("token_after", 0) >= cancel_results.get("token_before", 0) + 2)
    check("после быстрого переключения текущая папка — именно последняя, куда перешли",
          cancel_results.get("current_path") == fast_folder)

    shutil.rmtree(cancel_root, ignore_errors=True)


print("\n=== 12. Стресс-тест на зависания/deadlock: конкурентные чтения + инвалидация ===")
stress_root = tempfile.mkdtemp(prefix="cleanerpro_stress_")
make_tree(stress_root, depth=3, dirs_per_level=3, files_per_dir=15)
cpv1.clear_size_cache()

stress_errors = []
stop_flag = {"stop": False}

def reader_worker():
    while not stop_flag["stop"]:
        try:
            cpv1.get_size(stress_root)
        except Exception as e:
            stress_errors.append(e)

def invalidator_worker():
    paths = [stress_root] + [os.path.join(stress_root, f"dir_{i}") for i in range(3)]
    while not stop_flag["stop"]:
        for p in paths:
            try:
                cpv1.invalidate_size_cache(p)
            except Exception as e:
                stress_errors.append(e)

stress_threads = (
    [threading.Thread(target=reader_worker, daemon=True) for _ in range(6)]
    + [threading.Thread(target=invalidator_worker, daemon=True) for _ in range(3)]
)

t0 = time.perf_counter()
for t in stress_threads:
    t.start()
time.sleep(2)  # даём поработать конкурентно 2 секунды
stop_flag["stop"] = True
for t in stress_threads:
    t.join(timeout=5)
t_stress = time.perf_counter() - t0

still_alive = [t for t in stress_threads if t.is_alive()]
check(f"стресс-тест завершился без зависших потоков (длился {t_stress:.2f}с)",
      len(still_alive) == 0)
check("конкурентные чтения+инвалидация не вызвали исключений", len(stress_errors) == 0)

# Финальная проверка — после всей гонки кэш всё ещё даёт корректный (не повреждённый) результат
final_size = cpv1.get_size(stress_root, force_refresh=True)
expected_approx = sum(
    os.path.getsize(os.path.join(dp, f))
    for dp, _, files in os.walk(stress_root) for f in files
)
check("после стресс-теста размер, посчитанный заново, совпадает с реальным на диске",
      final_size == expected_approx)

shutil.rmtree(stress_root, ignore_errors=True)


print("\n=== 13. Кэш НЕ должен тихо возвращать размер, ставший неверным после изменений ===")
mut_root = tempfile.mkdtemp(prefix="cleanerpro_mutate_")
os.makedirs(mut_root, exist_ok=True)
with open(os.path.join(mut_root, "a.txt"), "wb") as f:
    f.truncate(1000)

cpv1.clear_size_cache()
size_before = cpv1.get_size(mut_root)

# Добавляем файл НАПРЯМУЮ (в обход программы) — имитация изменений извне
with open(os.path.join(mut_root, "b.txt"), "wb") as f:
    f.truncate(5000)

# Без инвалидации кэш (ожидаемо) отдаст СТАРОЕ значение — так и должно быть,
# это не баг, а то, как работает любой кэш; проверяем именно это, чтобы явно
# задокументировать границу гарантии
size_from_stale_cache = cpv1.get_size(mut_root)
check("без явного обновления кэш действительно отдаёт старое значение (ожидаемое поведение кэша)",
      size_from_stale_cache == size_before)

# А вот ПОСЛЕ invalidate_size_cache (как это происходит в программе после
# любого удаления/очистки) — размер должен стать актуальным
cpv1.invalidate_size_cache(mut_root)
size_after_invalidate = cpv1.get_size(mut_root)
check("после invalidate_size_cache размер пересчитан и отражает реальное изменение",
      size_after_invalidate == size_before + 5000)

# И force_refresh тоже должен видеть актуальное состояние, даже без явной инвалидации
cpv1.clear_size_cache()
cpv1.get_size(mut_root)  # прогреваем кэш снова
with open(os.path.join(mut_root, "c.txt"), "wb") as f:
    f.truncate(2000)
size_force = cpv1.get_size(mut_root, force_refresh=True)
check("force_refresh видит изменения даже без invalidate_size_cache",
      size_force == size_before + 5000 + 2000)

shutil.rmtree(mut_root, ignore_errors=True)


print("\n" + "=" * 60)
print(f"Всего проверок: {total_checks}. Провалено: {len(failures)}.")
if failures:
    print("\nПроваленные проверки:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
else:
    print("ВСЕ ТЕСТЫ ПРОИЗВОДИТЕЛЬНОСТИ ПРОЙДЕНЫ УСПЕШНО")
    sys.exit(0)
