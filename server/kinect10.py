"""
Kinect v1 backend via Microsoft's Kinect for Windows SDK 1.8 (Kinect10.dll).

Why this exists rather than just using OpenNI2 everywhere:

OpenNI2's PS1080 driver enumerates the PrimeSense driver's device interface
through SetupAPI. On Windows a Kinect v1 is claimed by Microsoft's KinectCamera
driver, so PS1080 reports zero devices no matter what you bind with Zadig.
Rebinding to libusbK satisfies libusb but not PS1080, and breaks the SDK.

Skanect did the same thing: OpenNI2 for the Structure Sensor, Microsoft's SDK
(ntk::Kin4WinGrabber) for Kinect v1 on Windows.

So: this backend on Windows, OpenNI2 on Linux. The module mirrors openni2.py's
shape so service.py can use either without caring which.

Requires the Kinect for Windows SDK 1.8 (or its Runtime) to be installed, and
the camera left on Microsoft's driver.
"""

from __future__ import annotations

import ctypes as C
import sys
from ctypes import wintypes

import numpy as np

# --- constants ---------------------------------------------------------------

SENSOR_IR, SENSOR_COLOR, SENSOR_DEPTH = 1, 2, 3      # match openni2.py

NUI_INITIALIZE_FLAG_USES_DEPTH = 0x00000020
NUI_INITIALIZE_FLAG_USES_COLOR = 0x00000002

NUI_IMAGE_TYPE_DEPTH = 4
NUI_IMAGE_TYPE_COLOR = 1

COLOR_BYTES_PER_PIXEL = 4        # BGRA

NUI_IMAGE_RESOLUTION_640x480 = 2

# Depth arrives packed with a 3-bit player-index field in the low bits, even
# for plain NUI_IMAGE_TYPE_DEPTH. Shifting right by 3 yields millimetres, which
# is what OpenNI2's PIXEL_DEPTH_1_MM gives us, so the wire format stays identical.
PLAYER_INDEX_SHIFT = 3

DLL = r'C:\Windows\System32\Kinect10.dll'


# --- structs -----------------------------------------------------------------

class _ViewArea(C.Structure):
    _fields_ = [('eDigitalZoom', C.c_int), ('lCenterX', C.c_long), ('lCenterY', C.c_long)]


class _ImageFrame(C.Structure):
    _fields_ = [('liTimeStamp', C.c_longlong),
                ('dwFrameNumber', wintypes.DWORD),
                ('eImageType', C.c_int),
                ('eResolution', C.c_int),
                ('pFrameTexture', C.c_void_p),
                ('dwFrameFlags', wintypes.DWORD),
                ('ViewArea', _ViewArea)]


def _unmirror(flat, w, h, channels):
    """Reverse the column order of a frame.

    The SDK hands back the image the way a mirror would: column index grows
    towards the sensor's left, which is the same convention
    NuiTransformDepthImageToSkeleton uses when it returns +x for a growing
    depthX and calls +x the sensor's left.

    Everything downstream is a standard pinhole camera, where X = (u - cx)z/fx
    with u growing to the right. Feeding it the SDK order negates X for every
    point, and no later step can recover it: the conversion out to GL negates
    Y and Z, which is a rotation and preserves handedness by design. The scan
    comes out as a mirror image of the room.

    Reversing here rather than in the projection maths keeps it to one place.
    The alternative was flipping the sign of X in the two integration kernels,
    the ICP kernel, the marker detector and the browser's own back-projection,
    and any one of those left behind would put half the pipeline in a
    different world to the other half.

    OpenNI2 already delivers the standard order, which is why this belongs to
    this backend and not to the tracker.
    """
    if flat.size != w * h * channels:
        return flat.tobytes()
    shaped = flat.reshape(h, w, channels) if channels > 1 else flat.reshape(h, w)
    return np.ascontiguousarray(shaped[:, ::-1]).tobytes()


class _LockedRect(C.Structure):
    _fields_ = [('Pitch', C.c_int), ('size', C.c_uint), ('pBits', C.POINTER(C.c_ubyte))]


# INuiFrameTexture vtable slots, after the three IUnknown entries.
_VT_LOCK_RECT = 5
_VT_UNLOCK_RECT = 7


