# Ghostlight for coding agents

Context for AI assistants working on this repository. Written to save you the exploration pass. Everything here was read off the source rather than remembered, but the code is the authority and this file will drift.

## What this is

A 3D scanner for the Xbox Kinect v1. A Python service owns the sensor and the GPU, a Vue 3 page in the browser is the interface, and they talk over a local WebSocket carrying both JSON and raw binary buffers.

There is no database, no server framework, no state management library, no test suite and no CI. The maths is written against NumPy and CuPy directly rather than pulled from libraries.

## Running it

```bash
npm install
pip install -r server/requirements.txt
pip install cupy-cuda12x          # matched to your CUDA major version

npm run build                     # required before the service can serve the page
npm run server                    # python ghostlight.py --no-open, serves on 8787
npm run dev                       # Vite dev server on 5180, for front-end work
```

CUDA is required. There is no CPU fallback for fusion, ICP or raycasting, so a machine without an NVIDIA card cannot run the service at all. Front-end work is still possible: with no service reachable the page falls back to mock state.

A Kinect v1 and the Kinect for Windows SDK 1.8 are required for anything involving real frames. See [INSTALL.md](INSTALL.md).

## Layout

### `server/`

| File | Responsibility |
|---|---|
| `service.py` | The process. WebSocket server, command dispatch, four asyncio loops, static file serving. Start here. |
| `tracker.py` | Frame-to-model ICP, pose history, failure classification, volume lifecycle. Owns a `gputsdf.Volume`. |
| `gputsdf.py` | The CUDA kernels as CuPy `RawKernel` source, the `Volume` class, VRAM planning, marching cubes entry point. |
| `markers.py` | Optical marker detection and the per-frame marker pose solve. |
| `framework.py` | The global marker survey and solve, run once before a marker-tracked scan. |
| `geometry.py` | Back-projection, RANSAC plane fitting, turntable circle and axis recovery. |
| `mesher.py` | Open3D mesh container, the Refine operation stack, and the four file writers. |
| `bundle.py` | The on-disk recording format. `Writer`, `Reader`, listing and pruning. |
| `backend.py` | Chooses a capture backend and hides which one is active. |
| `kinect10.py` | Kinect for Windows SDK 1.8 through `ctypes`. The only supported backend. |
| `openni2.py` | OpenNI2 through `ctypes`. Present, untested, unsupported, no runtime shipped. |
| `webapp.py` | Serves `dist/` from the same port as the WebSocket, via the `process_request` hook. |
| `usbdiag.py` | Windows USB diagnosis, to tell a missing sensor apart from a wrongly bound one. |

### `src/`

`App.vue` holds the stage machine and the two HUD columns. `stages/` has one component per workflow stage, `components/Viewport.vue` is the three.js scene, and `composables/useSensor.js` is the entire client side of the protocol.

`composables/useSession.js` is left over from the mock and is not the real state. Do not add to it.

## The service

Four loops run under one `asyncio.gather` in `main()`:

| Loop | Job |
|---|---|
| `capture_loop` | Reads the sensor as fast as it will go. Does no network work, deliberately, so slow clients cannot starve the tracker. |
| `stream_loop` | Sends the depth and colour preview to clients, decimated to `DEPTH_STREAM_HZ`. |
| `track_loop` | Registers the newest frame, writes it to the bundle first. |
| `model_loop` | Pushes the model point cloud out at `MODEL_PUSH_SECONDS`. |

Because they share one `gather`, an exception escaping any of them ends the process and disconnects every client. Loops that can fail should catch their own failures.

## The wire protocol

`useSensor.js` is the only client-side implementation, `service.py` the only server-side one. Change both together.

Client to server is JSON, one `type` per message:

```
volume  reset  record  mode  tilt  rescan  fuse  bundles  bundle_delete
reconstruct  refine  export  framework  turntable  map_colour  view
```

Server to client is JSON status broadcasts plus binary frames, each prefixed with a little-endian magic:

| Magic | Bytes | Payload |
|---|---|---|
| `GLDT` `0x474C4454` | `<IHHI` | Depth frame, `w`, `h`, index, then `w*h` uint16 millimetres |
| `GLRG` `0x474C5247` | `<IHH` | Colour preview, `w`, `h`, then BGRA bytes |
| `GLMD` `0x474C4D44` | `<II` | Model cloud, count, then positions f32 and normals f32 |
| `GLMS` `0x474C4D53` | `<III` | Mesh, vertex and face counts, then positions, normals, colours, indices |

