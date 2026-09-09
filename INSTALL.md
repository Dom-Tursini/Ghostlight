# Installing Ghostlight

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

## Set up the Kinect first

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

### Do not use Zadig

Do not rebind the Kinect camera to libusbK or WinUSB.

Ghostlight uses Microsoft's Kinect SDK, which requires Microsoft's own driver. Rebinding the camera prevents `NuiInitialize` from opening the device.

If you have already rebound it, uninstall the camera from Device Manager and scan for hardware changes to restore the Microsoft driver.

Ghostlight detects this condition separately from a missing sensor.

## Install

```bash
git clone https://github.com/Dom-Tursini/Ghostlight.git
cd Ghostlight

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

## Where recordings go

```text
~/Documents/Ghostlight/bundles
```

Expect roughly 250 MB per minute when recording colour. Recordings are not deleted automatically.
