import argparse
import collections
import configparser
import gc
import json
import os
import queue
import re
import sys
import threading
import time

import matcher

BASE = os.path.dirname(os.path.abspath(__file__))
if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

def whisper_prompt(wake):
    w = wake.capitalize()
    return (f"{w}, включи музыку. {w}, поставь таймер на пять минут. "
            f"{w}, найди рецепт борща. {w}, запиши купить хлеб.")


class Config:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(path, encoding="utf-8"):
            raise SystemExit(f"Не найден файл настроек: {path}")

        def get(sec, key, default=""):
            return cp.get(sec, key, fallback=default).strip()

        def get_list(sec, key):
            return [x.strip() for x in get(sec, key).split(",") if x.strip()]

        self.yandex_music = get("PATHS", "YANDEX_MUSIC")
        self.zapret_dir = get("PATHS", "ZAPRET_DIR")
        self.zapret_bat = get("PATHS", "ZAPRET_BAT")
        self.zapret_task = get("PATHS", "ZAPRET_TASK")

        self.vosk_model = get("SETTINGS", "VOSK_MODEL", "model")
        mic = get("SETTINGS", "MIC_DEVICE")
        self.mic = None if not mic else int(mic) if mic.isdigit() else mic
        self.threshold = float(get("SETTINGS", "VOICE_THRESHOLD", "0.015"))
        self.wake_ratio = float(get("SETTINGS", "WAKE_RATIO", "0.66"))
        self.whisper_model = get("SETTINGS", "WHISPER_MODEL", "base")
        self.whisper_device = get("SETTINGS", "WHISPER_DEVICE", "cuda")
        self.whisper_model_light = get("SETTINGS", "WHISPER_MODEL_LIGHT", "base")
        self.whisper_beam = int(get("SETTINGS", "WHISPER_BEAM", "5"))
        self.whisper_normalize = get("SETTINGS", "WHISPER_NORMALIZE", "true").lower() == "true"
        self.text_source = get("SETTINGS", "TEXT_SOURCE", "vosk").lower()
        self.tts_backend = get("SETTINGS", "TTS_BACKEND", "auto").lower()
        self.tts_device = get("SETTINGS", "TTS_DEVICE", "cpu")
        self.tts_speaker = get("SETTINGS", "TTS_SPEAKER", "kseniya")
        self.silero_model = get("SETTINGS", "SILERO_MODEL", "v5_5_ru")
        self.wake_word = get("SETTINGS", "WAKE_WORD", "люси").lower().replace("ё", "е")
        self.name = get("SETTINGS", "WAKE_WORD", "люси").strip().capitalize()
        self.pyttsx3_voice = get("SETTINGS", "PYTTSX3_VOICE")
        self.whisper_hints = get_list("SETTINGS", "WHISPER_HINTS")
        self.media_match = get_list("SETTINGS", "MEDIA_MATCH") or ["yandex", "яндекс", "ya_m"]
        self.music_hotkey = get("SETTINGS", "MUSIC_HOTKEY", "k")[:1] or "k"
        self.music_window = get("SETTINGS", "MUSIC_WINDOW", "Яндекс Музыка")
        self.music_start_delay = float(get("SETTINGS", "MUSIC_START_DELAY", "3"))
        self.music_minimize = get("SETTINGS", "MUSIC_MINIMIZE", "true").lower() == "true"
        self.corrections = {k.lower(): v for k, v in cp.items("CORRECTIONS")} \
            if cp.has_section("CORRECTIONS") else {}

        self.game_close = get_list("GAME_MODE", "CLOSE")
        self.game_keep = get_list("GAME_MODE", "KEEP")
        self.game_processes = get_list("GAME_MODE", "PROCESSES")
        self.game_auto = get("GAME_MODE", "AUTO_LIGHT", "true").lower() == "true"
        self.game_close_timeout = float(get("GAME_MODE", "CLOSE_TIMEOUT", "8"))
        self.game_force = get("GAME_MODE", "FORCE_KILL", "false").lower() == "true"
        self.game_power_plan = get("GAME_MODE", "POWER_PLAN", "false").lower() == "true"
        self.restore = get("GAME_MODE", "RESTORE", "true").lower() == "true"

        self.chat_enabled = get("CHAT", "ENABLED", "true").lower() == "true"
        self.chat_model = get("CHAT", "MODEL", "qwen3:8b")
        self.chat_host = get("CHAT", "HOST", "http://127.0.0.1:11434")
        self.chat_keep_alive = get("CHAT", "KEEP_ALIVE", "5m")
        self.chat_temperature = float(get("CHAT", "TEMPERATURE", "0.7"))
        self.chat_max_tokens = int(get("CHAT", "MAX_TOKENS", "160"))
        self.chat_max_sentences = int(get("CHAT", "MAX_SENTENCES", "3"))
        self.chat_memory_turns = int(get("CHAT", "MEMORY_TURNS", "8"))
        self.chat_memory_minutes = float(get("CHAT", "MEMORY_MINUTES", "10"))
        self.chat_in_game = get("CHAT", "IN_GAME_MODE", "false").lower() == "true"
        self.chat_autostart = get("CHAT", "AUTOSTART", "true").lower() == "true"
        self.chat_preload = get("CHAT", "PRELOAD", "false").lower() == "true"
        self.chat_persona_file = get("CHAT", "PERSONA_FILE", "persona.txt")

        self.web_enabled = get("WEB", "ENABLED", "true").lower() == "true"
        self.web_port = int(get("WEB", "PORT", "8080"))
        self.web_password = get("WEB", "PASSWORD")
        files_dir = get("WEB", "FILES_DIR", "lucy_files")
        self.web_files_dir = files_dir if os.path.isabs(files_dir) else os.path.join(BASE, files_dir)
        self.web_max_mb = int(get("WEB", "MAX_MB", "500"))
        self.web_speak = get("WEB", "SPEAK", "false").lower() == "true"
        self.web_allow_public = get("WEB", "ALLOW_PUBLIC", "false").lower() == "true"
        self.restore_args = {k.lower(): v for k, v in cp.items("RESTORE_ARGS")} \
            if cp.has_section("RESTORE_ARGS") else {}


