# Overview

A Home Assistant integration to communicate with Hikvision smart doorbells and NVR via Hik-Connect cloud.

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

### Maintenance notice

> This integration is **no longer actively maintained**, as I do not own a Hikvision device anymore. I will continue to review and merge pull requests from the community and release new versions, but I cannot provide personal support, implement new features or fix bugs myself. Thank you for your understanding and contributions! – @tomasbedrich

### Features
- Unlock a lock connected to Hikvision outdoor station.
- Report call status of an indoor station (idle, ringing, call in progress) + call source (number of building, floor, etc.).
- Arm, arm-silently, or disarm NVR alarm areas (camera groups) via `alarm_control_panel` entities.

Nothing more yet, sorry. :) Visit an [issue tracker] to discuss planned features.

## Alarm Areas

![Alarm Area](img/arm_area.png)

NVR alarm **areas** (groups of cameras) are exposed as Home Assistant `alarm_control_panel` entities.

### What you get

| HA entity | Description |
|-----------|-------------|
| `alarm_control_panel.<area_name>` | One entity per area defined on the NVR |

### Supported states

| HA alarm state | Hik-Connect mode | Description |
|----------------|-----------------|-------------|
| `disarmed` | mode 0 | Area disarmed |
| `armed_away` | mode 1 | Full arm (all cameras monitored) |
| `armed_home` | mode 2 | Silent arm (arm without sound alerts) |

### Supported actions (standard HA alarm services)

```yaml
# Disarm
action: alarm_control_panel.alarm_disarm
target:
  entity_id: alarm_control_panel.gate

# Arm away
action: alarm_control_panel.alarm_arm_away
target:
  entity_id: alarm_control_panel.gate

# Arm silently (home/stay)
action: alarm_control_panel.alarm_arm_home
target:
  entity_id: alarm_control_panel.gate
```

### Area management actions

Next actions are registered under the `hikconnect` domain to create, update, and delete areas:

```yaml
# Create a new area (cameras = resource_ids from entity attributes)
action: hikconnect.create_area
data:
  device_serial: "FK12345678"
  group_name: "Gate"
  resource_ids:
    - "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

# Update an existing area's cameras (deletes + recreates with new cameras)
action: hikconnect.update_area
data:
  device_serial: "FK12345678"
  group_id: 12345678          # from entity attribute 'group_id'
  group_name: "Gate"      # keep same name or rename
  resource_ids:
    - "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    - "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

# Delete an area
action: hikconnect.delete_area
data:
  device_serial: "FK12345678"
  group_id: 12345678

# List areas (returns data)
action: hikconnect.list_areas
data:
  device_serial: "FK12345678"

# Get area details (returns data)
action: hikconnect.get_area_details
data:
  device_serial: "FK12345678"
  group_id: 12345678

# List cameras (returns data)
action: hikconnect.list_cameras
data:
  device_serial: "FK12345678"
```

> **Area constraints (Hik-Connect API limitation):**
> - A camera can only belong to **one area** at a time.
> - An area must contain **at least one camera**.
> - `update_area` works by deleting and recreating the area — the `group_id` will change.

### Viewing cameras in an area

Each `alarm_control_panel` entity exposes its member cameras in `extra_state_attributes`:

| Attribute | Description |
|-----------|-------------|
| `group_id` | Numeric area ID (use for update/delete services) |
| `group_name` | Area name |
| `mode` | Raw API mode (0/1/2) |
| `device_serial` | Device serial |
| `cameras` | List of `{id, name}` dicts for each camera in the area |
| `camera_count` | Number of cameras in the area |

You can read these in **Developer Tools → States**, or use them in automations/templates:

```yaml
# Example: template sensor showing camera names in an area
{{ state_attr('alarm_control_panel.gate', 'cameras') | map(attribute='name') | join(', ') }}
```

To find a camera's `resource_id` (needed for `create_area` / `update_area`), check the `cameras` attribute of an existing area entity, or call the `hikconnect.list_cameras` service from Developer Tools.

### Configurable scan interval

The poll interval can be changed without restarting Home Assistant:

1. Go to **Settings → Devices & Services**.
2. Find _Hik-Connect_ and click **Configure**.
3. Set the **Scan interval (minutes)** (default: 30, range: 5–1440).
4. Save — the integration reloads automatically.

### Limitations

- Area status is polled on the coordinator schedule (default 30 min). It is **not** updated in real-time.
- After arm/disarm the integration immediately triggers a refresh, so state should update within seconds.
- `update_area` changes the `group_id` (delete + recreate). The entity re-appears after the next coordinator refresh. 
- Not all NVR firmware versions expose the area API. If no area entities appear, your device may not support area management over the Hik-Connect cloud.
- **Recommendation:** Because of how area creation and updates work (requiring entity recreation and polling refreshes), it is highly recommended to manage your areas in the official Hik-Connect application, unless you specifically need to automate their creation/modification via Home Assistant.




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