`GLRV` is defined but unused. A server-side raycast preview was tried and reverted, because the only place to put the returned image client-side was `scene.background`, which three.js draws in screen space.

Status is a heartbeat, rate limited to `STATUS_HZ`. Anything the client cannot infer for itself, such as a volume rebuild, must broadcast with `force=True` or it can be swallowed.

## Coordinate conventions

The volume, the kernels and the tracker all work in CV convention: X right, Y down, Z forward, matching the depth camera.

`tracker.to_gl()` converts to GL convention on the way out, by negating Y and Z. That is a rotation about X, so it preserves handedness and winding by design. It is applied once, at that boundary, so exactly one convention goes over the wire. The client back-projects the live depth frame itself and must match, in `Viewport.vue`.

The Kinect SDK hands back frames with the column order reversed relative to a standard pinhole camera, so `kinect10.py` reverses them as they leave the SDK, in `_unmirror`. Do not push that correction into the projection maths: it would have to be right in both integration kernels, the ICP kernel, the marker detector and the client, and one miss puts half the pipeline in a different world to the other half.

## Things that will bite you

**The volume is released before the new one is allocated.** `Tracker.reconfigure` does this on purpose, so the budget is not measured against memory the current volume is about to give back. The consequence is that a failed rebuild can leave `tracker.vol` as `None`, and every reader has to cope.

**VRAM is planned, not attempted.** `gputsdf.plan()` refuses before allocating. Overcommitting the device does not raise, it corrupts, and the first kernel to touch past the end dies with `CUDA_ERROR_ILLEGAL_ADDRESS` and takes the process with it.

**Marching cubes runs on the host.** The volume has to come back off the card, so a grid that fits comfortably in VRAM can still exhaust system RAM. `Volume.mesh()` streams it back a slab at a time for that reason.

**Re-fusing is two messages,** a volume rebuild and then a fuse. The rebuild can be refused. The fuse checks that the tracker is at the size the client asked for before running.

**Frames are re-registered on fusion, not replayed.** Poses are not stored in the bundle. Fusing the same recording twice at the same voxel size will not necessarily give an identical result.

**Nothing Node runs at runtime.** The Python service serves `dist/`. A front-end change is invisible to `npm run server` until `npm run build`.

## Tunable constants

Most were measured rather than chosen, and the reasoning is in the comment beside each one. Read that before changing them.

| Constant | Where | What it is |
|---|---|---|
| `COND_FLOOR` | `tracker.py` | ICP normal-equation conditioning below which the pose is unconstrained |
| `FIT_GOOD`, `FIT_WEAK` | `tracker.py` | ICP fitness bands |
| `MAX_STEP_M`, `MAX_STEP_RAD` | `tracker.py` | Per-frame motion limits |
| `MAX_STEP_CATCHUP` | `tracker.py` | Frames of slack after a rejection, so one drop does not reject everything after it |
| `VRAM_SHARE` | `gputsdf.py` | Fraction of free VRAM a volume may claim |
| `BYTES_GEOM`, `BYTES_COLOUR` | `gputsdf.py` | Bytes per voxel, with and without the colour channel |
| `ACCEPT_M`, `AMBIG_RATIO`, `AMBIG_LAYOUT` | `markers.py` | Marker bootstrap acceptance and rival-hypothesis rejection |
| `LOCAL_WIN` | `markers.py` | Window for the local contrast threshold |

## House style

Comments explain why, not what. Several modules document approaches that were tried and removed, and that history is worth keeping. If you replace something, say what it replaced and why the old way failed.

Prose in the repository and in the interface does not use em-dashes.

Do not add attribution for AI assistance to commits, code comments or documentation.

## Where the known bugs are

The README lists what does not work. The two front-end ones are self-contained and reproduce without a Kinect: the bounding box preset buttons in `stages/Prepare.vue` are decoupled from the sliders, and the drawn box in `components/Viewport.vue` trails what the panel says. Turntable plane fitting in `geometry.py` and the scan volume positioning that depends on it are both unreliable.
