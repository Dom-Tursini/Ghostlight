# How Ghostlight works

| Layer | Implementation |
|---|---|
| Capture | Kinect SDK 1.8 through `Kinect10.dll` |
| Volume | Dense GPU TSDF using CuPy kernels in `server/gputsdf.py` |
| Tracking | Frame-to-model point-to-plane ICP |
| Alternative tracking | Optical markers solved before the scan |
| Recording | Raw depth and colour frames written to disk |
| Surface reconstruction | Marching cubes, scikit-image on the CPU |
| Cleanup | Non-destructive Open3D operation stack |
| Front end | Vue 3 |
| Transport | Local WebSocket with raw depth and binary geometry buffers |

Ghostlight has only been developed and tested on Windows.

## Why TSDF instead of accumulating point clouds

An earlier version of Ghostlight registered each frame against an accumulated point cloud.

That works, but every frame adds another noisy copy of the same surface. Registration error causes surfaces to become thicker as more frames are added.

A TSDF stores a weighted signed distance for each voxel. Repeated observations of the same surface are averaged into the volume instead of being stacked on top of each other.

## Record first, fuse later

Ghostlight does not permanently lock the reconstruction resolution when you start scanning.

The raw frames are written to disk during capture. You can fuse the same recording at 3 mm, inspect it, then run it again at 1 mm without rescanning the object if your VRAM permits.

Frames are re-registered during each fusion pass rather than replayed against the original poses. A finer reconstruction can therefore also improve tracking because ICP has a sharper model to register against.

The saved recording is also useful when debugging failed scans.

## Tracking failure detection

A low tracking score does not always mean the same thing.

Ghostlight distinguishes between cases such as:

- nothing being inside the scan volume
- geometry that cannot constrain the camera pose
- movement that is too fast to track

It also detects degenerate geometry such as a flat wall or a smooth object viewed from one direction.

These scenes can report good ICP fitness while still allowing the estimated pose to slide along an unconstrained axis. Ghostlight detects this using the conditioning of the ICP normal equations.

This is particularly relevant for turntable scanning, where an unnoticed tracking ambiguity can smear the subject around the rotation axis.

## Marker tracking

Marker tracking uses a separate survey pass before geometry scanning begins.

Marker positions are solved globally and then locked for the scan. This avoids adding markers incrementally while also accumulating their initial positioning error.

The solve alternates between:

1. solving frame poses against fixed marker positions
2. solving marker positions against fixed frame poses

Frame poses use a Kabsch fit and marker positions use the mean of their observations.

## Reading the source

Several of the less obvious implementation decisions are documented in the module docstrings, including approaches that were tested and later removed.
