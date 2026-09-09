# Ghostlight

Open-source 3D scanning for the original Xbox Kinect, running on modern Nvidia GPUs and Windows.

<p align="center">
  <img src="public/brand/logo.svg" alt="Ghostlight" width="520">
</p>

Ghostlight is a GPU TSDF scanner built around the Kinect v1. It records the raw depth and colour frames, reconstructs them separately, provides a browser-based workflow for cleanup, and exports the result as a standard 3D mesh.

---

<p align="center">
  <a href="https://buymeacoffee.com/domtursini">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95%EF%B8%8F&slug=domtursini&button_colour=30BFFF&font_colour=000000&font_family=Poppins&outline_colour=000000&coffee_colour=FFDD00" alt="Buy me a coffee" />
  </a>
</p>

---

## Why this exists

The Kinect v1 hardware still works, but most of the software around it has not aged well.

Skanect was one of the better options for Kinect scanning, but it has been discontinued. Its CUDA kernels were compiled for `sm_30` through `sm_75` without embedded PTX, which prevents them from running on newer GPU architectures and results in `cudaErrorNoKernelImageForDevice`.

The old OpenNI and NiTE stacks have similar problems on current systems, particularly on Windows so the decision was made to use the `Microsoft Kinect for Windows SDK 1.8` .

The Kinect itself provides 640x480 depth at 30 Hz with a registered colour stream, and used units are easy to find.

Ghostlight replaces the old software stack while keeping the original sensor.

## What it does

The workflow is split into six stages:

| Stage | What happens |
|---|---|
| **Prepare** | Set the scan volume and choose geometry or marker tracking |
| **Record** | Capture depth frames, build a live TSDF preview, and save the raw frames |
| **Fuse** | Rebuild the recording at the voxel size you want |
| **Reconstruct** | Run marching cubes over the TSDF |
| **Refine** | Crop, remove the turntable, remove loose parts, smooth and simplify |
| **Export** | Export STL, PLY, OBJ or GLB |

### Record first, fuse later

Ghostlight does not permanently lock the reconstruction resolution when you start scanning.

The raw frames are written to disk during capture. You can fuse the same recording at 3 mm, inspect it, then run it again at 1 mm without rescanning the object if your VRAM permits.

Frames are re-registered during each fusion pass rather than replayed against the original poses. A finer reconstruction can therefore also improve tracking because ICP has a sharper model to register against.

The saved recording is also useful when debugging failed scans.

## How it works

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

### Sensor backends

Capture sits behind `server/backend.py`, so nothing above it needs to know which backend is active.

Only the Kinect SDK 1.8 backend is supported. Microsoft's driver owns the Kinect device on Windows, which is why OpenNI2 is not used there.

An OpenNI2 backend is present in the tree and is selected when the SDK is unavailable, but it has never been run on real hardware and is not supported. Ghostlight has only been developed and tested on Windows.

### Why TSDF instead of accumulating point clouds

An earlier version of Ghostlight registered each frame against an accumulated point cloud.

That works, but every frame adds another noisy copy of the same surface. Registration error causes surfaces to become thicker as more frames are added.

A TSDF stores a weighted signed distance for each voxel. Repeated observations of the same surface are averaged into the volume instead of being stacked on top of each other.

### Tracking failure detection

A low tracking score does not always mean the same thing.

Ghostlight distinguishes between cases such as:

- nothing being inside the scan volume
- geometry that cannot constrain the camera pose
- movement that is too fast to track

It also detects degenerate geometry such as a flat wall or a smooth object viewed from one direction.

These scenes can report good ICP fitness while still allowing the estimated pose to slide along an unconstrained axis. Ghostlight detects this using the conditioning of the ICP normal equations.

This is particularly relevant for turntable scanning, where an unnoticed tracking ambiguity can smear the subject around the rotation axis.

### Marker tracking

Marker tracking uses a separate survey pass before geometry scanning begins.

Marker positions are solved globally and then locked for the scan. This avoids adding markers incrementally while also accumulating their initial positioning error.

The solve alternates between:

1. solving frame poses against fixed marker positions
2. solving marker positions against fixed frame poses

Frame poses use a Kabsch fit and marker positions use the mean of their observations.

## Project status

Ghostlight is functional, but still under active development.

### Working

- Kinect v1 capture through SDK 1.8 on Windows
- 640x480 depth and colour at 30 Hz on real hardware
- GPU TSDF fusion
- Frame-to-model ICP
- Marching cubes for the final surface (CPU, and the slowest step)
- STL, PLY, OBJ and GLB export
- Raw recording and later re-fusion
- Re-fusion at different voxel sizes
- Turntable plane fitting and removal
- Scan volume positioning above the turntable
- Tracking failure classification
- VRAM estimation and allocation limits
- Marker detection with local contrast thresholding
- Marker physical-size filtering
- Marker plane constraints

### Known gaps

#### Turntable axis tracking

Turntable axis tracking is not connected to the main tracking pipeline yet.

`geometry.axis_from_poses` can already recover the axis from a sequence of poses. Constraining tracking to rotation around that axis should remove a large source of turntable drift.

#### Marker tracking needs more hardware testing

Marker detection works against recorded Kinect frames.

The complete survey, global solve and locked-framework scan pipeline has only been demonstrated with synthetic data so far.

