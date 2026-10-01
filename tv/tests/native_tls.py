import argparse
import hashlib
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading


def verify_connection(binary: Path, context: ssl.SSLContext, pin: str, expected: str) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    port = listener.getsockname()[1]
    received = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with context.wrap_socket(connection, server_side=True) as secure:
                secure.settimeout(10)
                data = secure.recv(4096)
                received.append(data)
                if data:
                    secure.sendall(data[:3])
                    secure.sendall(data[3:])
        except (ssl.SSLError, OSError):
            pass
        finally:
            listener.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    subprocess.run([str(binary), str(port), pin, expected], check=True, timeout=20)
    worker.join(15)
    if worker.is_alive() or received != ([b"video\x00binary"] if expected == "accept" else [b""]):
        raise AssertionError("Unexpected TLS application data")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tls-source", type=Path, required=True)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="intraswitch_camera_tv_tls_") as directory:
        work = Path(directory)
        source = work / "mbedtls"
        shutil.copytree(arguments.tls_source, source, ignore=shutil.ignore_patterns("*.o", "*.a", "*.so*"))
        flags = '-O2 -std=c99 -DMBEDTLS_USER_CONFIG_FILE=\\"tls_config.h\\" -I' + str(root / "native")
        with (work / "build.log").open("w") as log:
            build = subprocess.run(["make", "-C", str(source / "library"), "-j8", "CC=cc", "AR=ar", "CFLAGS=" + flags], stdout=log, stderr=subprocess.STDOUT)
        if build.returncode:
            raise RuntimeError((work / "build.log").read_text())
        binary = work / "native_tls"
        subprocess.run([
            "c++", "-std=c++11", "-Wall", "-Wextra", "-Werror",
            '-DMBEDTLS_USER_CONFIG_FILE="tls_config.h"',
            "-I" + str(source / "include"), "-I" + str(root / "native"),
            str(root / "tests/native_tls.cc"),
            str(source / "library/libmbedtls.a"), str(source / "library/libmbedx509.a"),
            str(source / "library/libmbedcrypto.a"), "-o", str(binary),
        ], check=True)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-subj", "/CN=Local Camera", "-keyout", str(work / "key.pem"),
            "-out", str(work / "cert.pem"), "-days", "1",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        certificate = ssl.PEM_cert_to_DER_cert((work / "cert.pem").read_text())
        pin = hashlib.sha256(certificate).hexdigest()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(work / "cert.pem", work / "key.pem")
        verify_connection(binary, context, pin, "accept")
        verify_connection(binary, context, "0" * 64, "reject")


if __name__ == "__main__":
    main()