Utterance = collections.namedtuple("Utterance", "text audio")


class Listener:
    RATE = 16000
    CHUNK = 1600
    HANG = 8
    MAX_SECONDS = 8

    def __init__(self, cfg):
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        if not os.path.isdir(cfg.vosk_model):
            raise SystemExit(f"Не найдена папка модели Vosk: {cfg.vosk_model}\n"
                             "Скачай vosk-model-small-ru и укажи путь в config.txt (VOSK_MODEL).")
        print("» [Слух] Загружаю Vosk...")
        self.rec = KaldiRecognizer(Model(cfg.vosk_model), self.RATE)
        self.thr = cfg.threshold * 32768
        self.q = queue.Queue(maxsize=300)
        self.muted = threading.Event()
        self.stream = sd.RawInputStream(samplerate=self.RATE, blocksize=self.CHUNK,
                                        dtype="int16", channels=1, device=cfg.mic,
                                        callback=self._callback)

    def _callback(self, indata, frames, time_info, status):
        if self.muted.is_set():
            return
        try:
            self.q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def start(self):
        self.stream.start()

    def drain(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def next_utterance(self, timeout=None):
        import numpy as np
        deadline = None if timeout is None else time.time() + timeout
        preroll = collections.deque(maxlen=3)
        active, quiet, audio, started = False, 0, [], 0.0
        while True:
            if not active and deadline is not None and time.time() > deadline:
                return None
            try:
                data = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            loud = int(np.abs(np.frombuffer(data, dtype=np.int16).astype(np.int32)).max()) > self.thr
            if not active:
                if not loud:
                    preroll.append(data)
                    continue
                active, quiet, started = True, 0, time.time()
                audio = list(preroll)
                preroll.clear()
                for d in audio:
                    self.rec.AcceptWaveform(d)
            audio.append(data)
            text = None
            if self.rec.AcceptWaveform(data):
                text = json.loads(self.rec.Result()).get("text", "")
            else:
                quiet = 0 if loud else quiet + 1
                if quiet >= self.HANG or time.time() - started > self.MAX_SECONDS:
                    text = json.loads(self.rec.FinalResult()).get("text", "")
            if text is not None:
                if text.strip():
                    return Utterance(text.strip(), b"".join(audio))
                active, quiet, audio = False, 0, []


class Speaker:

    def __init__(self, cfg, listener):
        self.cfg, self.listener = cfg, listener
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text, wait=False):
        print(f"{self.cfg.name}: {text}")
        done = threading.Event()
        self.q.put((text, done))
        if wait:
            done.wait(timeout=30)

    def _worker(self):
        import voice
        _, speak = voice.create(self.cfg)
        while True:
            text, done = self.q.get()
            self.listener.muted.set()
            try:
                if speak:
                    speak(text)
            except Exception as e:
                print(f"» [Голос] Ошибка озвучки: {e}")
            finally:
                done.set()
                if self.q.empty():
                    time.sleep(0.3)
                    self.listener.drain()
                    self.listener.muted.clear()


