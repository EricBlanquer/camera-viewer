#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import urllib.request

import websocket


def evaluate(endpoint, expression):
    with urllib.request.urlopen(endpoint + "/json", timeout=10) as response:
        pages = json.load(response)
    page = next((item for item in pages if "Camera Viewer" in item.get("title", "")), None)
    if not page:
        raise RuntimeError("Camera Viewer is not running in debug mode")
    connection = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
    try:
        connection.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}))
        while True:
            reply = json.loads(connection.recv())
            if reply.get("id") != 1:
                continue
            result = reply.get("result", {})
            if "exceptionDetails" in result or "error" in reply or result.get("result", {}).get("subtype") == "error":
                raise RuntimeError("TV operation failed; inspect the TV status panel")
            return result.get("result", {}).get("value")
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint", help="TV debug URL, including the port reported by sdb")
    parser.add_argument("--configure", type=Path, help="Private camera configuration JSON")
    arguments = parser.parse_args()
    if arguments.configure:
        descriptor = os.open(arguments.configure, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as source:
            attributes = os.fstat(source.fileno())
            if not stat.S_ISREG(attributes.st_mode) or attributes.st_mode & 0o077 or attributes.st_size > 1048576:
                raise RuntimeError("Camera configuration must be an owner-only regular file, at most 1 MiB")
            configuration = json.load(source)
        count = evaluate(arguments.endpoint, "cameraViewer.configure(" + json.dumps(configuration) + ")")
        print("Configured " + str(count) + " camera(s)")
    else:
        result = evaluate(arguments.endpoint, 'JSON.stringify({cameras:cameraViewer.state(),debugHidden:document.getElementById("debug-panel").hidden,events:cameraViewer.events.slice(-8)})')
        print(json.dumps(json.loads(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("TV operation failed; verify the debug endpoint and the private configuration file", file=sys.stderr)
        sys.exit(1)