class KinectError(RuntimeError):
    pass


_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    if sys.platform != 'win32':
        raise KinectError('Kinect SDK backend is Windows only')
    try:
        lib = C.WinDLL(DLL)
    except OSError as e:
        raise KinectError('Kinect for Windows SDK 1.8 not installed (%s)' % e)

    lib.NuiGetSensorCount.argtypes = [C.POINTER(C.c_int)]
    lib.NuiGetSensorCount.restype = C.c_long
    lib.NuiInitialize.argtypes = [wintypes.DWORD]
    lib.NuiInitialize.restype = C.c_long
    lib.NuiShutdown.argtypes = []
    lib.NuiImageStreamOpen.argtypes = [C.c_int, C.c_int, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.HANDLE, C.POINTER(wintypes.HANDLE)]
    lib.NuiImageStreamOpen.restype = C.c_long
    lib.NuiImageStreamGetNextFrame.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               C.POINTER(C.POINTER(_ImageFrame))]
    lib.NuiImageStreamGetNextFrame.restype = C.c_long
    lib.NuiImageStreamReleaseFrame.argtypes = [wintypes.HANDLE, C.POINTER(_ImageFrame)]
    lib.NuiImageStreamReleaseFrame.restype = C.c_long
    lib.NuiCameraElevationGetAngle.argtypes = [C.POINTER(C.c_long)]
    lib.NuiCameraElevationGetAngle.restype = C.c_long
    lib.NuiCameraElevationSetAngle.argtypes = [C.c_long]
    lib.NuiCameraElevationSetAngle.restype = C.c_long

    _lib = lib
    return lib


def _check(hr, what):
    if hr != 0:
        raise KinectError('%s failed: HRESULT 0x%08X' % (what, hr & 0xFFFFFFFF))


# --- api ---------------------------------------------------------------------

def initialize():
    _load()


def shutdown():
    if _lib:
        try:
            _lib.NuiShutdown()
        except Exception:
            pass


def reload():
    """Drop the SDK and load it again, so a hot-plugged sensor is seen.

    Kinect10.dll enumerates when it is loaded and does not revisit the decision.
    Plug a sensor in after that and NuiGetSensorCount keeps returning zero for
    the life of the process, however many times it is asked. Retrying the call
    is therefore pointless; the library itself has to go.

    Verified by the symptom that led here: a fresh process listed the sensor
    while the long-running service, started before the Kinect was connected,
    reported "no device enumerated" indefinitely.
    """
    global _lib
    if _lib is None:
        return _load()
    try:
        _lib.NuiShutdown()
    except Exception:
        pass
    handle = getattr(_lib, '_handle', None)
    _lib = None
    if handle:
        try:
            k32 = C.WinDLL('kernel32', use_last_error=True)
            k32.FreeLibrary.argtypes = [wintypes.HMODULE]
            k32.FreeLibrary.restype = wintypes.BOOL
            k32.FreeLibrary(handle)
        except Exception:
            pass
    return _load()


def list_devices():
    """Mirrors openni2.list_devices()."""
    try:
        lib = _load()
    except KinectError:
        return []
    n = C.c_int(0)
    if lib.NuiGetSensorCount(C.byref(n)) != 0 or n.value < 1:
        return []
    return [{
        'uri': 'nui:0',
        'vendor': 'Microsoft',
        'name': 'Xbox Kinect v1',
        'vid': '045e',
        'pid': '02ae',
    }]


