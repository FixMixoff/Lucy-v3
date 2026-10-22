import asyncio
import subprocess
import sys
import time

_backend_cache = None


class _PyBackend:
    def __init__(self, name, Manager, Status):
        self.name, self.Manager, self.Status = name, Manager, Status

    def _matches(self, session, words):
        try:
            app_id = (session.source_app_user_model_id or "").lower()
        except Exception:
            return False
        return any(w.lower() in app_id for w in words)

    async def _find(self, words):
        manager = await self.Manager.request_async()
        for s in manager.get_sessions():
            if self._matches(s, words):
                return s
        return None

    def _playing(self, session):
        return session.get_playback_info().playback_status == self.Status.PLAYING

    def list_sessions(self):
        async def run():
            manager = await self.Manager.request_async()
            return [(s.source_app_user_model_id,
                     "играет" if self._playing(s) else "не играет")
                    for s in manager.get_sessions()]
        return asyncio.run(run())

    def is_playing(self, words):
        async def run():
            s = await self._find(words)
            return None if s is None else self._playing(s)
        return asyncio.run(run())

    def command(self, words, name):
        ops = {"next": "try_skip_next_async", "prev": "try_skip_previous_async",
               "pause": "try_pause_async", "play": "try_play_async",
               "toggle": "try_toggle_play_pause_async"}

        async def run():
            s = await self._find(words)
            if s is None:
                return False
            return bool(await getattr(s, ops[name])())
        return asyncio.run(run())

    def now_playing(self, words):
        async def run():
            s = await self._find(words)
            if s is None:
                return None
            props = await s.try_get_media_properties_async()
            return props.artist or "", props.title or "", self._playing(s)
        return asyncio.run(run())


def _load_py_backend():
    attempts = (
        ("winrt", "winrt.windows.media.control"),
        ("winsdk", "winsdk.windows.media.control"),
    )
    for name, module in attempts:
        try:
            mod = __import__(module, fromlist=["x"])
            return _PyBackend(name,
                              mod.GlobalSystemMediaTransportControlsSessionManager,
                              mod.GlobalSystemMediaTransportControlsSessionPlaybackStatus)
        except Exception:
            continue
    return None


_PS_HEADER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
$mgr = Await ($mgrType::RequestAsync()) $mgrType
$found = $null
foreach ($s in $mgr.GetSessions()) {
    $id = $s.SourceAppUserModelId.ToLower()
    foreach ($w in $words) { if ($id.Contains($w.ToLower())) { $found = $s; break } }
    if ($found) { break }
}
"""

_PS_ACTIONS = {
    "list": r"""
foreach ($s in $mgr.GetSessions()) { "$($s.SourceAppUserModelId)|$($s.GetPlaybackInfo().PlaybackStatus)" }
""",
    "status": r"""
if (-not $found) { 'NONE' } else { [string]$found.GetPlaybackInfo().PlaybackStatus }
""",
    "info": r"""
if (-not $found) { 'NONE' } else {
    $p = Await ($found.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    "$($p.Artist)|$($p.Title)|$($found.GetPlaybackInfo().PlaybackStatus)"
}
""",
}
for _name, _method in (("play", "TryPlayAsync"), ("pause", "TryPauseAsync"),
                       ("toggle", "TryTogglePlayPauseAsync"),
                       ("next", "TrySkipNextAsync"), ("prev", "TrySkipPreviousAsync")):
    _PS_ACTIONS[_name] = (
        "if (-not $found) { 'NONE' } else { "
        f"if (Await ($found.{_method}()) ([bool])) {{ 'OK' }} else {{ 'FAIL' }} }}\n")


class _PowerShellBackend:
    name = "powershell"

    def _run(self, words, action):
        quoted = ",".join("'" + w.replace("'", "''") + "'" for w in words)
        script = f"$words = @({quoted})\n" + _PS_HEADER + _PS_ACTIONS[action]
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, timeout=30)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    def list_sessions(self):
        rows = []
        for ln in self._run(["x"], "list"):
            app_id, _, status = ln.partition("|")
            rows.append((app_id, "играет" if status == "Playing" else "не играет"))
        return rows

    def is_playing(self, words):
        out = self._run(words, "status")
        if not out or out[0] == "NONE":
            return None
        return out[0] == "Playing"

    def command(self, words, name):
        out = self._run(words, name)
        return bool(out) and out[0] == "OK"

    def now_playing(self, words):
        out = self._run(words, "info")
        if not out or out[0] == "NONE":
            return None
        artist, title, status = (out[0].split("|") + ["", "", ""])[:3]
        return artist, title, status == "Playing"


def _backend():
    global _backend_cache
    if _backend_cache is None:
        _backend_cache = _load_py_backend()
        if _backend_cache is None:
            if sys.platform != "win32":
                raise RuntimeError("Медиацентр есть только в Windows")
            _backend_cache = _PowerShellBackend()
    return _backend_cache


def backend_name():
    return _backend().name


def list_sessions():
    return _backend().list_sessions()


def is_playing(words):
    return _backend().is_playing(words)


def command(words, name):
    return _backend().command(words, name)


def now_playing(words):
    return _backend().now_playing(words)


def ensure_playing(words, wait=45, key_cb=None, key_after=12):
    b = _backend()
    started = time.time()
    pressed = False
    while time.time() - started < wait:
        state = b.is_playing(words)
        if state is None:
            if key_cb and not pressed and time.time() - started > key_after:
                key_cb()
                pressed = True
            time.sleep(1)
            continue
        for _ in range(5):
            if state:
                return True
            b.command(words, "play")
            time.sleep(1.5)
            state = b.is_playing(words)
            if state is None:
                break
        if state is not None:
            return bool(state)
    return False
