"""Does HDR go back off however the game is closed? A stand-in game with a real window loads the DLL and is then closed each way in turn.

THIS VISIBLY FLIPS THE PRIMARY MONITOR once per way, and shows a small window. Run it only when nobody is playing. It refuses while a game runs.

    python tests/close_check.py [path to HdrSwitch.dll]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flip_check import game_on_desktop, wait_for  # noqa: E402
from hdr_state import monitors  # noqa: E402
from watcher_check import documents  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OFF_LIMIT_S = 8.0

STAND_IN = r"""
import ctypes, sys, tkinter
class Interface(ctypes.Structure):
    _fields_ = [("xse", ctypes.c_uint32), ("runtime", ctypes.c_uint32), ("editor", ctypes.c_uint32), ("isEditor", ctypes.c_uint32), ("rest", ctypes.c_void_p * 4)]
dll = ctypes.WinDLL(sys.argv[1])
dll.SKSEPlugin_Load.restype = ctypes.c_bool
dll.SKSEPlugin_Load(ctypes.byref(Interface()))
how = sys.argv[3]
root = tkinter.Tk()
root.title(sys.argv[2])
root.geometry("320x60")
tkinter.Label(root, text="HDR Switch test: closes by itself").pack()
if how == "quits":
    root.after(1500, lambda: __import__("os")._exit(0))
elif how == "crashes":
    # A real crash: Python catches an ordinary bad read, this it can't. No Windows error box.
    ctypes.windll.kernel32.SetErrorMode(2)
    root.after(1500, lambda: ctypes.windll.kernel32.RaiseFailFastException(None, None, 0))
root.mainloop()
"""


def window_of(title: str, limit: float = 10.0) -> int:
    started = time.perf_counter()
    while time.perf_counter() - started < limit:
        hwnd = ctypes.WinDLL("user32").FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.05)
    return 0


def close(how: str, game: subprocess.Popen, hwnd: int) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    user32.EndTask.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    if how == "close button":
        user32.PostMessageW(hwnd, 0x0010, None, None)   # WM_CLOSE
    elif how == "force kill":
        game.kill()
    elif how == "End Task call":
        user32.EndTask(hwnd, 0, 1)
    elif how == "kill whole process tree":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(game.pid)], capture_output=True, timeout=30)


def put_back(dll: Path, primary: dict) -> None:
    """Turn HDR off with the plugin's own watcher, told to watch a process that is already gone."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait(timeout=20)
    rundll = Path(os.environ["SystemRoot"]) / "System32" / "rundll32.exe"
    subprocess.run(f'"{rundll}" "{dll}",HdrSwitchWatch {gone.pid} {primary["adapter"][0]} {primary["adapter"][1]} {primary["target"]} SKSE', timeout=20)


def main() -> int:
    dll = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "HdrSwitch.dll"
    if not dll.is_file():
        print(f"REFUSED: {dll} is not there; build first")
        return 2
    running = game_on_desktop()
    if running:
        print(f"REFUSED: {running} running; this check flips the monitor")
        return 2
    start_state = monitors()
    if next(m for m in start_state if m["primary"])["on"]:
        print("REFUSED: HDR is on already")
        return 2
    log = documents() / "My Games" / "Skyrim Special Edition" / "SKSE" / "HdrSwitch.log"
    saved = log.read_bytes() if log.exists() else None
    env = {k: v for k, v in os.environ.items() if k.upper() != "HDR_SWITCH_OFF"}
    failed = []
    try:
        for how in ("quits", "crashes", "close button", "force kill", "End Task call", "kill whole process tree"):
            title = f"HDR Switch test {os.getpid()}"
            game = subprocess.Popen([sys.executable, "-c", STAND_IN, str(dll), title, how], env=env)
            hwnd = window_of(title)
            on = wait_for(True, 10.0)
            time.sleep(0.5)
            closed = time.perf_counter()
            close(how, game, hwnd)
            try:
                game.wait(timeout=20)
            except subprocess.TimeoutExpired:
                print(f"FAILED  {how}: the stand-in did not close; killing it")
                failed.append(how)
                game.kill()
                game.wait(timeout=20)
                wait_for(False, OFF_LIMIT_S)
                continue
            off = wait_for(False, OFF_LIMIT_S)
            after = monitors()
            good = on is not None and off is not None and after == start_state
            taken = "never" if off is None else f"{time.perf_counter() - closed:.2f} s"
            print(f"{'PASSED' if good else 'FAILED'}  {how}: HDR off after {taken}, game exit code {game.returncode}")
            if not good:
                failed.append(how)
                if off is None:
                    print("  HDR stayed on; putting it back and stopping here")
                    put_back(dll, next(m for m in start_state if m["primary"]))
                    print("  monitors now:", monitors())
                    break
            time.sleep(1)
    finally:
        if saved is None:
            log.unlink(missing_ok=True)
        else:
            log.write_bytes(saved)
    print("PASSED" if not failed else f"FAILED: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
