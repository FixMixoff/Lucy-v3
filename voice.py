import base64
import re
import subprocess
import threading

RU_HINTS = ("russian", "ru-ru", "ru_ru", "irina", "anna", "pavel", "elena", "aleksandr",
            "ирина", "анна", "павел", "елена", "русск")


def _clean(text):
    return re.sub(r"[^\w\s.,!?\-:;()]", "", text).strip()


def load_silero(cfg):
    import torch
    torch.set_num_threads(2)
    last = None
    for model_id in dict.fromkeys([cfg.silero_model, "v5_ru", "v4_ru"]):
        try:
            print(f"» [Голос] Загружаю Silero {model_id} на {cfg.tts_device}...")
            model, _ = torch.hub.load("snakers4/silero-models", "silero_tts",
                                      language="ru", speaker=model_id)
            model.to(torch.device(cfg.tts_device))
            return model
        except Exception as e:
            last = e
            print(f"» [Голос] Silero {model_id} не загрузилась: {type(e).__name__}: {e}")
    raise last


def _silero_audio(model, text, speaker):
    try:
        return model.apply_tts(text=text, speaker=speaker, sample_rate=48000,
                               put_accent=True, put_yo=True)
    except TypeError:
        return model.apply_tts(text=text, speaker=speaker, sample_rate=48000)


def make_silero(cfg):
    import sounddevice as sd
    model = load_silero(cfg)
    speakers = list(getattr(model, "speakers", []) or [])
    speaker = cfg.tts_speaker
    if speakers and speaker not in speakers:
        fallback = next((n for n in ("kseniya", "xenia", "baya") if n in speakers), speakers[0])
        print(f"» [Голос] Голоса «{speaker}» нет, беру «{fallback}». Доступны: {', '.join(speakers)}")
        speaker = fallback

    def speak(text):
        clean = _clean(text)
        if clean:
            audio = _silero_audio(model, clean, speaker)
            sd.play(audio.cpu().numpy(), 48000)
            sd.wait()
    return speak


def silero_demo(cfg, phrase):
    import sounddevice as sd
    model = load_silero(cfg)
    for speaker in getattr(model, "speakers", []) or [cfg.tts_speaker]:
        print(f"\nГолос: {speaker}")
        audio = _silero_audio(model, _clean(phrase), speaker)
        sd.play(audio.cpu().numpy(), 48000)
        sd.wait()
    print("\nВыбранный голос впишите в config.txt: TTS_SPEAKER = имя")


def make_pyttsx3(cfg):
    import pyttsx3
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    want = cfg.pyttsx3_voice
    chosen = None
    if want.upper().startswith("HKEY_"):
        chosen = want
    else:
        for v in voices:
            blob = f"{v.name} {v.id} {getattr(v, 'languages', '')}".lower()
            if (want and want.lower() in blob) or (not want and any(h in blob for h in RU_HINTS)):
                chosen = v.id
                break
    if not chosen:
        raise RuntimeError("в Windows не найден русский голос (SAPI5); посмотрите: python lucy.py --voices")
    engine.setProperty("voice", chosen)
    engine.setProperty("rate", 175)

    def speak(text):
        clean = _clean(text)
        if clean:
            engine.say(clean)
            engine.runAndWait()
    return speak


_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$want = '__WANT__'
$voice = $null
foreach ($v in $s.GetInstalledVoices()) {
    $i = $v.VoiceInfo
    if ($want -ne '' -and $i.Name.ToLower().Contains($want.ToLower())) { $voice = $i; break }
    if ($want -eq '' -and $i.Culture.Name -like 'ru*') { $voice = $i; break }
}
if ($voice -eq $null) { [Console]::Out.WriteLine('NOVOICE'); [Console]::Out.Flush(); exit 1 }
$s.SelectVoice($voice.Name)
[Console]::Out.WriteLine('READY|' + $voice.Name); [Console]::Out.Flush()
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($line -eq $null) { break }
    if ($line.Trim().Length -gt 0) { try { $s.Speak($line) } catch {} }
    [Console]::Out.WriteLine('DONE'); [Console]::Out.Flush()
}
"""


def make_sapi(cfg):
    want = cfg.pyttsx3_voice if not cfg.pyttsx3_voice.upper().startswith("HKEY_") else ""
    script = _SAPI_SCRIPT.replace("__WANT__", want.replace("'", "''"))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=0x08000000)
    watchdog = threading.Timer(20, proc.kill)
    watchdog.start()
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("PowerShell завершился без ответа")
            line = line.strip().lstrip("\ufeff")
            if line.startswith("NOVOICE"):
                proc.kill()
                raise RuntimeError("в Windows нет русского голоса (SAPI5); посмотрите: python lucy.py --voices")
            if line.startswith("READY"):
                print("» [Голос] Голос Windows:", line.partition("|")[2])
                break
    finally:
        watchdog.cancel()
    lock = threading.Lock()

    def speak(text):
        clean = _clean(text)
        if not clean:
            return
        with lock:
            proc.stdin.write(clean.replace("\n", " ") + "\n")
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("PowerShell-озвучка остановилась")
                if line.strip().lstrip("\ufeff") == "DONE":
                    return
    return speak


BACKENDS = {"silero": make_silero, "pyttsx3": make_pyttsx3, "sapi": make_sapi}


def create(cfg, log=print):
    order = [cfg.tts_backend] if cfg.tts_backend in BACKENDS else []
    order += [n for n in ("silero", "pyttsx3", "sapi") if n not in order]
    for name in order:
        try:
            speak = BACKENDS[name](cfg)
            log(f"» [Голос] Озвучка работает: {name}")
            return name, speak
        except Exception as e:
            log(f"» [Голос] {name}: не получилось ({type(e).__name__}: {e})")
    log("» [Голос] Озвучка не заработала, Lucy будет отвечать только текстом. "
        "Подробности: python lucy.py --voices")
    return None, None


def list_voices(log=print):
    log("Способы озвучки:")
    for pkg in ("torch", "sounddevice", "pyttsx3"):
        try:
            __import__(pkg)
            log(f"  пакет {pkg}: установлен")
        except Exception as e:
            log(f"  пакет {pkg}: НЕТ ({type(e).__name__})")
    log("\nГолоса Windows (SAPI5):")
    script = ("Add-Type -AssemblyName System.Speech; "
              "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
              "ForEach-Object { $_.VoiceInfo.Name + ' | ' + $_.VoiceInfo.Culture.Name }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30,
                           creationflags=0x08000000)
        rows = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if rows:
            for ln in rows:
                log("  " + ln)
            if not any("ru" in ln.split("|")[-1].lower() for ln in rows):
                log("  (русского голоса среди них нет)")
        else:
            log("  голосов не найдено")
    except Exception as e:
        log(f"  не удалось получить список: {e}")
