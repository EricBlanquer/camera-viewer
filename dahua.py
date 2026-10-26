from __future__ import annotations

import copy
import hashlib
import json
import socket
import struct
from typing import Callable


DAHUA_DVRIP_PORT = 37777
DAHUA_TIMEOUT_SECONDS = 10
DAHUA_HEADER_BYTES = 32
DAHUA_RESPONSE_LIMIT = 1024 * 1024
DAHUA_MAX_SKIPPED_REPLIES = 16
DAHUA_REALM_REQUEST = bytes.fromhex("a0010000") + bytes(20) + bytes.fromhex("050201010000a1aa")
DAHUA_LOGIN_COMMAND = bytes.fromhex("a0050000")
DAHUA_LOGIN_FLAGS = bytes.fromhex("050200080000a1aa")
DAHUA_LOGIN_REPLY = 0xB0
DAHUA_LOGIN_ACCEPTED = b"\x00\x08"
DAHUA_LOGIN_ERRORS = {
    b"\x01\x00": "The camera rejected its device password.",
    b"\x01\x01": "The camera rejected its username.",
    b"\x01\x04": "The camera account is locked; try again later.",
}
DAHUA_CONNECTION_MESSAGE = "The camera configuration connection failed."
DAHUA_CLOSED_MESSAGE = "The camera configuration connection is closed."
DAHUA_JSON_COMMAND = b"\xf6\x00\x00\x00"
DAHUA_JSON_REPLY = 0xF6
DAHUA_REALM_PREFIX = "Realm:"
DAHUA_RANDOM_PREFIX = "Random:"
DAHUA_DAY_NIGHT_CONFIG = "VideoInDayNight"
DAHUA_DAY_NIGHT_FORCED = "BlackWhite"
DAHUA_DAY_NIGHT_AUTOMATIC = "Brightness"


class DahuaError(OSError):
    pass


def dahua_md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().upper()


def dahua_legacy_hash(password: str) -> str:
    digest = hashlib.md5(password.encode()).digest()
    characters = []
    for index in range(0, len(digest), 2):
        value = (digest[index] + digest[index + 1]) % 62
        characters.append(chr(value + 48 if value < 10 else value + 55 if value < 36 else value + 61))
    return "".join(characters)


def dahua_login_proof(username: str, password: str, realm: str, random: str) -> str:
    realm_hash = dahua_md5(f"{username}:{realm}:{password}")
    return (f"{username}&&{dahua_md5(f'{username}:{random}:{realm_hash}')}"
            f"{dahua_md5(f'{username}:{random}:{dahua_legacy_hash(password)}')}")


def dahua_challenge(text: str) -> tuple[str, str]:
    values = {}
    for line in text.split("\r\n"):
        for prefix in (DAHUA_REALM_PREFIX, DAHUA_RANDOM_PREFIX):
            if line.startswith(prefix):
                values[prefix] = line.removeprefix(prefix)
    realm = values.get(DAHUA_REALM_PREFIX, "")
    random = values.get(DAHUA_RANDOM_PREFIX, "")
    if not realm or not random:
        raise DahuaError("The camera did not offer a login challenge.")
    return realm, random


