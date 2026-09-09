"""
Optical marker detection for turntable and low-feature tracking.

Why this exists: a smooth object rotating about its own axis is close to a
degenerate case for point-to-plane ICP. "No rotation" fits nearly as well as the
true rotation, so ICP reports an excellent fit, integrates every orientation at
the same pose, and the subject smears into a solid of revolution. More geometry
does not help when the geometry is ambiguous.

Markers fix it by supplying unambiguous correspondences. They are flat stickers
on a flat surface, so depth cannot see them at all: they exist only in the
colour image and have to be lifted into 3D using the depth frame.

Detection is scoped to a region of interest in 3D rather than run over the whole
picture. A lit room is far brighter than a matte black turntable, so a global
"find the bright things" search returns the walls every time. Working inside the
ROI also means we can operate in depth space and sample colour forward through
the calibration, instead of trying to invert a mapping that depends on depth.

The ROI narrows what is considered; it does not set how bright a marker must be.
That distinction was learned the hard way. Thresholding against the ROI median
means a wall or a white box inside the region raises the bar for everything, and
markers on a dark subject vanish while still looking obviously white on screen.
Brightness is judged locally instead, so a dot competes with the few centimetres
around it rather than with the brightest thing in the room.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage
from skimage import measure

# Depth -> colour pixel mapping, least-squares fitted against the SDK's own
# per-pixel mapper (mean residual 2.1 px). Same constants as the integrate kernel.
CU_A, CU_B, CU_C = 0.924115, 11.917571, 16.4471
CV_A, CV_C = 0.920909, 30.2714

FX = FY = 571.26
CX, CY = 320.0, 240.0

# Bootstrap acceptance. The tracking gate is for nearest-neighbour matching when
# the pose is already roughly known; recognising a map from nothing is a much
# stronger claim and needs a much higher bar, so these are deliberately not the
# same number. ACCEPT_M is the worst mean residual an identification may have;
# AMBIG_RATIO is how much better it must be than the best rival reading.
ACCEPT_M = 0.004
AMBIG_RATIO = 1.8
# Above this, the layout is symmetric enough that a partial view genuinely does
# not say which way round it is, and no threshold can recover what is not there.
# Refusing outright beats a filter that is right most of the time, because the
# times it is wrong produce a complete scan at the wrong orientation with
# nothing on screen to suggest it.
AMBIG_LAYOUT = 0.3

# Width of the local-background window, in pixels. A marker spans 3 to 8 px over
# the Kinect's working range, so 31 is several times wider than anything it
# should be measuring against itself, while still small enough to follow the
# subject's own brightness rather than the room's.
LOCAL_WIN = 31


def _luma(bgra: np.ndarray) -> np.ndarray:
    return (0.114 * bgra[:, :, 0].astype(np.float32) +
            0.587 * bgra[:, :, 1].astype(np.float32) +
            0.299 * bgra[:, :, 2].astype(np.float32))


def detect(depth_mm: np.ndarray, bgra: np.ndarray, roi,
           min_area=4, max_area=4000, contrast=1.55,
           min_mm=4.0, max_mm=30.0, plane=None, plane_tol=0.025):
    """Find markers inside `roi` and return (blobs, 3D positions).

    roi is (xmin, xmax, ymin, ymax, zmin, zmax) in depth-camera metres. For a
    turntable this should cover the platter, which usually means extending below
    the scan volume: the markers ride on the turntable, not on the subject.

    `contrast` is relative to each pixel's *local* background, not to the region
    as a whole, so a white sticker on a dark subject is found whether or not a
    lit wall happens to fall inside the ROI. The ROI still decides what is
    considered at all; it no longer decides how bright a marker has to be.

    `min_mm` and `max_mm` bound the marker's real diameter. This is the filter
    that does the work: brightness alone finds every highlight in the room, and
    a pixel-area bound means something different at every range. The pixel
    bounds survive only as a cheap prefilter, so they are deliberately loose.

    `plane`, when given, is the fitted turntable and detections must lie within
    `plane_tol` of it. Markers are stuck to the platter, so anything off it is
    not a marker however convincing it looks. On a real rig this was the
    difference between eight markers and eight markers plus seven printed labels
    on the front of the box underneath, which sat 30 to 70 mm lower and were the
    same size and brightness. No amount of tuning separates those in 2D; their
    height separates them immediately.
    """
    if depth_mm is None or bgra is None:
        return [], np.zeros((0, 3), dtype=np.float32)

    h, w = depth_mm.shape
    z = depth_mm.astype(np.float32) * 0.001
    us = np.arange(w, dtype=np.float32)[None, :]
    vs = np.arange(h, dtype=np.float32)[:, None]
    X = (us - CX) * z / FX
    Y = (vs - CY) * z / FY

    xmin, xmax, ymin, ymax, zmin, zmax = roi
    inroi = ((z > 0) & (X >= xmin) & (X <= xmax) &
             (Y >= ymin) & (Y <= ymax) & (z >= zmin) & (z <= zmax))
    if inroi.sum() < 200:
        return [], np.zeros((0, 3), dtype=np.float32)

    # Sample colour forward through the calibration, per depth pixel.
    lum = _luma(bgra)
    cu = np.clip((CU_A * us + CU_B / np.where(z > 0, z, 1.0) + CU_C), 0, w - 1)
    cv = np.clip((CV_A * vs + CV_C), 0, h - 1)
    sampled = lum[cv.astype(np.int32) * np.ones((1, w), dtype=np.int32),
                  cu.astype(np.int32)]

    # Bright compared to its own surroundings, not to the region as a whole.
    #
    # A single threshold taken from the ROI median only works when the ROI
    # contains the turntable and little else. Let a lit wall or a white box into
    # it and the median climbs, the threshold climbs with it, and markers on a
    # dark subject drop below the line while still being obviously white to the
    # eye. Measured on a real survey: dots at luma 162 on a tower of median 82,
    # room-wide threshold 200, so not one marker was ever detected while the
    # preview showed them plainly.
    #
    # A local background fixes it at the cause. The window is far wider than a
    # marker, so a marker barely lifts its own background, but it is narrow
    # enough to track the subject's own brightness across the frame. The
    # absolute floor keeps sensor noise in genuinely dark corners from becoming
    # thousands of tiny blobs, since anything is bright relative to nothing.
    bg = ndimage.uniform_filter(sampled, size=LOCAL_WIN, mode='nearest')
    mask = (inroi & (sampled > bg * contrast) & (sampled > bg + 18.0)
            & (sampled > 45.0))

    labels = measure.label(mask, connectivity=2)
    blobs, pts = [], []
    for p in measure.regionprops(labels):
        if p.area < min_area or p.area > max_area:
            continue
        minr, minc, maxr, maxc = p.bbox
        hgt, wid = maxr - minr, maxc - minc
        if hgt == 0 or wid == 0:
            continue
        aspect = max(hgt, wid) / min(hgt, wid)
        fill = p.area / float(hgt * wid)
        if aspect > 2.6 or fill < 0.40:
            continue

        rr, cc = p.coords[:, 0], p.coords[:, 1]
        zz = float(np.median(z[rr, cc]))
        if not np.isfinite(zz) or zz <= 0:
            continue

        # How big the thing actually is, now that its range is known. A pixel
        # area cannot express "a sticker": the same 10 mm dot covers about 40
        # pixels at 0.8 m and 7 at 2 m, so any fixed pixel window either throws
        # away real markers at the far end of the volume or waves through
        # anything bright and nearby. Carpet texture, the buttons on a remote
        # and the lit edge of a box all pass a pixel test and none of them are
        # 10 mm across at the range they were found.
        diam = 2.0 * float(np.sqrt(p.area / np.pi)) * zz / FX
        if diam < min_mm * 0.001 or diam > max_mm * 0.001:
            continue

        uu, vv = float(np.mean(cc)), float(np.mean(rr))

        if plane is not None:
            pt = np.array([(uu - CX) * zz / FX, (vv - CY) * zz / FY, zz],
                          dtype=np.float32)
            if abs(float(pt @ np.asarray(plane[0], dtype=np.float32)) + plane[1]) > plane_tol:
                continue

        # Pixels across, which is what actually limits whether a marker can be
        # found at all. Reported so the caller can say "your markers are four
        # pixels wide, use bigger ones" instead of leaving someone to tune
        # thresholds against a sensor limit.
        px = 2.0 * float(np.sqrt(p.area / np.pi))
        blobs.append({'u': uu, 'v': vv, 'area': int(p.area),
                      'mm': round(diam * 1000, 1), 'px': round(px, 1),
                      'aspect': round(float(aspect), 2),
                      'fill': round(float(fill), 2), 'z': round(zz, 3)})
        pts.append(((uu - CX) * zz / FX, (vv - CY) * zz / FY, zz))

    return blobs, np.asarray(pts, dtype=np.float32).reshape(-1, 3)


def kabsch(a: np.ndarray, b: np.ndarray):
    """Rigid transform taking `a` onto `b`, from matched 3D pairs.

    Closed form: no iteration, no local minimum to fall into. That is the whole
    advantage over ICP on ambiguous geometry. With known correspondences the
    pose is solved rather than searched for.
    """
    if len(a) < 3 or len(a) != len(b):
        return None
    ca, cb = a.mean(0), b.mean(0)
    H = (a - ca).T @ (b - cb)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    T = np.eye(4, dtype=np.float32)
    T[:3, :3] = R
    T[:3, 3] = cb - R @ ca
    return T


def layout_ambiguity(pts: np.ndarray) -> float:
    """How easily a marker layout can be mistaken for a rotation of itself.

    Stickers placed evenly round the rim of a turntable look the same from every
    angle, which makes recognising the map from a single frame genuinely
    impossible rather than merely hard. bootstrap() refuses in that case, and
    refusing with no explanation is a bad experience, so the layout is measured
    once when the framework is installed and the number is reported.

    Each marker is described by the sorted distances to its neighbours. If two
    different markers have nearly the same description, they are
    interchangeable. The score is the fraction of markers with a twin, so 0 is a
    layout with no symmetry to trip over and anything much above 0 wants the
    stickers moved.
    """
    n = len(pts)
    if n < 4:
        return 0.0
    d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
    sig = np.sort(d, axis=1)[:, 1:]           # drop the zero to itself
    # Tolerance scales with the layout, so a big turntable is not judged by the
    # same absolute millimetre as a small one.
    tol = max(0.004, 0.04 * float(np.median(sig)))
    twins = 0
    for i in range(n):
        for j in range(n):
            if i != j and np.abs(sig[i] - sig[j]).max() < tol:
                twins += 1
                break
    return round(twins / float(n), 3)


class MarkerTracker:
    """Pose from markers, solved against a persistent map rather than the
    previous frame.

    Frame-to-frame would compound its own error every step, exactly as
    frame-to-frame ICP does. Matching against a map of markers in world space
    keeps the pose anchored to everything seen so far, and lets markers drop out
    of view and come back without losing the thread.

    The map can be in one of two states, and the difference matters more than it
    looks. Unlocked, it grows as new markers appear, which is enough to scan
    with but has a flaw it cannot recover from: whatever error a marker carries
    when it is first added stays in the map forever, and every later pose is
    measured against it. Locked, the map came from a framework pass that solved
    every marker against every frame at once (see framework.py), so it is fixed
    ground truth and nothing during the scan can move it.

    Locked is the better mode and is what CR Scan does. Unlocked stays because
    it works without a separate pass and is the right default for a quick scan.

    The markers are identical to each other, so correspondence is nearest
    neighbour under the current pose estimate with a distance gate. That is
    sound as long as consecutive frames are close together, which at 20 Hz on a
    turntable they comfortably are.
    """

    def __init__(self, gate=0.035, min_pairs=3):
        self.gate = gate
        self.min_pairs = min_pairs
        self.locked = False
        self.reset()

    def reset(self):
        self.map = np.zeros((0, 3), dtype=np.float32)
        self.matched = 0
        self.rmse = 0.0
        self.locked = False
        self._anchored = False
        self.ambiguity = 0.0

    def load(self, points: np.ndarray):
        """Adopt a solved framework as the map, and stop it changing.

        Called between the framework pass and the geometry scan. After this the
        map is a measurement, not an accumulation, so _extend is disabled: a
        marker the framework did not solve for is one the framework decided not
        to trust, and letting the scan add it back defeats the point.
        """
        pts = np.asarray(points, dtype=np.float32).reshape(-1, 3)
        if len(pts) < self.min_pairs:
            return False
        self.map = pts
        self.ambiguity = layout_ambiguity(pts)
        self.locked = True
        self._anchored = False
        self.matched = 0
        self.rmse = 0.0
        return True

    def unlock(self):
        self.locked = False
        self._anchored = False
        self.ambiguity = 0.0

    @property
    def anchored(self):
        """True once a locked map has been recognised in the live frame."""
        return self._anchored or not self.locked

    def track(self, cam_pts: np.ndarray, pose: np.ndarray):
        """cam_pts: markers in camera space. pose: current camera-to-world.

        Returns (new_pose, matched_count, rmse). new_pose is None when there are
        not enough matches to solve, in which case the caller should hold the old one.
        """
        if len(cam_pts) == 0:
            self.matched = 0
            return None, 0, 0.0

        # A locked map is in the framework's world frame, which the scan has no
        # relationship to until one frame has been recognised in it. Until that
        # happens the incoming pose is meaningless as a starting estimate.
        if self.locked and not self._anchored:
            T = self.bootstrap(cam_pts)
            if T is None:
                self.matched = 0
                return None, 0, 0.0
            self._anchored = True
            pose = T

        R, t = pose[:3, :3], pose[:3, 3]
        world = (R @ cam_pts.T).T + t

        if len(self.map) == 0:
            self.map = world.astype(np.float32)
            self.matched = len(world)
            self.rmse = 0.0
            return pose, self.matched, 0.0

        # nearest neighbour under the current estimate, with a gate
        src, dst = [], []
        used = set()
        for i, p in enumerate(world):
            d = np.linalg.norm(self.map - p, axis=1)
            j = int(np.argmin(d))
            if d[j] < self.gate and j not in used:
                used.add(j)
                src.append(cam_pts[i])
                dst.append(self.map[j])

        self.matched = len(src)
        if self.matched < self.min_pairs:
            # Not solvable, but still worth remembering anything new so the map
            # keeps growing while the subject turns.
            self._extend(world)
            return None, self.matched, 0.0

        A = np.asarray(src, dtype=np.float32)
        B = np.asarray(dst, dtype=np.float32)
        T, inl = self._ransac(A, B)
        if T is None:
            return None, self.matched, 0.0

        # Report on the inliers only. Averaging in the outliers would hide the
        # very thing RANSAC is there to find.
        self.matched = int(inl.sum())
        moved = (T[:3, :3] @ A[inl].T).T + T[:3, 3]
        self.rmse = float(np.sqrt(((moved - B[inl]) ** 2).sum(1).mean()))

        newpose = T.astype(np.float32)
        R2, t2 = newpose[:3, :3], newpose[:3, 3]
        # Only inliers earn a place in the map. Letting phantoms in would poison
        # every future frame's matching.
        self._extend((R2 @ A[inl].T).T + t2)
        return newpose, self.matched, self.rmse

    def _ransac(self, A, B, thresh=0.008, iters=64):
        """Pose from the largest self-consistent subset of correspondences.

        Blob detection picks up more than the stickers: specular highlights, a
        metal plaque, bright edges. Those are real detections with real 3D
        positions, they just do not correspond to anything stable, and a
        least-squares fit over all of them is dragged by every one. Solving from
        minimal samples and keeping the pose that most points agree with throws
        them out instead of averaging them in.
        """
        n = len(A)
        if n < self.min_pairs:
            return None, np.zeros(n, dtype=bool)
        if n == self.min_pairs:
            T = kabsch(A, B)
            return T, np.ones(n, dtype=bool)

        rng = np.random.default_rng(12345)
        best_T, best_inl = None, np.zeros(n, dtype=bool)
        for _ in range(iters):
            idx = rng.choice(n, 3, replace=False)
            T = kabsch(A[idx], B[idx])
            if T is None:
                continue
            moved = (T[:3, :3] @ A.T).T + T[:3, 3]
            err = np.linalg.norm(moved - B, axis=1)
            inl = err < thresh
            if inl.sum() > best_inl.sum():
                best_T, best_inl = T, inl
            if best_inl.sum() == n:
                break

        if best_T is None or best_inl.sum() < self.min_pairs:
            return None, np.zeros(n, dtype=bool)
        # Refit on everything that agreed, which is more accurate than the
        # three points that happened to be sampled.
        refit = kabsch(A[best_inl], B[best_inl])
        return (refit if refit is not None else best_T), best_inl

    def bootstrap(self, cam_pts: np.ndarray, tol=0.004):
        """First pose against a locked map, with no prior estimate to start from.

        A framework is solved in its own pass, so by the time the geometry scan
        starts the turntable has usually been moved and the scan's first frame
        is at some unknown rotation relative to the framework's world frame.
        Nearest-neighbour matching cannot get started from there, because it
        needs the pose it is trying to find.

        The way in is that the markers are identical but their *arrangement* is
        not. The distances from one marker to all the others are a signature
        that does not change when the whole set is rotated or moved, so a marker
        can be recognised by its neighbourhood before any pose is known. Only
        some of the map is visible in one frame, so the test is that the
        observed distances appear among the map point's distances, not that the
        two lists match.

        Getting this wrong is worse than failing at it. Markers placed evenly
        round the rim of a turntable are close to rotationally symmetric, and a
        handful of them will sit on a *different* subset of the map, rotated, to
        within a few millimetres. That hypothesis passes any test based on the
        tracking gate, and the reward is an entire scan built at the wrong
        orientation with nothing on screen to suggest it.

        So there are two defences. A layout measured as self-similar is refused
        outright, before any matching, because the information needed to tell
        the difference is not in the frame and no threshold can conjure it.
        Anything that gets past that has to fit tightly in absolute terms and
        beat the best genuinely different reading of the same view.

        Returns a pose, or None. None here is not a failure to try hard enough;
        it means the answer is not determined, and the caller should say so
        rather than pick one.
        """
        cam = np.asarray(cam_pts, dtype=np.float32)
        if len(self.map) < self.min_pairs or len(cam) < self.min_pairs:
            return None
        if self.ambiguity >= AMBIG_LAYOUT:
            return None

        dc = np.linalg.norm(cam[:, None, :] - cam[None, :, :], axis=2)
        dm = np.linalg.norm(self.map[:, None, :] - self.map[None, :, :], axis=2)

        # A candidate pairing needs most of its neighbourhood to agree, not two
        # coincidences. With four markers in view that is three of three.
        need = max(2, min(3, len(cam) - 1))
        pairs = []
        for i in range(len(cam)):
            di = np.delete(dc[i], i)
            for j in range(len(self.map)):
                dj = np.delete(dm[j], j)
                hits = int((np.abs(di[:, None] - dj[None, :]) < tol).any(axis=1).sum())
                if hits >= need:
                    pairs.append((hits, i, j))
        if len(pairs) < self.min_pairs:
            return None
        pairs.sort(reverse=True)
        pairs = pairs[:40]

        rng = np.random.default_rng(4242)
        seen = {}
        for _ in range(384):
            pick = rng.choice(len(pairs), 3, replace=False)
            sel = [pairs[k] for k in pick]
            if len({p[1] for p in sel}) < 3 or len({p[2] for p in sel}) < 3:
                continue
            T = kabsch(np.array([cam[p[1]] for p in sel], dtype=np.float32),
                       np.array([self.map[p[2]] for p in sel], dtype=np.float32))
            if T is None:
                continue
            moved = (T[:3, :3] @ cam.T).T + T[:3, 3]
            d = np.linalg.norm(self.map[None, :, :] - moved[:, None, :], axis=2)
            near = d.argmin(axis=1)
            best = d.min(axis=1)
            inl = best < self.gate
            n = int(inl.sum())
            if n < self.min_pairs:
                continue
            key = tuple(np.where(inl, near, -1).tolist())
            resid = float(best[inl].mean())
            prev = seen.get(key)
            if prev is None or resid < prev[1]:
                seen[key] = (n, resid, T)

        if not seen:
            return None
        order = sorted(seen.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
        ranked_keys = [k for k, _ in order]
        ranked = [v for _, v in order]
        n, resid, T = ranked[0]

        # Three points always fit three points, so agreeing with the triple that
        # produced the pose proves nothing. A fourth marker landing on the map is
        # the first piece of evidence the pose is real.
        if n < 4 or resid > ACCEPT_M:
            return None

        # The rival has to be a genuinely different reading, not the same one
        # with a marker or two dropped. Hypotheses that differ by a single
        # assignment are the same answer either way, and treating them as rivals
        # would refuse every honest match.
        best_key = ranked_keys[0]
        for k, (alt_n, alt_resid, _) in zip(ranked_keys[1:], ranked[1:]):
            differs = sum(1 for a, b in zip(best_key, k) if a != b and a >= 0 and b >= 0)
            if differs < 2:
                continue
            if alt_n >= n and alt_resid < resid * AMBIG_RATIO:
                # Two different readings of the same view, both plausible. They
                # really are: refusing is the only answer that is not a guess.
                return None
            break

        # Refit on everything that agreed. The three sampled points carry their
        # own detection noise and a pose fitted to them alone inherits all of it;
        # over eight or nine markers most of it averages out.
        moved = (T[:3, :3] @ cam.T).T + T[:3, 3]
        src, dst, used = [], [], set()
        for i, p in enumerate(moved):
            d = np.linalg.norm(self.map - p, axis=1)
            j = int(np.argmin(d))
            if d[j] < self.gate and j not in used:
                used.add(j)
                src.append(cam[i])
                dst.append(self.map[j])
        if len(src) >= self.min_pairs:
            refit = kabsch(np.asarray(src, dtype=np.float32),
                           np.asarray(dst, dtype=np.float32))
            if refit is not None:
                return refit.astype(np.float32)
        return T.astype(np.float32)

    def _extend(self, world_pts):
        """Add markers we have not seen before, so the map covers a full turn."""
        if self.locked:
            return
        if len(self.map) == 0:
            self.map = world_pts.astype(np.float32)
            return
        keep = []
        for p in world_pts:
            if np.min(np.linalg.norm(self.map - p, axis=1)) > self.gate:
                keep.append(p)
        if keep:
            self.map = np.vstack([self.map, np.asarray(keep, dtype=np.float32)])
