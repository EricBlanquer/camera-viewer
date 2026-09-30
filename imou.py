from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable


IMOU_REGIONS = {
    "Europe": "openapi-fk.easy4ip.com",
    "Singapore": "openapi-sg.easy4ip.com",
    "North America": "openapi-or.easy4ip.com",
}
IMOU_ACCOUNT_URL = "https://open.imoulife.com/consoleNew/myApp/appInfo"
IMOU_PRIVACY_MESSAGE = "Privacy mode is enabled in Imou Life."
IMOU_SECRET_MISSING_MESSAGE = "The Imou account key is unavailable in the desktop keyring."
IMOU_RESPONSE_LIMIT = 4 * 1024 * 1024
IMOU_REQUEST_TIMEOUT = 15
IMOU_PAGE_SIZE = 50
IMOU_MAX_PAGES = 100
IMOU_AUTH_CODES = frozenset(("SN1001", "SN1004", "TK1001"))
IMOU_TOKEN_EXPIRED = "TK1002"


class ImouError(OSError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImouAccount:
    email: str
    app_id: str
    region: str = "Europe"
    cloud_recording: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.email, str) or not self.email.strip()
                or not isinstance(self.app_id, str) or not self.app_id.strip()
                or not isinstance(self.region, str) or self.region not in IMOU_REGIONS
                or not isinstance(self.cloud_recording, bool)):
            raise ValueError("Enter an Imou account email, AppId, and server region.")


@dataclass(frozen=True)
class ImouDevice:
    device_id: str
    name: str
    channel_id: str
    product_id: str = ""
    privacy: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.device_id, str) or not self.device_id.strip()
                or not isinstance(self.name, str) or not self.name.strip()
                or not isinstance(self.channel_id, str) or not self.channel_id.isascii()
                or not self.channel_id.isdigit() or not isinstance(self.product_id, str)
                or not isinstance(self.privacy, bool)):
            raise ValueError("The Imou service returned an invalid camera identity.")

    def uid(self, app_id: str) -> str:
        identity = f"{app_id}:{self.device_id}:{self.channel_id}"
        return f"rtsp:{hashlib.sha256(identity.encode()).hexdigest()[:24]}-imou"


def imou_signature(secret: str, timestamp: int, nonce: str) -> str:
    key = hashlib.sha256(secret.encode()).hexdigest().encode()
    message = f"time:{timestamp},nonce:{nonce},appSecret:{secret}".encode()
    return base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()


def imou_request(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=IMOU_REQUEST_TIMEOUT) as response:
            payload = response.read(IMOU_RESPONSE_LIMIT + 1)
            if len(payload) > IMOU_RESPONSE_LIMIT:
                raise ImouError("The Imou service response is too large.")
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                raise ImouError("The Imou service returned an invalid response.")
            return decoded
    except urllib.error.HTTPError as ex:
        raise ImouError(f"The Imou service failed (HTTP {ex.code}).") from None
    except (urllib.error.URLError, TimeoutError):
        raise ImouError("The Imou account service is unreachable.") from None
    except (ValueError, UnicodeError):
        raise ImouError("The Imou service returned an invalid response.") from None


