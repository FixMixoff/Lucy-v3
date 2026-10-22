import ctypes
import datetime
import os
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import quote_plus

from matcher import parse_duration

VK_VOLUME_MUTE = 0xAD
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

PROTECTED = {
    "explorer.exe", "svchost.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "lsass.exe",
    "services.exe", "system", "wininit.exe", "smss.exe", "python.exe", "pythonw.exe",
    "cmd.exe", "conhost.exe", "windowsterminal.exe", "taskmgr.exe",
}


def press(code):
    u = ctypes.windll.user32
    u.keybd_event(code, 0, 0, 0)
    u.keybd_event(code, 0, 2, 0)


_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь",
          "девять", "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
          "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят"]


def num_words(n, fem=False):
    if n < 20:
        w = _UNITS[n]
        return {"один": "одна", "два": "две"}.get(w, w) if fem else w
    if n >= 60:
        return str(n)
    t, u = divmod(n, 10)
    return _TENS[t] if u == 0 else f"{_TENS[t]} {num_words(u, fem)}"


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def human_duration(sec):
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{num_words(h)} {plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{num_words(m, True)} {plural(m, 'минута', 'минуты', 'минут')}")
    if s or not parts:
        parts.append(f"{num_words(s, True)} {plural(s, 'секунда', 'секунды', 'секунд')}")
    return " ".join(parts)


def is_running(exe_name):
    import psutil
    name = exe_name.lower()
    return any((p.info["name"] or "").lower() == name for p in psutil.process_iter(["name"]))


def ensure_zapret(ctx):
    cfg = ctx.cfg
    if is_running("winws.exe"):
        return
    if cfg.zapret_task:
        r = subprocess.run(["schtasks", "/run", "/tn", cfg.zapret_task],
                           capture_output=True, text=True, encoding="cp866", errors="replace")
        if r.returncode == 0:
            time.sleep(2)
            return
        print("» [Zapret] Задача не запустилась:", (r.stderr or r.stdout).strip())
        print("   (если «отказано в доступе» - запустите Lucy от имени администратора)")
    bat = os.path.join(cfg.zapret_dir, cfg.zapret_bat)
    if os.path.exists(bat):
        ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c call "{bat}"',
                                            cfg.zapret_dir, 1)
        time.sleep(2)

BROWSER_EXES = {"opera.exe"}


def _show_browser():
    import psutil
    import win32con
    import win32gui
    import win32process
    pids = {p.pid for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower() in BROWSER_EXES}
    best, best_area = None, 0

    def callback(hwnd, _):
        nonlocal best, best_area
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                left, top, right, bottom = win32gui.GetWindowPlacement(hwnd)[4]  
            except Exception:
                return True
            area = (right - left) * (bottom - top)
            if pid in pids and area > best_area:  
                best, best_area = hwnd, area
        return True

    win32gui.EnumWindows(callback, None)
    if not best:
        return False
    win32gui.ShowWindow(best, win32con.SW_RESTORE if win32gui.IsIconic(best) else win32con.SW_SHOW)
    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)  
    ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
    try:
        win32gui.SetForegroundWindow(best)
    except Exception:
        pass
    return True


def open_url(url):
    webbrowser.open(url)
    try:
        for _ in range(10):                 
            time.sleep(0.5)
            if _show_browser():
                break
    except Exception as e:
        print("» [Браузер] не удалось развернуть окно:", e)
def _media_cmd(ctx, name, fallback_key, times=1):
    ok = False
    try:
        import media
        for i in range(times):
            ok = media.command(ctx.cfg.media_match, name)
            if i + 1 < times:
                time.sleep(0.3)
    except Exception as e:
        print("» [Музыка] медиацентр недоступен:", e)
    if not ok:
        for _ in range(times):
            press(fallback_key)


def _find_music_window(cfg):
    import psutil
    import win32gui
    import win32process
    exe = os.path.basename(cfg.yandex_music).lower()
    pids = {p.pid for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower() == exe}
    part = cfg.music_window.lower()
    found = []

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and part in title.lower():
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                except Exception:
                    return True
                if pid in pids:
                    found.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return found[0] if found else None


