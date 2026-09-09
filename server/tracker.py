"""
KinectFusion-style tracking against a GPU TSDF volume.

This replaced an earlier version that accumulated raw point clouds and
registered each frame against the growing pile. That approach cannot get
better with more data: every frame adds another noisy copy of the same
surface, offset by its own registration error, so surfaces thicken and smear
the longer you scan.

A TSDF fixes it at the data structure level. Each voxel holds a running
weighted average of its signed distance to the surface, so a surface seen
fifty times converges instead of accumulating. That is the difference between
a scanner that builds up and one that mashes together.

Per frame, in KinectFusion's order:

    bilateral filter  ->  ICP against a raycast of the model  ->  integrate

Tracking registers to a raycast of the volume rather than to the previous
frame. Frame-to-frame compounds its own error every step; frame-to-model is
anchored to everything seen so far.

Failures are classified rather than lumped into "lost". This is worth the code:
"nothing is inside the scan volume", "the shape cannot pin down a pose" and
"you moved too fast" all present as a low score, and they have nothing to do
with each other. Telling someone their scan failed without telling them which
of those happened leaves them to guess, and the guess is usually wrong. CR Scan
reached the same conclusion, shipping OB_SCAN_TRACKING_AMBIGUOUS_PLANE,
_FAILURE_INSUFF_VALID_POINTS and _FAILURE_MOVING_TOO_FAST as distinct results.

Poses are camera-to-world 4x4 in the CV convention (X right, Y down, Z
forward), matching the depth camera. to_gl() converts on the way out.
"""

from __future__ import annotations

import numpy as np

import geometry as geo
import gputsdf
import markers as mk
from framework import Framework

GOOD, WEAK, LOST = 'good', 'weak', 'lost'

FIT_GOOD = 0.35
FIT_WEAK = 0.12

# Reasons. Every one of these has a detector below; there is no code here that
# can never fire.
R_NONE = None
R_FEW_POINTS = 'few_points'
R_AMBIGUOUS = 'ambiguous_plane'
R_TOO_FAST = 'too_fast'
R_NO_MARKERS = 'no_markers'
R_NOT_ANCHORED = 'not_anchored'
R_AMBIGUOUS_LAYOUT = 'ambiguous_layout'
R_LOST = 'lost'

# Below this, the normal equations are close to singular in at least one
# direction and the pose is free to slide.
#
# Measured, not guessed, against rendered scenes with known geometry (median
# over ten frames, 4 mm voxels):
#
#     sphere alone, no floor                3e-6      degenerate
#     floor plus one sphere                 1.1e-5    degenerate
#     flat wall filling the view            2.0e-5    degenerate, fitness 0.96
#     floor, sphere and a second lump       3.4e-4    fine
#     floor and five lumps                  5.8e-4    fine
#
# A sphere on its own belongs with the degenerate cases and is a good check that
# the number means what it claims: it is a surface of revolution about every
# axis, so it fixes position and says nothing at all about orientation. Note the
# wall scores fitness 0.96 while being useless, which is the whole reason this
# measure exists.
#
# The threshold sits at the geometric mean of the 17x gap between the two
# groups. Worth re-measuring if the voxel size or the ICP iteration count
# changes much, since both move the absolute scale.
COND_FLOOR = 8e-5

# Conditioning is low for the first few seconds of every scan, because the model
# being registered against is thin rather than because the subject is degenerate.
# Measured on a scan that ends up perfectly well conditioned: 1e-5 at frame 6,
# 1.1e-4 by frame 24, 3.6e-4 by frame 35. Reporting that as an ambiguous shape
# would be technically true and completely useless, since the fix is to carry on
# scanning, which is what the user is already doing. So the check waits until the
# model has had a chance to build, and wants the condition to persist rather than
# firing on one dip.
AMBIG_GRACE = 12
AMBIG_RUN = 5

# One frame of plausible movement. Past this the frame is motion-blurred anyway,
# so the registration that produced the jump is not worth trusting.
#
# The allowance grows while frames are being dropped, and that is not a fudge:
# the comparison is against the last pose actually accepted, so after three
# rejected frames the sensor has genuinely had three frames' worth of time to
# move. Without this the check traps itself. One rejection makes the next frame
# look like a bigger jump, which rejects it too, and the scan never recovers.
# The cap stops the allowance widening until it waves everything through.
MAX_STEP_M = 0.040
MAX_STEP_RAD = np.radians(10.0)
MAX_STEP_CATCHUP = 5

POSE_HISTORY = 400


