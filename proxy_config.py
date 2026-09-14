"""Build sing-box configurations and quarantine invalid subscription entries.

Never print a raw outbound or sing-box stderr: both can contain credentials.
"""

import copy
import json
import os
import re
import subprocess
import time

from proxy_runtime import singbox_binary


PROBE_URL = "https://www.gstatic.com/generate_204"
KNOWN_FINGERPRINTS = {
    "", "chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq",
    "random", "randomized", "chrome_psk", "chrome_pske", "chrome_padding_psk",
    "chrome_padding_pske", "chrome_psk_shuffle", "chrome_pades_padding_psk",
    "chrome_final_psk", "chrome_final_pske",
}
SUPPORTED_TYPES = {
    "http", "socks", "shadowsocks", "vmess", "vless", "trojan", "hysteria",
    "hysteria2", "tuic", "anytls", "shadowtls",
}


class ProxyConfigurationError(RuntimeError):
    pass


def write_private_json(path, data):
    """Runtime files contain secrets; create them with owner-only permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def sanitize_outbound(outbound):
    """Normalize supported aliases without mutating subscription objects."""
    if not isinstance(outbound, dict):
        return None
    ob = copy.deepcopy(outbound)
    ob.pop("name", None)
    ob.pop("tag", None)
    if ob.get("type") not in SUPPORTED_TYPES:
        return None
    if not isinstance(ob.get("server"), str) or not ob["server"].strip():
        return None
    try:
        if isinstance(ob.get("server_port"), bool):
            return None
        ob["server_port"] = int(ob["server_port"])
        if not 1 <= ob["server_port"] <= 65535:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    if ob.get("detour"):
        # Imported group references cannot survive independent node selection.
        return None
    if ob["type"] == "vless":
        if ob.get("flow") == "xtls-rprx-vision-udp443":
            ob["flow"] = "xtls-rprx-vision"
        if ob.get("flow", "") not in ("", "xtls-rprx-vision"):
            return None
    transport = ob.get("transport")
    if transport is not None:
        if not isinstance(transport, dict) or transport.get("type") not in {
            "ws", "grpc", "http", "httpupgrade", "quic",
        }:
            return None
    tls = ob.get("tls")
    if tls is not None:
        if not isinstance(tls, dict):
            return None
        tls.setdefault("enabled", True)
        utls = tls.get("utls")
        if utls is not None:
            if not isinstance(utls, dict):
                return None
            if utls.get("fingerprint", "") not in KNOWN_FINGERPRINTS:
                return None
            utls.setdefault("enabled", True)
    return ob


def build_config(nodes, *, probe=False):
    if not nodes:
        raise ProxyConfigurationError("No supported proxy nodes remain")
    outbounds = []
    for index, (_, outbound) in enumerate(nodes, 1):
        ob = copy.deepcopy(outbound)
        ob["tag"] = f"node-{index}"
        outbounds.append(ob)
    tags = [ob["tag"] for ob in outbounds]
    if probe:
        group = {"type": "urltest", "tag": "proxy", "outbounds": tags,
                 "url": PROBE_URL, "interval": "30s"}
    else:
        # No latency-based auto group: retries must use the requested node,
        # and standby proxies must not be contacted until their turn.
        group = {"type": "selector", "tag": "proxy", "outbounds": tags,
                 "default": tags[0], "interrupt_exist_connections": True}
    outbounds.append(group)
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{"type": "http", "tag": "http-in", "listen": "127.0.0.1",
                      "listen_port": 8080}],
        "outbounds": outbounds,
        "route": {"final": "proxy"},
        "experimental": {"clash_api": {"external_controller": "127.0.0.1:9099"}},
    }


def validate_nodes(nodes, *, path="probe_cfg.json", probe=True):
    """Keep good entries when one invalid node would reject the whole pool.

    Prefer sing-box's outbound index. If no index is available, bisect only
    the failing subsets. Bound both subprocess count and wall-clock time.
    The final file and returned node list always describe the same nodes.
    """
    deadline = time.monotonic() + 120
    checks = 0
    removed = 0

    def check(group):
        nonlocal checks
        remaining = deadline - time.monotonic()
        if checks >= 128 or remaining <= 0:
            raise ProxyConfigurationError("Proxy validation budget exhausted")
        checks += 1
        write_private_json(path, build_config(group, probe=probe))
        result = subprocess.run(
            [singbox_binary(), "check", "-c", str(path)], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=min(15, remaining),
        )
        return result.returncode == 0, result.stderr + result.stdout

    def isolate(group):
        nonlocal removed
        if not group:
            return []
        ok, error = check(group)
        if ok:
            return group
        match = re.search(r"outbounds?\[(\d+)\]", error)
        if match and int(match.group(1)) < len(group):
            bad_index = int(match.group(1))
            removed += 1
            print(f"Quarantining incompatible outbound #{bad_index + 1}.", flush=True)
            return isolate(group[:bad_index] + group[bad_index + 1:])
        if len(group) == 1:
            removed += 1
            return []
        middle = len(group) // 2
        return isolate(group[:middle]) + isolate(group[middle:])

    usable = isolate(list(nodes))
    if not usable:
        raise ProxyConfigurationError("sing-box rejected all candidate nodes")
    # Bisection leaves its last subset on disk; always check the merged pool.
    if removed:
        ok, _ = check(usable)
        if not ok:
            raise ProxyConfigurationError("Merged proxy configuration is invalid")
        print(f"Isolated {removed} incompatible entries; kept {len(usable)} nodes.", flush=True)
    return usable
