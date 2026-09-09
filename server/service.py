"""
Ghostlight capture service.

Owns the sensor and the tracker, and talks to the front end over a local
WebSocket.

  text frames   -> JSON status and control
  binary frames -> two kinds, distinguished by a magic word

      'GLDT'  live depth         magic u32, width u16, height u16, index u32,
                                 then u16 * w * h, millimetres, 0 = no return

      'GLMD'  model surface      magic u32, count u32, then f32 xyz * count,
                                 then f32 normals * count. Every surface voxel
                                 in world space, not a render: the client has
                                 its own camera and projects them itself.

      'GLMS'  reconstructed mesh  magic u32, nv u32, nf u32, then f32 xyz * nv,
                                 f32 normals * nv, f32 rgb * nv (0..1),
                                 u32 indices * 3nf

      'GLRG'  colour preview      magic u32, width u16, height u16, then
                                 BGRA * w * h, half resolution

Depth goes out raw so the client owns the colour ramp, and because 640x480x2 at
30 Hz is about 18 MB/s, which is nothing over loopback.

Two loops. capture_loop pumps frames at sensor rate. track_loop registers the
most recent frame, which on CPU runs at 5-10 Hz. Frames arriving mid-ICP are
dropped rather than queued, so tracking always works on current data instead of
falling steadily behind.

Recording also writes every frame the tracker consumed to a bundle on disk (see
bundle.py). Live fusion still happens, because a scan with no visible result is
no way to work, but it is now a preview rather than the deliverable: the Fuse
stage replays the bundle into a fresh volume at whatever voxel size is set then.
Choosing 1 mm after seeing the 3 mm result no longer means scanning the object
again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import struct
import time
import traceback
import webbrowser

import numpy as np
import websockets

import backend
import bundle
import gputsdf
import mesher
import usbdiag
import webapp
from tracker import Tracker

HOST = os.environ.get('GHOSTLIGHT_HOST', '127.0.0.1')
PORT = int(os.environ.get('GHOSTLIGHT_PORT', '8787'))

MAGIC_DEPTH = 0x474C4454        # GLDT
MAGIC_MODEL = 0x474C4D44        # GLMD
MAGIC_MESH  = 0x474C4D53        # GLMS
MAGIC_RGB   = 0x474C5247        # GLRG
MAGIC_VIEW  = 0x474C5256        # GLRV, shaded raycast of the model

RESCAN_SECONDS = 2.0
STATUS_HZ = 10
DIAG_SECONDS = 10.0
MODEL_PUSH_SECONDS = 0.5        # how often the model raycast goes out
MODEL_WIRE_POINTS = 70000       # decimate the preview; it is a preview
DEPTH_STREAM_HZ = 12            # display rate; the tracker still gets every frame
# Full resolution. This was halved back when a pile of leaked clients was
# saturating the event loop, but that was fixed by skipping backed-up clients,
# and 640x480x2 at 12 Hz is about 7 MB/s over loopback. Halving it was quietly
# costing three quarters of the points in the live view.
DEPTH_STRIDE = 1


class Sensor:
    """Wraps one depth device from whichever backend claimed it."""

    def __init__(self):
        self.device = None
        self.depth = None
        self.colour = None
        self.info = None
        self.error = None
        self.backend = None
        self.mod = None
        self._diag = None
        self._diag_at = 0.0

    @property
    def online(self):
        return self.device is not None

    def status(self):
        if self.online:
            mode = self.depth.get_mode()
            return {
                'online': True,
                'backend': backend.LABELS.get(self.backend, self.backend),
                'name': self.info.get('name') or 'Depth sensor',
                'vendor': self.info.get('vendor'),
                'uri': self.info.get('uri'),
                'vid': self.info.get('vid'),
                'pid': self.info.get('pid'),
                'width': mode['width'],
                'height': mode['height'],
                'fps': mode['fps'],
                'colour': self.colour is not None,
                'tilt': (self.device.tilt() if hasattr(self.device, 'tilt') else None),
            }
        return {'online': False, 'error': self.error, 'diagnosis': self.diagnosis()}

    def diagnosis(self):
        """Why the sensor is missing. Shells out to PnP, so it is rate limited."""
        now = time.time()
        if self._diag is None or now - self._diag_at > DIAG_SECONDS:
            self._diag = usbdiag.diagnose()
            self._diag_at = now
        return self._diag

    def rescan(self):
        """Force a fresh look at the bus, including reloading the backend."""
        self._diag = None
        self._diag_at = 0.0
        backend.reload()
        return self.try_open()

    def try_open(self):
        name, mod, info = backend.find()
        if mod is None:
            self.error = 'no device enumerated'
            return False
        try:
            self.backend, self.mod, self.info = name, mod, info
            self.device = mod.Device(info['uri'])
            self.depth = self.device.create_stream(mod.SENSOR_DEPTH)
            self.depth.set_mode(640, 480, 30, getattr(mod, 'PIXEL_DEPTH_1_MM', 100))
            self.depth.start()
            try:
                self.colour = self.device.create_stream(mod.SENSOR_COLOR)
                self.colour.set_mode(640, 480, 30, 0)
                self.colour.start()
            except Exception as e:
                self.colour = None
                print('[sensor] no colour stream:', e)
            self.error = None
            print('[sensor] opened %s via %s' % (info['name'], backend.LABELS.get(name, name)))
            return True
        except Exception as e:
            self.error = str(e)
            self.close()
            return False

    def read(self):
        return self.depth.read()

    def read_colour(self):
        if self.colour is None:
            return None
        try:
            buf, w, h, _idx, _ts = self.colour.read()
            return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        except Exception:
            return None

    def close(self):
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
        self.device = None
        self.depth = None
        self.colour = None


sensor = Sensor()
tracker = Tracker(size=2.0, voxel=0.006, centre_z=1.6, dmin=0.8, dmax=2.8)
clients: dict = {}          # insertion-ordered, so we can evict the oldest

recording = False
_latest = None          # newest (depth_2d, index) awaiting registration
_display = None         # newest frame for the display stream
_display_rgb = None     # newest colour frame
_view = None            # viewer camera (camera-to-world, CV), for the preview
_view_size = (900, 600)
_view_fov = 0.8726      # radians, kept in step with the client camera
_track_status = None
_cap_n = 0
_reg_n = 0
_cap_t0 = 0.0

_raw_mesh = None        # straight out of Poisson, never mutated
_mesh = None            # after the Refine operation stack
_mesh_stats = None
_busy = None            # name of the long job in flight, or None
_ops = {'crop': False, 'turntable': False, 'islands': True,
        'smooth': False, 'simplify': False}

_writer = None          # open bundle while recording
_bundle = None          # summary of the last completed bundle
_fuse = None            # {'done', 'total'} while a re-fusion runs
# Voxel-averaged colour is muddy, and colour earns its keep helping tracking
# rather than texturing the result. Off unless asked for.
_map_colour = False
_volume_error = None    # why the last volume rebuild was refused, if it was


MAX_QUEUED_BYTES = 4 << 20      # skip a client that is this far behind
MAX_CLIENTS = 8                 # a guard against runaway reconnects, not a real limit


async def send_all(payload):
    """Broadcast, skipping clients that cannot keep up.

    Awaiting every client's send meant one slow or idle browser tab applied
    backpressure straight onto capture and tracking. Display data is
    disposable, so a client that is behind simply misses frames rather than
    slowing the scan for everyone.
    """
    if not clients:
        return
    dead = []
    for c in list(clients):
        try:
            tr = getattr(c, 'transport', None)
            if tr is not None and tr.get_write_buffer_size() > MAX_QUEUED_BYTES:
                continue
            await c.send(payload)
        except Exception:
            dead.append(c)
    for c in dead:
        clients.pop(c, None)


GPU = None
_gpu_at = 0.0


def _gpu_now():
    """Static device details, with the memory figures kept current.

    Two corrections live here. Free VRAM moves as volumes are built and
    released, so a figure captured at startup would describe room that has
    since been spent. And the budget for a *replacement* volume has to include
    what the current one gives back, because reconfigure releases before it
    allocates. Without that the volume you are already using is measured
    against memory it is itself occupying, and a perfectly valid setting is
    reported as too large to fit.
    """
    global GPU, _gpu_at
    if not GPU:
        return None
    now = time.time()
    if now - _gpu_at > 1.0:
        _gpu_at = now
        fresh = gputsdf.device_info()
        if fresh:
            GPU = fresh
    g = dict(GPU)
    held = 0
    try:
        held = tracker.vol.bytes if tracker.vol is not None else 0
    except Exception:
        held = 0
    g['volume_gb'] = round(held / 1e9, 2)
    g['bytes_per_voxel'] = tracker.bytes_per_voxel()
    g['budget_gb'] = round(((g.get('free_gb') or 0) + held / 1e9)
                           * gputsdf.VRAM_SHARE, 1)
    return g


def full_status():
    v = tracker.vol
    if v is None:
        return json.dumps({'type': 'status', 'sensor': sensor.status(),
                           'recording': recording, 'busy': _busy,
                           'error': 'no scan volume allocated'})
    st = {'type': 'status', 'sensor': sensor.status(), 'recording': recording,
          'busy': _busy, 'ops': _ops, 'map_colour': _map_colour,
          'gpu': _gpu_now(),
          'volume': {'origin': [float(x) for x in v.origin], 'size': float(tracker.size),
                     'centre': [float(v.origin[i] + tracker.size / 2) for i in range(3)],
                     'voxel': float(tracker.voxel), 'dmin': v.dmin, 'dmax': v.dmax}}
    if _track_status:
        st['track'] = _track_status
    # Marker pixel positions for the colour overlay. Small enough for JSON, and
    # placing markers blind against a number in a log would be miserable.
    if tracker.mode != 'geometry':
        st['markers'] = [[round(b['u'], 1), round(b['v'], 1), b['area']]
                         for b in tracker.blobs]
    if _mesh_stats:
        st['mesh'] = _mesh_stats
    if _writer is not None:
        st['bundle'] = {'recording': True, 'frames': _writer.count,
                        'bytes': _writer.bytes, 'name': os.path.basename(_writer.path)}
    elif _bundle:
        st['bundle'] = {'recording': False, **_bundle}
    if _fuse:
        st['fuse'] = _fuse
    if tracker.turntable:
        st['turntable'] = tracker.turntable
    return json.dumps(st)


async def send_mesh():
    """Push the mesh, or the absence of one, and the status that goes with it.

    The status broadcast used to sit only on the success path. A reconstruct
    that failed cleared _busy on the server and then returned here, sent an
    empty mesh and went home without telling anyone, so the client kept the
    last status it had: the button stayed on "Reconstructing..." and disabled
    with the failure printed underneath it. Nothing else broadcasts while the
    sensor is idle, so it stayed that way until some unrelated command
    happened to refresh it.
    """
    global _mesh_stats
    if _mesh is None:
        _mesh_stats = None
        await send_all(struct.pack('<III', MAGIC_MESH, 0, 0))
    else:
        v, n, c, f = await asyncio.to_thread(mesher.wire, _mesh)
        _mesh_stats = mesher.stats(_mesh)
        await send_all(struct.pack('<III', MAGIC_MESH, len(v), len(f))
                       + v.tobytes() + n.tobytes() + c.tobytes() + f.tobytes())
    await broadcast_status(force=True)


def _build_mesh():
    """Marching cubes over the TSDF, then the current operation stack.

    Runs off the event loop: extraction pulls the volume back to host memory
    and Open3D's cleanup is CPU-bound.
    """
    v, f, n, c = tracker.mesh()
    raw = mesher.from_arrays(v, f, n, c if _map_colour else None)
    return raw, mesher.apply_ops(raw, _ops, None, tracker.mesh_plane())


_last_status_at = 0.0


async def broadcast_status(force=False):
    """Status is a heartbeat, not a per-frame event; 30 Hz of JSON to every
    client was pure overhead."""
    global _last_status_at
    now = time.perf_counter()
    if not force and now - _last_status_at < 1.0 / STATUS_HZ:
        return
    _last_status_at = now
    await send_all(full_status())


async def capture_loop():
    """Reads the sensor as fast as it will go and hands frames on.

    Deliberately does no network work. An earlier version broadcast from here,
    which meant a slow or numerous set of clients applied backpressure straight
    onto capture and starved the tracker. Sending happens in stream_loop.
    """
    global _latest, _display, _display_rgb, _cap_n, _reg_n, _cap_t0
    last_sensor = None
    _cap_t0 = time.perf_counter()

    while True:
        if not sensor.online:
            # Deliberately no backend reload here. Re-enumerating means
            # FreeLibrary on Kinect10.dll, and doing that on a timer beside a
            # live capture thread is a plausible cause of a segfault seen after
            # it was added. Hot-plug recovery is still available, but only when
            # the user presses Recheck, which cannot fire while a sensor is
            # open. Losing automatic recovery is a smaller cost than a service
            # that dies mid-scan.
            sensor.try_open()
            state = sensor.status()
            if state != last_sensor:
                last_sensor = state
                await broadcast_status()
            if not sensor.online:
                await asyncio.sleep(RESCAN_SECONDS)
                continue
            last_sensor = sensor.status()
            await broadcast_status()

        try:
            buf, w, h, idx, _ts = await asyncio.to_thread(sensor.read)
        except Exception as e:
            print('[sensor] lost:', e)
            sensor.close()
            sensor.error = str(e)
            await broadcast_status()
            last_sensor = sensor.status()
            continue

        arr = np.frombuffer(buf, dtype='<u2').reshape(h, w)
        rgb = await asyncio.to_thread(sensor.read_colour) if sensor.colour else None
        if recording:
            _latest = (arr, rgb, idx)
        _display = (arr, w, h, idx)
        if rgb is not None:
            _display_rgb = rgb

        _cap_n += 1
        if _cap_n >= 300:
            dt = time.perf_counter() - _cap_t0
            print('[rate] capture %.1f Hz | registration %.1f Hz | clients %d'
                  % (_cap_n / dt, _reg_n / dt, len(clients)))
            _cap_n = 0; _reg_n = 0; _cap_t0 = time.perf_counter()


async def stream_loop():
    """Pushes the most recent frame to clients at a fixed, modest rate.

    Half resolution and 12 Hz: this feeds a 160 px thumbnail and a preview
    cloud, so full-rate 640x480 was ~18 MB/s per client for no visible gain.
    """
    period = 1.0 / DEPTH_STREAM_HZ
    while True:
        await asyncio.sleep(period)
        if not clients or _display is None:
            continue
        arr, w, h, idx = _display
        small = np.ascontiguousarray(arr[::DEPTH_STRIDE, ::DEPTH_STRIDE])
        sh, sw = small.shape
        await send_all(struct.pack('<IHHI', MAGIC_DEPTH, sw, sh, idx) + small.tobytes())

        if _display_rgb is not None:
            c = np.ascontiguousarray(_display_rgb[::DEPTH_STRIDE, ::DEPTH_STRIDE])
            ch, cw = c.shape[:2]
            await send_all(struct.pack('<IHH', MAGIC_RGB, cw, ch) + c.tobytes())


async def track_loop():
    """Registers the newest frame into the model, then pushes the model out."""
    global _latest, _track_status
    last_push = 0.0

    while True:
        if not recording or _latest is None:
            await asyncio.sleep(0.05)
            continue

        depth, rgb, idx = _latest
        _latest = None                      # drop whatever arrives mid-ICP

        # Written before registration, and written whether or not it succeeds.
        # A frame that failed to track is the most interesting one in the file:
        # it is the only evidence of why, and it used to be thrown away at
        # exactly the moment it mattered.
        if _writer is not None:
            try:
                await asyncio.to_thread(_writer.write, depth, rgb, idx, time.time())
            except Exception as e:
                print('[bundle] write failed:', e)

        try:
            _track_status = await asyncio.to_thread(tracker.add, depth, rgb)
        except Exception as e:
            print('[track] failed:', e)
            await asyncio.sleep(0.1)
            continue

        global _reg_n
        _reg_n += 1
        await broadcast_status()


async def model_loop():
    """Pushes the model preview on its own schedule.

    This used to sit at the end of track_loop, where the raycast plus a few
    megabytes to every client blocked registration for the best part of a
    second each time. Tracking must never wait on the network.
    """
    last = 0.0
    while True:
        await asyncio.sleep(0.15)
        if not clients or not tracker._started:
            continue
        now = time.time()
        if now - last < MODEL_PUSH_SECONDS:
            continue
        last = now
        # The model goes out as points. A server-side raycast was tried here
        # and reverted: the shading was correct but the only place to put the
        # returned image was scene.background, which three draws in screen
        # space, so it sat flat against the viewport and slid about as the
        # camera moved instead of staying in the world. Doing it properly means
        # rendering the volume client-side, not shipping pictures of it.
        try:
            pts, nrm = await asyncio.to_thread(tracker.model_xyz)
            if len(pts) > MODEL_WIRE_POINTS:
                step = len(pts) // MODEL_WIRE_POINTS + 1
                pts = np.ascontiguousarray(pts[::step])
                nrm = np.ascontiguousarray(nrm[::step])
            await send_all(struct.pack('<II', MAGIC_MODEL, len(pts))
                           + pts.tobytes() + nrm.tobytes())
        except Exception as e:
            # A preview that cannot be built is not worth the session. The
            # loops share one gather, so letting this out ends the process and
            # every client with it.
            print('[model] push failed:', e)
            await asyncio.sleep(0.5)


def _open_bundle():
    """Start a new bundle for this take."""
    global _writer
    _close_bundle()
    v = tracker.vol
    name = time.strftime('%Y-%m-%d_%H%M%S')
    path = os.path.join(bundle.default_root(), name)
    meta = {
        'width': v.W, 'height': v.H,
        'fx': v.fx, 'fy': v.fy, 'cx': v.cx, 'cy': v.cy,
        'colour': sensor.colour is not None,
        'mode': tracker.mode,
        'voxel': tracker.voxel, 'size': tracker.size,
        'sensor': sensor.status().get('name'),
        'backend': sensor.status().get('backend'),
    }
    try:
        _writer = bundle.Writer(path, meta)
        print('[bundle] recording to', path)
    except Exception as e:
        _writer = None
        print('[bundle] could not open:', e)


def _close_bundle():
    global _writer, _bundle
    if _writer is None:
        return
    try:
        _writer.close()
        _bundle = bundle.summary(_writer.path)
        if _bundle:
            print('[bundle] wrote %d frames, %.1f MB'
                  % (_bundle['frames'], _bundle['bytes'] / 1e6))
    except Exception as e:
        print('[bundle] close failed:', e)
    _writer = None


FUSE_CHUNK = 8


async def _refuse(path):
    """Rebuild the volume from a recording, at the current voxel size.

    Frames go through the same tracker they went through live, so the poses are
    re-solved rather than replayed. That is deliberate: a finer voxel size gives
    ICP a sharper model to register against, so re-fusing at 1 mm is not merely
    the same scan stored more precisely, it is a better-registered one.

    Work is done in chunks with an await between them. A single long to_thread
    would block nothing, but it would also report nothing for a minute, and a
    progress bar that only moves at the end is not a progress bar.
    """
    global _busy, _fuse, _raw_mesh, _mesh, _mesh_stats, _track_status
    _busy = 'fuse'
    _raw_mesh = _mesh = _mesh_stats = None
    try:
        rdr = await asyncio.to_thread(bundle.Reader, path)
    except Exception as e:
        _busy = None
        await send_all(json.dumps({'type': 'error', 'message': 'Cannot read recording: %s' % e}))
        return

    total = len(rdr)
    _fuse = {'done': 0, 'total': total, 'name': os.path.basename(path)}
    tracker.reset()
    await send_all(struct.pack('<III', MAGIC_MESH, 0, 0))
    await broadcast_status(force=True)
    print('[fuse] %d frames at %.1f mm' % (total, tracker.voxel * 1000))

    def _chunk(start):
        for i in range(start, min(start + FUSE_CHUNK, total)):
            depth, rgb, _idx, _t = rdr.frame(i)
            tracker.add(depth, rgb)
        return tracker.status()

    try:
        for start in range(0, total, FUSE_CHUNK):
            _track_status = await asyncio.to_thread(_chunk, start)
            _fuse['done'] = min(start + FUSE_CHUNK, total)
            await broadcast_status()
    except Exception as e:
        traceback.print_exc()
        await send_all(json.dumps({'type': 'error', 'message': 'Fusion failed: %s' % e}))
    finally:
        await asyncio.to_thread(rdr.close)
        _fuse = None
        _busy = None

    _track_status = tracker.status()
    print('[fuse] done, %d registered of %d' % (tracker.registered, total))
    await broadcast_status(force=True)


async def handle_command(msg):
    global recording, _latest, _track_status
    global _raw_mesh, _mesh, _mesh_stats, _busy, _ops, _view, _map_colour
    global _writer, _bundle, _fuse, _volume_error
    kind = msg.get('type')

    if kind == 'record':
        want = bool(msg.get('on'))
        if want and not recording:
            tracker.reset()
            _track_status = None
            _latest = None
            _open_bundle()
        elif not want and recording:
            _close_bundle()
        recording = want
        print('[record]', 'started' if want else 'stopped')
        await broadcast_status(force=True)

    elif kind == 'bundles':
        await send_all(json.dumps({'type': 'bundles',
                                   'items': await asyncio.to_thread(bundle.listing)}))

    elif kind == 'bundle_delete':
        path = msg.get('path')
        root = os.path.abspath(bundle.default_root())
        # Only ever inside the bundle root. This deletes a directory tree and
        # the path arrives over a socket, so the containment check is the point.
        if path and os.path.abspath(path).startswith(root + os.sep):
            await asyncio.to_thread(shutil.rmtree, path, True)
            if _bundle and _bundle.get('path') == path:
                _bundle = None
        await broadcast_status(force=True)

    elif kind == 'fuse':
        if _busy:
            return
        # Re-fusing is two messages, a volume rebuild and then this. The
        # rebuild can be refused, and it used to go ahead at the old size
        # anyway: a scan that looked like it ran, at a resolution nobody asked
        # for. The client says what it expected so the mismatch is visible.
        want = msg.get('voxel')
        if want and abs(float(want) - tracker.voxel) > 1e-6:
            await send_all(json.dumps({'type': 'error', 'message':
                'Nothing was re-fused. The volume could not be rebuilt at '
                '%.1f mm and is still at %.1f mm. %s'
                % (float(want) * 1000, tracker.voxel * 1000,
                   _volume_error or '')}))
            return
        path = msg.get('path') or (_bundle or {}).get('path')
        if not path or not os.path.isdir(path):
            await send_all(json.dumps({'type': 'error',
                                       'message': 'No recording to fuse. Record something first.'}))
            return
        await _refuse(path)

    elif kind == 'turntable':
        if _display is None:
            await send_all(json.dumps({'type': 'error', 'message': 'No depth frame yet.'}))
            return
        plane = await asyncio.to_thread(tracker.find_turntable, _display[0])
        if plane is None:
            await send_all(json.dumps({
                'type': 'error',
                'message': ('No flat surface found below the subject. Aim lower so '
                            'more of the turntable is in frame.')}))
        elif msg.get('place'):
            v = tracker.volume_above_turntable()
            if v:
                tracker.reconfigure(**v)
                _raw_mesh = _mesh = _mesh_stats = None
                _track_status = tracker.status()
                await send_all(struct.pack('<III', MAGIC_MESH, 0, 0))
                await send_all(struct.pack('<II', MAGIC_MODEL, 0))
        await broadcast_status(force=True)

    elif kind == 'framework':
        act = msg.get('action')
        if act == 'solve':
            rep_ = await asyncio.to_thread(tracker.solve_framework)
            await send_all(json.dumps({'type': 'framework', **rep_}))
        elif act == 'apply':
            ok = tracker.apply_framework()
            await send_all(json.dumps({'type': 'framework',
                                       **tracker.fw.report(), 'applied': bool(ok)}))
            if ok:
                tracker.mode = 'markers'
        elif act == 'clear':
            tracker.clear_framework()
            await send_all(json.dumps({'type': 'framework', **tracker.fw.report()}))
        await broadcast_status(force=True)

    elif kind == 'reconstruct':
        if _busy:
            return
        if tracker.status()['points'] < 1000:
            await send_all(json.dumps({'type': 'error',
                                       'message': 'Nothing scanned yet. Record something first.'}))
            return
        _busy = 'reconstruct'
        await broadcast_status(force=True)
        try:
            _raw_mesh, _mesh = await asyncio.to_thread(_build_mesh)
            _mesh_stats = mesher.stats(_mesh)
        except Exception as e:
            print('[mesh] failed:', e)
            await send_all(json.dumps({'type': 'error', 'message': str(e)}))
            _raw_mesh = _mesh = None
        finally:
            _busy = None
        await send_mesh()

    elif kind == 'refine':
        _ops = {**_ops, **(msg.get('ops') or {})}
        if _raw_mesh is None:
            await broadcast_status()
            return
        _busy = 'refine'
        await broadcast_status(force=True)
        try:
            _mesh = await asyncio.to_thread(mesher.apply_ops, _raw_mesh, _ops,
                                            msg.get('bbox'), tracker.mesh_plane())
        except Exception as e:
            print('[refine] failed:', e)
            await send_all(json.dumps({'type': 'error', 'message': str(e)}))
        finally:
            _busy = None
        await send_mesh()

    elif kind == 'export':
        if _mesh is None:
            await send_all(json.dumps({'type': 'error', 'message': 'Nothing to export.'}))
            return
        path = msg.get('path') or os.path.join(
            os.path.expanduser('~'), 'Documents', 'Ghostlight',
            'scan_%s.%s' % (time.strftime('%Y-%m-%d_%H%M%S'),
                            (msg.get('format') or 'stl').lower()))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            ok = await asyncio.to_thread(mesher.save, _mesh, path)
            await send_all(json.dumps({'type': 'exported', 'ok': bool(ok), 'path': path}))
            print('[export]', 'wrote' if ok else 'FAILED', path)
        except Exception as e:
            await send_all(json.dumps({'type': 'error', 'message': str(e)}))

    elif kind == 'view':
        global _view_size, _view_fov
        w, h = int(msg.get('w') or 0), int(msg.get('h') or 0)
        if w > 32 and h > 32:
            # Cap the long edge. Past this the extra pixels are invisible on a
            # preview and only cost bandwidth.
            s = min(1.0, 900.0 / max(w, h))
            _view_size = (max(64, int(w * s)), max(64, int(h * s)))
        f = msg.get('fov')
        if f:
            _view_fov = float(f)
        # The viewer's camera, so the model preview can be rendered from where
        # they are actually looking instead of from the sensor.
        v = msg.get('c2w')
        _view = v if (isinstance(v, list) and len(v) == 16) else None

    elif kind == 'rescan':
        if not sensor.online:
            await asyncio.to_thread(sensor.rescan)
            print('[sensor]', 'found' if sensor.online else 'still nothing')
        await broadcast_status(force=True)

    elif kind == 'tilt':
        # The tilt gearbox is fragile: Microsoft's limit is one move a second
        # and no more than 15 in 20 seconds. This is a nudge, never a slider.
        try:
            dev = sensor.device
            if dev is not None and hasattr(dev, 'set_tilt'):
                cur = dev.tilt() or 0
                dev.set_tilt(cur + int(msg.get('degrees', 0)))
        except Exception as e:
            await send_all(json.dumps({'type': 'error', 'message': str(e)}))
        await broadcast_status(force=True)

    elif kind == 'mode':
        m = msg.get('mode')
        if m in ('geometry', 'markers', 'framework'):
            tracker.mode = m
            print('[track] mode ->', m)
        await broadcast_status(force=True)

    elif kind == 'map_colour':
        _map_colour = bool(msg.get('on'))
        # The volume has to be rebuilt around the change: turning colour on
        # needs a channel that was never allocated, and turning it off hands
        # back the 60% of the volume it was occupying. Frames are on disk, so
        # re-fusing restores the scan either way.
        if tracker.map_colour != _map_colour:
            prev = tracker.map_colour
            tracker.map_colour = _map_colour
            try:
                tracker.reconfigure()
                _raw_mesh = _mesh = _mesh_stats = None
                _track_status = tracker.status()
                await send_all(struct.pack('<III', MAGIC_MESH, 0, 0))
                await send_all(struct.pack('<II', MAGIC_MODEL, 0))
            except Exception as e:
                # The rebuild was refused, so the card still holds a
                # geometry-only volume. Report the setting that is actually in
                # force rather than the one that was asked for.
                tracker.map_colour = prev
                _map_colour = prev
                await send_all(json.dumps({'type': 'error', 'message': str(e)}))
        await broadcast_status(force=True)

    elif kind == 'volume':
        try:
            tracker.reconfigure(
                size=float(msg['size']) if 'size' in msg else None,
                voxel=float(msg['voxel']) if 'voxel' in msg else None,
                centre_z=float(msg['centre_z']) if 'centre_z' in msg else None,
                centre_x=float(msg['centre_x']) if 'centre_x' in msg else None,
                centre_y=float(msg['centre_y']) if 'centre_y' in msg else None,
                dmin=float(msg['dmin']) if 'dmin' in msg else None,
                dmax=float(msg['dmax']) if 'dmax' in msg else None)
            _raw_mesh = _mesh = _mesh_stats = None
            # Zeroed rather than omitted: the client only overwrites `track`
            # when the field is present, so leaving it out left the old point
            # count and frame count on screen after the volume was rebuilt.
            _track_status = tracker.status()
            await send_all(struct.pack('<III', MAGIC_MESH, 0, 0))
            await send_all(struct.pack('<II', MAGIC_MODEL, 0) + b'')
            _volume_error = None
            print('[volume] %.2f m at %.1f mm' % (tracker.size, tracker.voxel * 1000))
        except Exception as e:
            # Held so a fuse arriving straight after can say why it stopped.
            # reconfigure rolls the old geometry back, so the card still holds
            # a working volume at the previous size.
            _volume_error = str(e)
            await send_all(json.dumps({'type': 'error', 'message': str(e)}))
        # Forced. The throttle exists to stop 30 Hz of tracker JSON, but a
        # volume change is a one-off the client cannot infer: sensor.vol drives
        # the drawn box and the header, and a swallowed broadcast left both
        # showing an older volume than the one on the card. Clicking through
        # presets faster than the throttle made them stall several steps back.
        await broadcast_status(force=True)

    elif kind == 'reset':
        tracker.reset()
        _track_status = None
        _latest = None
        _raw_mesh = _mesh = _mesh_stats = None
        await send_all(struct.pack('<III', MAGIC_MESH, 0, 0))
        await broadcast_status()
        await send_all(struct.pack('<II', MAGIC_MODEL, 0))


async def handler(ws):
    # Refuse rather than evict. A page that leaked sockets reconnects forever,
    # and evicting the oldest just kicks out whoever is actually using the app.
    if len(clients) >= MAX_CLIENTS:
        print('[ws] refusing connection, already serving %d' % len(clients))
        await ws.close(1013, 'too many clients')
        return

    clients[ws] = True
    print('[ws] client connected (%d total)' % len(clients))
    try:
        await ws.send(json.dumps({'type': 'hello', 'service': 'ghostlight', 'version': '0.1.0'}))
        await ws.send(full_status())
        # A reload would otherwise show mesh stats with no geometry behind them.
        if _mesh is not None:
            v, n, c, f = await asyncio.to_thread(mesher.wire, _mesh)
            await ws.send(struct.pack('<III', MAGIC_MESH, len(v), len(f))
                          + v.tobytes() + n.tobytes() + c.tobytes() + f.tobytes())
        async for raw in ws:
            if isinstance(raw, str):
                try:
                    await handle_command(json.loads(raw))
                except Exception as e:
                    print('[ws] bad command:', e)
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.pop(ws, None)
        print('[ws] client gone (%d left)' % len(clients))


async def main(open_browser=False):
    global GPU
    GPU = gputsdf.device_info()
    if GPU:
        print('[gpu] %s, %s, CUDA %s, %.1f GB'
              % (GPU['name'], GPU['arch'], GPU['cuda'], GPU['vram_gb']))
    backend.initialize()
    print('[backend] available: %s' % ', '.join(n for n, _ in backend.available()))
    root = webapp.web_root()
    shown = 'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST
    if root:
        print('[web] serving %s' % root)
    else:
        print('[web] no build found, run "npm run build" (or use "npm run dev")')
    print('[ws] listening on http://%s:%d' % (shown, PORT))

    # The page and the socket come off the same port, so the front end can just
    # connect back to wherever it was loaded from.
    async with websockets.serve(handler, HOST, PORT, max_size=None,
                                process_request=webapp.http_handler):
        if open_browser:
            asyncio.get_running_loop().call_later(
                0.4, webbrowser.open, 'http://%s:%d' % (shown, PORT))
        await asyncio.gather(capture_loop(), stream_loop(), track_loop(), model_loop())


def run(host=None, port=None, open_browser=False):
    """Entry point used by ghostlight.py as well as __main__."""
    global HOST, PORT
    if host:
        HOST = host
    if port:
        PORT = int(port)
    try:
        asyncio.run(main(open_browser))
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
    finally:
        # Finalise the bundle before anything else. An index that never got
        # written leaves the frames on disk with no way to read them back.
        _close_bundle()
        sensor.close()
        backend.shutdown()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Ghostlight capture service')
    ap.add_argument('--host', default=HOST,
                    help='bind address (0.0.0.0 to serve a remote front end)')
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--open', action='store_true', help='open a browser once bound')
    args = ap.parse_args()
    run(args.host, args.port, args.open)
