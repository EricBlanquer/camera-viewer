from __future__ import annotations

import dataclasses
import json
import urllib.request
from dataclasses import dataclass


CLOUD_ORIGIN = "https://api-we01.tange365.com"
DEVICE_DETAIL_URL = f"{CLOUD_ORIGIN}/app/device/list/detail"
LOGIN_URL = f"{CLOUD_ORIGIN}/app/user/login"
QUERY_KEYS = frozenset((
    "X-Tg-Sdk-Version", "version_no", "country_code", "appstore", "pkgname",
    "X-Tg-App-Sdk-Version", "language", "version", "app_version_no", "platform",
))
MAX_RESPONSE_SIZE = 1024 * 1024
MAX_TOKEN_LENGTH = 8192
MAX_USERNAME_LENGTH = 254
MAX_AREA_CODE_LENGTH = 6
SUCCESS_CODE = 200
CREDENTIALS_REJECTED_CODE = 51021
CLOUD_SESSION_ERROR = "Refresh the iCam365 account session to retrieve the camera credential."
ACCOUNT_SIGN_IN_ERROR = "Sign in to the iCam365 account again to retrieve the camera credential."


class CloudSessionRejected(OSError):
    pass


class AccountCredentialsRejected(OSError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


def valid_token(token: object) -> bool:
    return (isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_LENGTH and token.isascii()
            and not any(character.isspace() for character in token))


@dataclass(frozen=True, repr=False)
class CloudSession:
    token: str
    appid: str
    uuid: str
    query: dict[str, str]
    username: str = ""
    area_code: str = ""

    @classmethod
    def from_record(cls, record: dict) -> CloudSession:
        if (
            not isinstance(record, dict)
            or record.get("origin") != CLOUD_ORIGIN
            or not all(isinstance(record.get(key), str) and record[key] for key in ("token", "appid", "uuid"))
            or not valid_token(record["token"]) or len(record["uuid"]) > 128
            or not record["appid"].isascii() or not record["appid"].isdigit() or len(record["appid"]) > 32
            or not isinstance(record.get("query"), dict)
            or set(record["query"]) - QUERY_KEYS
            or not all(isinstance(value, str) and len(value) <= 256 for value in record["query"].values())
            or not isinstance(record.get("username", ""), str)
            or len(record.get("username", "")) > MAX_USERNAME_LENGTH
            or any(character.isspace() for character in record.get("username", ""))
            or not isinstance(record.get("area_code", ""), str)
            or len(record.get("area_code", "")) > MAX_AREA_CODE_LENGTH
            or not all(character in "0123456789" for character in record.get("area_code", ""))
        ):
            raise OSError("Invalid iCam365 account session configuration.")
        return cls(record["token"], record["appid"], record["uuid"], dict(record["query"]),
                   record.get("username", ""), record.get("area_code", ""))

    def record(self) -> dict:
        record = {"origin": CLOUD_ORIGIN, "token": self.token, "appid": self.appid, "uuid": self.uuid,
                  "query": dict(self.query)}
        if self.username:
            record["username"] = self.username
        if self.area_code:
            record["area_code"] = self.area_code
        return record

    def _post(self, url: str, body: dict, extra_headers: dict[str, str]) -> dict:
        request = urllib.request.Request(url, data=json.dumps(self.query | body).encode(), headers=extra_headers | {
            "Content-Type": "application/json", "Accept-Language": "fr",
            "X-Tg-App-Id": self.appid, "X-Tg-App-Pkgname": "com.tange365.icam365",
            "X-Tg-App-Platform": "android",
        })
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                payload = response.read(MAX_RESPONSE_SIZE + 1)
            if len(payload) > MAX_RESPONSE_SIZE:
                raise ValueError("Response too large")
            response = json.loads(payload)
            if not isinstance(response, dict):
                raise ValueError("Unexpected response")
            return response
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            raise OSError(CLOUD_SESSION_ERROR) from None

    def device_record(self) -> dict:
        body = {"token": self.token, "appid": self.appid, "uuid": self.uuid, "page": "1", "limit": "1"}
        response = self._post(DEVICE_DETAIL_URL, body, {"Authorization": self.token})
        if response.get("code") != SUCCESS_CODE:
            raise CloudSessionRejected(CLOUD_SESSION_ERROR)
        try:
            items = response.get("data", {}).get("items", [])
            if len(items) != 1 or items[0].get("uuid") != self.uuid:
                raise ValueError("Unexpected device response")
            return items[0]
        except (ValueError, TypeError, AttributeError, IndexError):
            raise OSError(CLOUD_SESSION_ERROR) from None

    def signed_in(self, password: str) -> CloudSession:
        if not self.username or not password:
            raise OSError(ACCOUNT_SIGN_IN_ERROR)
        body = {"username": self.username, "pwd": password, "area_code": self.area_code, "appid": self.appid}
        response = self._post(LOGIN_URL, body, {})
        if response.get("code") == CREDENTIALS_REJECTED_CODE:
            raise AccountCredentialsRejected(ACCOUNT_SIGN_IN_ERROR)
        data = response.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if response.get("code") != SUCCESS_CODE or not valid_token(token):
            raise OSError(CLOUD_SESSION_ERROR)
        return dataclasses.replace(self, token=token)
