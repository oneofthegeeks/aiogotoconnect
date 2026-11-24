"""Starts the standalone fake GoTo and the FastAPI example under uvicorn for the browser tests.

Fixed ports, as in the Node e2e: the fake and the example agree on them through env vars.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE_PORT = 4010
APP_PORT = 4011
ADMIN_PASSWORD = "e2e-pass"


def _wait_for(url: str, process: subprocess.Popen[bytes], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{url}: the server exited early (code {process.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{url}: not up after {timeout}s")


def _stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="session")
def servers() -> Iterator[None]:
    for port in (FAKE_PORT, APP_PORT):
        if not _port_free(port):
            pytest.fail(f"port {port} is already in use")
    data_dir = tempfile.mkdtemp(prefix="goto-sms-e2e-")
    fake = subprocess.Popen(
        [sys.executable, "-m", "tests.sms.fake_goto"],
        cwd=ROOT,
        env={**os.environ, "FAKE_GOTO_PORT": str(FAKE_PORT)},
    )
    app: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(f"http://127.0.0.1:{FAKE_PORT}/__sent", fake)
        app = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--app-dir",
                str(ROOT / "examples" / "fastapi-app"),
                "--port",
                str(APP_PORT),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "ADMIN_PASSWORD": ADMIN_PASSWORD,
                "GOTO_SMS_PUBLIC_URL": f"http://localhost:{APP_PORT}",
                "GOTO_SMS_DATA_DIR": data_dir,
                "GOTO_SMS_AUTH_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
                "GOTO_SMS_API_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
            },
        )
        _wait_for(f"http://localhost:{APP_PORT}/healthz", app)
        yield
    finally:
        if app is not None:
            _stop(app)
        _stop(fake)


@pytest.fixture(scope="session")
def base_url(servers: None) -> str:
    return f"http://localhost:{APP_PORT}"
