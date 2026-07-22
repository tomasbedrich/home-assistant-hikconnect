"""
Hik-Connect alarm control panel platform.

Each NVR area (group) is exposed as a Home Assistant alarm_control_panel entity.
Users can arm (away), arm silently (home/stay), or disarm from the HA UI or
automations using the standard alarm_control_panel services.

Area data is fetched by the shared coordinator (GET /v3/devices/group/{serial}/list).
Arm/disarm actions call POST /v3/devices/group/{serial}/switchDefenceMode.

Mode mapping
------------
  API mode 0 → STATE_ALARM_DISARMED
  API mode 1 → STATE_ALARM_ARMED_AWAY
  API mode 2 → STATE_ALARM_ARMED_HOME  (arm-silent / stay)
"""

import logging

from hikconnect.api import HikConnect
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
)
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Map API mode integer to HA alarm state
_MODE_TO_STATE = {
    0: AlarmControlPanelState.DISARMED,
    1: AlarmControlPanelState.ARMED_AWAY,
    2: AlarmControlPanelState.ARMED_HOME,
}

# Map HA alarm state back to API mode (for informational logging)
_STATE_TO_MODE = {v: k for k, v in _MODE_TO_STATE.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]

    new_entities = []
    for device_info in coordinator.data:
        for area_info in device_info.get("areas", []):
            new_entities.append(
                HikConnectAlarmArea(api, coordinator, device_info, area_info)
            )

    if new_entities:
        async_add_entities(new_entities)


class HikConnectAlarmArea(CoordinatorEntity, AlarmControlPanelEntity):
    """
    Represents a single NVR alarm area (camera group) in Hik-Connect.

    The entity is backed by the shared coordinator so its state updates
    automatically on every coordinator refresh (default every 30 minutes,
    configurable via integration options).

    Supported actions
    -----------------
    - arm_away    → POST switchDefenceMode mode=1
    - arm_home    → POST switchDefenceMode mode=2  (arm-silent)
    - disarm      → POST switchDefenceMode mode=0
    """

    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
    )
    _attr_code_arm_required = False

    def __init__(
        self,
        api: HikConnect,
        coordinator: DataUpdateCoordinator,
        device_info: dict,
        area_info: dict,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._device_info = device_info
        self._area_info = area_info

    # ------------------------------------------------------------------
    # Helpers to retrieve up-to-date area data from the coordinator
    # ------------------------------------------------------------------

    def _current_area_info(self) -> dict:
        """Return the latest area dict from coordinator data."""
        group_id = self._area_info.get("group_id")
        for device in self.coordinator.data:
            if device["id"] == self._device_info["id"]:
                for area in device.get("areas", []):
                    if area.get("group_id") == group_id:
                        return area
        # Fall back to cached value if not found in coordinator
        return self._area_info

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        area = self._current_area_info()
        return area.get("group_name", f"Area {area.get('group_id', '?')}")

    @property
    def unique_id(self) -> str:
        group_id = self._area_info.get("group_id", "unknown")
        return f"{DOMAIN}-{self._device_info['id']}-area-{group_id}"

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        area = self._current_area_info()
        mode = area.get("mode")
        if mode is None:
            return None
        return _MODE_TO_STATE.get(int(mode))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self._device_info["id"])},
        }

    @property
    def extra_state_attributes(self) -> dict:
        """Expose raw area fields for diagnostics and camera management."""
        area = self._current_area_info()

        # 'resources' is populated by the coordinator from the area-detail endpoint.
        # Confirmed response shape (GET /v3/devices/group/{serial}/{groupId}):
        #   {"list": [{"groupId": …, "groupDevSerial": …, "memberId": "<cameraId>"}, …]}
        resources = area.get("resources", [])

        # Build a lookup from camera id → name using the device's camera list
        # (already fetched by the coordinator, no extra API call needed).
        camera_name_by_id: dict[str, str] = {
            c["id"]: c["name"]
            for c in self._device_info.get("cameras", [])
            if "id" in c and "name" in c
        }

        cameras = []
        for r in resources:
            if isinstance(r, dict):
                # Confirmed field name is "memberId"; keep fallbacks for other firmware variants
                cam_id = (
                    r.get("member_id")
                    or r.get("memberId")
                    or r.get("resourceId")
                    or r.get("cameraId")
                    or r.get("id", "")
                )
                cam_name = (
                    camera_name_by_id.get(cam_id, "")
                    or r.get("cameraName")
                    or r.get("name")
                    or r.get("resourceName", "")
                )
                cameras.append({"id": cam_id, "name": cam_name})
            elif isinstance(r, str):
                cameras.append({"id": r, "name": camera_name_by_id.get(r, "")})

        return {
            "group_id": area.get("group_id"),
            "group_name": area.get("group_name"),
            "mode": area.get("mode"),
            "device_serial": self._device_info.get("serial"),
            "cameras": cameras,
            "camera_count": len(cameras),
        }

    @property
    def icon(self) -> str:
        state = self.alarm_state
        if state == AlarmControlPanelState.ARMED_AWAY:
            return "mdi:shield-lock"
        if state == AlarmControlPanelState.ARMED_HOME:
            return "mdi:shield-half-full"
        if state == AlarmControlPanelState.DISARMED:
            return "mdi:shield-off"
        return "mdi:shield-alert"

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_alarm_disarm(self, code=None) -> None:
        """Disarm the alarm area (mode 0)."""
        group_id = self._current_area_info().get("group_id")
        _LOGGER.info(
            "Disarming area groupId=%s on device '%s'",
            group_id, self._device_info["serial"],
        )
        await self._api.disarm_area(self._device_info["serial"], group_id)
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_away(self, code=None) -> None:
        """Arm the alarm area in 'away' mode (mode 1)."""
        group_id = self._current_area_info().get("group_id")
        _LOGGER.info(
            "Arming area groupId=%s on device '%s' (away)",
            group_id, self._device_info["serial"],
        )
        await self._api.arm_area(self._device_info["serial"], group_id)
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_home(self, code=None) -> None:
        """Arm the alarm area silently in 'home/stay' mode (mode 2)."""
        group_id = self._current_area_info().get("group_id")
        _LOGGER.info(
            "Arming area groupId=%s on device '%s' (home/silent)",
            group_id, self._device_info["serial"],
        )
        await self._api.arm_area_silent(self._device_info["serial"], group_id)
        await self.coordinator.async_request_refresh()
