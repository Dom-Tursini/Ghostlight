"""
Marker framework: a dedicated pass that solves the marker layout globally,
before any geometry is scanned.

This replaces an earlier design that grew the marker map during the scan, one
frame at a time. That approach has a defect it cannot recover from: whatever
error is in the map when a marker is first added is baked into it permanently,
and every later frame is registered against that error. The map is the reference
frame, so it is exactly the thing that must not drift, and an incremental map
drifts by construction.

Dissecting CR Scan settled the question. OB_SCAN_MARKER_FRAMEWORK is a scan type
in its own right, with obscan_scan_marker_framework_optimization to solve it and
obscan_session_import_marker_framework_datas to bring the answer into a geometry
scan. Markers first, solve, then scan against a fixed answer.

The solve here is block coordinate descent on the same objective a bundle
adjuster minimises:

    sum over frames i, markers j    || T_i * p_ij  -  m_j || ^2

Alternate between the two blocks. Hold the marker positions m fixed and each
frame's pose T_i is a closed-form Kabsch fit. Hold the poses fixed and each
marker is the mean of its observations. Both halves are exact, so the objective
falls monotonically and it converges in a couple of dozen passes. Orbbec reach
for Ceres because they are also solving intrinsics and a full sensor model; for
rigid points and rigid poses this is the same answer without the dependency.
"""

from __future__ import annotations

import numpy as np

from markers import AMBIG_LAYOUT, kabsch, layout_ambiguity

MIN_MARKERS = 3

# A revolution's worth of views is what the solve needs; more is the same
# information again. Left uncapped a survey banks a frame every 50 ms for as
# long as it is running, and the solve is linear in frames per iteration over
# 25 iterations, so a minute and a half of surveying turns a two second solve
# into a two minute one for no extra accuracy.
MAX_OBS = 300

# No real marker framework has hundreds of markers. If the seed map grows past
# this, what is being detected is not markers: it is reflections, highlights and
# bright edges, each appearing once and never matching anything again. Say so
# instead of grinding through a solve that cannot converge.
MAX_MAP = 200


