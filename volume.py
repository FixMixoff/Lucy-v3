

def _com_init():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def _endpoint():
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    ev = getattr(dev, "EndpointVolume", None)
    if ev is not None:
        return ev
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _clamp(percent):
    return max(0, min(100, int(percent))) / 100


def get_master():
    _com_init()
    return round(_endpoint().GetMasterVolumeLevelScalar() * 100)


def set_master(percent):
    _com_init()
    _endpoint().SetMasterVolumeLevelScalar(_clamp(percent), None)


def _app_sessions(exe_name):
    from pycaw.pycaw import AudioUtilities
    wanted = exe_name.lower()
    found = []
    for s in AudioUtilities.GetAllSessions():
        proc = getattr(s, "Process", None)
        try:
            name = (proc.name() if proc else "").lower()
        except Exception:
            name = ""
        if name == wanted:
            found.append(s)
    return found


def get_app(exe_name):
    _com_init()
    sessions = _app_sessions(exe_name)
    if not sessions:
        return None
    return round(sessions[0].SimpleAudioVolume.GetMasterVolume() * 100)


def set_app(exe_name, percent):
    _com_init()
    sessions = _app_sessions(exe_name)
    for s in sessions:
        s.SimpleAudioVolume.SetMasterVolume(_clamp(percent), None)
    return bool(sessions)