class WhisperBox:

    def __init__(self, cfg):
        self.cfg, self.model, self.device = cfg, None, None
        self.failed = False
        self.lock = threading.Lock()

    def _load(self, device):
        from faster_whisper import WhisperModel
        compute = "float16" if device == "cuda" else "int8"
        name = self.cfg.whisper_model if device == "cuda" else self.cfg.whisper_model_light
        print(f"» [Whisper] Загружаю {name} на {device}...")
        return WhisperModel(name, device=device, compute_type=compute)

    def get(self, light):
        device = "cpu" if light else self.cfg.whisper_device
        with self.lock:
            if self.failed:
                return None
            if self.model is not None and self.device == device:
                return self.model
            self.model = None
            gc.collect()
            for dev in ([device, "cpu"] if device != "cpu" else ["cpu"]):
                try:
                    self.model, self.device = self._load(dev), dev
                    return self.model
                except Exception as e:
                    print(f"» [Whisper] Не удалось на {dev}: {e}")
            self.failed = True
            return None

    def release(self):
        with self.lock:
            self.model = None
            gc.collect()

    def transcribe(self, pcm, light):
        import numpy as np
        model = self.get(light)
        if model is None:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if self.cfg.whisper_normalize:
            peak = float(np.abs(audio).max())
            if peak < 0.005:
                return ""
            audio = audio * min(0.9 / peak, 30.0)
        prompt = whisper_prompt(self.cfg.wake_word)
        if self.cfg.whisper_hints:
            prompt += " " + ", ".join(self.cfg.whisper_hints) + "."
        segments, _ = model.transcribe(audio, language="ru", beam_size=self.cfg.whisper_beam,
                                       temperature=0.0, condition_on_previous_text=False,
                                       without_timestamps=True, initial_prompt=prompt)
        return "".join(s.text for s in segments).strip()


class Ctx:
    def __init__(self, cfg, speaker, whisper):
        self.cfg, self.speaker, self.whisper = cfg, speaker, whisper
        self.base = BASE
        self.manual_light = False
        self.auto_light = False
        self.closed = {}
        self.brain = None

    @property
    def light(self):
        return self.manual_light or self.auto_light

    def say(self, text, wait=False):
        self.speaker.say(text, wait)

    def _changed(self, was_light):
        if self.light and not was_light:
            self.whisper.release()
            if self.brain:
                threading.Thread(target=self.brain.unload, daemon=True).start()
            print("» [Режим] Лёгкий режим: Whisper на процессоре, разговорная модель выгружена")
        elif was_light and not self.light:
            if self.brain and self.cfg.chat_preload:
                threading.Thread(target=self.brain.warmup, daemon=True).start()
            print("» [Режим] Обычный режим")

    def set_manual_light(self, on):
        was = self.light
        self.manual_light = on
        self._changed(was)

    def set_auto_light(self, on):
        was = self.light
        self.auto_light = on
        self._changed(was)

    def transcribe(self, audio):
        return self.whisper.transcribe(audio, self.light)


