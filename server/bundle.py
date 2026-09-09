"""
Record-then-fuse. Raw frames go to disk during the scan; fusion is a separate
step that reads them back.

Why this is worth the disk space: fusing live means the voxel size is chosen
before you have seen a single frame, and it is baked in permanently. Get it
wrong and the only remedy is to scan the object again. With the frames on disk
you fuse at 2 mm, look at the result, and re-fuse at 1 mm without touching the
sensor. The scan and the reconstruction stop being the same decision.

It also means a failed scan is diagnosable. When tracking smears something, the
evidence used to be gone the moment it happened; now the frames are still there
to replay.

Layout, one directory per session:

    meta.json     sensor, intrinsics, and the volume settings at record time
    index.json    per-frame [offset, depth_len, colour_len, index, t, flags]
    frames.bin    the frames themselves, appended

The split mirrors what Orbbec do (their strings log "save bundle frame" and
"save bundle meta" separately), and for the same reason: the index has to be
written whole and atomically at the end, while frames stream out during capture.

Compression is zlib at level 1. Depth bytes are de-interleaved first, low bytes
then high bytes, because a depth frame's high bytes are nearly constant across
the image while the raw little-endian stream alternates them with noisy low
bytes and defeats the compressor. That one line roughly halves the file.

Measured on a rendered scene with realistic sensor noise: 640x480 depth plus
BGRA colour is 1.8 MB a frame raw and lands at about 400 KB, so roughly 250 MB
a minute at the 10 Hz the tracker actually consumes. Depth only is a fifth of
that. Frames are written only when the tracker takes one, not at sensor rate:
the extra frames could not be used for fusion either, since a re-fuse replays
through the same tracker.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import time
import zlib

import numpy as np

FORMAT = 1
LEVEL = 1               # fast. Level 6 costs ~30 ms a frame for ~8% more.
FLAG_COLOUR = 1


def _pack_depth(depth: np.ndarray) -> bytes:
    raw = np.ascontiguousarray(depth, dtype='<u2').view(np.uint8).reshape(-1, 2)
    return zlib.compress(np.concatenate([raw[:, 0], raw[:, 1]]).tobytes(), LEVEL)


def _unpack_depth(blob: bytes, h: int, w: int) -> np.ndarray:
    flat = np.frombuffer(zlib.decompress(blob), dtype=np.uint8)
    half = flat.size // 2
    out = np.empty((half, 2), dtype=np.uint8)
    out[:, 0] = flat[:half]
    out[:, 1] = flat[half:]
    return out.view('<u2').reshape(h, w)


def _pack_colour(bgra: np.ndarray) -> bytes:
    # Alpha is a constant 255 from every backend we support, so it is a quarter
    # of the bytes carrying no information.
    return zlib.compress(np.ascontiguousarray(bgra[:, :, :3]).tobytes(), LEVEL)


def _unpack_colour(blob: bytes, h: int, w: int) -> np.ndarray:
    bgr = np.frombuffer(zlib.decompress(blob), dtype=np.uint8).reshape(h, w, 3)
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[:, :, :3] = bgr
    out[:, :, 3] = 255
    return out


def default_root() -> str:
    return os.path.join(os.path.expanduser('~'), 'Documents', 'Ghostlight', 'bundles')


class Writer:
    """Appends frames to a bundle. Everything here runs off the event loop."""

    def __init__(self, path: str, meta: dict):
        self.path = path
        self.meta = dict(meta)
        self.meta['format'] = FORMAT
        self.meta['started'] = time.time()
        os.makedirs(path, exist_ok=True)
        self._fh = open(os.path.join(path, 'frames.bin'), 'wb')
        self._index = []
        self._offset = 0
        self.closed = False

    @property
    def count(self):
        return len(self._index)

    @property
    def bytes(self):
        return self._offset

    def write(self, depth: np.ndarray, bgra: np.ndarray | None, index: int, t: float):
        if self.closed:
            return
        d = _pack_depth(depth)
        c = _pack_colour(bgra) if bgra is not None else b''
        self._fh.write(d)
        if c:
            self._fh.write(c)
        self._index.append([self._offset, len(d), len(c), int(index), round(t, 4),
                            FLAG_COLOUR if c else 0])
        self._offset += len(d) + len(c)

    def close(self):
        """Finalise. Safe to call twice; a bundle with no frames is removed."""
        if self.closed:
            return
        self.closed = True
        try:
            self._fh.close()
        except Exception:
            pass
        if not self._index:
            shutil.rmtree(self.path, ignore_errors=True)
            return
        self.meta['ended'] = time.time()
        self.meta['frames'] = len(self._index)
        self.meta['bytes'] = self._offset
        with open(os.path.join(self.path, 'index.json'), 'w') as f:
            json.dump({'frames': self._index}, f)
        with open(os.path.join(self.path, 'meta.json'), 'w') as f:
            json.dump(self.meta, f, indent=1)


class Reader:
    """Reads a bundle back. Frames are decompressed on demand, never all at once.

    A full scan is several gigabytes uncompressed, so loading it into memory to
    fuse it would defeat the point of having written it out.
    """

    def __init__(self, path: str):
        self.path = path
        with open(os.path.join(path, 'meta.json')) as f:
            self.meta = json.load(f)
        with open(os.path.join(path, 'index.json')) as f:
            self._index = json.load(f)['frames']
        if self.meta.get('format') != FORMAT:
            raise ValueError('bundle format %s, expected %s'
                             % (self.meta.get('format'), FORMAT))
        self.width = int(self.meta.get('width', 640))
        self.height = int(self.meta.get('height', 480))
        self._fh = open(os.path.join(path, 'frames.bin'), 'rb')

    def __len__(self):
        return len(self._index)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def frame(self, i: int):
        """Returns (depth_mm, bgra or None, sensor index, timestamp)."""
        off, dl, cl, idx, t, flags = self._index[i]
        self._fh.seek(off)
        depth = _unpack_depth(self._fh.read(dl), self.height, self.width)
        colour = None
        if cl:
            colour = _unpack_colour(self._fh.read(cl), self.height, self.width)
        return depth, colour, idx, t

    def __iter__(self):
        for i in range(len(self._index)):
            yield self.frame(i)

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def summary(path: str) -> dict | None:
    """Cheap description of a bundle without opening frames.bin."""
    try:
        with open(os.path.join(path, 'meta.json')) as f:
            m = json.load(f)
    except Exception:
        return None
    return {
        'path': path,
        'name': os.path.basename(path),
        'frames': m.get('frames', 0),
        'bytes': m.get('bytes', 0),
        'seconds': round(m.get('ended', 0) - m.get('started', 0), 1),
        'colour': bool(m.get('colour')),
        'mode': m.get('mode'),
        'voxel': m.get('voxel'),
        'started': m.get('started'),
    }


def listing(root: str | None = None, limit: int = 20) -> list:
    """Bundles on disk, newest first."""
    root = root or default_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        s = summary(os.path.join(root, name))
        if s and s['frames']:
            out.append(s)
    out.sort(key=lambda s: s.get('started') or 0, reverse=True)
    return out[:limit]


def prune(root: str | None = None, keep: int = 10):
    """Delete all but the newest `keep` bundles.

    Raw frames are large and a bundle is only interesting until its mesh is
    exported, so without this the disk fills quietly over a few sessions.
    """
    root = root or default_root()
    all_ = listing(root, limit=10_000)
    removed = 0
    for s in all_[keep:]:
        shutil.rmtree(s['path'], ignore_errors=True)
        removed += 1
    return removed
