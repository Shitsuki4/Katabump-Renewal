"""Own sing-box's lifecycle; distinguish local readiness from route health."""

import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import time

import requests


PROXY_URL = "http://127.0.0.1:8080"
CONTROL_URL = "http://127.0.0.1:9099/proxies/proxy"
CHECK_URLS = ("https://api.ip.sb/ip", "https://api.ipify.org")
TARGET_URL = "https://dashboard.katabump.com/auth/login"


class ProxyRuntimeError(RuntimeError):
    pass


def singbox_binary():
    return str(Path("sing-box.exe" if os.name == "nt" else "sing-box").resolve())


def spawn_singbox(config_path, log_path):
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
               else {"start_new_session": True})
    with open(log_path, "ab") as log:
        return subprocess.Popen(
            [singbox_binary(), "run", "-c", str(config_path)],
            stdout=log, stderr=subprocess.STDOUT, **options,
        )


def stop_process(process):
    """Only terminate the process we started, never a global pkill match."""
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_for_listener(process, port, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ProxyRuntimeError("sing-box exited before opening its listener")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("Proxy listener startup timed out")


def wait_for_proxy(process, timeout=20):
    """Readiness must not depend on the first node or one external IP API.

    If the selected node dies, the local controller must still become ready
    so the runner can select the next node (including the standby source).
    """
    deadline = time.monotonic() + timeout
    with requests.Session() as session:
        session.trust_env = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ProxyRuntimeError("sing-box exited during startup")
            try:
                response = session.get(CONTROL_URL, timeout=1)
                response.raise_for_status()
                state = response.json()
                if state.get("now") and process.poll() is None:
                    return state["now"]
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.2)
    raise TimeoutError("Proxy controller startup timed out")


def _require_free_ports():
    for port in (8080, 9099):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                raise ProxyRuntimeError(f"Local port {port} is already in use")
        except OSError:
            pass


def start_proxy(timeout=20):
    _require_free_ports()
    check = subprocess.run(
        [singbox_binary(), "check", "-c", "config.json"],
        capture_output=True, timeout=15,
    )
    if check.returncode:
        raise ProxyRuntimeError("sing-box rejected config.json")
    process = spawn_singbox("config.json", "singbox.log")
    try:
        wait_for_proxy(process, timeout)
        wait_for_listener(process, 8080, timeout=5)
    except BaseException:
        stop_process(process)
        raise
    print(f"Proxy controller ready (owned PID {process.pid}).", flush=True)
    return process


def pin_node(tag):
    """Fail closed if selection cannot be confirmed; never reuse a failed node."""
    with requests.Session() as session:
        session.trust_env = False
        response = session.put(CONTROL_URL, json={"name": tag}, timeout=5)
        response.raise_for_status()
        response = session.get(CONTROL_URL, timeout=5)
        response.raise_for_status()
        if response.json().get("now") != tag:
            raise ProxyRuntimeError("Proxy selector did not accept the requested node")


def route_is_reachable(proxy_url=PROXY_URL):
    """Check the actual HTTPS target, not just an unrelated IP lookup service.

    Cloudflare's HTTP 403 challenge still proves that the tunnel works; the
    existing browser flow handles the challenge. Reject proxy auth failures,
    rate limits, server errors, TLS failures and connection timeouts.
    """
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.get(
                TARGET_URL, proxies={"http": proxy_url, "https": proxy_url},
                timeout=(5, 12), allow_redirects=True,
            )
            return response.status_code < 500 and response.status_code not in (407, 429)
        except requests.RequestException:
            return False


def get_exit_ip(proxy_url, timeout=6):
    with requests.Session() as session:
        session.trust_env = False
        for url in CHECK_URLS:
            try:
                response = session.get(
                    url, proxies={"http": proxy_url, "https": proxy_url},
                    timeout=timeout,
                )
                response.raise_for_status()
                return str(ipaddress.ip_address(response.text.strip()))
            except (requests.RequestException, ValueError):
                continue
    return None


if __name__ == "__main__":
    try:
        start_proxy()
    except Exception as exc:
        print(f"Proxy startup failed: {type(exc).__name__}.", flush=True)
        raise SystemExit(1)
