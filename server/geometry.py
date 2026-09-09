"""
Frame geometry: back-projection, plane fitting, and turntable axis recovery.

Two jobs here, both about knowing what the sensor is actually looking at rather
than guessing.

The first is the turntable. A subject on a platter sits on a large flat surface
that is static while the subject turns, which is precisely the geometry that
hijacks ICP. Finding that plane explicitly means it can be reported, excluded
from the scan volume, and cut out of the finished mesh, instead of being fought
with a slider.

The second is the rotation axis. On a turntable the camera is physically still
and the subject rotates, but the tracker solves the equivalent fiction: a static
subject with the camera orbiting it. So the recovered camera centres trace a
circle, and the axis of that circle is the turntable axis. Once it is known, a
pose is one number instead of six, which removes the entire class of drift that
makes turntable scanning hard.
"""

from __future__ import annotations

import numpy as np

FX = FY = 571.26
CX, CY = 320.0, 240.0


def backproject(depth_mm: np.ndarray, stride: int = 4) -> np.ndarray:
    """Depth frame to an (N, 3) cloud in camera metres, invalid pixels dropped.

    Strided: nothing here needs every pixel, and 640x480 at stride 4 is 19200
    points, which every operation below handles in single-digit milliseconds.
    """
    d = depth_mm[::stride, ::stride].astype(np.float32) * 0.001
    h, w = d.shape
    us = (np.arange(w, dtype=np.float32) * stride)[None, :]
    vs = (np.arange(h, dtype=np.float32) * stride)[:, None]
    ok = d > 0
    if not ok.any():
        return np.zeros((0, 3), dtype=np.float32)
    x = (us - CX) * d / FX
    y = (vs - CY) * d / FY
    return np.stack([x[ok], y[ok], d[ok]], axis=1).astype(np.float32)


def fit_plane(pts: np.ndarray, thresh: float = 0.006, iters: int = 128,
              rng: np.random.Generator | None = None):
    """RANSAC plane fit. Returns (normal, offset, inlier_mask) or None.

    The plane is n . x + offset = 0 with n unit length. `thresh` is the
    point-to-plane distance in metres that counts as an inlier; 6 mm is roughly
    three times the Kinect's own noise at turntable range, so a genuinely flat
    platter lands well inside it and a subject sitting on it does not.
    """
    n = len(pts)
    if n < 50:
        return None
    rng = rng or np.random.default_rng(7)

    best_inl = None
    best_count = 0
    for _ in range(iters):
        idx = rng.choice(n, 3, replace=False)
        a, b, c = pts[idx]
        nv = np.cross(b - a, c - a)
        ln = np.linalg.norm(nv)
        if ln < 1e-9:
            continue
        nv = nv / ln
        off = -float(nv @ a)
        dist = np.abs(pts @ nv + off)
        inl = dist < thresh
        k = int(inl.sum())
        if k > best_count:
            best_count, best_inl = k, inl
            if best_count > 0.85 * n:
                break

    if best_inl is None or best_count < 50:
        return None

    # Refit on every inlier. Three random points fix a plane but do not fit one,
    # and the least-squares normal over a few thousand points is far steadier.
    sel = pts[best_inl]
    centroid = sel.mean(0)
    _, _, vt = np.linalg.svd(sel - centroid, full_matrices=False)
    nv = vt[2]
    nv = nv / np.linalg.norm(nv)
    off = -float(nv @ centroid)
    return nv.astype(np.float32), float(off), best_inl


def find_turntable(depth_mm: np.ndarray, below_y: float = 0.0,
                   zmin: float = 0.5, zmax: float = 2.5):
    """Find the platter: the dominant plane in the lower part of the frame.

    Restricting the search downward matters. A room contains several large
    planes and the biggest one is usually a wall or the floor, so an unrestricted
    "find the biggest plane" returns the wrong answer nearly every time.

    Returns a dict with the plane, its centroid, and how much of the searched
    region agreed, or None. `up` is forced to point back towards the camera, so
    "above the turntable" is unambiguous for every caller.
    """
    pts = backproject(depth_mm, stride=4)
    if not len(pts):
        return None
    sel = pts[(pts[:, 1] > below_y) & (pts[:, 2] > zmin) & (pts[:, 2] < zmax)]
    if len(sel) < 200:
        return None

    fit = fit_plane(sel)
    if fit is None:
        return None
    nv, off, inl = fit

    # Y is down in the camera convention, so the upward normal has negative Y.
    if nv[1] > 0:
        nv, off = -nv, -off

    centroid = sel[inl].mean(0)
    return {
        'up': [float(v) for v in nv],
        'offset': off,
        'centre': [float(v) for v in centroid],
        'inliers': int(inl.sum()),
        'coverage': round(float(inl.sum()) / len(sel), 3),
        'tilt_deg': round(float(np.degrees(np.arccos(min(1.0, abs(nv[1]))))), 1),
    }


def height_above(plane: dict, pts: np.ndarray) -> np.ndarray:
    """Signed height of each point above the plane, positive on the `up` side."""
    nv = np.asarray(plane['up'], dtype=np.float32)
    return pts @ nv + plane['offset']


def fit_circle_axis(centres: np.ndarray):
    """Rotation axis from a set of camera centres that orbit it.

    Plane fit first, then an algebraic circle fit inside that plane. The circle
    fit is the standard linearisation: |p|^2 = 2 c . p + (r^2 - |c|^2) is linear
    in the unknowns, so it is one least-squares solve rather than an iteration
    that can wander off.

    Returns (axis unit vector, point on the axis, radius, rms residual) or None.
    Needs a decent arc: three nearly collinear centres fit a circle of almost
    any radius, so the residual is the thing to check, not the fact it returned.
    """
    if len(centres) < 8:
        return None
    p = np.asarray(centres, dtype=np.float64)
    mid = p.mean(0)
    _, sv, vt = np.linalg.svd(p - mid, full_matrices=False)
    if sv[1] < 1e-6:
        return None
    axis = vt[2]
    e1, e2 = vt[0], vt[1]

    q = p - mid
    u, v = q @ e1, q @ e2
    A = np.stack([2 * u, 2 * v, np.ones_like(u)], axis=1)
    b = u * u + v * v
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cu, cv, k = sol
    r2 = k + cu * cu + cv * cv
    if not np.isfinite(r2) or r2 <= 0:
        return None
    r = float(np.sqrt(r2))

    centre = mid + cu * e1 + cv * e2
    resid = float(np.sqrt(np.mean((np.sqrt((u - cu) ** 2 + (v - cv) ** 2) - r) ** 2)))
    return (axis / np.linalg.norm(axis)).astype(np.float32), centre.astype(np.float32), r, resid


def axis_from_poses(poses):
    """Turntable axis from a run of camera-to-world poses.

    Wraps fit_circle_axis with the sanity checks the raw fit cannot make: an
    orbit has to actually be an orbit. A residual worse than a tenth of the
    radius, or a radius outside anything a desk turntable could produce, means
    the poses were not going round anything and the answer would be noise.
    """
    if len(poses) < 8:
        return None
    centres = np.array([p[:3, 3] for p in poses], dtype=np.float64)
    fit = fit_circle_axis(centres)
    if fit is None:
        return None
    axis, centre, r, resid = fit
    if r < 0.05 or r > 3.0 or resid > 0.1 * r:
        return None
    return {
        'axis': [float(v) for v in axis],
        'centre': [float(v) for v in centre],
        'radius': round(r, 4),
        'residual': round(resid, 5),
        'samples': len(poses),
    }
