# Overview

A Home Assistant integration to communicate with Hikvision smart doorbells via Hik-Connect cloud.

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

### Maintenance notice

> This integration is **no longer actively maintained**, as I do not own a Hikvision device anymore. I will continue to review and merge pull requests from the community and release new versions, but I cannot provide personal support, implement new features or fix bugs myself. Thank you for your understanding and contributions! – @tomasbedrich

### Features
- Unlock a lock connected to Hikvision outdoor station.
- Report call status of an indoor station (idle, ringing, call in progress) + call source (number of building, floor, etc.).

Nothing more yet, sorry. :) Visit an [issue tracker] to discuss planned features.

### Warning
If you have direct LAN access to your Hikvision device, **you may NOT want to use this integration.**

Why? The scope of this project is to mirror functionality available in [Hik-Connect mobile application] - nothing more.
You can usually get more functions, faster responses and more stability by connecting to your device locally, if possible.
Please see [forum thread about LAN based integration] for more info.

The target audience of this integration is people living in block of flats where other Hikvision devices (outdoor stations,
recorders) are managed by someone else, and you don't have physical (admin) access to any of these.

To be clear - if you are satisfied with this integration with regard to limited functionality and Hik-Connect cloud dependency,
feel free to use it even for LAN connected devices.

## Installation

### HACS
This installation method is **preferred** since it allows automatic updates in the future.

Install by searching for _Hik-Connect_ integration in [HACS].

### Manual
1. [Download this integration].
2. Copy the folder `custom_components/hikconnect` from the zip to your config directory.
3. Restart Home Assistant.


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