def _focus_and_press(hwnd, key):
    import win32con
    import win32gui
    u = ctypes.windll.user32
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE if win32gui.IsIconic(hwnd) else win32con.SW_SHOW)
    u.keybd_event(0x12, 0, 0, 0)   
    u.keybd_event(0x12, 0, 2, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:
        print("» [Музыка] не удалось вывести окно вперёд:", e)
    time.sleep(0.4)
    vk = ord(key.upper())
    scan = u.MapVirtualKeyW(vk, 0)
    u.keybd_event(vk, scan, 0, 0)
    time.sleep(0.05)
    u.keybd_event(vk, scan, 2, 0)


def _minimize(hwnd):
    import win32con
    import win32gui
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    except Exception:
        pass


def _music_state(ctx):
    try:
        import media
        return media.is_playing(ctx.cfg.media_match), True
    except Exception as e:
        print("» [Музыка] медиацентр недоступен:", e)
        return None, False


def radio(ctx, param):
    cfg = ctx.cfg
    path = cfg.yandex_music
    if not (path and os.path.exists(path)):
        open_url("https://music.yandex.ru")
        return
    running = is_running(os.path.basename(path))
    hwnd = None
    if not running:
        ctx.say("Включаю музыку")
        subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):             
            hwnd = _find_music_window(cfg)
            if hwnd:
                break
            time.sleep(0.5)
        print("» [Музыка] окно приложения:", "найдено" if hwnd else "не найдено")
        time.sleep(cfg.music_start_delay) 

    state, media_ok = _music_state(ctx)
    print(f"» [Музыка] сейчас играет: {state}")
    if state is True:
        return
    if state is False:                 
        try:
            import media
            media.command(cfg.media_match, "play")
            time.sleep(2)
            state, _ = _music_state(ctx)
            print(f"» [Музыка] после команды play играет: {state}")
        except Exception as e:
            print("» [Музыка] команда play не прошла:", e)
        if state:
            return

    for attempt in (1, 2):
        hwnd = _find_music_window(cfg) or hwnd
        if hwnd:
            _focus_and_press(hwnd, cfg.music_hotkey)
        else:
            press(VK_MEDIA_PLAY_PAUSE)
        print(f"» [Музыка] нажала {'«' + cfg.music_hotkey + '» в окне' if hwnd else 'медиа-клавишу'} (попытка {attempt})")
        if not media_ok:
            break                       
        for _ in range(8):             
            time.sleep(0.5)
            state, _ = _music_state(ctx)
            if state:
                break
        if state:
            break
    if hwnd and cfg.music_minimize:
        _minimize(hwnd)
    if media_ok and not state:
        ctx.say("Не получилось включить воспроизведение")


def next_track(ctx, param):
    _media_cmd(ctx, "next", VK_MEDIA_NEXT_TRACK)


def prev_track(ctx, param):
    _media_cmd(ctx, "prev", VK_MEDIA_PREV_TRACK, times=2)


def play_pause(ctx, param):
    _media_cmd(ctx, "toggle", VK_MEDIA_PLAY_PAUSE)


def now_playing(ctx, param):
    try:
        import media
        info = media.now_playing(ctx.cfg.media_match)
    except Exception as e:
        print("» [Музыка] медиацентр недоступен:", e)
        info = None
    if not info or not info[1]:
        ctx.say("Сейчас ничего не играет")
        return
    artist, title, playing = info
    what = f"{title}, {artist}" if artist else title
    ctx.say(("Играет " if playing else "На паузе: ") + what)


def mute(ctx, param):
    press(VK_VOLUME_MUTE)


def desk(ctx, param):
    u = ctypes.windll.user32
    u.keybd_event(0x5B, 0, 0, 0)   
    u.keybd_event(0x44, 0, 0, 0)  
    u.keybd_event(0x44, 0, 2, 0)
    u.keybd_event(0x5B, 0, 2, 0)


def screenshot(ctx, param):
    from PIL import ImageGrab
    folder = os.path.join(ctx.base, "screenshots")
    os.makedirs(folder, exist_ok=True)
    name = datetime.datetime.now().strftime("screen_%Y-%m-%d_%H-%M-%S.png")
    ImageGrab.grab().save(os.path.join(folder, name))
    ctx.say("Снимок готов")


def mon_off(ctx, param):
    ctx.say("Отключаю мониторы")
    time.sleep(1)
    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)