def to_gl(a: np.ndarray) -> np.ndarray:
    """CV convention (Y down, +Z forward) to GL (Y up, -Z forward).

    Negating Y and Z is a 180 degree rotation about X, so handedness and
    triangle winding are preserved. Applied once, here, so exactly one
    convention ever goes over the wire.
    """
    out = np.array(a, dtype=np.float32, copy=True)
    if out.size:
        out[:, 1] *= -1.0
        out[:, 2] *= -1.0
    return out


def _step(a: np.ndarray, b: np.ndarray):
    """Translation and rotation between two poses."""
    dt = float(np.linalg.norm(b[:3, 3] - a[:3, 3]))
    R = a[:3, :3].T @ b[:3, :3]
    c = (float(np.trace(R)) - 1.0) * 0.5
    return dt, float(np.arccos(max(-1.0, min(1.0, c))))


class Tracker:
    def __init__(self, size=1.2, voxel=0.005, centre_z=1.3,
                 centre_x=0.0, centre_y=0.0,
                 dmin=0.5, dmax=2.0, width=640, height=480):
        self.size = size
        self.voxel = voxel
        self.map_colour = False
        self.vol = gputsdf.Volume(size=size, voxel=voxel, centre_z=centre_z,
                                  centre_x=centre_x, centre_y=centre_y,
                                  dmin=dmin, dmax=dmax, reserve_colour=False,
                                  width=width, height=height)
        self.mtrack = mk.MarkerTracker()
        self.fw = Framework()
        self.mode = 'geometry'
        self.blobs = []
        self.turntable = None
        # Colour costs three of the five float32 channels a voxel carries, so
        # it is 60% of the volume. It is off by default and only earns that
        # when the mesh is actually going to be textured; marker detection uses
        # the colour *frame* and never touches the volume's colour channel.
        self.map_colour = False
        self.reset()

    # -- lifecycle -----------------------------------------------------------

    def reset(self):
        self.vol.reset()
        self.pose = np.eye(4, dtype=np.float32)
        self.frames = 0
        self.registered = 0
        self.fitness = 0.0
        self.rmse = 0.0
        self.cond = 0.0
        self.state = GOOD
        self.reason = R_NONE
        self._started = False
        self._surface = 0
        self._surface_at = -99
        self._dropped = 0
        self._ambig_run = 0
        self._poses = []
        self.blobs = []
        # A locked framework survives a scan reset on purpose: it was solved in
        # its own pass and clearing the scan does not make it any less true.
        if self.mtrack.locked:
            self.mtrack._anchored = False
        else:
            self.mtrack.reset()

    def reconfigure(self, size=None, voxel=None, centre_z=None,
                    centre_x=None, centre_y=None, dmin=None, dmax=None):
        """Rebuild the volume. Discards the scan, so callers should confirm.

        The old volume is released first. Building the new one alongside it
        would need both on the card at once, which doubles the peak for no
        benefit: the scan is being discarded either way. It also made the
        budget self-defeating, since free VRAM counts the current volume as
        spent and the volume you are already using then looks too big to fit.
        """
        v = self.vol
        old_size, old_voxel = self.size, self.voxel
        geom = dict(
            centre_x=centre_x if centre_x is not None else v.origin[0] + old_size / 2,
            centre_y=centre_y if centre_y is not None else v.origin[1] + old_size / 2,
            centre_z=centre_z if centre_z is not None else v.origin[2] + old_size / 2,
            dmin=dmin if dmin is not None else v.dmin,
            dmax=dmax if dmax is not None else v.dmax,
            width=v.W, height=v.H)
        self.size = size if size is not None else self.size
        self.voxel = voxel if voxel is not None else self.voxel

        self.vol = None
        del v
        gputsdf.release()

        try:
            self.vol = gputsdf.Volume(size=self.size, voxel=self.voxel,
                                      reserve_colour=self.map_colour, **geom)
        except Exception:
            # Nothing is on the card now, so the old geometry has to be rebuilt
            # rather than merely remembered. If that fails too there is no
            # volume at all, and the caller has to hear about the original
            # reason rather than a second failure masking it.
            self.size, self.voxel = old_size, old_voxel
            # Geometry only, whatever was asked for. The request that just
            # failed was too large, and repeating it with the same colour
            # setting fails identically, which is how this left no volume at
            # all and took every websocket connection down with it.
            self.map_colour = False
            try:
                self.vol = gputsdf.Volume(size=old_size, voxel=old_voxel,
                                          reserve_colour=False, **geom)
            except Exception:
                pass
            raise
        self.reset()

    # -- main ----------------------------------------------------------------

    def marker_roi(self):
        """Where to look for markers: the scan volume, extended downward.

        Markers ride on the turntable, which sits below the subject and is
        deliberately outside the scan volume so it never gets integrated. They
        still have to be *seen*, so the search region and the scan region are
        not the same box.
        """
        ox, oy, oz = self.vol.origin
        s = self.size
        return (ox - 0.12, ox + s + 0.12,
                oy - 0.06, oy + s + 0.35,      # +Y is down: extend under the subject
                oz - 0.12, oz + s + 0.12)

    def marker_plane(self):
        """The turntable as (normal, offset) in camera space, if it is known.

        Markers are stuck to the platter, so this is the strongest filter
        available: it is geometric rather than photometric, and nothing about
        lighting or exposure can defeat it.
        """
        t = self.turntable
        if not t:
            return None
        return np.asarray(t['up'], dtype=np.float32), float(t['offset'])

    def _detect(self, depth_mm, bgra):
        return mk.detect(depth_mm, bgra, self.marker_roi(), plane=self.marker_plane())

    def add(self, depth_mm: np.ndarray, bgra: np.ndarray | None = None) -> dict:
        self.frames += 1

        if self.mode == 'framework':
            return self._survey(depth_mm, bgra)

        depth = self.vol.denoise(depth_mm)

        def _integrate(pose):
            if bgra is not None and self.map_colour:
                self.vol.integrate_rgb(depth, bgra, pose)
            else:
                self.vol.integrate(depth, pose)

        if not self._started:
            # The very first frame defines the world frame, unless a solved
            # framework already defines it, in which case we have to find our
            # place in it before anything may be integrated.
            if self.mode == 'markers' and self.mtrack.locked and bgra is not None:
                self.blobs, cam = self._detect(depth_mm, bgra)
                T = self.mtrack.bootstrap(cam) if len(cam) else None
                if T is None:
                    self.fitness, self.rmse = 0.0, 0.0
                    self.state, self.reason = LOST, self._anchor_reason(len(cam))
                    return self.status()
                self.pose = T
                self.mtrack._anchored = True
            _integrate(self.pose)
            self._started = True
            self.registered = 1
            self._dropped = 0
            self._ambig_run = 0
            self._poses = [self.pose.copy()]
            self._surface = self.vol.surface_voxels()
            self._surface_at = self.frames
            self.fitness, self.rmse, self.state, self.reason = 1.0, 0.0, GOOD, R_NONE
            if self.mode == 'markers' and bgra is not None and not self.mtrack.locked:
                self.blobs, cam = self._detect(depth_mm, bgra)
                self.mtrack.track(cam, self.pose)
            return self.status()

        if self.mode == 'markers' and bgra is not None:
            return self._track_markers(depth_mm, bgra, _integrate)

        pose, fitness, rmse, cond = self.vol.track(depth, self.pose, self.pose)
        self.fitness = float(fitness)
        self.rmse = float(rmse)
        self.cond = float(cond)

        self.state, self.reason = self._classify(pose)

        # Integrating at a pose we do not trust corrupts the volume in a way
        # nothing downstream can undo, so a lost frame is simply dropped and
        # the previous pose is kept for the next attempt.
        if self.state != LOST and np.all(np.isfinite(pose)):
            self.pose = pose
            _integrate(self.pose)
            self.registered += 1
            self._dropped = 0
            self._remember(pose)
        else:
            self._dropped += 1

        # A full-grid reduction purely to show a number; every tenth frame is
        # plenty and keeps the tracking loop at ~5 ms instead of ~12 ms.
        if self.frames - self._surface_at >= 10:
            self._surface = self.vol.surface_voxels()
            self._surface_at = self.frames
        return self.status()

    def _classify(self, pose):
        """Why the frame did or did not register.

        Order matters. A frame with nothing in the volume also has terrible
        conditioning, and reporting the conditioning would send someone off to
        fix their subject's shape when the real problem is that they are
        pointing at the wrong place. Cheapest and most concrete explanation
        first.
        """
        if self.vol.inbox < 1500:
            return LOST, R_FEW_POINTS

        if np.all(np.isfinite(pose)) and self._started:
            dt, dr = _step(self.pose, pose)
            k = min(1 + self._dropped, MAX_STEP_CATCHUP)
            if dt > MAX_STEP_M * k or dr > MAX_STEP_RAD * k:
                return LOST, R_TOO_FAST

        # The dangerous case: ICP is delighted, and wrong. Fitness is high
        # because the frame does match the model, and the pose is unconstrained
        # anyway because everything in view is flat or a surface of revolution.
        if self.fitness >= FIT_WEAK and self.cond < COND_FLOOR:
            self._ambig_run += 1
            if self.registered >= AMBIG_GRACE and self._ambig_run >= AMBIG_RUN:
                return WEAK, R_AMBIGUOUS
        else:
            self._ambig_run = 0

        if self.fitness >= FIT_GOOD:
            return GOOD, R_NONE
        if self.fitness >= FIT_WEAK:
            return WEAK, R_LOST
        return LOST, R_LOST

    def _track_markers(self, depth_mm, bgra, integrate):
        self.blobs, cam = self._detect(depth_mm, bgra)
        mpose, matched, mrmse = self.mtrack.track(cam, self.pose)
        # Fitness here is how many markers were matched against the map,
        # normalised against a comfortable working number rather than a
        # pixel count, so it stays comparable to the geometric figure.
        self.fitness = min(1.0, matched / 5.0)
        self.rmse = mrmse

        if len(cam) < 3:
            self.state, self.reason = LOST, R_NO_MARKERS
        elif mpose is None and self.mtrack.locked and not self.mtrack._anchored:
            self.state, self.reason = LOST, self._anchor_reason(len(cam))
        elif mpose is not None and np.all(np.isfinite(mpose)):
            dt, dr = _step(self.pose, mpose)
            k = min(1 + self._dropped, MAX_STEP_CATCHUP)
            if dt > MAX_STEP_M * k or dr > MAX_STEP_RAD * k:
                self.state, self.reason = LOST, R_TOO_FAST
            else:
                self.state = GOOD if matched >= 4 else WEAK
                self.reason = R_NONE if matched >= 4 else R_LOST
                self.pose = mpose.astype(np.float32)
                integrate(self.pose)
                self.registered += 1
                self._remember(self.pose)
        else:
            self.state, self.reason = LOST, R_LOST

        if self.state == LOST:
            self._dropped += 1
        else:
            self._dropped = 0

        if self.frames - self._surface_at >= 10:
            self._surface = self.vol.surface_voxels()
            self._surface_at = self.frames
        return self.status()

    def _anchor_reason(self, seen):
        """Why a locked framework has not been recognised yet.

        Three markers is the floor for a pose at all; a layout too regular to
        identify is a different problem with a different fix, and conflating
        them sends people to move the sensor when they should be moving the
        stickers."""
        if seen < 3:
            return R_NO_MARKERS
        if self.mtrack.ambiguity >= mk.AMBIG_LAYOUT:
            return R_AMBIGUOUS_LAYOUT
        return R_NOT_ANCHORED

    def _survey(self, depth_mm, bgra):
        """Framework pass: look at markers, integrate nothing.

        Deliberately does not touch the volume. This pass exists to measure
        where the markers are, and mixing it with fusion would mean the geometry
        was built against the very poses the pass has not finished solving yet.
        """
        if bgra is None:
            self.state, self.reason = LOST, R_NO_MARKERS
            return self.status()
        self.blobs, cam = self._detect(depth_mm, bgra)
        self.fw.observe(cam)
        self.fitness = min(1.0, len(cam) / 5.0)
        self.rmse = 0.0
        if len(cam) < 3:
            self.state, self.reason = LOST, R_NO_MARKERS
        else:
            self.state = GOOD if len(cam) >= 4 else WEAK
            self.reason = R_NONE if len(cam) >= 4 else R_LOST
        return self.status()

    def _remember(self, pose):
        self._poses.append(np.array(pose, dtype=np.float32, copy=True))
        if len(self._poses) > POSE_HISTORY:
            del self._poses[0]

    # -- framework -----------------------------------------------------------

    def solve_framework(self) -> dict:
        return self.fw.solve()

    def apply_framework(self) -> bool:
        """Install the solved framework as the tracker's fixed reference."""
        if not self.fw.solved:
            return False
        return self.mtrack.load(self.fw.points)

    def clear_framework(self):
        self.fw.reset()
        self.mtrack.unlock()
        self.mtrack.reset()

    # -- turntable -----------------------------------------------------------

    def find_turntable(self, depth_mm) -> dict | None:
        """Fit the platter plane and, if there is enough orbit, its axis.

        The plane comes from one frame. The axis needs a run of poses, because
        it is recovered from the circle the camera appears to trace around a
        static subject, and one frame is not a circle.
        """
        plane = geo.find_turntable(depth_mm)
        if plane is None:
            self.turntable = None
            return None
        axis = geo.axis_from_poses(self._poses) if len(self._poses) >= 8 else None
        if axis:
            plane['axis'] = axis
        self.turntable = plane
        return plane

    def volume_above_turntable(self, clearance=0.01):
        """Lift the box so its lower face clears the platter.

        The single most common way a turntable scan fails is the table being
        inside the scan volume: it is static while the subject turns, so it
        dominates the registration and the subject smears. Placing the box by
        eye with three sliders is a fiddly way to solve a problem the sensor can
        measure directly.

        The box moves along the plane normal only, keeping wherever it already
        sits laterally. Snapping it to the centroid of the fitted plane would be
        right for a small platter and wrong for everything else: fit a desk or a
        floor and the centroid is metres from the subject, so pressing this
        would sail the box off the thing being scanned. Height is what the
        sensor can measure; where to aim is the user's business.
        """
        t = self.turntable
        if not t:
            return None
        up = np.asarray(t['up'], dtype=np.float32)
        cur = np.asarray([self.vol.origin[i] + self.size / 2.0 for i in range(3)],
                         dtype=np.float32)
        want = self.size / 2.0 + clearance
        c = cur + up * (want - (float(up @ cur) + t['offset']))
        return {
            'centre_x': round(float(c[0]), 4),
            'centre_y': round(float(c[1]), 4),
            'centre_z': round(float(c[2]), 4),
            'dmin': round(max(0.4, float(c[2]) - self.size * 0.9), 3),
            'dmax': round(float(c[2]) + self.size * 0.9, 3),
        }

    # -- output --------------------------------------------------------------

    def bytes_per_voxel(self):
        """What a voxel costs with the current settings."""
        return gputsdf.BYTES_GEOM + (gputsdf.BYTES_COLOUR if self.map_colour else 0)

    def plan(self, size=None, voxel=None, colour=True):
        """Cost of a volume the caller is considering, without building it."""
        return gputsdf.plan(self.size if size is None else size,
                            self.voxel if voxel is None else voxel,
                            colour=colour)

    def status(self) -> dict:
        st = {
            'frames': self.frames,
            'registered': self.registered,
            'points': self._surface,
            'tracking': self.state,
            'reason': self.reason,
            'fitness': round(self.fitness, 3),
            'rmse': round(self.rmse, 4),
            'cond': round(self.cond, 5),
            'inbox': int(self.vol.inbox),
            'voxel_mm': round(self.voxel * 1000, 1),
            'volume_m': round(self.size, 2),
            'mode': self.mode,
            'markers_seen': len(self.blobs),
            'markers_mapped': int(len(self.mtrack.map)),
            'locked': bool(self.mtrack.locked),
            'anchored': bool(self.mtrack.anchored),
            'ambiguity': float(self.mtrack.ambiguity),
        }
        if self.mode == 'framework':
            st['framework'] = self.fw.report()
        return st

    def model_xyz(self, view=None) -> np.ndarray:
        """The accumulated model as points, in world space.

        Deliberately not a render: the client has its own camera and can
        project these itself. `view` is accepted and ignored, kept so the
        service's call site does not need to care.
        """
        if not self._started or self.vol is None:
            return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 3), dtype=np.float32)
        pts, nrm = self.vol.points()
        return to_gl(pts), to_gl(nrm)

    def render(self, c2w, width, height, fov_y):
        """Shaded view of the model from the viewer's camera."""
        if not self._started or self.vol is None:
            return None
        return self.vol.render(np.asarray(c2w, dtype=np.float32).reshape(4, 4),
                               width, height, fov_y)

    def full_xyz(self, max_points=250_000) -> np.ndarray:
        if self.vol is None:
            return np.zeros((0, 3), dtype=np.float32)
        return to_gl(self.vol.points(max_points))

    def mesh(self):
        """Marching cubes over the TSDF.

        Returns (verts, faces, normals, colours). Colours are sampled from the
        volume before the GL flip, since the volume is in CV space.
        """
        if self.vol is None:
            raise RuntimeError('No scan volume. Pick a smaller volume or a '
                               'coarser voxel size and try again.')
        v, f, n = self.vol.mesh()
        c = self.vol.sample_colour(v)
        return to_gl(v), f, to_gl(n), c

    def mesh_plane(self):
        """The turntable plane in GL space, for cutting the mesh.

        Returned as (normal, offset) with the same sign convention as the CV
        version: positive is above the platter.
        """
        if not self.turntable:
            return None
        n = np.asarray(self.turntable['up'], dtype=np.float32)
        c = np.asarray(self.turntable['centre'], dtype=np.float32)
        ngl = to_gl(n.reshape(1, 3))[0]
        cgl = to_gl(c.reshape(1, 3))[0]
        return [float(v) for v in ngl], float(-(ngl @ cgl))

    @property
    def has_colour(self):
        return self.vol.colour is not None
