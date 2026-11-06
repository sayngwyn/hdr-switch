"""Does the watcher notice a hard-killed game within a second? Harmless to run at any time: the watcher is given a monitor that does not exist, so Windows refuses the
change and nothing on screen flips; only its reaction time is measured. It writes to the Skyrim log, so that log is put back afterwards.

    python tests/watcher_check.py [path to HdrSwitch.dll]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = 3
LIMIT_S = 1.0


def documents() -> Path:
    guid = ctypes.c_char_p(bytes.fromhex("D0 9A D3 FD 8F 23 AF 46 AD B4 6C 85 48 03 69 C7".replace(" ", "")))   # FOLDERID_Documents
    out = ctypes.c_wchar_p()
    if ctypes.WinDLL("shell32").SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out)):
        raise OSError("no Documents folder")
    return Path(out.value)


def main() -> int:
    dll = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "HdrSwitch.dll"
    if not dll.is_file():   # rundll32 shows an error box on screen for a missing DLL
        print(f"REFUSED: {dll} is not there; build first")
        return 2
    log = documents() / "My Games" / "Skyrim Special Edition" / "SKSE" / "HdrSwitch.log"
    before = log.read_bytes() if log.exists() else None
    rundll = Path(os.environ["SystemRoot"]) / "System32" / "rundll32.exe"
    worst = 0.0
    try:
        for _ in range(RUNS):
            game = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            watcher = subprocess.Popen(f'"{rundll}" "{dll}",HdrSwitchWatch {game.pid} 0 0 4294967295 SKSE')
            time.sleep(1.0)
            if watcher.poll() is not None:
                print("FAILED: the watcher did not stay running while the game ran")
                game.kill()
                return 1
            killed = time.perf_counter()
            game.kill()
            watcher.wait(timeout=15)
            taken = time.perf_counter() - killed
            worst = max(worst, taken)
            print(f"watcher ended {taken:.3f} s after the kill")
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if text.count("game ended") < RUNS:
            print(f"FAILED: expected {RUNS} 'game ended' lines in the log, got: {text!r}")
            return 1
    finally:
        if before is None:
            log.unlink(missing_ok=True)
        else:
            log.write_bytes(before)
    # set_hdr returns at once when Windows refuses, so this is the reaction time alone
    if worst > LIMIT_S:
        print(f"FAILED: worst {worst:.3f} s, limit {LIMIT_S} s")
        return 1
    print(f"PASSED: worst {worst:.3f} s, limit {LIMIT_S} s, log put back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
