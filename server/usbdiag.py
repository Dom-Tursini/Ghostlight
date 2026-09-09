"""
Windows USB diagnosis for Kinect v1.

OpenNI2 tells you a device is missing. It cannot tell you *why*, which is the
part that costs people an evening. This reads the PnP tree and turns the common
failure modes into a plain sentence.

The three devices a healthy Kinect v1 presents, behind its own internal hub:

    045E:02B0   Xbox NUI Motor    powered from USB 5V
    045E:02AE   Xbox NUI Camera   powered from the 12V rail
    045E:02AD   Xbox NUI Audio    powered from the 12V rail

Motor-present + camera-absent has two known causes and we cannot tell them
apart from here: the sensor may have no 12V, or the USB host stack may be
failing to enumerate the camera. Observed on this project: the same sensor and
cable that showed motor-only on two Windows machines enumerated all three
functions on Linux, on an Intel xHCI controller. So do not assert power.
"""

from __future__ import annotations

import json
import subprocess

PS = r'''
$ErrorActionPreference = 'SilentlyContinue'

$kinect = Get-PnpDevice | Where-Object { $_.InstanceId -match 'VID_045E&PID_02(AE|AD|B0)' -and $_.Present } |
  ForEach-Object {
    $prob = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode').Data
    $svc  = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_Service').Data
    [pscustomobject]@{
      pid     = ($_.InstanceId -replace '.*PID_([0-9A-Fa-f]{4}).*','$1').ToLower()
      name    = $_.FriendlyName
      status  = $_.Status
      problem = $prob
      service = $svc
      id      = $_.InstanceId
    }
  }

$ctrl = Get-PnpDevice -Class USB | Where-Object { $_.FriendlyName -match 'Host Controller' } |
  Select-Object -ExpandProperty FriendlyName

[pscustomobject]@{
  kinect      = @($kinect)
  controllers = @($ctrl)
} | ConvertTo-Json -Depth 5 -Compress
'''


def _query():
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', PS],
            capture_output=True, text=True, timeout=25,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def diagnose():
    """Returns {code, title, detail, action} or None when we cannot tell."""
    data = _query()
    if data is None:
        return None

    devices = data.get('kinect') or []
    if isinstance(devices, dict):
        devices = [devices]
    by_pid = {d.get('pid'): d for d in devices if isinstance(d, dict)}

    controllers = data.get('controllers') or []
    if isinstance(controllers, str):
        controllers = [controllers]
    motor = by_pid.get('02b0')
    camera = by_pid.get('02ae')

    if not devices:
        return {
            'code': 'absent',
            'title': 'Nothing connected',
            'detail': 'No Kinect appears on the USB bus at all.',
            'action': 'Check the USB lead is plugged in.',
        }

    if motor and not camera:
        return {
            'code': 'camera_missing',
            'title': 'Camera not enumerating',
            'detail': ('The motor is on the USB bus but the camera is not. That is either the '
                       'sensor having no 12V, or this machine failing to enumerate the camera.'),
            'action': ('If the green LED on the front of the sensor is lit, it has power and the '
                       'fault is this machine. Try another USB controller, or another computer.'),
        }

    svc = (camera or {}).get('service') or ''
    if camera and svc.lower() in ('libusbk', 'winusb', 'libusb0'):
        return {
            'code': 'wrong_driver',
            'title': 'Camera is bound to a generic USB driver',
            'detail': ('The camera has been rebound to %s, probably with Zadig. '
                       'Ghostlight opens Kinect v1 through the Kinect SDK on Windows, '
                       'which needs the Microsoft KinectCamera driver.' % svc),
            'action': ('In Device Manager, uninstall the camera device, then Scan for '
                       'hardware changes so Windows re-binds KinectCamera.'),
        }

    if camera and camera.get('problem') == 28:
        return {
            'code': 'no_driver',
            'title': 'Camera needs a driver',
            'detail': 'The camera is on the bus but nothing is bound to it.',
            'action': 'Bind libusbK to the Xbox NUI Camera interface.',
        }

    if camera:
        return {
            'code': 'ok',
            'title': 'Sensor ready',
            'detail': 'Camera present and bound to the Microsoft driver.',
            'action': None,
        }

    return None


if __name__ == '__main__':
    print(json.dumps(diagnose(), indent=2))
