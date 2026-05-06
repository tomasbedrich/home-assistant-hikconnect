class HikConnectError(Exception):
    """Base integration error."""


class LoginError(HikConnectError, ValueError):
    """Login failed."""


class DeviceOffline(HikConnectError):
    """Device is offline."""