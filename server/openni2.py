"""
Minimal ctypes binding for OpenNI2.

There is no maintained Python binding for OpenNI2, so this wraps the parts of
the C API we actually need: enumerate devices, open one, create depth/colour
streams, pull frames. The C API is small and has not changed since 2.2.

The runtime lives in ./runtime and is Occipital's 2021 build (Apache 2.0).
"""

from __future__ import annotations

import ctypes as C
import os
import sys
from pathlib import Path

# --- constants ---------------------------------------------------------------

ONI_API_VERSION = 2002          # major*1000 + minor, i.e. 2.2
ONI_MAX_STR = 256

STATUS = {
    0: 'OK', 1: 'ERROR', 2: 'NOT_IMPLEMENTED', 3: 'NOT_SUPPORTED',
    4: 'BAD_PARAMETER', 5: 'OUT_OF_FLOW', 6: 'NO_DEVICE', 102: 'TIME_OUT',
}

SENSOR_IR, SENSOR_COLOR, SENSOR_DEPTH = 1, 2, 3

PIXEL_DEPTH_1_MM = 100
PIXEL_DEPTH_100_UM = 101
PIXEL_RGB888 = 200

STREAM_PROPERTY_VIDEO_MODE = 3

RUNTIME = Path(__file__).parent / 'runtime'
WINDOWS = sys.platform == 'win32'

# Where to look for the OpenNI2 shared library, in order. On Windows we ship it;
# on Linux we prefer the system package (libopenni2-0) and fall back to a local
# runtime directory if someone has dropped one in.
def _candidates():
    if WINDOWS:
        return [RUNTIME / 'OpenNI2.dll', Path('OpenNI2.dll')]
    names = ['libOpenNI2.so', 'libOpenNI2.so.0']
    here = [RUNTIME / n for n in names]
    system = [Path('/usr/lib/libOpenNI2.so'),
              Path('/usr/lib/x86_64-linux-gnu/libOpenNI2.so'),
              Path('/usr/local/lib/libOpenNI2.so')]
    return here + system + [Path(n) for n in names]


# --- structs -----------------------------------------------------------------

class OniVersion(C.Structure):
    _fields_ = [('major', C.c_int), ('minor', C.c_int),
                ('maintenance', C.c_int), ('build', C.c_int)]


class OniDeviceInfo(C.Structure):
    _fields_ = [('uri', C.c_char * ONI_MAX_STR),
                ('vendor', C.c_char * ONI_MAX_STR),
                ('name', C.c_char * ONI_MAX_STR),
                ('usbVendorId', C.c_uint16),
                ('usbProductId', C.c_uint16)]


class OniVideoMode(C.Structure):
    _fields_ = [('pixelFormat', C.c_int), ('resolutionX', C.c_int),
                ('resolutionY', C.c_int), ('fps', C.c_int)]


class OniFrame(C.Structure):
    _fields_ = [('dataSize', C.c_int),
                ('data', C.c_void_p),
                ('sensorType', C.c_int),
                ('timestamp', C.c_uint64),
                ('frameIndex', C.c_int),
                ('width', C.c_int),
                ('height', C.c_int),
                ('videoMode', OniVideoMode),
                ('croppingEnabled', C.c_int),
                ('cropOriginX', C.c_int),
                ('cropOriginY', C.c_int),
                ('stride', C.c_int)]


# --- library -----------------------------------------------------------------

class OpenNIError(RuntimeError):
    pass


_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib

    target = None
    for c in _candidates():
        if c.is_absolute():
            if c.exists():
                target = c
                break
        else:
            target = c          # bare name, let the loader search
            break
    if target is None:
        raise OpenNIError(
            'OpenNI2 library not found. On Debian/Ubuntu: apt install libopenni2-0')

    # OpenNI2 locates its drivers relative to its own module path, and on
    # py3.8+ ctypes will not search PATH for dependent DLLs.
    if WINDOWS and hasattr(os, 'add_dll_directory') and RUNTIME.exists():
        os.add_dll_directory(str(RUNTIME.resolve()))
        os.environ['PATH'] = str(RUNTIME.resolve()) + os.pathsep + os.environ.get('PATH', '')

    try:
        lib = C.CDLL(str(target))
    except OSError as e:
        raise OpenNIError('could not load %s: %s' % (target, e))

    lib.oniInitialize.argtypes = [C.c_int]
    lib.oniInitialize.restype = C.c_int
    lib.oniShutdown.argtypes = []
    lib.oniGetDeviceList.argtypes = [C.POINTER(C.POINTER(OniDeviceInfo)), C.POINTER(C.c_int)]
    lib.oniGetDeviceList.restype = C.c_int
    lib.oniReleaseDeviceList.argtypes = [C.POINTER(OniDeviceInfo)]
    lib.oniDeviceOpen.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
    lib.oniDeviceOpen.restype = C.c_int
    lib.oniDeviceClose.argtypes = [C.c_void_p]
    lib.oniDeviceClose.restype = C.c_int
    lib.oniDeviceCreateStream.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_void_p)]
    lib.oniDeviceCreateStream.restype = C.c_int
    lib.oniStreamStart.argtypes = [C.c_void_p]
    lib.oniStreamStart.restype = C.c_int
    lib.oniStreamStop.argtypes = [C.c_void_p]
    lib.oniStreamDestroy.argtypes = [C.c_void_p]
    lib.oniStreamReadFrame.argtypes = [C.c_void_p, C.POINTER(C.POINTER(OniFrame))]
    lib.oniStreamReadFrame.restype = C.c_int
    lib.oniFrameRelease.argtypes = [C.POINTER(OniFrame)]
    lib.oniStreamSetProperty.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_int]
    lib.oniStreamSetProperty.restype = C.c_int
    lib.oniStreamGetProperty.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.POINTER(C.c_int)]
    lib.oniStreamGetProperty.restype = C.c_int
    lib.oniWaitForAnyStream.argtypes = [C.POINTER(C.c_void_p), C.c_int, C.POINTER(C.c_int), C.c_int]
    lib.oniWaitForAnyStream.restype = C.c_int
    lib.oniGetExtendedError.argtypes = []
    lib.oniGetExtendedError.restype = C.c_char_p

    _lib = lib
    return lib


