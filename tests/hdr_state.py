"""Print every active monitor and its Windows HDR state. Read-only: it never changes anything on the monitors.

    python tests/hdr_state.py
"""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes

QDC_ONLY_ACTIVE_PATHS = 2
SOURCE_MODE = 1
INVALID_IDX = 0xFFFFFFFF
GET_ADVANCED_COLOR = 9
GET_ADVANCED_COLOR_2 = 15


class Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class Source(ctypes.Structure):
    _fields_ = [("adapter", Luid), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class Target(ctypes.Structure):
    _fields_ = [("adapter", Luid), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32), ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", ctypes.c_int), ("statusFlags", ctypes.c_uint32)]


class Path(ctypes.Structure):
    _fields_ = [("source", Source), ("target", Target), ("flags", ctypes.c_uint32)]


class Mode(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapter", Luid), ("raw", ctypes.c_byte * 48)]


class Header(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapter", Luid), ("id", ctypes.c_uint32)]


class ColorInfo2(ctypes.Structure):
    _fields_ = [("header", Header), ("value", ctypes.c_uint32), ("encoding", ctypes.c_uint32), ("bits", ctypes.c_uint32), ("mode", ctypes.c_uint32)]


class ColorInfo(ctypes.Structure):
    _fields_ = [("header", Header), ("value", ctypes.c_uint32), ("encoding", ctypes.c_uint32), ("bits", ctypes.c_uint32)]


class TargetName(ctypes.Structure):
    _fields_ = [("header", Header), ("flags", ctypes.c_uint32), ("technology", ctypes.c_uint32), ("manufacturer", ctypes.c_uint16), ("product", ctypes.c_uint16),
                ("connector", ctypes.c_uint32), ("name", ctypes.c_wchar * 64), ("path", ctypes.c_wchar * 128)]


def monitors() -> list[dict]:
    user32 = ctypes.WinDLL("user32")
    path_count, mode_count = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count)):
        raise OSError("GetDisplayConfigBufferSizes failed")
    paths, modes = (Path * path_count.value)(), (Mode * mode_count.value)()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), paths, ctypes.byref(mode_count), modes, None):
        raise OSError("QueryDisplayConfig failed")
    found = []
    for path in paths[: path_count.value]:
        index = path.source.modeInfoIdx
        primary = False
        if index != INVALID_IDX and index < mode_count.value and modes[index].infoType == SOURCE_MODE:
            # DISPLAYCONFIG_SOURCE_MODE: width, height, pixel format, then the desktop position
            x, y = ctypes.c_int32.from_buffer(modes[index].raw, 12).value, ctypes.c_int32.from_buffer(modes[index].raw, 16).value
            primary = x == 0 and y == 0
        name = TargetName()
        name.header = Header(2, ctypes.sizeof(TargetName), path.target.adapter, path.target.id)
        user32.DisplayConfigGetDeviceInfo(ctypes.byref(name.header))
        row = {"name": name.name, "target": path.target.id, "adapter": [path.target.adapter.low, path.target.adapter.high], "primary": primary, "supported": None, "on": None}
        modern = ColorInfo2()
        modern.header = Header(GET_ADVANCED_COLOR_2, ctypes.sizeof(ColorInfo2), path.target.adapter, path.target.id)
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(modern.header)) == 0:
            row["supported"], row["on"] = bool(modern.value & 1 << 4), bool(modern.value & 1 << 5)
        else:
            older = ColorInfo()
            older.header = Header(GET_ADVANCED_COLOR, ctypes.sizeof(ColorInfo), path.target.adapter, path.target.id)
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(older.header)) == 0:
                row["supported"], row["on"] = bool(older.value & 1), bool(older.value & 2)
        found.append(row)
    return found


if __name__ == "__main__":
    print(json.dumps(monitors(), indent=1))