def game_watcher(ctx):
    import psutil
    games = {n.lower() for n in ctx.cfg.game_processes}
    while True:
        time.sleep(10)
        try:
            running = any((p.info["name"] or "").lower() in games
                          for p in psutil.process_iter(["name"]))
        except Exception:
            continue
        if running != ctx.auto_light:
            print("» [Режим]", "обнаружена игра" if running else "игра закрыта")
            ctx.set_auto_light(running)


def ask_param(ctx, listener, cmd):
    ctx.say(cmd.ask, wait=True)
    u = listener.next_utterance(timeout=8)
    if not u:
        ctx.say("Не услышала")
        return None
    if ctx.cfg.text_source == "vosk":
        return matcher.strip_wake(u.text)
    return matcher.strip_wake(ctx.transcribe(u.audio) or u.text)


def chat_reply(ctx, text):
    brain = ctx.brain
    if brain is None:
        ctx.say("Не поняла команду")
        return
    if ctx.light and not ctx.cfg.chat_in_game:
        ctx.say("В игровом режиме я отвечаю только на команды")
        return

    def run():
        try:
            if not brain.ensure_server():
                print("» [Разговор] Ollama не запущена. Установите её (ollama.com) "
                      f"и выполните: ollama pull {ctx.cfg.chat_model}")
                ctx.say("Разговорная модель не запущена")
                return
            for sentence in brain.reply_stream(text):
                ctx.say(sentence)
        except Exception as e:
            print(f"» [Разговор] Ошибка: {brain.explain(e)}")
            ctx.say("Разговорная модель не отвечает")
    threading.Thread(target=run, daemon=True).start()