def _check(status, what):
    if status != 0:
        detail = (_lib.oniGetExtendedError() or b'').decode(errors='replace').strip()
        name = STATUS.get(status, str(status))
        msg = '%s failed: %s' % (what, name)
        if detail:
            msg += ' (%s)' % detail
        raise OpenNIError(msg)


# --- api ---------------------------------------------------------------------

def initialize():
    lib = _load()
    _check(lib.oniInitialize(ONI_API_VERSION), 'oniInitialize')


def shutdown():
    if _lib:
        _lib.oniShutdown()



def reload():
    """OpenNI2 re-enumerates on demand, so there is nothing to reload."""
    return None

def list_devices():
    lib = _load()
    arr = C.POINTER(OniDeviceInfo)()
    n = C.c_int(0)
    _check(lib.oniGetDeviceList(C.byref(arr), C.byref(n)), 'oniGetDeviceList')
    out = []
    for i in range(n.value):
        d = arr[i]
        out.append({
            'uri': d.uri.decode(errors='replace'),
            'vendor': d.vendor.decode(errors='replace'),
            'name': d.name.decode(errors='replace'),
            'vid': '%04x' % d.usbVendorId,
            'pid': '%04x' % d.usbProductId,
        })
    lib.oniReleaseDeviceList(arr)
    return out


class Stream:
    def __init__(self, handle, kind):
        self._h = handle
        self.kind = kind

    def set_mode(self, width, height, fps, pixel_format):
        m = OniVideoMode(pixel_format, width, height, fps)
        _check(_lib.oniStreamSetProperty(self._h, STREAM_PROPERTY_VIDEO_MODE,
                                         C.byref(m), C.sizeof(m)), 'set video mode')

    def get_mode(self):
        m = OniVideoMode()
        size = C.c_int(C.sizeof(m))
        _check(_lib.oniStreamGetProperty(self._h, STREAM_PROPERTY_VIDEO_MODE,
                                         C.byref(m), C.byref(size)), 'get video mode')
        return {'width': m.resolutionX, 'height': m.resolutionY,
                'fps': m.fps, 'pixel_format': m.pixelFormat}

    def start(self):
        _check(_lib.oniStreamStart(self._h), 'oniStreamStart')

    def stop(self):
        if self._h:
            _lib.oniStreamStop(self._h)

    def read(self):
        """Returns (raw_bytes, width, height, frame_index, timestamp_us)."""
        fp = C.POINTER(OniFrame)()
        _check(_lib.oniStreamReadFrame(self._h, C.byref(fp)), 'oniStreamReadFrame')
        f = fp.contents
        buf = C.string_at(f.data, f.dataSize)
        meta = (buf, f.width, f.height, f.frameIndex, f.timestamp)
        _lib.oniFrameRelease(fp)
        return meta

    def destroy(self):
        if self._h:
            _lib.oniStreamDestroy(self._h)
            self._h = None


class Device:
    def __init__(self, uri=None):
        lib = _load()
        self._h = C.c_void_p()
        _check(lib.oniDeviceOpen(uri.encode() if uri else None, C.byref(self._h)),
               'oniDeviceOpen')
        self._streams = []

    def create_stream(self, sensor_type):
        h = C.c_void_p()
        _check(_lib.oniDeviceCreateStream(self._h, sensor_type, C.byref(h)),
               'oniDeviceCreateStream')
        s = Stream(h, sensor_type)
        self._streams.append(s)
        return s

    def close(self):
        for s in self._streams:
            s.stop()
            s.destroy()
        self._streams.clear()
        if self._h:
            _lib.oniDeviceClose(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