def ctime(ctx, param):
    now = datetime.datetime.now()
    h, m = now.hour, now.minute
    hours = f"{num_words(h)} {plural(h, 'час', 'часа', 'часов')}"
    if m == 0:
        ctx.say(f"Сейчас ровно {hours}")
    else:
        ctx.say(f"Сейчас {hours} {num_words(m, True)} {plural(m, 'минута', 'минуты', 'минут')}")


def search(ctx, param):
    ctx.say("Ищу")
    open_url("https://www.google.com/search?q=" + quote_plus(param))


def _beep_melody():
    import winsound
    notes = [(659, 150), (659, 150), (0, 150), (659, 150), (0, 150), (523, 150),
             (659, 150), (0, 150), (783, 300)]
    for _ in range(3):
        for freq, ms in notes:
            if freq == 0:
                time.sleep(ms / 1000)
            else:
                winsound.Beep(freq, ms)
        time.sleep(0.5)


def timer(ctx, param):
    sec = parse_duration(param)
    if sec <= 0:
        ctx.say("Не поняла, на сколько")
        return
    ctx.say(f"Таймер на {human_duration(sec)}")
    try:
        subprocess.Popen([sys.executable, os.path.join(ctx.base, "timer_window.py"), str(sec)],
                         creationflags=0x08000000)  
    except Exception as e:
        print("» [Таймер] не удалось открыть окошко:", e)

    def finish():
        ctx.say("Время вышло")
        _beep_melody()

    t = threading.Timer(sec, finish)
    t.daemon = True
    t.start()


def _notes_path(ctx):
    return os.path.join(ctx.base, "notes.txt")


def add_note(ctx, param):
    text = param.strip()
    for lead in ("заметку", "заметка", "что"):
        if text.lower().startswith(lead + " "):
            text = text[len(lead) + 1:]
    if not text:
        ctx.say("Что записать?")
        return
    with open(_notes_path(ctx), "a", encoding="utf-8") as f:
        f.write(f"- {text}\n")
    ctx.say("Записала")


def read_notes(ctx, param):
    path = _notes_path(ctx)
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    if not lines:
        ctx.say("Заметок пока нет")
        return
    ctx.say("Твои заметки")
    for ln in lines[:5]:
        ctx.say(ln.lstrip("- "), wait=True)
    if len(lines) > 5:
        ctx.say("Остальное в файле заметок")


def clear_notes(ctx, param):
    path = _notes_path(ctx)
    if os.path.exists(path):
        os.remove(path)
        ctx.say("Все заметки удалены")
    else:
        ctx.say("Удалять нечего")


def save_clipboard(ctx, param):
    import pyperclip
    text = pyperclip.paste()
    if not text:
        ctx.say("В буфере обмена ничего нет")
        return
    with open(os.path.join(ctx.base, "buffer_history.txt"), "a", encoding="utf-8") as f:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"[{now}]: {text}\n" + "-" * 30 + "\n")
    ctx.say("Текст из буфера сохранён")


def _unique(folder, name):
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return path


def copy_to_outbox(cfg, paths):
    import shutil
    folder = os.path.join(cfg.web_files_dir, "outbox")
    os.makedirs(folder, exist_ok=True)
    count = 0
    for p in paths:
        if os.path.isfile(p):
            shutil.copy2(p, _unique(folder, os.path.basename(p)))
            count += 1
    return count


def _clipboard_files():
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return [f for f in win32clipboard.GetClipboardData(win32con.CF_HDROP)]
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    return []


def send_to_phone(ctx, param):
    files = _clipboard_files()
    if files:
        n = copy_to_outbox(ctx.cfg, files)
        if n:
            ctx.say(f"Отправила на телефон: {n} {plural(n, 'файл', 'файла', 'файлов')}")
        else:
            ctx.say("Папки отправить нельзя, только файлы")
        return
    import pyperclip
    text = pyperclip.paste()
    if not text or not text.strip():
        ctx.say("В буфере обмена ничего нет")
        return
    folder = os.path.join(ctx.cfg.web_files_dir, "outbox")
    os.makedirs(folder, exist_ok=True)
    name = datetime.datetime.now().strftime("буфер_%Y-%m-%d_%H-%M-%S.txt")
    with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
        f.write(text)
    ctx.say("Отправила на телефон" if ctx.cfg.web_enabled else "Сохранила в папку для телефона")


def youtube(ctx, param):
    ctx.say("Запускаю ютуб")
    ensure_zapret(ctx)
    open_url("https://www.youtube.com")