class DahuaClient:
    def __init__(
        self, host: str, username: str, password: str, port: int = DAHUA_DVRIP_PORT,
        connect: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        if not host or not username or not password:
            raise DahuaError("Enter the camera address, username, and device password.")
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.connect = connect
        self.connection: socket.socket | None = None
        self.session = 0
        self.request = 0

    def __enter__(self) -> DahuaClient:
        try:
            self.connection = self.connect((self.host, self.port), timeout=DAHUA_TIMEOUT_SECONDS)
            self.login()
        except (OSError, ValueError) as ex:
            self.close()
            if isinstance(ex, DahuaError):
                raise
            raise DahuaError("The camera configuration service is unreachable.") from None
        return self

    def __exit__(self, *exception: object) -> None:
        try:
            if self.session:
                self.call("global.logout")
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def send(self, data: bytes) -> None:
        if self.connection is None:
            raise DahuaError(DAHUA_CLOSED_MESSAGE)
        try:
            self.connection.sendall(data)
        except OSError:
            raise DahuaError(DAHUA_CONNECTION_MESSAGE) from None

    def read(self, size: int) -> bytes:
        if self.connection is None:
            raise DahuaError(DAHUA_CLOSED_MESSAGE)
        data = bytearray()
        while len(data) < size:
            try:
                chunk = self.connection.recv(size - len(data))
            except OSError:
                raise DahuaError(DAHUA_CONNECTION_MESSAGE) from None
            if not chunk:
                raise DahuaError("The camera closed its configuration connection.")
            data += chunk
        return bytes(data)

    def read_reply(self) -> tuple[bytes, bytes]:
        header = self.read(DAHUA_HEADER_BYTES)
        length = struct.unpack("<I", header[4:8])[0]
        if header[0] != DAHUA_LOGIN_REPLY or length > DAHUA_RESPONSE_LIMIT:
            raise DahuaError("The camera returned an invalid login reply.")
        return header, self.read(length)

    def login(self) -> None:
        self.send(DAHUA_REALM_REQUEST)
        realm, random = dahua_challenge(self.read_reply()[1].decode("latin-1"))
        proof = dahua_login_proof(self.username, self.password, realm, random).encode()
        self.send(DAHUA_LOGIN_COMMAND + struct.pack("<I", len(proof)) + bytes(16) + DAHUA_LOGIN_FLAGS + proof)
        header = self.read_reply()[0]
        result = header[8:10]
        if result != DAHUA_LOGIN_ACCEPTED:
            raise DahuaError(DAHUA_LOGIN_ERRORS.get(result, "The camera refused the configuration login."))
        self.session = struct.unpack("<I", header[16:20])[0]
        if not self.session:
            raise DahuaError("The camera returned no configuration session.")

    def call(self, method: str, params: dict | None = None) -> dict:
        self.request += 1
        payload = json.dumps({"method": method, "params": params, "id": self.request, "session": self.session}).encode()
        self.send(DAHUA_JSON_COMMAND + struct.pack(
            "<IIIIIII", len(payload), self.request, 0, len(payload), 0, self.session, 0,
        ) + payload)
        for _ in range(DAHUA_MAX_SKIPPED_REPLIES + 1):
            reply = self.read_message()
            if reply.get("id") == self.request:
                return reply
        raise DahuaError("The camera did not answer the configuration request.")

    def read_message(self) -> dict:
        data = bytearray()
        while True:
            header = self.read(DAHUA_HEADER_BYTES)
            length, total = struct.unpack("<I", header[4:8])[0], struct.unpack("<I", header[16:20])[0]
            if header[0] != DAHUA_JSON_REPLY or total > DAHUA_RESPONSE_LIMIT or len(data) + length > total:
                raise DahuaError("The camera returned an invalid configuration reply.")
            data += self.read(length)
            if len(data) == total:
                break
        try:
            reply = json.loads(bytes(data).rstrip(b"\x00").decode("utf-8").split("\n", 1)[0])
        except (UnicodeError, ValueError):
            raise DahuaError("The camera returned an invalid configuration reply.") from None
        if not isinstance(reply, dict):
            raise DahuaError("The camera returned an invalid configuration reply.")
        return reply

    def config(self, name: str) -> list:
        reply = self.call("configManager.getConfig", {"name": name})
        table = reply.get("params", {}).get("table") if isinstance(reply.get("params"), dict) else None
        if reply.get("result") is not True or not isinstance(table, list):
            raise DahuaError(f"The camera does not provide its {name} configuration.")
        return table

    def set_config(self, name: str, table: list) -> None:
        if self.call("configManager.setConfig", {"name": name, "table": table, "options": []}).get("result") is not True:
            raise DahuaError(f"The camera refused its {name} configuration.")


def set_forced_night_vision(client: DahuaClient, channel: int, forced: bool) -> bool:
    table = client.config(DAHUA_DAY_NIGHT_CONFIG)
    if not 0 <= channel < len(table) or not isinstance(table[channel], list) or not table[channel] or any(
        not isinstance(profile, dict) or not isinstance(profile.get("Mode"), str) for profile in table[channel]
    ):
        raise DahuaError("The camera does not provide a day and night mode for this channel.")
    updated = copy.deepcopy(table)
    for profile in updated[channel]:
        if forced:
            profile["Mode"] = DAHUA_DAY_NIGHT_FORCED
        elif profile["Mode"] == DAHUA_DAY_NIGHT_FORCED:
            profile["Mode"] = DAHUA_DAY_NIGHT_AUTOMATIC
    if updated == table:
        return False
    client.set_config(DAHUA_DAY_NIGHT_CONFIG, updated)
    return True
