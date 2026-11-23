# Hik-Connect for Home Assistant

A Home Assistant integration to communicate with Hikvision smart doorbells and NVR via Hik-Connect cloud.

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

> This integration is **no longer actively maintained**, as I do not own a Hikvision device anymore. I will continue to review and merge pull requests from the community and release new versions, but I cannot provide personal support, implement new features or fix bugs myself. Thank you for your understanding and contributions! – @tomasbedrich

## Features

- Still image and live video of each camera channel via `camera` entities.
- Unlock a lock connected to Hikvision outdoor station.
- Report call status of an indoor station (idle, ringing, call in progress) + call source (number of building, floor, etc.).
- Answer, cancel, or hang up a call on an indoor station via `button` entities.
- Arm, arm-silently, or disarm NVR alarm areas (camera groups) via `alarm_control_panel` entities.
- Diagnostic entities per device (online, update available, local IP, WAN IP, WiFi signal), disabled by default.

Tested devices are listed in [COMPATIBILITY.md](COMPATIBILITY.md).

### Cloud only

The scope of this project is to mirror functionality available in [Hik-Connect mobile application] - nothing more.
If you have direct LAN access to your Hikvision device, you can usually get more functions, faster responses and more stability by connecting to it locally. Please see [forum thread about LAN based integration] for more info.

The target audience of this integration is people living in block of flats where other Hikvision devices (outdoor stations,
recorders) are managed by someone else, and you don't have physical (admin) access to any of these.
If you are satisfied with the limited functionality and Hik-Connect cloud dependency, feel free to use it even for LAN connected devices.

## Installation

### HACS
This installation method is **preferred** since it allows automatic updates in the future.

Install by searching for _Hik-Connect_ integration in [HACS].

### Manual
1. [Download this integration].
2. Copy the folder `custom_components/hikconnect` from the zip to your config directory.
3. Restart Home Assistant.

Then add the integration in **Settings → Devices & Services** and log in with your Hik-Connect account.

## Configuration

Options are in **Settings → Devices & Services → Hik-Connect → Configure**. The integration reloads after saving.

| Option | Description |
|--------|-------------|
| Scan interval (minutes) | How often device and area state is polled (default: 30, range: 5–1440). |
| Device verification code | Needed only for devices with **Image and Video Encryption** enabled. It is printed on the device label and used for both images and live video. One code is used for all devices of the account. |

## Cameras

Each visible camera channel gets a `camera` entity showing a still image. The image is captured by the device on request through the Hik-Connect cloud, so it takes a few seconds and is refreshed at most once per minute. Devices which cannot capture (some intercoms) get the image from the live video instead.

**Live video** is relayed from the Hik-Connect cloud (same as in the mobile app) and played by Home Assistant's built-in `stream` integration (part of `default_config`), no add-on needed. The cloud stream only runs while somebody watches. The cloud cuts streams after about 30 minutes, the integration reconnects automatically. The integration serves the video over RTSP on a random port of `127.0.0.1` to Home Assistant's built-in go2rtc and `stream` (RTSP is needed for two-way audio), so a go2rtc running elsewhere cannot use it.

**Two-way audio** (experimental, Home Assistant 2026.11+): devices which support talking in the Hik-Connect app get the talk button on the live view. Enable **Settings → System → Labs → Two-way audio** and open Home Assistant over HTTPS (browsers allow the microphone only there). Your voice goes through the Hik-Connect cloud like in the app. Only devices taking G.711 audio work for now.

## Alarm areas

Each NVR alarm area (group of cameras) is an `alarm_control_panel` entity:

| HA alarm state | Description |
|----------------|-------------|
| `disarmed` | Area disarmed |
| `armed_away` | Full arm |
| `armed_home` | Silent arm (without sound alerts) |

Use the standard `alarm_control_panel.alarm_disarm`, `alarm_arm_away` and `alarm_arm_home` actions. State is polled on the scan interval and refreshed right after arm/disarm.

The integration also provides actions to create, update and delete areas. It is recommended to manage areas in the Hik-Connect app instead, see [docs/alarm-areas.md](docs/alarm-areas.md) for details and limitations.

## Reporting problems

Please attach a debug log: Settings -> Devices & services -> Hik-Connect -> Enable debug logging,
reproduce the problem (e.g. open the live view for a minute), then disable debug logging and the log
file is downloaded. It contains no passwords or stream tokens, but please check it before posting.

Feature requests and discussion go to the [issue tracker].

## Links

- [`hikconnect` Python library]
- [forum thread about this integration]
- [forum thread about LAN based integration]


[issue tracker]: https://github.com/tomasbedrich/home-assistant-hikconnect/issues
[Hik-Connect mobile application]: https://www.hik-connect.com/views/qrcode/hc/index.html
[HACS]: https://hacs.xyz/
[Download this integration]: https://github.com/tomasbedrich/home-assistant-hikconnect/archive/master.zip
[`hikconnect` Python library]: https://github.com/tomasbedrich/hikconnect
[forum thread about this integration]: https://community.home-assistant.io/t/hik-connect/342202
[forum thread about LAN based integration]: https://community.home-assistant.io/t/ds-kd8003-ds-kv8113-ds-kv8213-ds-kv6113-ds-kv8413-and-integration-hikvision-hikconnect-video-intercom-doorbell/238535
