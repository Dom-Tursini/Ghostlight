# Third-party notices

Ghostlight is licensed under the GNU General Public License v3 or later. See LICENSE.

This file lists the third-party software Ghostlight requires. No third-party binaries are distributed with it.

## Required but not distributed

### OpenNI2 and the PS1080 driver

```
Licence      GNU LGPL v3
Files        none
```

`server/openni2.py` is the non-Windows capture backend, and is selected when the Kinect SDK is unavailable. It has never been run on real hardware and is not supported.

No OpenNI2 runtime ships with Ghostlight. On Linux the backend looks for the system `libopenni2-0` package. On Windows it looks in `server/runtime/`, which is empty and gitignored, so supply your own build if you want that path to load at all.

### Microsoft Kinect for Windows SDK 1.8

```
Licence      Microsoft Software License Terms for the SDK
Source       https://www.microsoft.com/en-us/download/details.aspx?id=40278
Files        none
```

Installed by the user. `server/kinect10.py` loads `C:\Windows\System32\Kinect10.dll` through `ctypes` at run time. No part of the SDK is redistributed with Ghostlight, and no SDK header, library or sample code was copied into it.

### NVIDIA CUDA runtime

```
Licence      NVIDIA CUDA Toolkit end user licence agreement
Files        none
```

Supplied by the CuPy wheel the user installs, or by an existing CUDA installation. Not redistributed.

## Python dependencies

Installed by the user from `server/requirements.txt`. Not distributed with the source.

| Component | Version | Licence |
|---|---|---|
| NumPy | 2.0.2 | BSD 3-Clause |
| SciPy | 1.13.1 | BSD 3-Clause |
| scikit-image | 0.24.0 | BSD 3-Clause |
| Open3D | 0.19.0 | MIT |
| websockets | 15.0.1 | BSD 3-Clause |
| psutil | 7.2.2 | BSD 3-Clause |

CuPy is installed separately, as its wheel depends on the installed CUDA major version. CuPy itself is MIT. The `cupy-cuda12x` and `cupy-cuda11x` wheels bundle NVIDIA libraries covered by NVIDIA's own licence terms.

## Front-end dependencies

Compiled into the built application in `dist/`.

| Component | Version | Licence |
|---|---|---|
| Vue | 3.5.42 | MIT |
| three.js | 0.185.1 | MIT |

Build tooling, which is not part of the shipped output:

| Component | Version | Licence |
|---|---|---|
| Vite | 6.4.3 | MIT |
| @vitejs/plugin-vue | 5.2.4 | MIT |

The complete pinned dependency set is in `package-lock.json`.

## Fonts

No fonts are distributed with Ghostlight. The interface names Inter and JetBrains Mono in its font stacks and falls back to the platform's own faces when they are not installed.

## Trademarks

The Ghostlight name, logo and mark in `public/brand` are not covered by the GNU General Public License v3 grant covering the source code.
