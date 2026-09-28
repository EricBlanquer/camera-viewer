from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


DEVICE_DETAIL_URL = "https://api-we01.tange365.com/app/device/list/detail"
QUERY_KEYS = frozenset((
    "X-Tg-Sdk-Version", "version_no", "country_code", "appstore", "pkgname",
    "X-Tg-App-Sdk-Version", "language", "version", "app_version_no", "platform",
))
MAX_RESPONSE_SIZE = 1024 * 1024
CLOUD_SESSION_ERROR = "Refresh the iCam365 account session to retrieve the camera credential."


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


@dataclass(frozen=True, repr=False)
class CloudSession:
    token: str
    appid: str
    uuid: str
    query: dict[str, str]

    @classmethod
    def from_record(cls, record: dict) -> CloudSession:
        if (
            not isinstance(record, dict)
            or record.get("origin") != "https://api-we01.tange365.com"
            or not all(isinstance(record.get(key), str) and record[key] for key in ("token", "appid", "uuid"))
            or len(record["token"]) > 8192 or len(record["uuid"]) > 128
            or not record["token"].isascii() or any(character.isspace() for character in record["token"])
            or not record["appid"].isascii() or not record["appid"].isdigit() or len(record["appid"]) > 32
            or not isinstance(record.get("query"), dict)
            or set(record["query"]) - QUERY_KEYS
            or not all(isinstance(value, str) and len(value) <= 256 for value in record["query"].values())
        ):
            raise OSError("Invalid iCam365 account session configuration.")
        return cls(record["token"], record["appid"], record["uuid"], dict(record["query"]))

    def device_record(self) -> dict:
        body = self.query | {
            "token": self.token, "appid": self.appid, "uuid": self.uuid, "page": "1", "limit": "1",
        }
        request = urllib.request.Request(DEVICE_DETAIL_URL, data=json.dumps(body).encode(), headers={
            "Authorization": self.token, "Content-Type": "application/json", "Accept-Language": "fr",
            "X-Tg-App-Id": self.appid, "X-Tg-App-Pkgname": "com.tange365.icam365",
            "X-Tg-App-Platform": "android",
        })
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                payload = response.read(MAX_RESPONSE_SIZE + 1)
            if len(payload) > MAX_RESPONSE_SIZE:
                raise ValueError("Response too large")
            response = json.loads(payload)
            items = response.get("data", {}).get("items", [])
            if response.get("code") != 200 or len(items) != 1 or items[0].get("uuid") != self.uuid:
                raise ValueError("Unexpected device response")
            return items[0]
        except (OSError, ValueError, TypeError, AttributeError, IndexError, UnicodeError):
            raise OSError(CLOUD_SESSION_ERROR) from None
