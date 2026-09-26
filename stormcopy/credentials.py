"""Read a Steam Web API key without keeping it in the project or database."""

import os
import re


_NAME = "STEAM_API_KEY"
_WINDOWS_USER_ENVIRONMENT = "Environment"


def _usable(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _saved_windows_key():
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _WINDOWS_USER_ENVIRONMENT) as key:
            value, _kind = winreg.QueryValueEx(key, _NAME)
    except FileNotFoundError:
        return None
    return _usable(value)


def get_api_key(explicit=None):
    """Prefer a supplied key, then this process's environment, then HKCU.

    An explicitly present empty environment value suppresses the saved key.
    This lets callers deliberately disable discovery for one process.
    """
    if explicit is not None:
        return _usable(explicit)
    if _NAME in os.environ:
        return _usable(os.environ[_NAME])
    return _saved_windows_key()


def _broadcast_environment_change():
    """Ask Windows to refresh user variables in newly opened terminals."""
    if os.name != "nt":
        return
    try:
        import ctypes

        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                         ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                         ctypes.POINTER(ctypes.c_size_t))
        send.restype = ctypes.c_void_p
        result = ctypes.c_size_t()
        send(0xFFFF, 0x001A, 0, _WINDOWS_USER_ENVIRONMENT,
             0x0002, 5000, ctypes.byref(result))
    except (AttributeError, OSError):
        # Registry fallback works even when there is no desktop to notify.
        pass


def save_api_key(value):
    """Persist the key in this Windows user's environment (REG_SZ)."""
    if os.name != "nt":
        raise ValueError("set-api-key currently saves a user variable on Windows only")
    value = _usable(value)
    if value is None or any(character.isspace() for character in value):
        raise ValueError("Steam API key must be a nonempty value without whitespace")
    if (len(value) == 64 and value[:32].casefold() == value[32:].casefold()
            and re.fullmatch(r"[0-9a-fA-F]{32}", value[:32])):
        raise ValueError("Steam API key appears to have been pasted twice; "
                         "enter the single key shown at "
                         "https://steamcommunity.com/dev/apikey")
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            _WINDOWS_USER_ENVIRONMENT, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _NAME, 0, winreg.REG_SZ, value)
    _broadcast_environment_change()
    return {"saved": True, "scope": "Windows user environment"}


def clear_api_key():
    """Remove the saved user variable, if present."""
    if os.name != "nt":
        raise ValueError("set-api-key currently saves a user variable on Windows only")
    import winreg

    removed = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _WINDOWS_USER_ENVIRONMENT, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _NAME)
            removed = True
    except FileNotFoundError:
        pass
    if removed:
        _broadcast_environment_change()
    return {"saved": False, "removed": removed,
            "scope": "Windows user environment"}
