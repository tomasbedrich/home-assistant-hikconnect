# Known compatible systems

- [DS-KH6210-L] indoor station + [DS-KD3002-VM] door station + [DS-KV8202-IM] door station + unknown infrastructure in between?
- [DS-KH8301-WT] indoor station (FW 1.5.1) + [DS-KV8102-IM] door station (FW 1.5.1)

[DS-KH6210-L]: https://www.hikvision.com/nl/products/Video-Intercom-Products/IP-Series/Value-Series/DS-KH6210-L/
[DS-KD3002-VM]: https://www.hikvision.com/hk/products/Video-Intercom-Products/IP-Series/Value-Series/DS-KD3002-VM/
[DS-KV8202-IM]: https://www.hikvision.com/hk/products/Video-Intercom-Products/IP-Series/Pro-Series/DS-KV8202-IM/
[DS-KH8301-WT]: https://us.hikvision.com/en/products/more-products/discontinued-products/video-intercom/video-intercom-indoor-station-7-inch-0
[DS-KV8102-IM]: https://www.hikvision.com/hk/products/Video-Intercom-Products/IP-Series/Pro-Series/DS-KV8102-IM/

---

## Alarm Area (NVR Group) Compatibility

Alarm area management uses the native Hik-Connect cloud API (`/v3/devices/group/`), **not** ISAPI SecurityCP.  
This means it works with **NVR/DVR devices** that support camera grouping in the Hik-Connect mobile app.

### Confirmed working

| Device type | Serial pattern | Notes |
|-------------|---------------|-------|
| Hikvision NVR | `FK*` | Confirmed via traffic capture and CLI testing. Area list, arm, arm-silent, and disarm all functional. |

### Known incompatible

| Scenario | Reason |
|----------|--------|
| Alarm panels (AX Pro, AX Hub, DS-PWA…) | These use ISAPI SecurityCP subsystems, **not** NVR groups. Use a dedicated alarm panel integration. |
| NVRs with no areas defined | No `alarm_control_panel` entities will appear — this is expected. Create areas first in the Hik-Connect mobile app or via the `hikconnect.create_area` service. |


