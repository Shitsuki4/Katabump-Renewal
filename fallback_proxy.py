"""Parse authenticated standby proxies without exposing their credentials."""

import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit


class FallbackProxyError(ValueError):
    pass


def _parse_entry(entry):
    if "://" not in entry:
        match = re.fullmatch(r"(\[[^\]]+\]|[^:\s/]+):(\d+)(?::([^:]+):(.+))?", entry)
        if not match:
            raise ValueError("Use host:port:username:password or a proxy URL")
        host, port, username, password = match.groups()
        host = host.strip("[]")
        scheme = "http"
    else:
        parsed = urlsplit(entry)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks", "socks5", "socks5h"}:
            raise ValueError("Unsupported proxy scheme")
        if parsed.path not in ("", "/") or parsed.query:
            raise ValueError("Proxy URLs must not contain a path or query")
        host = parsed.hostname
        port = parsed.port or (1080 if scheme.startswith("socks") else 443 if scheme == "https" else 80)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
    if not host or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError("Invalid proxy host")
    if ":" in host:
        ipaddress.IPv6Address(host)
    if not 1 <= int(port) <= 65535:
        raise ValueError("Invalid proxy port")
    if bool(username) != bool(password):
        raise ValueError("Proxy username and password must be provided together")
    outbound = {"type": "socks" if scheme.startswith("socks") else "http",
                "server": host, "server_port": int(port)}
    if scheme.startswith("socks"):
        outbound["version"] = "5"
    if scheme == "https":
        outbound["tls"] = {"enabled": True, "server_name": host}
    if username:
        outbound.update(username=username, password=password)
    return outbound


def parse_fallback_proxies(raw):
    """Accept multiline host:port:user:pass, URL lines, or a JSON string array.

    Exact duplicates are removed, preserving the user's preferred order.
    Errors report entry numbers, never proxy addresses or credentials.
    """
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            entries = json.loads(raw)
        except (ValueError, TypeError):
            # A bracketed IPv6 address is a plain entry, not JSON.
            if re.match(r"^\[[0-9a-fA-F:]+\]:\d+", raw):
                entries = raw.splitlines()
            else:
                raise FallbackProxyError("FALLBACK_PROXIES is not a valid JSON array") from None
        if not isinstance(entries, list):
            raise FallbackProxyError("FALLBACK_PROXIES must be a string array")
    else:
        entries = raw.splitlines()
    nodes, seen = [], set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, str):
            raise FallbackProxyError(f"FALLBACK_PROXIES entry {index} must be a string")
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            outbound = _parse_entry(entry)
        except (ValueError, TypeError):
            raise FallbackProxyError(f"FALLBACK_PROXIES entry {index} is invalid") from None
        key = json.dumps(outbound, sort_keys=True)
        if key not in seen:
            seen.add(key)
            nodes.append((f"fallback-{len(nodes) + 1:02d}", outbound))
    if not nodes:
        raise FallbackProxyError("FALLBACK_PROXIES contains no proxy entries")
    if len(nodes) > 50:
        raise FallbackProxyError("FALLBACK_PROXIES supports at most 50 unique entries")
    return nodes
