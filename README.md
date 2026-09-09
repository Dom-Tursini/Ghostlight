<p align="center">
  <img src="public/brand/logo.svg" alt="Ghostlight" width="520">
</p>

---


Open-source 3D scanning for the original Xbox Kinect, running on modern Nvidia GPUs and Windows.

Ghostlight is a GPU TSDF scanner built around the Kinect v1. It records the raw depth and colour frames, reconstructs them separately, provides a browser-based workflow for cleanup, and exports the result as a standard 3D mesh.

---

<p align="center">
  <a href="https://buymeacoffee.com/domtursini">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95%EF%B8%8F&slug=domtursini&button_colour=30BFFF&font_colour=000000&font_family=Poppins&outline_colour=000000&coffee_colour=FFDD00" alt="Buy me a coffee" />
  </a>
</p>

---

[Install and setup](INSTALL.md) · [How it works](TECHNICAL.md) · [Third-party notices](THIRD-PARTY-NOTICES.md)

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

Recording is separate from reconstruction. The raw frames go to disk during capture, so the same take can be fused again at a finer voxel size without rescanning the object.

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

**Turntable axis tracking is not connected.** `geometry.axis_from_poses` can already recover the axis from a sequence of poses. Constraining tracking to rotation around that axis should remove a large source of turntable drift.

**Marker tracking needs more hardware testing.** Marker detection works against recorded Kinect frames, but the complete survey, global solve and locked-framework scan pipeline has only been demonstrated with synthetic data.

**Marker viewing angle.** Small markers become difficult to detect at shallow viewing angles. Testing with 10 mm markers showed that around 40 degrees of elevation is needed for reliable detection. The UI does not warn about this.

**No test suite yet.** Synthetic scene harnesses exist for running the pipeline without Kinect hardware, but they have not been committed or connected to CI.

**NVIDIA only.** There is no CPU or OpenCL fusion backend.

**Open3D cleanup runs on CPU.** The current Open3D wheel performs the Refine operations on the CPU.

**Colour meshing does not work.** The volume can average colour into its voxels and the mesh writers can carry vertex colours, but the result is too muddy to be worth anything, so the option has been taken out of the interface. Exports are geometry only. Colour is still useful for tracking.

**Mock and real front-end state are separate.** Mock state still exists alongside the real application state in `src/composables/useSession.js`. These paths should eventually be merged.

**Tracking thresholds need more real-world validation.** The current conditioning and movement thresholds were calibrated against synthetic scenes with known ground truth. They behave correctly on the real data tested so far, but need a larger hardware test set.

**Kinect v2 and RealSense are not supported.** The backend interface was written to allow other sensors, but support will not be claimed until those backends have been tested on real hardware.

**No packaging yet.** The current setup still requires Python, CUDA, Node and a terminal.

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

## License

Ghostlight is licensed under the GNU General Public License v3.0 (GPLv3).
You are free to use, modify and redistribute the project under the terms of the GPLv3. If you distribute modified versions, the corresponding source code must remain available under the same license.