#### Marker viewing angle

Small markers become difficult to detect at shallow viewing angles.

Testing with 10 mm markers showed that around 40 degrees of elevation is needed for reliable detection. The UI does not currently warn about this.

#### No repository test suite yet

Synthetic scene harnesses exist for running the pipeline without Kinect hardware, but they have not yet been committed or connected to CI.

#### NVIDIA only

There is currently no CPU or OpenCL fusion backend.

#### Open3D cleanup runs on CPU

The current Open3D wheel performs the Refine operations on the CPU.

#### Colour meshing does not work

The volume can average colour into its voxels and the mesh writers can carry
vertex colours, but the result is too muddy to be worth anything, so the option
has been taken out of the interface. Exports are geometry only.

Colour is still useful for tracking. It is not useful for the final surface.

#### Mock and real front-end state are separate

Mock state still exists alongside the real application state in:

```text
src/composables/useSession.js
```

These paths should eventually be merged.

#### Tracking thresholds need more real-world validation

The current conditioning and movement thresholds were calibrated against synthetic scenes with known ground truth.

They behave correctly on the real data tested so far, but need a larger hardware test set.

#### Kinect v2 and RealSense are not supported

The backend interface was written to allow other sensors, but support will not be claimed until those backends have been tested on real hardware.

#### No packaging yet

The current setup still requires Python, CUDA, Node and a terminal.

## Requirements

### Hardware

- Kinect v1, model 1414 or 1473
- Kinect mains power adapter
- NVIDIA GPU with CUDA
- Minimum 8 GB VRAM for a typical object scan

CUDA is currently required. Fusion, raycasting and ICP are CuPy kernels with no CPU fallback yet. Marching cubes runs on the CPU through scikit-image.

Ghostlight was developed on an RTX 3090 using `sm_86` and CUDA 12.8.

The app estimates VRAM usage before allocating the scan volume and rejects configurations that will not fit.

### Software

- Windows
- Python 3.9 or newer
- Node 18 or newer
- Kinect for Windows SDK 1.8

## Install

```bash
git clone https://github.com/<your-username>/ghostlight.git
cd ghostlight

npm install
pip install -r server/requirements.txt
```

Install the CuPy package that matches your CUDA version:

```bash
pip install cupy-cuda12x
```

For CUDA 11:

```bash
pip install cupy-cuda11x
```

CuPy is not included in `requirements.txt` because its wheel depends on the installed CUDA major version.

## Running it

Ghostlight uses two processes.

Start the front end:

```bash
npm run dev
```

Start the sensor service:

```bash
npm run server
```

Then open:

```text
http://localhost:5180
```

The service checks for the Kinect every two seconds, so the sensor can be connected while the application is already running.

Without the sensor service, the front end falls back to mock state. This allows UI development without Kinect hardware connected.

Recordings are stored in:

```text
~/Documents/Ghostlight/bundles
```

Expect roughly 250 MB per minute when recording colour. Recordings are not deleted automatically.

## Kinect v1 on Windows

Install **Kinect for Windows SDK 1.8 before plugging in the sensor**.

Microsoft download:

https://www.microsoft.com/en-us/download/details.aspx?id=40278

Use SDK **1.8**, not 2.0. Kinect SDK 2.0 targets the Kinect v2.

If Kinect drivers are already installed and the v1 is not enumerating correctly, uninstall the existing Kinect devices and reboot before installing SDK 1.8.

With the SDK installed, connect the Kinect mains adapter and then USB.

Device Manager should eventually show:

```text
Kinect for Windows Camera
Kinect for Windows Device
Kinect for Windows Audio Array Control
Kinect for Windows Security Control
```

The camera entry is the important one.

### Do not use Zadig on Windows

Do not rebind the Kinect camera to libusbK or WinUSB.

Ghostlight uses Microsoft's Kinect SDK on Windows. The SDK requires Microsoft's own driver, and rebinding the camera prevents `NuiInitialize` from opening the device.

If you have already rebound it, uninstall the camera from Device Manager and scan for hardware changes to restore the Microsoft driver.

Ghostlight detects this condition separately from a missing sensor.

## Contributing

Contributions are welcome.

Some useful areas to work on:

- **Turntable axis tracking**  
  Connect `geometry.axis_from_poses` to the tracker and constrain turntable scans to the recovered axis.

- **CPU or OpenCL TSDF backend**  
  The CUDA kernels in `server/gputsdf.py` are relatively self-contained and provide a starting point for another backend.

- **Synthetic tests and CI**  
  Move the existing synthetic scene harnesses into the repository and make the pipeline testable without a Kinect.

- **Additional sensor backends**  
  Kinect v2 and RealSense can be implemented behind `server/backend.py`.

- **Better colour reconstruction**  
  Sample vertex colour from suitable recorded keyframes instead of averaging colour into the TSDF voxels.

If you fix a problem, include a short note describing what you observed and why the change works.

Several of the less obvious implementation decisions are documented in the module docstrings, including approaches that were tested and later removed.

## License
Ghostlight is licensed under the GNU General Public License v3.0 (GPLv3).
You are free to use, modify and redistribute the project under the terms of the GPLv3. If you distribute modified versions, the corresponding source code must remain available under the same license.
