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
from datetime import datetime
from typing import Callable


IMOU_REGIONS = {
    "Europe": "openapi-fk.easy4ip.com",
    "Singapore": "openapi-sg.easy4ip.com",
    "North America": "openapi-or.easy4ip.com",
}
IMOU_ACCOUNT_URL = "https://open.imoulife.com/consoleNew/myApp/appInfo"
IMOU_PRIVACY_MESSAGE = "Privacy mode is enabled in Imou Life."
IMOU_SECRET_MISSING_MESSAGE = "The Imou account key is unavailable in the desktop keyring."
IMOU_TRAFFIC_MESSAGE = "Imou cloud traffic is exhausted. Add traffic in Imou Cloud, then refresh the camera list."
IMOU_TRAFFIC_CODES = frozenset(("FL1004", "FL1005"))
IMOU_RESPONSE_LIMIT = 4 * 1024 * 1024
IMOU_REQUEST_TIMEOUT = 15
IMOU_PAGE_SIZE = 50
IMOU_MAX_PAGES = 100
IMOU_AUTH_CODES = frozenset(("SN1001", "SN1004", "TK1001"))
IMOU_TOKEN_EXPIRED = "TK1002"
IMOU_MOVE_OPERATIONS = {"Up": "0", "Down": "1", "Left": "2", "Right": "3"}
IMOU_MOVE_DURATION_MS = 500
IMOU_RECORD_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
IMOU_RECORD_PAGE_SIZE = 30


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
    abilities: str = ""

    def __post_init__(self) -> None:
        if (not isinstance(self.device_id, str) or not self.device_id.strip()
                or not isinstance(self.name, str) or not self.name.strip()
                or not isinstance(self.channel_id, str) or not self.channel_id.isascii()
                or not self.channel_id.isdigit() or not isinstance(self.product_id, str)
                or not isinstance(self.privacy, bool) or not isinstance(self.abilities, str)):
            raise ValueError("The Imou service returned an invalid camera identity.")

    def supports(self, *abilities: str) -> bool:
        return bool(set(abilities).intersection(self.abilities.split(",")))

    def uid(self, app_id: str) -> str:
        identity = f"{app_id}:{self.device_id}:{self.channel_id}"
        return f"rtsp:{hashlib.sha256(identity.encode()).hexdigest()[:24]}-imou"


