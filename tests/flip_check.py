"""The real-monitor check: a stand-in game loads the DLL, HDR must turn on for the primary monitor only, and off again soon after a hard kill, even one right after it came on. Then the same
stand-in on a hidden desktop, with no test setting, must change nothing.

THIS VISIBLY FLIPS THE PRIMARY MONITOR. Run it only when nobody is playing or watching the screen. It refuses while a game runs on the normal desktop.

    python tests/flip_check.py [path to HdrSwitch.dll]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hdr_state import monitors  # noqa: E402
from watcher_check import documents  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GAMES = ("SkyrimSE.exe", "Fallout4.exe", "Fallout4VR.exe", "SkyrimVR.exe")
ON_LIMIT_S = 10.0
OFF_LIMIT_S = 6.0   # the watcher reacts at once (watcher_check); Windows then takes its moment to change modes, which set_hdr waits out for up to 5 s

STAND_IN = r"""
import ctypes, sys, time
class Interface(ctypes.Structure):
    _fields_ = [("xse", ctypes.c_uint32), ("runtime", ctypes.c_uint32), ("editor", ctypes.c_uint32), ("isEditor", ctypes.c_uint32), ("rest", ctypes.c_void_p * 4)]
dll = ctypes.WinDLL(sys.argv[1])
load = getattr(dll, "SKSEPlugin_Load")
load.restype = ctypes.c_bool
load(ctypes.byref(Interface()))
print("loaded", flush=True)
time.sleep(120)
"""


def game_on_desktop() -> list[str]:
    listing = subprocess.run(["tasklist", "/fo", "csv", "/nh", "/v"], capture_output=True, text=True, timeout=60).stdout
    return [name for name in GAMES if name.lower() in listing.lower()]


def wait_for(wanted: bool, limit: float) -> float | None:
    started = time.perf_counter()
    while time.perf_counter() - started < limit:
        if next(m for m in monitors() if m["primary"])["on"] is wanted:
            return time.perf_counter() - started
        time.sleep(0.02)
    return None


def others(state: list[dict]) -> list:
    return [(m["target"], m["on"]) for m in state if not m["primary"]]


def hidden_desktop_run(dll: Path) -> bool:
    """The stand-in on a desktop of its own, without HDR_SWITCH_OFF. CreateProcess with a lpDesktop, which Python's subprocess cannot pass."""

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR), ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                    ("xs", wintypes.DWORD), ("ys", wintypes.DWORD), ("xc", wintypes.DWORD), ("yc", wintypes.DWORD), ("attr", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("show", wintypes.WORD), ("r2", wintypes.WORD), ("r3", ctypes.c_void_p), ("i", wintypes.HANDLE), ("o", wintypes.HANDLE), ("e", wintypes.HANDLE)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE), ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]

    user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    desktop = user32.CreateDesktopW("HdrSwitchTest", None, None, 0, 0x10000000, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    log = documents() / "My Games" / "Skyrim Special Edition" / "SKSE" / "HdrSwitch.log"
    script = Path(os.environ["TEMP"]) / f"hdrswitch-standin-{os.getpid()}.py"
    script.write_text(STAND_IN, encoding="utf-8")
    start = StartupInfo(cb=ctypes.sizeof(StartupInfo), desktop="HdrSwitchTest")
    info = ProcessInfo()
    command = ctypes.create_unicode_buffer(f'"{sys.executable}" "{script}" "{dll}"')
    environment = {k: v for k, v in os.environ.items() if k.upper() != "HDR_SWITCH_OFF"}
    block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in environment.items()) + "\0")
    try:
        if not kernel32.CreateProcessW(None, command, None, None, False, 0x08000400, block, None, ctypes.byref(start), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        before = monitors()
        started = time.perf_counter()
        text = ""
        while time.perf_counter() - started < 20:
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            if "skipped" in text or "HDR on" in text:
                break
            time.sleep(0.1)
        time.sleep(1)
        unchanged = monitors() == before
        print(f"hidden desktop: log said {text.strip()!r}; monitors unchanged: {unchanged}")
        kernel32.TerminateProcess(info.process, 1)
        kernel32.WaitForSingleObject(info.process, 5000)
        return unchanged and "hidden desktop" in text
    finally:
        kernel32.CloseHandle(info.process)
        kernel32.CloseHandle(info.thread)
        user32.CloseDesktop(desktop)
        script.unlink(missing_ok=True)


def main() -> int:
    dll = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "HdrSwitch.dll"
    running = game_on_desktop()
    if running:
        print(f"REFUSED: {running} running; this check flips the monitor")
        return 2
    start_state = monitors()
    primary = next(m for m in start_state if m["primary"])
    print("before:", start_state)
    if not primary["supported"] or primary["on"]:
        print("REFUSED: the primary monitor must offer HDR and have it off")
        return 2
    log = documents() / "My Games" / "Skyrim Special Edition" / "SKSE" / "HdrSwitch.log"
    saved = log.read_bytes() if log.exists() else None
    env = {k: v for k, v in os.environ.items() if k.upper() != "HDR_SWITCH_OFF"}
    passed = True
    game = None
    try:
        # Round 1 kills the stand-in the moment HDR comes on, the earliest a crash could strand it; round 2 kills it once settled, the usual case.
        for settle in (0.0, 3.0):
            game = subprocess.Popen([sys.executable, "-c", STAND_IN, str(dll)], env=env)
            on = wait_for(True, ON_LIMIT_S)
            during = monitors()
            print(f"HDR on after {on} s:", during)
            if on is None or others(during) != others(start_state):
                print("FAILED: the primary did not turn on, or another monitor changed")
                passed = False
            time.sleep(settle)
            killed = time.perf_counter()
            game.kill()
            off = wait_for(False, OFF_LIMIT_S)
            taken = None if off is None else time.perf_counter() - killed
            after = monitors()
            print(f"settled {settle} s; HDR off {taken} s after the kill:", after)
            if off is None or after != start_state:
                print("FAILED: HDR did not go back to how it was")
                passed = False
            time.sleep(1)
        print("log:", log.read_text(encoding="utf-8") if log.exists() else "(none)")
        time.sleep(1)
        log.unlink(missing_ok=True)
        if not hidden_desktop_run(dll):
            print("FAILED: the hidden stand-in did not stay quiet")
            passed = False
    finally:
        if game is not None and game.poll() is None:
            game.kill()
        if saved is None:
            log.unlink(missing_ok=True)
        else:
            log.write_bytes(saved)
    print("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
