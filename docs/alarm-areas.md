# Alarm areas

NVR alarm **areas** (groups of cameras) are exposed as `alarm_control_panel` entities, one per area defined on the NVR. See [COMPATIBILITY.md](../COMPATIBILITY.md) for tested devices.

## Supported states

| HA alarm state | Hik-Connect mode | Description |
|----------------|-----------------|-------------|
| `disarmed` | mode 0 | Area disarmed |
| `armed_away` | mode 1 | Full arm (all cameras monitored) |
| `armed_home` | mode 2 | Silent arm (arm without sound alerts) |

## Supported actions (standard HA alarm services)

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

## Area management actions

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

## Viewing cameras in an area

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

## Limitations

- Area status is polled on the coordinator schedule (default 30 min). It is **not** updated in real-time.
- After arm/disarm the integration immediately triggers a refresh, so state should update within seconds.
- `update_area` changes the `group_id` (delete + recreate). The entity re-appears after the next coordinator refresh.
- Not all NVR firmware versions expose the area API. If no area entities appear, your device may not support area management over the Hik-Connect cloud.
- **Recommendation:** Because of how area creation and updates work (requiring entity recreation and polling refreshes), it is highly recommended to manage your areas in the official Hik-Connect application, unless you specifically need to automate their creation/modification via Home Assistant.