class ImouClient:
    def __init__(
        self, account: ImouAccount, secret: str,
        opener: Callable[[urllib.request.Request], dict] = imou_request,
    ) -> None:
        if not secret:
            raise ImouError(IMOU_SECRET_MISSING_MESSAGE)
        self.account = account
        self.secret = secret
        self.opener = opener
        self.host = IMOU_REGIONS[account.region]
        self.token = ""
        self.token_expires = 0.0

    def call(self, method: str, params: dict | None = None, retry_token: bool = True) -> dict:
        if method != "accessToken" and (not self.token or time.monotonic() >= self.token_expires):
            self.authenticate()
        timestamp = int(time.time())
        nonce = str(uuid.uuid4())
        payload = dict(params or {})
        if method != "accessToken":
            payload["token"] = self.token
        request = urllib.request.Request(
            f"https://{self.host}/openapi/{method}",
            data=json.dumps({
                "system": {
                    "ver": "1.0", "appId": self.account.app_id,
                    "time": timestamp, "nonce": nonce,
                    "sign": imou_signature(self.secret, timestamp, nonce),
                },
                "params": payload, "id": str(uuid.uuid4()),
            }).encode(),
            headers={"Content-Type": "application/json", "Client-Type": "CameraViewer"},
            method="POST",
        )
        response = self.opener(request)
        result = response.get("result")
        if not isinstance(result, dict):
            raise ImouError("The Imou service returned an invalid response.")
        code = str(result.get("code", ""))
        if code == IMOU_TOKEN_EXPIRED and method != "accessToken" and retry_token:
            self.token = ""
            return self.call(method, params, False)
        if code != "0":
            if code in IMOU_AUTH_CODES:
                raise ImouError("Imou rejected the application credentials.", code)
            raise ImouError(f"The Imou {method} request failed ({code or 'invalid response'}).", code)
        data = result.get("data", {})
        if not isinstance(data, dict):
            raise ImouError("The Imou service returned invalid response data.")
        return data

    def authenticate(self) -> None:
        data = self.call("accessToken")
        token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            raise ImouError("Imou returned no account access token.")
        try:
            expires = max(1, int(data.get("expireTime", 3600)))
        except (ValueError, TypeError):
            raise ImouError("Imou returned an invalid token expiry.") from None
        domain = data.get("currentDomain")
        if domain:
            try:
                parsed = urllib.parse.urlsplit(domain if "://" in str(domain) else f"https://{domain}")
                if (parsed.scheme != "https" or parsed.hostname not in IMOU_REGIONS.values()
                        or parsed.username or parsed.password or parsed.port not in (None, 443)
                        or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ImouError("Imou returned an unexpected account server.") from None
            self.host = parsed.hostname
        self.token = token
        self.token_expires = time.monotonic() + max(1, expires - 60)

    def devices(self) -> list[ImouDevice]:
        cameras: dict[str, ImouDevice] = {}
        previous_page = None
        for page in range(1, IMOU_MAX_PAGES + 1):
            data = self.call("listDeviceDetailsByPage", {"page": page, "pageSize": IMOU_PAGE_SIZE, "source": "bindAndShare"})
            records = data.get("deviceList", [])
            if not isinstance(records, list):
                raise ImouError("Imou returned an invalid camera list.")
            if records and records == previous_page:
                raise ImouError("The Imou service repeated the same camera page.")
            previous_page = records
            for record in records:
                if not isinstance(record, dict):
                    raise ImouError("Imou returned an invalid camera entry.")
                channels = record.get("channelList", [])
                if not isinstance(channels, list):
                    raise ImouError("Imou returned invalid camera channels.")
                for channel in channels:
                    if not isinstance(channel, dict):
                        raise ImouError("Imou returned an invalid camera channel.")
                    try:
                        camera = ImouDevice(
                            record.get("deviceId", ""),
                            (record.get("deviceName") if len(channels) == 1 else channel.get("channelName"))
                            or record.get("deviceName") or record.get("deviceId", ""),
                            str(channel.get("channelId", "")), record.get("productId") or "",
                            channel.get("cameraStatus") == "on",
                        )
                    except (ValueError, AttributeError):
                        raise ImouError("Imou returned an invalid camera identity.") from None
                    cameras[camera.uid(self.account.app_id)] = camera
            if len(records) < IMOU_PAGE_SIZE:
                return list(cameras.values())
        raise ImouError("The Imou camera list exceeded the pagination limit.")

    def stream_url(self, camera: ImouDevice) -> str:
        if camera.privacy:
            raise ImouError(IMOU_PRIVACY_MESSAGE)
        params = {"deviceId": camera.device_id, "channelId": camera.channel_id, "streamId": 0}
        if camera.product_id:
            params["productId"] = camera.product_id
        url = self.call("getStreamUrl", params).get("url")
        try:
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
            if (parsed is None or parsed.scheme != "rtsp" or not parsed.hostname
                    or parsed.port == 0 or any(character in url for character in ("\r", "\n", "\x00"))):
                raise ValueError()
        except ValueError:
            raise ImouError("Imou returned no usable private camera stream.") from None
        return url