@dataclass(frozen=True)
class ImouRecording:
    record_id: str
    start: datetime
    end: datetime
    detection: bool
    size: int

    @property
    def key(self) -> str:
        return hashlib.sha256(self.record_id.encode()).hexdigest()


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
        self.kit_tokens: dict[str, tuple[str, float]] = {}

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
            if code in IMOU_TRAFFIC_CODES:
                raise ImouError(IMOU_TRAFFIC_MESSAGE, code)
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
                            channel.get("channelAbility") or record.get("deviceAbility") or "",
                        )
                    except (ValueError, AttributeError):
                        raise ImouError("Imou returned an invalid camera identity.") from None
                    cameras[camera.uid(self.account.app_id)] = camera
            if len(records) < IMOU_PAGE_SIZE:
                return list(cameras.values())
        raise ImouError("The Imou camera list exceeded the pagination limit.")

    def stream_url(self, camera: ImouDevice, stream_id: int = 0) -> str:
        if camera.privacy:
            raise ImouError(IMOU_PRIVACY_MESSAGE)
        if stream_id not in (0, 1):
            raise ValueError("Unsupported Imou video quality.")
        params = {"deviceId": camera.device_id, "channelId": camera.channel_id, "streamId": stream_id}
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

    def collections(self, camera: ImouDevice) -> dict[str, str]:
        if not camera.supports("CollectionPoint"):
            return {}
        records = self.call("getCollection", self.camera_params(camera)).get("collections", [])
        if not isinstance(records, list) or any(
            not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"]
            for record in records
        ):
            raise ImouError("Imou returned invalid saved camera positions.")
        return {f"Preset {index}": record["name"] for index, record in enumerate(records, 1)}

    def move(self, camera: ImouDevice, direction: str, collections: dict[str, str]) -> None:
        if camera.privacy:
            raise ImouError(IMOU_PRIVACY_MESSAGE)
        params = self.camera_params(camera)
        if direction in collections and camera.supports("CollectionPoint"):
            self.call("turnCollection", dict(params, name=collections[direction]))
        elif direction in IMOU_MOVE_OPERATIONS and camera.supports("PT", "PTZ"):
            self.call("controlMovePTZ", dict(
                params, operation=IMOU_MOVE_OPERATIONS[direction], duration=IMOU_MOVE_DURATION_MS,
            ))
        else:
            raise ImouError("This camera movement is unavailable.")

    def recordings(self, camera: ImouDevice, start: datetime, end: datetime) -> list[ImouRecording]:
        params = dict(self.camera_params(camera), beginTime=start.strftime(IMOU_RECORD_TIME_FORMAT),
                      endTime=end.strftime(IMOU_RECORD_TIME_FORMAT), type="All")
        recordings: dict[str, ImouRecording] = {}
        seen: set[str] = set()
        for page in range(IMOU_MAX_PAGES):
            first = page * IMOU_RECORD_PAGE_SIZE + 1
            data = self.call("queryLocalRecords", dict(params, queryRange=f"{first}-{first + IMOU_RECORD_PAGE_SIZE - 1}"))
            records = data.get("records", [])
            if not isinstance(records, list):
                raise ImouError("Imou returned an invalid recording list.")
            before = len(seen)
            for record in records:
                if not isinstance(record, dict):
                    raise ImouError("Imou returned invalid recording metadata.")
                try:
                    recording = ImouRecording(
                        record["recordId"], datetime.strptime(record["beginTime"], IMOU_RECORD_TIME_FORMAT),
                        datetime.strptime(record["endTime"], IMOU_RECORD_TIME_FORMAT),
                        str(record.get("type", "normal")).lower() not in ("normal", "regular"), int(record.get("fileLength", 0)),
                    )
                    if (not isinstance(recording.record_id, str) or not recording.record_id
                            or recording.end < recording.start or recording.size < 0):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ImouError("Imou returned invalid recording metadata.") from None
                seen.add(recording.key)
                if recording.end > recording.start:
                    recordings[recording.key] = recording
            if len(records) < IMOU_RECORD_PAGE_SIZE:
                return sorted(recordings.values(), key=lambda recording: recording.start)
            if len(seen) == before:
                raise ImouError("Imou repeated the same recording page.")
        raise ImouError("The Imou recording list exceeded the pagination limit.")

    def replay_url(self, camera: ImouDevice, recording: ImouRecording) -> str:
        return self.secure_stream_url(camera, recording=recording, native=True)

    def secure_stream_url(self, camera: ImouDevice, stream_id: int = 0,
                          recording: ImouRecording | None = None, native: bool = False) -> str:
        if camera.privacy:
            raise ImouError(IMOU_PRIVACY_MESSAGE)
        if stream_id not in (0, 1):
            raise ValueError("Unsupported Imou video quality.")
        permission = "1" if recording is None else "2"
        key = f"{camera.device_id}:{camera.channel_id}:{permission}"
        token, expires = self.kit_tokens.get(key, ("", 0.0))
        if not token or time.monotonic() >= expires:
            data = self.call("getKitToken", dict(self.camera_params(camera), type=permission))
            token = data.get("kitToken")
            if not isinstance(token, str) or not token:
                raise ImouError("Imou returned no camera stream token.")
            self.kit_tokens[key] = token, time.monotonic() + 3600
        params = dict(
            self.camera_params(camera), kitToken=token, streamId=stream_id,
            businessType="real" if recording is None else "localRecord",
            beginTime=recording.start.strftime(IMOU_RECORD_TIME_FORMAT) if recording is not None else "",
            endTime=recording.end.strftime(IMOU_RECORD_TIME_FORMAT) if recording is not None else "",
            encryptStreamFlag=False, rtsvEnable=native, nvrLinkFlag=True,
        )
        if not native:
            params["protoType"] = "rtsp"
        data = self.call("getEncryptKitStreamUrl", params)
        url = data.get("url")
        if data.get("isEncrypt") is not False or not isinstance(url, str):
            raise ImouError("The camera requires an encrypted video format.")
        if native and data.get("streamType") != "rtsv":
            raise ImouError("Native camera playback is unavailable.")
        return url

    @staticmethod
    def camera_params(camera: ImouDevice) -> dict:
        return {"deviceId": camera.device_id, "channelId": camera.channel_id}