class Framework:
    """Observations from a marker pass, and the global solve over them."""

    def __init__(self, gate=0.035, min_obs=3):
        self.gate = gate
        self.min_obs = min_obs
        self.reset()

    def reset(self):
        self.obs = []           # per frame: (N, 3) markers in camera space
        self._stride = 1        # accept one frame in this many
        self._seq = 0
        self.points = np.zeros((0, 3), dtype=np.float32)
        self.poses = []
        self.residuals = np.zeros((0,), dtype=np.float32)
        self.counts = np.zeros((0,), dtype=np.int32)
        self.rmse = 0.0
        self.solved = False
        self.note = None

    # -- collection ----------------------------------------------------------

    def observe(self, cam_pts: np.ndarray) -> int:
        """Record one frame's markers.

        Frames carrying fewer than three markers are dropped rather than stored.
        Three non-collinear points are the minimum that fixes a pose, so a frame
        below that can never contribute a constraint and would only dilute the
        counts the solve prunes on.

        Past MAX_OBS the set is halved and the acceptance rate halves with it, so
        what is kept stays evenly spread over the whole survey however long it
        runs. A hard stop at the cap would be worse than it looks: someone
        turning the table slowly would have their survey filled by the first
        quarter of a revolution and the rest of the object never sampled.
        """
        if cam_pts is None or len(cam_pts) < MIN_MARKERS:
            return len(self.obs)
        self._seq += 1
        if self._seq % self._stride:
            return len(self.obs)
        self.obs.append(np.asarray(cam_pts, dtype=np.float32))
        if len(self.obs) >= MAX_OBS:
            del self.obs[1::2]
            self._stride *= 2
        self.solved = False
        return len(self.obs)

    @property
    def frames(self):
        return len(self.obs)

    # -- solve ---------------------------------------------------------------

    def solve(self, iters=25, tol=1e-6) -> dict:
        """Solve the framework. Returns a report; also sets .points and .rmse."""
        if len(self.obs) < 4:
            self.note = 'Not enough frames. Turn the subject through a full revolution.'
            return self.report()

        pts, poses = self._seed()
        if pts is None and poses == 'noise':
            self.note = ('These are not markers. Nothing being detected stays '
                         'put between frames, which is what reflections and '
                         'bright edges do. Shrink the scan volume so the search '
                         'only covers the turntable, and check the marker '
                         'overlay before surveying.')
            return self.report()
        if pts is None:
            self.note = ('Could not link the frames together. Markers need to '
                         'overlap between one frame and the next, so turn more '
                         'slowly or add more of them.')
            return self.report()

        last = None
        for _ in range(iters):
            poses, assign = self._fit_poses(pts, poses)
            pts, counts, resid, rmse = self._fit_points(poses, assign, len(pts))
            if pts is None:
                self.note = 'The solve collapsed. Too few markers seen consistently.'
                return self.report()
            keep = counts >= self.min_obs
            if keep.sum() >= MIN_MARKERS and not keep.all():
                # Drop markers only a couple of frames ever agreed on. These are
                # usually specular highlights that happened to be stable for a
                # moment, and leaving them in drags every pose that sees them.
                pts, counts, resid = pts[keep], counts[keep], resid[keep]
            if last is not None and abs(last - rmse) < tol:
                break
            last = rmse

        self.points = pts.astype(np.float32)
        self.poses = poses
        self.counts = counts.astype(np.int32)
        self.residuals = resid.astype(np.float32)
        self.rmse = float(last or 0.0)
        self.solved = len(self.points) >= MIN_MARKERS
        self.note = None if self.solved else 'Fewer than three markers survived the solve.'
        return self.report()

    def _seed(self):
        """A first guess, built frame to frame.

        Only a starting point. Its drift is the whole reason the refinement
        below exists, so it does not need to be good, just connected.
        """
        pts = self.obs[0].copy()
        poses = [np.eye(4, dtype=np.float32)]
        for cam in self.obs[1:]:
            T = self._pose_against(cam, pts, poses[-1])
            if T is None:
                # A break in the chain. Reuse the last good pose so the frame is
                # still available to the refinement, which matches globally and
                # can often place it once the map is better.
                T = poses[-1]
            poses.append(T)
            world = (T[:3, :3] @ cam.T).T + T[:3, 3]
            fresh = [p for p in world
                     if len(pts) == 0 or np.min(np.linalg.norm(pts - p, axis=1)) > self.gate]
            if fresh:
                pts = np.vstack([pts, np.asarray(fresh, dtype=np.float32)])
            if len(pts) > MAX_MAP:
                # Every frame is contributing points that match nothing seen
                # before, which is what noise looks like and what markers never
                # look like. Stopping here is not giving up early: there is no
                # answer in this data and the solve would only take longer to
                # say so.
                return None, 'noise'
        if len(pts) < MIN_MARKERS:
            return None, None
        return pts, poses

    def _pose_against(self, cam, pts, init):
        """Kabsch against the map, from nearest-neighbour correspondences."""
        world = (init[:3, :3] @ cam.T).T + init[:3, 3]
        src, dst, used = [], [], set()
        for i, p in enumerate(world):
            d = np.linalg.norm(pts - p, axis=1)
            j = int(np.argmin(d))
            if d[j] < self.gate and j not in used:
                used.add(j)
                src.append(cam[i])
                dst.append(pts[j])
        if len(src) < MIN_MARKERS:
            return None
        return kabsch(np.asarray(src, dtype=np.float32),
                      np.asarray(dst, dtype=np.float32))

    def _fit_poses(self, pts, poses):
        """Block one: markers fixed, re-solve every pose independently.

        Independently is the point. Nothing here is sequential, so a frame that
        was badly placed during seeding is not stuck with its neighbours'
        mistakes; it is simply re-fitted against the current best map.
        """
        out, assign = [], []
        for cam, init in zip(self.obs, poses):
            T = self._pose_against(cam, pts, init)
            if T is None:
                T = init
            out.append(T.astype(np.float32))
            world = (T[:3, :3] @ cam.T).T + T[:3, 3]
            pairs, used = [], set()
            for i, p in enumerate(world):
                d = np.linalg.norm(pts - p, axis=1)
                j = int(np.argmin(d))
                if d[j] < self.gate and j not in used:
                    used.add(j)
                    pairs.append((i, j))
            assign.append(pairs)
        return out, assign

    def _fit_points(self, poses, assign, n_pts):
        """Block two: poses fixed, every marker becomes the mean of its
        observations."""
        acc = np.zeros((n_pts, 3), dtype=np.float64)
        cnt = np.zeros((n_pts,), dtype=np.int64)
        for T, cam, pairs in zip(poses, self.obs, assign):
            if not pairs:
                continue
            world = (T[:3, :3] @ cam.T).T + T[:3, 3]
            for i, j in pairs:
                acc[j] += world[i]
                cnt[j] += 1
        live = cnt > 0
        if live.sum() < MIN_MARKERS:
            return None, None, None, 0.0
        pts = np.zeros((n_pts, 3), dtype=np.float32)
        pts[live] = (acc[live] / cnt[live, None]).astype(np.float32)

        # Residuals per marker, so the report can name the bad ones rather than
        # just reporting one number for the whole framework.
        sq = np.zeros((n_pts,), dtype=np.float64)
        total, n = 0.0, 0
        for T, cam, pairs in zip(poses, self.obs, assign):
            if not pairs:
                continue
            world = (T[:3, :3] @ cam.T).T + T[:3, 3]
            for i, j in pairs:
                e = float(np.sum((world[i] - pts[j]) ** 2))
                sq[j] += e
                total += e
                n += 1
        resid = np.zeros((n_pts,), dtype=np.float32)
        resid[live] = np.sqrt(sq[live] / np.maximum(cnt[live], 1)).astype(np.float32)
        rmse = float(np.sqrt(total / max(n, 1)))

        keep = live
        return pts[keep], cnt[keep], resid[keep], rmse

    # -- output --------------------------------------------------------------

    def report(self) -> dict:
        worst = float(self.residuals.max()) if len(self.residuals) else 0.0
        # Checked here, before the scan, rather than left to fail later. A
        # layout that cannot be recognised from a single frame is a placement
        # problem, and the moment to say so is while the stickers are still
        # easy to move.
        amb = layout_ambiguity(self.points) if len(self.points) else 0.0
        note = self.note
        if note is None and amb >= AMBIG_LAYOUT:
            note = ('The markers are too evenly spaced to tell apart from one '
                    'frame. Move a few of them in or out so no two have the '
                    'same neighbours.')

        # One marker fitting far worse than the rest has a specific cause worth
        # naming. Two stickers closer together than the matching gate collapse
        # into a single map point that then sits between them, so its residual
        # is roughly half their separation while every other marker stays at
        # sensor noise. The count comes back one short and nothing else looks
        # wrong, which is a miserable thing to debug from a millimetre figure.
        # Compared against the median rather than the rms, because the rms
        # already contains the outlier and gets dragged towards hiding it.
        if note is None and len(self.residuals) >= 3:
            med = float(np.median(self.residuals))
            if worst > 0.005 and med > 0 and worst > 3.0 * med:
                note = ('One marker fits %.0f times worse than the others. Two '
                        'stickers close enough to merge will do that, and so '
                        'will something bright that is not a marker at all. '
                        'Keep them at least 50 mm apart.' % (worst / med))
        return {
            'solved': self.solved,
            'markers': int(len(self.points)),
            'frames': len(self.obs),
            'rmse_mm': round(self.rmse * 1000, 2),
            'worst_mm': round(worst * 1000, 2),
            'min_obs': int(self.counts.min()) if len(self.counts) else 0,
            'ambiguity': amb,
            'usable': bool(self.solved and amb < AMBIG_LAYOUT),
            'note': note,
        }

    def export(self) -> dict:
        return {
            'points': [[float(v) for v in p] for p in self.points],
            'rmse': self.rmse,
            'counts': [int(c) for c in self.counts],
        }

    def load(self, data: dict) -> bool:
        pts = np.asarray(data.get('points') or [], dtype=np.float32).reshape(-1, 3)
        if len(pts) < MIN_MARKERS:
            return False
        self.reset()
        self.points = pts
        self.rmse = float(data.get('rmse') or 0.0)
        self.counts = np.asarray(data.get('counts') or [0] * len(pts), dtype=np.int32)
        self.residuals = np.zeros((len(pts),), dtype=np.float32)
        self.solved = True
        return True
