import json

from hikconnect.api import HikConnect

CALL_STATUS_MAP = {
    "callInProgress": "call in progress",
}


async def isapi_request(api: HikConnect, serial: str, path: str) -> dict:
    """Send a GET request via the HikConnect cloud ISAPI tunnel. Returns parsed inner JSON."""
    data = {
        "subSerial": serial,
        "cmdId": "19713",
        "transmissionData": f"GET {path}?format=json\r\n",
    }
    async with api.client.post(f"{api.BASE_URL}/api/device/isapi", data=data) as res:
        outer = await res.json(content_type=None)

    if outer.get("resultCode") != "0":
        raise ValueError(f"ISAPI tunnel error: {outer.get('resultCode')} {outer.get('resultDes')}")

    return json.loads(outer["data"])


async def isapi_call_status(api: HikConnect, serial: str) -> str:
    """GET /ISAPI/VideoIntercom/callStatus via cloud tunnel. Returns idle/ringing/call in progress."""
    inner = await isapi_request(api, serial, "/ISAPI/VideoIntercom/callStatus")
    raw = inner.get("CallStatus", {}).get("status")
    if raw is None:
        raise ValueError(f"Unexpected ISAPI response schema: {inner}")
    return CALL_STATUS_MAP.get(raw, raw)