class Stream:
    def __init__(self, handle, kind):
        self._h = handle
        self.kind = kind
        self._w, self._h_px = 640, 480

    def set_mode(self, width, height, fps, pixel_format):
        # The SDK fixes both streams at 640x480 @30 for this sensor.
        if (width, height) != (640, 480):
            raise KinectError('Kinect v1 streams are 640x480 here')

    def get_mode(self):
        return {'width': self._w, 'height': self._h_px, 'fps': 30, 'pixel_format': 100}

    def start(self):
        pass        # the stream is live from NuiImageStreamOpen

    def stop(self):
        pass

    def read(self):
        """Depth: (uint16 mm bytes, width, height, frame_index, timestamp_us).
        Colour: (BGRA bytes, width, height, frame_index, timestamp_us)."""
        fp = C.POINTER(_ImageFrame)()
        _check(_lib.NuiImageStreamGetNextFrame(self._h, 1000, C.byref(fp)),
               'NuiImageStreamGetNextFrame')
        f = fp.contents
        try:
            tex = C.c_void_p(f.pFrameTexture)
            vt = C.cast(tex, C.POINTER(C.POINTER(C.c_void_p))).contents
            lock = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.c_uint,
                                 C.POINTER(_LockedRect), C.c_void_p,
                                 wintypes.DWORD)(vt[_VT_LOCK_RECT])
            unlock = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.c_uint)(vt[_VT_UNLOCK_RECT])

            lr = _LockedRect()
            _check(lock(tex, 0, C.byref(lr), None, 0), 'LockRect')
            if not lr.size:
                raise KinectError('empty frame')

            if self.kind == SENSOR_COLOR:
                bgra = np.frombuffer(C.string_at(lr.pBits, lr.size), dtype=np.uint8)
                out = _unmirror(bgra, self._w, self._h_px, 4)
            else:
                raw = np.frombuffer(C.string_at(lr.pBits, lr.size), dtype='<u2')
                raw = (raw >> PLAYER_INDEX_SHIFT).astype('<u2', copy=False)
                out = _unmirror(raw, self._w, self._h_px, 1)
            unlock(tex, 0)

            return (out, self._w, self._h_px, f.dwFrameNumber, f.liTimeStamp)
        finally:
            _lib.NuiImageStreamReleaseFrame(self._h, fp)

    def destroy(self):
        self._h = None


class Device:
    def __init__(self, uri=None):
        lib = _load()
        # Ask for colour up front: NuiInitialize's flags cannot be changed later
        # without shutting the sensor down, and we would rather have the stream
        # available than restart mid-session.
        _check(lib.NuiInitialize(NUI_INITIALIZE_FLAG_USES_DEPTH |
                                 NUI_INITIALIZE_FLAG_USES_COLOR), 'NuiInitialize')
        self._open = True
        self._streams = []

    def create_stream(self, sensor_type):
        if sensor_type == SENSOR_DEPTH:
            kind = NUI_IMAGE_TYPE_DEPTH
        elif sensor_type == SENSOR_COLOR:
            kind = NUI_IMAGE_TYPE_COLOR
        else:
            raise KinectError('unsupported stream type %r' % sensor_type)
        h = wintypes.HANDLE()
        _check(_lib.NuiImageStreamOpen(kind, NUI_IMAGE_RESOLUTION_640x480,
                                       0, 2, None, C.byref(h)), 'NuiImageStreamOpen')
        s = Stream(h, sensor_type)
        self._streams.append(s)
        return s

    # -- tilt motor ----------------------------------------------------------
    #
    # The gearbox is fragile: Microsoft's guidance is at most one move a second
    # and no more than 15 in any 20 seconds. Treat this as a nudge control.

    def tilt(self):
        a = C.c_long(0)
        if _lib.NuiCameraElevationGetAngle(C.byref(a)) != 0:
            return None
        return int(a.value)

    def set_tilt(self, degrees):
        d = max(-27, min(27, int(degrees)))
        _check(_lib.NuiCameraElevationSetAngle(C.c_long(d)), 'NuiCameraElevationSetAngle')
        return d

    def close(self):
        self._streams.clear()
        if getattr(self, '_open', False):
            try:
                _lib.NuiShutdown()
            except Exception:
                pass
            self._open = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == '__main__':
    initialize()
    devs = list_devices()
    print('devices:', devs)
    if devs:
        with Device() as d:
            s = d.create_stream(SENSOR_DEPTH)
            s.start()
            buf, w, h, idx, ts = s.read()
            a = np.frombuffer(buf, dtype='<u2')
            nz = a[a > 0]
            print('frame %dx%d #%d  valid %d/%d  %d..%d mm'
                  % (w, h, idx, nz.size, a.size,
                     nz.min() if nz.size else 0, nz.max() if nz.size else 0))
