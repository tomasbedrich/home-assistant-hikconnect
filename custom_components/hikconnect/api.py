import datetime
import hashlib
import json
import logging
from base64 import urlsafe_b64decode
from contextlib import contextmanager

from aiohttp import ClientSession

from .exceptions import DeviceOffline, LoginError
from .local_stream import has_local_bridge_support

log = logging.getLogger(__name__)


class _HikConnectClient(ClientSession):
    FEATURE_CODE = "deadbeef"

    def __init__(self):
        headers = {
            "clientType": "55",
            "lang": "en-US",
            "featureCode": self.FEATURE_CODE,
        }
        super().__init__(raise_for_status=True, headers=headers)

    def set_session_id(self, session_id):
        self.headers.update({"sessionId": session_id})

    @contextmanager
    def without_session_id(self):
        if "sessionId" not in self.headers:
            yield self
            return

        session_id = self.headers.pop("sessionId")
        try:
            yield self
        finally:
            self.headers["sessionId"] = session_id


class HikConnect:
    BASE_URL = "https://api.hik-connect.com"

    CALL_STATUS_MAPPING = {
        1: "idle",
        2: "ringing",
        3: "call in progress",
    }
    CALL_INFO_MAPPING = {
        "buildingNo": "building_number",
        "floorNo": "floor_number",
        "zoneNo": "zone_number",
        "unitNo": "unit_number",
        "devNo": "device_number",
        "devType": "device_type",
        "lockNum": "lock_number",
    }
    CALLING_FALLBACK_RING_SECONDS = 20

    @staticmethod
    def _build_stream_candidates(device_info: dict, camera_info: dict) -> list[dict]:
        connection = device_info.get("connection", {})
        vtm = camera_info.get("vtm_info", {})
        stream_biz_url = camera_info.get("stream_biz_url")

        if not stream_biz_url:
            return []

        candidates = []
        local_ip = connection.get("localIp")
        local_rtsp_port = connection.get("localRtspPort")
        local_stream_port = connection.get("localStreamPort")
        vtm_domain = vtm.get("domain")
        vtm_external_ip = vtm.get("externalIp")
        vtm_port = vtm.get("port")

        if has_local_bridge_support(
            {
                "local_sdk": {
                    "local_ip": local_ip,
                    "local_cmd_port": connection.get("localCmdPort"),
                    "local_stream_port": local_stream_port,
                }
            }
        ):
            candidates.append(
                {
                    "kind": "local_hik_bridge",
                    "url": f"hikconnect://local-bridge/{camera_info['id']}",
                }
            )

        if local_ip and local_rtsp_port:
            candidates.append(
                {
                    "kind": "local_rtsp",
                    "url": f"rtsp://{local_ip}:{local_rtsp_port}/?{stream_biz_url}",
                }
            )

        if local_ip and local_stream_port:
            candidates.append(
                {
                    "kind": "local_sdk_rtsp_guess",
                    "url": f"rtsp://{local_ip}:{local_stream_port}/?{stream_biz_url}",
                }
            )

        if vtm_domain and vtm_port:
            candidates.append(
                {
                    "kind": "vtm_rtsp_guess",
                    "url": f"rtsp://{vtm_domain}:{vtm_port}/?{stream_biz_url}",
                }
            )
            candidates.append(
                {
                    "kind": "vtm_rtsps_guess",
                    "url": f"rtsps://{vtm_domain}:{vtm_port}/?{stream_biz_url}",
                }
            )

        if vtm_external_ip and vtm_port:
            candidates.append(
                {
                    "kind": "vtm_external_rtsp_guess",
                    "url": f"rtsp://{vtm_external_ip}:{vtm_port}/?{stream_biz_url}",
                }
            )

        return candidates

    @staticmethod
    def build_stream_bootstrap(device_info: dict, camera_info: dict) -> dict:
        connection = device_info.get("connection", {})
        kms = device_info.get("kms", {})
        vtm = camera_info.get("vtm_info", {})

        return {
            "device_id": device_info["id"],
            "device_serial": device_info["serial"],
            "camera_id": camera_info["id"],
            "camera_name": camera_info["name"],
            "channel_number": camera_info["channel_number"],
            "signal_status": camera_info["signal_status"],
            "stream_transport": "hik_sdk",
            "stream_biz_url": camera_info.get("stream_biz_url"),
            "stream_candidates": HikConnect._build_stream_candidates(
                device_info, camera_info
            ),
            "video_level": camera_info.get("video_level"),
            "video_quality_infos": camera_info.get("video_quality_infos", []),
            "local_sdk": {
                "local_ip": connection.get("localIp"),
                "local_cmd_port": connection.get("localCmdPort"),
                "local_stream_port": connection.get("localStreamPort"),
                "local_rtsp_port": connection.get("localRtspPort"),
                "net_type": connection.get("netType"),
                "wan_ip": connection.get("wanIp"),
                "upnp": connection.get("upnp"),
            },
            "vtm": {
                "domain": vtm.get("domain"),
                "external_ip": vtm.get("externalIp"),
                "internal_ip": vtm.get("internalIp"),
                "port": vtm.get("port"),
                "force_stream_type": vtm.get("forceStreamType"),
                "is_backup": vtm.get("isBackup"),
            },
            "kms": {
                "secret_key": kms.get("secretKey"),
                "version": kms.get("version"),
            },
            "p2p": device_info.get("p2p", []),
            "hiddns": device_info.get("hiddns", {}),
            "experimental": True,
        }

    def __init__(self):
        self._refresh_session_id = None
        self.login_valid_until = None
        self._calling_fallback_state = {}
        self.client = _HikConnectClient()

    async def login(self, username: str, password: str):
        data = {
            "account": username,
            "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
        }
        async with self.client.post(
            f"{self.BASE_URL}/v3/users/login/v2", data=data
        ) as res:
            res_json = await res.json()
        log.debug("Got login response '%s'", res_json)

        if res_json["meta"]["code"] in (1013, 1014):
            raise LoginError(
                "Login failed, probably wrong username/password combination."
            )

        if res_json["meta"]["code"] == 1015:
            raise LoginError(
                "CAPTCHA hit, please login using Hik-Connect app and then retry."
            )

        if res_json["meta"]["code"] == 1100:
            new_api_domain = res_json["loginArea"]["apiDomain"]
            self.BASE_URL = f"https://{new_api_domain}"
            log.debug("Switching API domain to '%s'", self.BASE_URL)
            return await self.login(username, password)

        try:
            session_id = res_json["loginSession"]["sessionId"]
            refresh_session_id = res_json["loginSession"]["rfSessionId"]
        except KeyError as err:
            raise LoginError("Unable to parse login session from response.") from err

        self._handle_login_response(session_id, refresh_session_id)
        log.info("Login successful as username '%s'", username)

    async def refresh_login(self):
        data = {
            "refreshSessionId": self._refresh_session_id,
            "featureCode": _HikConnectClient.FEATURE_CODE,
        }
        with self.client.without_session_id() as client:
            async with client.put(
                f"{self.BASE_URL}/v3/apigateway/login", data=data
            ) as res:
                res_json = await res.json()
        log.debug("Got refresh login response '%s'", res_json)

        try:
            session_id = res_json["sessionInfo"]["sessionId"]
            refresh_session_id = res_json["sessionInfo"]["refreshSessionId"]
        except KeyError as err:
            raise LoginError("Unable to parse refresh session from response.") from err

        self._handle_login_response(session_id, refresh_session_id)
        log.info("Login refreshed successfully")

    def _handle_login_response(self, session_id, refresh_session_id):
        self.client.set_session_id(session_id)
        self.login_valid_until = self._decode_jwt_expiration(session_id)
        self._refresh_session_id = refresh_session_id

    def is_refresh_login_needed(self):
        if not self.login_valid_until:
            return True
        return (self.login_valid_until - datetime.datetime.now()) < datetime.timedelta(
            hours=1
        )

    async def _get_devices_page(self, limit: int, offset: int):
        async with self.client.get(
            f"{self.BASE_URL}/v3/userdevices/v1/devices/pagelist"
            f"?groupId=-1&limit={limit}&offset={offset}"
            f"&filter=TIME_PLAN,CONNECTION,SWITCH,STATUS,STATUS_EXT,WIFI,NODISTURB,P2P,KMS,HIDDNS"
        ) as res:
            return await res.json()

    async def get_devices(self):
        limit, offset, has_next_page = 50, 0, True
        while has_next_page:
            res_json = await self._get_devices_page(limit, offset)
            log.debug("Got device list response '%s'", res_json)

            connection_infos = res_json.get("connectionInfos", {})
            kms_infos = res_json.get("kmsInfos", {})
            p2p_infos = res_json.get("p2pInfos", {})
            hiddns_infos = res_json.get("hiddnsInfos", {})

            for device in res_json["deviceInfos"]:
                serial = device["deviceSerial"]
                try:
                    locks_json = json.loads(
                        res_json["statusInfos"][serial]["optionals"]["lockNum"]
                    )
                    locks = {int(key): value for key, value in locks_json.items()}
                except KeyError:
                    locks = {}

                yield {
                    "id": device["fullSerial"],
                    "name": device["name"],
                    "serial": serial,
                    "type": device["deviceType"],
                    "version": device["version"],
                    "locks": locks,
                    "connection": connection_infos.get(serial, {}),
                    "kms": kms_infos.get(serial, {}),
                    "p2p": p2p_infos.get(serial, []),
                    "hiddns": hiddns_infos.get(serial, {}),
                }

            offset += limit
            has_next_page = res_json["page"]["hasNext"]

    async def get_cameras(self, device_serial: str):
        async with self.client.get(
            f"{self.BASE_URL}/v3/userdevices/v1/cameras/info?deviceSerial={device_serial}"
        ) as res:
            res_json = await res.json()
        log.debug("Got camera list response '%s'", res_json)

        for camera in res_json["cameraInfos"]:
            yield {
                "id": camera["cameraId"],
                "name": camera["cameraName"],
                "device_serial": camera["deviceSerial"],
                "channel_number": camera["channelNo"],
                "signal_status": camera["deviceChannelInfo"]["signalStatus"],
                "is_shown": camera["isShow"],
                "cover_url": camera.get("cameraCover"),
                "stream_biz_url": camera.get("streamBizUrl"),
                "video_level": camera.get("videoLevel"),
                "video_quality_infos": camera.get("videoQualityInfos", []),
                "vtm_info": camera.get("vtmInfo", {}),
            }

    async def unlock(self, device_serial: str, channel_number: int, lock_index: int = 0):
        async with self.client.put(
            f"{self.BASE_URL}/v3/devconfig/v1/call/{device_serial}/{channel_number}/remote/unlock?srcId=1&lockId={lock_index}&userType=0"
        ) as res:
            await res.json()

    async def get_call_status(self, device_serial: str):
        try:
            return await self._get_call_status_legacy(device_serial)
        except DeviceOffline:
            raise
        except Exception as err:
            log.debug(
                "Legacy call status endpoint failed for %s, using calling fallback: %s",
                device_serial,
                err,
            )
            return await self._get_call_status_from_calling_api(device_serial)

    async def _get_call_status_legacy(self, device_serial: str):
        async with self.client.get(
            f"{self.BASE_URL}/v3/devconfig/v1/call/{device_serial}/status"
        ) as res:
            res_json = await res.json()
        if res_json["meta"]["code"] == 2003:
            raise DeviceOffline()

        data = json.loads(res_json["data"])
        status = self.CALL_STATUS_MAPPING.get(data["callStatus"], "unknown")

        info = {}
        for in_key, out_key in self.CALL_INFO_MAPPING.items():
            try:
                info[out_key] = data["callerInfo"][in_key]
            except KeyError:
                continue

        return {"status": status, "info": info}

    async def _get_call_status_from_calling_api(self, device_serial: str):
        now = datetime.datetime.now()
        state = self._calling_fallback_state.setdefault(
            device_serial,
            {
                "last_calling_id": None,
                "last_count": None,
                "ringing_until": None,
            },
        )

        count = await self._get_calling_unread_count()
        latest = await self._get_latest_calling_event(device_serial)

        latest_id = latest.get("callingId") if latest else None
        if latest_id and latest_id != state["last_calling_id"]:
            state["last_calling_id"] = latest_id
            state["ringing_until"] = now + datetime.timedelta(
                seconds=self.CALLING_FALLBACK_RING_SECONDS
            )

        if count is not None and state["last_count"] is not None and count > state["last_count"]:
            state["ringing_until"] = now + datetime.timedelta(
                seconds=self.CALLING_FALLBACK_RING_SECONDS
            )
        state["last_count"] = count

        ringing_until = state.get("ringing_until")
        status = "ringing" if ringing_until and now < ringing_until else "idle"

        info = {
            "fallback_source": "calling_api",
            "unread_count": count,
        }
        if latest:
            info["latest_calling_id"] = latest.get("callingId")
            info["latest_calling_time"] = latest.get("callingTime")
            info["latest_calling_message"] = latest.get("callingMessage")
            info["latest_calling_status"] = latest.get("callingStatus")
            info["latest_msg_status"] = latest.get("msgStatus")

            custom_info = latest.get("customInfo")
            if custom_info:
                try:
                    info["custom_info"] = json.loads(custom_info)
                except (TypeError, json.JSONDecodeError):
                    info["custom_info"] = custom_info

        return {"status": status, "info": info}

    async def _get_calling_unread_count(self):
        async with self.client.get(
            f"{self.BASE_URL}/v3/calling/countByUser?msgStatus=0"
        ) as res:
            res_json = await res.json()

        meta = res_json.get("meta", {})
        if meta.get("code") != 200:
            raise RuntimeError(f"Unexpected calling count response: {res_json}")

        return res_json.get("count")

    async def _get_latest_calling_event(self, device_serial: str):
        least_time = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime(
            "%Y-%m-%d 00:00:00"
        )
        async with self.client.get(
            f"{self.BASE_URL}/v3/calling/{device_serial}/list"
            f"?leastTime={least_time}&msgStatus=-1&pageSize=1"
        ) as res:
            res_json = await res.json()

        data = res_json.get("data")
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def answer_call(self, device_serial: str):
        async with self.client.put(
            f"{self.BASE_URL}/v3/devconfig/v1/call/{device_serial}/operation?cmdId=2"
        ) as res:
            await res.json()

    async def cancel_call(self, device_serial: str):
        async with self.client.put(
            f"{self.BASE_URL}/v3/devconfig/v1/call/{device_serial}/operation?cmdId=3"
        ) as res:
            await res.json()

    async def hangup_call(self, device_serial: str):
        async with self.client.put(
            f"{self.BASE_URL}/v3/devconfig/v1/call/{device_serial}/operation?cmdId=5"
        ) as res:
            await res.json()

    @staticmethod
    def _decode_jwt_expiration(jwt):
        parts = jwt.split(".")
        claims_raw = parts[1]
        missing_padding = len(claims_raw) % 4
        if missing_padding:
            claims_raw += "=" * (4 - missing_padding)
        claims_json_raw = urlsafe_b64decode(claims_raw)
        claims = json.loads(claims_json_raw)
        return datetime.datetime.fromtimestamp(claims["exp"])

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.__aexit__(exc_type, exc_val, exc_tb)

    async def close(self):
        await self.client.close()