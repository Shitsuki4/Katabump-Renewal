"""Start the owned sing-box process and wait for actual proxy readiness."""

import ipaddress
import os
from pathlib import Path
import subprocess
import time

import requests


PROXY_URL = "http://127.0.0.1:8080"
CHECK_URL = "https://api.ip.sb/ip"


def singbox_binary():
    return str(Path("sing-box.exe" if os.name == "nt" else "sing-box").resolve())


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_for_proxy(process, timeout=60):
    deadline = time.monotonic() + timeout
    with requests.Session() as session:
        session.trust_env = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"sing-box exited with code {process.returncode}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = session.get(
                    CHECK_URL,
                    proxies={"http": PROXY_URL, "https": PROXY_URL},
                    timeout=min(5, remaining / 2),
                )
                response.raise_for_status()
                address = str(ipaddress.ip_address(response.text.strip()))
                if process.poll() is None:
                    return address
            except (requests.RequestException, ValueError):
                pass
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError("Proxy did not become ready within the startup deadline")


def start_proxy(timeout=60):
    check = subprocess.run(
        [singbox_binary(), "check", "-c", "config.json"],
        capture_output=True, timeout=30,
    )
    if check.returncode:
        raise RuntimeError("sing-box rejected config.json; startup cancelled")
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
               else {"start_new_session": True})
    with open("singbox.log", "ab") as log:
        process = subprocess.Popen(
            [singbox_binary(), "run", "-c", "config.json"],
            stdout=log, stderr=subprocess.STDOUT, **options,
        )
    try:
        address = wait_for_proxy(process, timeout)
    except BaseException:
        stop_process(process)
        raise
    print(f"Proxy ready (PID {process.pid}, exit IP {address}).", flush=True)
    return process


if __name__ == "__main__":
    try:
        start_proxy()
    except Exception as exc:
        print(f"Proxy startup failed: {type(exc).__name__}.", flush=True)
        raise SystemExit(1)