def discord(ctx, param):
    ctx.say("Запускаю дискорд")
    ensure_zapret(ctx)
    os.system("start discord://")



def empty_trash(ctx, param):
    import winshell
    try:
        winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=True)
        ctx.say("Корзина очищена")
    except Exception:
        ctx.say("Корзина и так пуста")


def power_off(ctx, param):
    os.system("shutdown /s /t 10")
    ctx.say(f"Выключаю через десять секунд. Чтобы отменить, скажи: {ctx.cfg.wake_word}, отмени выключение")


def cancel_off(ctx, param):
    os.system("shutdown /a")
    ctx.say("Выключение отменено")


def game_targets(cfg):
    import psutil
    close = {n.lower() for n in cfg.game_close}
    keep = {n.lower() for n in cfg.game_keep} | PROTECTED
    own = os.getpid()
    found = {}
    for p in psutil.process_iter(["pid", "name"]):
        name = (p.info["name"] or "").lower()
        if name in close and name not in keep and p.info["pid"] != own:
            found.setdefault(name, []).append(p)
    return found


def close_gracefully(targets, timeout, force=False):
    import psutil
    import win32con
    import win32gui
    import win32process
    procs = [p for plist in targets.values() for p in plist]
    pids = {p.pid for p in procs}

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return True
            if pid in pids:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True

    win32gui.EnumWindows(callback, None)
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    if alive and force:
        for p in alive:
            try:
                p.terminate()
            except Exception:
                pass
        _, alive = psutil.wait_procs(alive, timeout=3)
    return alive


def _snapshot(targets):
    snap = {}
    for name, plist in targets.items():
        main = None
        for p in plist:
            try:
                parent = p.parent()
                if parent is None or (parent.name() or "").lower() != name:
                    main = p      
                    break
            except Exception:
                pass
        main = main or plist[0]
        try:
            snap[name] = main.exe()
        except Exception:
            pass
    return snap


def game_on(ctx, param):
    targets = game_targets(ctx.cfg)
    snap = _snapshot(targets)
    ctx.set_manual_light(True)
    if ctx.cfg.game_power_plan:
        subprocess.run(["powercfg", "/setactive", "SCHEME_MIN"], capture_output=True)
    if not targets:
        ctx.say("Игровой режим включён")
        return
    print("» [Игровой режим] закрываю:", ", ".join(sorted(targets)))
    alive = close_gracefully(targets, ctx.cfg.game_close_timeout, ctx.cfg.game_force)
    alive_names = set()
    for p in alive:
        try:
            alive_names.add(p.name().lower())
        except Exception:
            pass
    ctx.closed.update({n: exe for n, exe in snap.items() if n not in alive_names})
    if alive:
        names = alive_names
        print("» [Игровой режим] не закрылись:", ", ".join(sorted(names)))
        ctx.say("Игровой режим включён, но кое-что не закрылось")
    else:
        ctx.say("Игровой режим включён")


def game_off(ctx, param):
    ctx.set_manual_light(False)
    if ctx.cfg.game_power_plan:
        subprocess.run(["powercfg", "/setactive", "SCHEME_BALANCED"], capture_output=True)
    restored = []
    if ctx.cfg.restore:
        for name, exe in list(ctx.closed.items()):
            if is_running(name):
                continue
            args = ctx.cfg.restore_args.get(name, "").split()
            try:
                subprocess.Popen([exe] + args, creationflags=0x00000008 | 0x00000200,
                                 close_fds=True)
                restored.append(name)
            except Exception as e:
                print(f"» [Игровой режим] не смог открыть {name}: {e}")
    ctx.closed.clear()
    ctx.say("Обычный режим" + (", возвращаю программы" if restored else ""))


HANDLERS = {
    "radio": radio, "ctime": ctime, "next": next_track, "prev": prev_track,
    "now_playing": now_playing,
    "plpa": play_pause, "mute": mute, "desk": desk, "search": search, "timer": timer,
    "screenshot": screenshot, "add_note": add_note, "read_notes": read_notes,
    "clear_notes": clear_notes, "save_clipboard": save_clipboard, "mon_off": mon_off,
    "empty_trash": empty_trash, "off": power_off, "cancel_off": cancel_off,
    "youtube": youtube, "discord": discord, "dnevnik": dnevnik, "send_to_phone": send_to_phone,
    "game_on": game_on, "game_off": game_off,
}