def process(ctx, listener, utt, wake_required=True):
    from actions import HANDLERS
    text_v = utt.text
    if wake_required and not matcher.has_wake(text_v):
        print(f"[фон] {text_v}")
        return
    print(f"\nВы: {text_v}")
    rest_v = matcher.strip_wake(text_v)

    if not rest_v:
        ctx.say("Слушаю", wait=True)
        nxt = listener.next_utterance(timeout=6)
        if nxt:
            process(ctx, listener, nxt, wake_required=False)
        return

    m = matcher.match(rest_v)
    w = ""
    need_whisper = (m is None
                    or (m.cmd.param == "text" and ctx.cfg.text_source == "whisper")
                    or (m.cmd.param == "duration" and matcher.parse_duration(m.param) == 0))
    if need_whisper:
        w = ctx.transcribe(utt.audio)
        if w:
            print(f"   (Whisper: {w})")
            m_w = matcher.match(matcher.strip_wake(w))
            if m_w is not None:
                m = m_w
    if m is None:
        chat_reply(ctx, matcher.strip_wake_text(w) if w else rest_v)
        return

    cmd, param = m.cmd, m.param
    if cmd.param == "text" and not param.strip():
        param = ask_param(ctx, listener, cmd)
    elif cmd.param == "duration" and matcher.parse_duration(param) == 0:
        param = ask_param(ctx, listener, cmd)
    if param is None:
        return
    if cmd.param == "text" and ctx.cfg.corrections:
        param = matcher.apply_corrections(param, ctx.cfg.corrections)

    if cmd.confirm:
        ctx.say(cmd.confirm, wait=True)
        ans = listener.next_utterance(timeout=6)
        if not (ans and matcher.is_yes(ans.text)):
            ctx.say("Отменила")
            return

    print(f"» [Команда] {cmd.name} ({param})")

    def run():
        try:
            HANDLERS[cmd.name](ctx, param)
        except Exception as e:
            print(f"» [Ошибка команды {cmd.name}]: {e}")
            ctx.say("Не получилось")
    threading.Thread(target=run, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(BASE, "config.txt"))
    ap.add_argument("--test", metavar="ФРАЗА")
    ap.add_argument("--mic-test", action="store_true")
    ap.add_argument("--test-game", action="store_true")
    ap.add_argument("--chat", metavar="ТЕКСТ", nargs="+",
                    help="проверить разговорную модель без микрофона: --chat Привет, как дела?")
    ap.add_argument("--say", metavar="ТЕКСТ", help="проверить озвучку: Lucy произнесёт этот текст")
    ap.add_argument("--voice-demo", action="store_true", help="прослушать все голоса Silero по очереди")
    ap.add_argument("--send", metavar="ФАЙЛ", nargs="+", help="положить файлы в папку для телефона")
    ap.add_argument("--install-sendto", action="store_true",
                    help="добавить в «Отправить» (правая кнопка по файлу) пункт «Lucy на телефон»")
    ap.add_argument("--voices", action="store_true", help="показать, какие голоса и способы озвучки доступны")
    ap.add_argument("--web-check", action="store_true", help="проверить, почему сайт не открывается с телефона")
    ap.add_argument("--open-firewall", action="store_true",
                    help="открыть порт сайта в брандмауэре Windows (запросит права администратора)")
    ap.add_argument("--run", metavar="КОМАНДА", nargs="+",
                    help="выполнить команду без микрофона, например: --run radio  или  --run timer 5 секунд")
    ap.add_argument("--list-media", action="store_true",
                    help="показать плееры, которые видит Windows (для настройки MEDIA_MATCH)")
    args = ap.parse_args()

    if args.test:
        text = args.test
        print(f"Фраза: {text!r}")
        print("Обращение к Lucy распознано:", matcher.has_wake(text))
        m = matcher.match(matcher.strip_wake(text))
        if m is None:
            print("Команда: не найдена, фраза уйдёт разговорной модели")
        else:
            print(f"Команда: {m.cmd.name}")
            print(f"Параметр: {m.param!r}")
            if m.cmd.param == "duration":
                print(f"Время: {matcher.parse_duration(m.param)} сек")
            if m.cmd.confirm:
                print(f"Переспросит: {m.cmd.confirm}")
        return

    if args.voices:
        import voice
        voice.list_voices()
        return

    if args.list_media:
        import media
        try:
            rows = media.list_sessions()
        except Exception as e:
            print("Не получилось обратиться к медиацентру Windows:", e)
            print("Попробуйте: pip install winrt-runtime winrt-Windows.Media.Control "
                  "winrt-Windows.Foundation winrt-Windows.Foundation.Collections")
            return
        print(f"Способ доступа: {media.backend_name()}\n")
        if not rows:
            print("Windows сейчас не видит ни одного плеера. Включите музыку в Яндекс Музыке и повторите.")
        for app_id, status in rows:
            print(f"{app_id}  [{status}]")
        print("\nИмя Яндекс Музыки должно содержать слово из MEDIA_MATCH в config.txt.")
        return

    cfg = Config(args.config)
    matcher.WAKE_RATIO = cfg.wake_ratio
    matcher.WAKE_WORD = cfg.wake_word

    if args.chat:
        from brain import Brain
        brain = Brain(cfg, BASE)
        if not brain.ensure_server():
            raise SystemExit("Ollama не запущена. Установите её с ollama.com и запустите.")
        try:
            for sentence in brain.reply_stream(" ".join(args.chat)):
                print(f"{cfg.wake_word.capitalize()}: {sentence}")
        except Exception as e:
            raise SystemExit(f"Ошибка: {brain.explain(e)}")
        return

    if args.voice_demo:
        import voice
        voice.silero_demo(cfg, f"Привет! Я {cfg.wake_word.capitalize()}. Чем могу помочь?")
        return

    if args.send:
        import datetime
        import traceback

        def log_send(text):
            try:
                with open(os.path.join(BASE, "lucy_send.log"), "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
            except Exception:
                pass

        def beep(ok):
            try:
                import winsound
                for i in range(1 if ok else 2):
                    winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
                    time.sleep(0.25)
            except Exception:
                pass

        try:
            from actions import copy_to_outbox
            n = copy_to_outbox(cfg, args.send)
        except Exception:
            log_send("ОШИБКА при копировании:\n" + traceback.format_exc())
            print("Не удалось отправить, подробности в lucy_send.log")
            beep(False)
            return
        if n:
            log_send(f"Отправлено файлов: {n} из {len(args.send)}")
            print(f"Отправлено файлов на телефон: {n}")
        else:
            log_send(f"Ничего не отправлено (папки не поддерживаются, файлы не найдены): {args.send}")
            print("Ничего не отправлено: нужны файлы, а не папки.")
        beep(bool(n))
        return

    if args.install_sendto:
        import win32com.client
        folder = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "SendTo")
        exe = sys.executable
        pyw = exe.replace("python.exe", "pythonw.exe")
        link = win32com.client.Dispatch("WScript.Shell").CreateShortCut(
            os.path.join(folder, "Lucy на телефон.lnk"))
        link.TargetPath = pyw if os.path.exists(pyw) else exe
        link.Arguments = f'"{os.path.join(BASE, "lucy.py")}" --send'
        link.WorkingDirectory = BASE
        link.WindowStyle = 7
        link.save()
        print("Готово: теперь правая кнопка по файлу > Отправить > «Lucy на телефон».")
        return

    if args.say:
        import voice
        name, speak = voice.create(cfg)
        if speak:
            speak(args.say)
            print("Готово. Если вы ничего не услышали, проверьте громкость и устройство вывода звука.")
        return

    if args.web_check or args.open_firewall:
        import web
        (web.open_firewall if args.open_firewall else web.web_check)(cfg.web_port)
        return

    if args.run:
        from actions import HANDLERS

        class PrintSpeaker:
            def say(self, text, wait=False):
                print(f"{cfg.name}: {text}")
        ctx = Ctx(cfg, PrintSpeaker(), WhisperBox(cfg))
        name, param = args.run[0], " ".join(args.run[1:])
        if name not in HANDLERS:
            raise SystemExit(f"Нет такой команды: {name}. Есть: {', '.join(sorted(HANDLERS))}")
        HANDLERS[name](ctx, param)
        time.sleep(1)
        return

    if args.test_game:
        from actions import game_targets, PROTECTED
        print("В списке CLOSE:", ", ".join(cfg.game_close) or "(пусто)")
        print("Не трогаем (KEEP):", ", ".join(cfg.game_keep) or "(пусто)")
        targets = game_targets(cfg)
        if targets:
            print("\nСейчас были бы закрыты (запущены):")
            for name, procs in sorted(targets.items()):
                print(f"  {name}: процессов {len(procs)}")
        else:
            print("\nИз списка CLOSE сейчас ничего не запущено.")
        blocked = [n for n in cfg.game_close if n.lower() in PROTECTED]
        if blocked:
            print("\nЗащищены и не будут закрыты:", ", ".join(blocked))
        return

    listener = Listener(cfg)
    listener.start()

    if args.mic_test:
        print("Проверка микрофона. Говорите; Ctrl+C - выход.\n")
        try:
            while True:
                u = listener.next_utterance()
                print(f"Слышу: {u.text!r}  | «{cfg.name}»: {matcher.has_wake(u.text)}")
                m = matcher.match(matcher.strip_wake(u.text))
                if m:
                    print(f"          команда: {m.cmd.name}, параметр: {m.param!r}")
        except KeyboardInterrupt:
            return

    speaker = Speaker(cfg, listener)
    whisper = WhisperBox(cfg)
    ctx = Ctx(cfg, speaker, whisper)
    if cfg.game_auto and cfg.game_processes:
        threading.Thread(target=game_watcher, args=(ctx,), daemon=True).start()
    if not ctx.light:
        threading.Thread(target=lambda: whisper.get(False), daemon=True).start()

    if cfg.chat_enabled:
        from brain import Brain
        ctx.brain = Brain(cfg, BASE)
        if cfg.chat_preload and not ctx.light:
            threading.Thread(target=ctx.brain.warmup, daemon=True).start()

    if cfg.web_enabled:
        try:
            from web import WebServer
            WebServer(ctx).start()
        except ImportError as e:
            print(f"» [Сайт] Не хватает пакета ({e}). Установите: pip install flask")

    print(f"\n» [Готово] Скажите: «{cfg.wake_word.capitalize()}, ...». Ctrl+C - выход.")
    while True:
        try:
            u = listener.next_utterance()
            if u:
                process(ctx, listener, u)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"» [Ошибка] {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
