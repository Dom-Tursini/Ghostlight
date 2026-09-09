"""
Sensor backend selection.

Two backends, each the right one for its platform:

  kinect10   Microsoft Kinect for Windows SDK 1.8. The only thing that opens a
             Kinect v1 on Windows, because Microsoft's KinectCamera driver owns
             the device and OpenNI2's PS1080 cannot see past it.

  openni2    OpenNI2 + PS1080. Works on Linux, where the kernel enumerates the
             sensor and nothing claims it exclusively. Also the path for
             PrimeSense and Structure sensors.

Order matters: on Windows we try the SDK first, since PS1080 will always come
back empty there. Both modules expose the same shape, so callers never care
which one they got.
"""

from __future__ import annotations

import sys

_ORDER = ['kinect10', 'openni2'] if sys.platform == 'win32' else ['openni2']

LABELS = {
    'kinect10': 'Kinect for Windows SDK',
    'openni2': 'OpenNI2',
}

_modules = {}


def _load(name):
    if name not in _modules:
        try:
            _modules[name] = __import__(name)
        except Exception:
            _modules[name] = None
    return _modules[name]


def available():
    """Backends that imported cleanly, as [(name, module)]."""
    out = []
    for name in _ORDER:
        mod = _load(name)
        if mod is not None:
            out.append((name, mod))
    return out


def initialize():
    """Initialise every backend that loads. Failures are not fatal."""
    for _name, mod in available():
        try:
            mod.initialize()
        except Exception:
            pass


def shutdown():
    for _name, mod in available():
        try:
            mod.shutdown()
        except Exception:
            pass


def reload():
    """Ask each backend to re-enumerate from scratch.

    Only needed for the Windows SDK, which decides what exists at load time and
    never looks again. Cheap enough to call when nothing has been found, and
    never called while a sensor is open.
    """
    for _name, mod in available():
        fn = getattr(mod, 'reload', None)
        if fn is None:
            continue
        try:
            fn()
        except Exception:
            pass


def find():
    """First backend reporting a device.

    Returns (name, module, device_info) or (None, None, None).
    """
    for name, mod in available():
        try:
            devices = mod.list_devices()
        except Exception:
            continue
        if devices:
            return name, mod, devices[0]
    return None, None, None
