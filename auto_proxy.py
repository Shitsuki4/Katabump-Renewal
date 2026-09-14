#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare a validated primary or standby proxy pool.

Subscription formats: sing-box JSON, Clash YAML, plain or base64 share links.
Bad entries are quarantined; metadata APIs affect ranking, never availability.
Standby HTTP/SOCKS credentials are read only from FALLBACK_PROXIES at runtime.
"""

import argparse
import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request

from fallback_proxy import parse_fallback_proxies
from proxy_config import (
    ProxyConfigurationError, sanitize_outbound, validate_nodes,
    write_private_json,
)
from proxy_runtime import (
    get_exit_ip, spawn_singbox, stop_process, wait_for_listener,
)
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, unquote

CLASH_API_PORT = 9099
ASN_TIMEOUT = 6
MAX_POOL = 25
# Up to 80 free-tier queries per day at the default six-hour schedule,
# leaving room for a manual run. Unchecked exits still participate.
PURITY_CHECK_LIMIT = 20
# Exit-kind ranking (lower is better) used to order the pool.
KIND_SCORE = {"residential": 0, "isp": 1, "unknown": 2, "datacenter": 3}
# Checked pool order is written here; main.py pins one node per retry attempt
# via the Clash API instead of relying on urltest's latency-only choice.
RANKED_POOL_FILE = "ranked_pool.json"

SKIP_KEYWORDS = ["剩余流量", "距离下次", "套餐到期", "流量剩余", "重置剩余"]

# ==========================================================================
# Format sniffing + parsing
# ==========================================================================

def _strip(s):
    return s.strip().lstrip("﻿")

class SubscriptionError(RuntimeError):
    pass


def fetch_subscription(url):
    """Retry transient errors, with bounded downloads and credential-safe logs."""
    if urlparse(url).scheme not in ("http", "https"):
        raise SubscriptionError("Subscription URLs must use HTTP or HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": "sing-box/1.13"})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise SubscriptionError("Subscription exceeds the 8 MiB limit")
            raw = _strip(data.decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException) as exc:
            status = getattr(exc, "code", None)
            retryable = status is None or status == 429 or status >= 500
            print(f"Subscription fetch {attempt}/3 failed ({type(exc).__name__}).", flush=True)
            if attempt == 3 or not retryable:
                raise SubscriptionError("Subscription fetch failed") from None
            time.sleep(2 ** attempt)
    if raw.startswith("{"):
        return _from_singbox(raw)
    if re.search(r"^proxies\s*:", raw, re.MULTILINE):
        return _from_clash(raw)
    return _from_base64(raw)


def _from_singbox(raw):
    config = json.loads(raw)
    nodes = config.get("outbounds", []) if isinstance(config, dict) else []
    return [("singbox", node) for node in nodes
            if isinstance(node, dict) and node.get("server") and node.get("server_port")]


def _from_clash(raw):
    import yaml
    config = yaml.safe_load(raw)
    nodes = config.get("proxies", []) if isinstance(config, dict) else []
    return [("clash", node) for node in nodes
            if isinstance(node, dict) and node.get("server") and node.get("port")]


def _from_base64(raw):
    # Do not strip newlines from a plaintext list: that concatenated every
    # share link into one malformed node in the previous implementation.
    if "://" not in raw:
        encoded = re.sub(r"\s+", "", raw)
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeError):
            return []
    nodes = []
    for line in raw.splitlines():
        node = parse_share_link(line.strip())
        if node:
            nodes.append(("link", node))
    return nodes


def _normalize_node_item(item, index):
    source, node = item
    if not isinstance(node, dict):
        return f"candidate-{index}", None
    outbound = node if source == "singbox" else to_outbound(node, f"node-{index}")
    # Subscription-provided display names can themselves contain passwords.
    return f"candidate-{index}", sanitize_outbound(outbound)


def normalize_nodes(items):
    nodes = []
    for index, item in enumerate(items, 1):
        try:
            name = str(item[1].get("name") or item[1].get("tag") or "")
            if any(word in name for word in SKIP_KEYWORDS):
                continue
            name, outbound = _normalize_node_item(item, index)
            if outbound:
                nodes.append((name, outbound))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            continue
    print(f"Parsed {len(items)} entries; {len(nodes)} supported candidates.", flush=True)
    return nodes


def write_pool(scored, source):
    if not scored:
        raise ProxyConfigurationError("No usable nodes for this source")
    nodes = validate_nodes([(item[0], item[1]) for item in scored],
                           path="config.json", probe=False)
    metadata = {item[0]: item for item in scored}
    pool = []
    for index, (name, _) in enumerate(nodes, 1):
        item = metadata[name]
        pool.append({"tag": f"node-{index}", "name": f"{source}-{index}",
                     "ip": item[2], "kind": item[3], "risk": item[5], "source": source})
    write_private_json(RANKED_POOL_FILE, pool)
    print(f"Prepared {len(pool)} validated nodes for {source}.", flush=True)
    return pool


def write_single_node_config(items, source="proxy_url"):
    nodes = normalize_nodes(items)
    return write_pool([(name, ob, None, "unknown", "", None) for name, ob in nodes], source)


# --- share-link parsers (produce clash-ish dicts) ---

def parse_share_link(link):
    scheme = link.split("://")[0].lower()
    try:
        if scheme == "vmess":
            return _parse_vmess_link(link)
        parsed = urlparse(link)
        params = parse_qs(parsed.query)
        if scheme == "vless":
            return _parse_vless_link(parsed, params)
        if scheme in ("hy2", "hysteria2"):
            return _parse_hy2_link(parsed, params)
        if scheme == "trojan":
            return _parse_trojan_link(parsed, params)
        if scheme == "anytls":
            return _parse_anytls_link(parsed, params)
        if scheme == "tuic":
            return _parse_tuic_link(parsed, params)
        if scheme in ("ss", "shadowsocks"):
            return _parse_ss_link(link, parsed)
        if scheme in ("socks5", "socks5h", "socks"):
            return _parse_socks_link(parsed)
    except Exception:
        return None
    return None

def _name(link):
    return unquote(urlparse(link).fragment) or "unnamed-node"

def _parse_vmess_link(link):
    enc = link[len("vmess://"):]
    enc += "=" * (-len(enc) % 4)
    cfg = json.loads(base64.b64decode(enc).decode("utf-8"))
    return {"name": cfg.get("ps", cfg.get("add", "")),
            "type": "vmess", "server": cfg.get("add", ""), "port": int(cfg.get("port", 443)),
            "uuid": cfg.get("id", ""), "alterId": int(cfg.get("aid", 0)), "cipher": cfg.get("scy", "auto"),
            "tls": cfg.get("tls") == "tls", "sni": cfg.get("sni", cfg.get("host", "")),
            "network": cfg.get("net", "tcp"),
            "ws-opts": ({"path": cfg.get("path", "/"), "headers": {"Host": cfg.get("host", "")}}
                        if cfg.get("net") == "ws" else None)}

def _parse_vless_link(p, q):
    n = {"name": _name_str(p), "type": "vless", "server": p.hostname, "port": p.port or 443,
         "uuid": p.username, "flow": q.get("flow", [""])[0],
         "client-fingerprint": q.get("fp", [""])[0], "network": q.get("type", ["tcp"])[0]}
    sec = q.get("security", [""])[0]
    if sec in ("tls", "reality"):
        n["tls"] = True
        n["servername"] = q.get("sni", [""])[0]
        if q.get("insecure", ["0"])[0] == "1": n["skip-cert-verify"] = True
        if sec == "reality":
            n["reality-opts"] = {"public-key": q.get("pbk", [""])[0], "short-id": q.get("sid", [""])[0]}
    if n["network"] in ("ws", "httpupgrade"):
        n["ws-opts"] = {"path": q.get("path", ["/"])[0], "headers": {"Host": q.get("host", [""])[0]}}
    if n["network"] == "grpc":
        n["grpc-opts"] = {"grpc-service-name": q.get("serviceName", [""])[0]}
    return n

def _name_str(p):
    return unquote(p.fragment) or f"{p.hostname}:{p.port}"

def _parse_hy2_link(p, q):
    return {"name": _name_str(p), "type": "hysteria2", "server": p.hostname,
            "port": p.port or 443, "password": unquote(p.username or ""),
            "sni": q.get("sni", [""])[0], "skip-cert-verify": q.get("insecure", ["0"])[0] == "1",
            "obfs": q.get("obfs", [""])[0] or None,
            "obfs-password": q.get("obfs-password", [""])[0]}

def _parse_trojan_link(p, q):
    return {"name": _name_str(p), "type": "trojan", "server": p.hostname,
            "port": p.port or 443, "password": unquote(p.username or ""),
            "sni": q.get("sni", [""])[0], "skip-cert-verify": q.get("insecure", ["0"])[0] == "1",
            "network": q.get("type", ["tcp"])[0],
            "ws-opts": ({"path": unquote(q.get("path", ["/"])[0]), "headers": {"Host": q.get("host", [""])[0]}}
                        if q.get("type", [""])[0] == "ws" else None)}

def _parse_anytls_link(p, q):
    return {"name": _name_str(p), "type": "anytls", "server": p.hostname,
            "port": p.port or 443, "password": unquote(p.username or ""),
            "sni": q.get("sni", [""])[0], "skip-cert-verify": q.get("insecure", ["0"])[0] == "1",
            "client-fingerprint": q.get("fp", [""])[0]}

def _parse_tuic_link(p, q):
    up = unquote(p.username or ""); pp = unquote(p.password or "")
    uuid = up; pwd = pp
    if ":" in up and not pp:
        uuid, pwd = up.split(":", 1)
    return {"name": _name_str(p), "type": "tuic", "server": p.hostname,
            "port": p.port or 443, "uuid": uuid, "password": pwd,
            "sni": q.get("sni", [""])[0], "skip-cert-verify": q.get("insecure", ["0"])[0] == "1"}

def _parse_ss_link(link, p):
    # SIP002: encoded userinfo; legacy: the entire method:password@host:port.
    body = link.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0]
    if "@" not in body:
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        credentials, endpoint = decoded.rsplit("@", 1)
        p = urlparse("ss://" + endpoint)
    elif p.password is not None:
        credentials = unquote(p.username or "") + ":" + unquote(p.password)
    else:
        encoded = unquote(p.username or "")
        credentials = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    method, password = credentials.split(":", 1)
    return {"name": _name(link), "type": "ss", "server": p.hostname,
            "port": p.port or 8388, "cipher": method, "password": password}

def _parse_socks_link(p):
    return {"name": _name_str(p), "type": "socks5", "server": p.hostname,
            "port": p.port or 1080, "username": unquote(p.username or ""),
            "password": unquote(p.password or "")}

# ==========================================================================
# node dict -> sing-box outbound
# ==========================================================================

def _tls(n, security):
    tls = {"enabled": True}
    sni = n.get("servername") or n.get("sni", "")
    if sni: tls["server_name"] = sni
    fp = n.get("client-fingerprint", "")
    if fp: tls["utls"] = {"enabled": True, "fingerprint": fp}
    if str(n.get("skip-cert-verify", "false")).lower() == "true": tls["insecure"] = True
    ro = n.get("reality-opts") or {}
    if security == "reality":
        tls["reality"] = {"enabled": True}
        if ro.get("public-key"): tls["reality"]["public_key"] = ro["public-key"]
        if ro.get("short-id"): tls["reality"]["short_id"] = ro["short-id"]
    return tls

def to_outbound(n, tag):
    """Convert one Clash/share-link entry, preserving TLS and transports."""
    kind = n.get("type")
    port = n.get("port") or n.get("server_port")
    if not (n.get("server") and port):
        return None
    base = {"server": n["server"], "server_port": int(port), "tag": tag}
    if kind in ("ss", "shadowsocks"):
        return {"type": "shadowsocks", **base,
                "method": n.get("cipher") or n.get("method"), "password": n.get("password", "")}
    if kind in ("socks", "socks5", "http"):
        ob = {"type": "http" if kind == "http" else "socks", **base}
        if kind != "http":
            ob["version"] = "5"
        if n.get("username"):
            ob["username"] = n["username"]
        if n.get("password"):
            ob["password"] = n["password"]
        if kind == "http" and n.get("tls"):
            ob["tls"] = _tls(n, "tls")
        return ob
    if kind not in {"vless", "vmess", "trojan", "hysteria2", "tuic", "anytls"}:
        return None
    ob = {"type": kind, **base}
    if kind in {"vless", "vmess", "tuic"}:
        ob["uuid"] = n.get("uuid", "")
    if kind in {"trojan", "hysteria2", "tuic", "anytls"}:
        ob["password"] = n.get("password", "")
    if kind == "vless" and n.get("flow"):
        ob["flow"] = n["flow"]
    if kind == "vmess":
        ob.update(alter_id=int(n.get("alterId", 0)), security=n.get("cipher", "auto"))
    security = "reality" if n.get("reality-opts") else "tls"
    if n.get("tls") or n.get("reality-opts") or kind in {"trojan", "hysteria2", "tuic", "anytls"}:
        ob["tls"] = _tls(n, security)
    if kind == "hysteria2":
        obfs = n.get("obfs") or n.get("obfs-param")
        if obfs:
            ob["obfs"] = {"type": "salamander", "password": n.get("obfs-password", obfs)}
    if kind in {"vless", "vmess", "trojan"}:
        network = n.get("network", "tcp")
        if network in ("ws", "httpupgrade"):
            opts = n.get("ws-opts") or {}
            transport = {"type": network, "path": opts.get("path", "/")}
            host = (opts.get("headers") or {}).get("Host")
            if host:
                if network == "ws":
                    transport["headers"] = {"Host": host}
                else:
                    transport["host"] = host
            ob["transport"] = transport
        elif network == "grpc":
            opts = n.get("grpc-opts") or {}
            ob["transport"] = {"type": "grpc", "service_name": opts.get("grpc-service-name", "")}
        elif network not in ("tcp", ""):
            # Never silently turn an unsupported transport into plain TCP.
            return None
    return ob

# ==========================================================================
# IP classification
# ==========================================================================

def classify_ip(ip):
    try:
        req = urllib.request.Request(
            f"http://ip-api.com/json/{ip}?fields=status,isp,org,as",
            headers={"User-Agent": "curl/8"})
        d = json.loads(urllib.request.urlopen(req, timeout=ASN_TIMEOUT).read().decode())
        if d.get("status") != "success": return "unknown", ""
        org = (d.get("org") or d.get("isp") or "").lower()
        asname = (d.get("as") or "").lower()
        dc = ["amazon", "aws", "microsoft", "azure", "google", "gcp", "oracle",
              "digitalocean", "vultr", "linode", "akamai", "cloudflare", "hetzner",
              "ovh", "leaseweb", "contabo", "choopa", "m247", "tencent", "alibaba",
              "huawei", "github", "fastly", "upyun"]
        if any(s in org or s in asname for s in dc):
            return "datacenter", d.get("org") or d.get("isp") or ""
        res = ["telecom", "telekom", "verizon", "comcast", "at&t", "charter",
               "spectrum", "cox", "orange", "telefonica", "deutsche", "vodafone",
               "rogers", "bell", "telia", "telenor", "broadband", "cable", "dsl", "fiber"]
        if any(s in org or s in asname for s in res):
            return "residential", d.get("org") or d.get("isp") or ""
        return "isp", d.get("org") or d.get("isp") or ""
    except Exception:
        return "unknown", ""

# ==========================================================================
# IP purity (risk) check via proxycheck.io free tier (no API key, 100/day)
# ==========================================================================

def purity_risk(ip):
    """Return proxycheck.io risk score (0-100) for an IP, or None on failure.

    `proxy: yes` is expected for every node (they ARE proxies), so we only
    look at the risk score and recent attack history — those capture the
    "dirty" exits that Cloudflare Turnstile refuses to let through. Queried
    from the runner directly, not through the node."""
    try:
        req = urllib.request.Request(
            f"https://proxycheck.io/v2/{ip}?vpn=3&risk=1&seen=1&days=7&tag=renew",
            headers={"User-Agent": "curl/8"})
        d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
        if d.get("status") != "ok":
            return None
        info = d.get(ip) or {}
        if "risk" not in info:
            return None
        try:
            return max(0, min(100, int(info["risk"])))
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


def rank_nodes_by_purity(scored):
    """Order reachable nodes by exit quality, then by proxycheck risk."""
    return sorted(
        scored,
        key=lambda node: (
            KIND_SCORE.get(node[3], 2),
            node[5] if node[5] is not None else 50,
        ),
    )

# ==========================================================================
# Main: parallel probe via one sing-box + clash_api
# ==========================================================================

def _probe_once(sub_url):
    print("Fetching primary subscription (URL redacted).", flush=True)
    nodes = normalize_nodes(fetch_subscription(sub_url))
    if not nodes:
        return None
    nodes = validate_nodes(nodes, path="probe_cfg.json", probe=True)
    tag_map = [(f"node-{i}", name) for i, (name, _) in enumerate(nodes, 1)]
    process = spawn_singbox("probe_cfg.json", "probe_sb.log")
    alive = set()
    try:
        wait_for_listener(process, CLASH_API_PORT)
        # Local control requests must not inherit HTTP_PROXY from the host.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for delay in (8, 12, 15):
            time.sleep(delay)
            if process.poll() is not None:
                raise ProxyConfigurationError("Subscription probe process exited")
            try:
                with opener.open(f"http://127.0.0.1:{CLASH_API_PORT}/proxies", timeout=5) as response:
                    data = json.load(response)
                for tag, info in data.get("proxies", {}).items():
                    history = info.get("history") or []
                    if tag.startswith("node-") and history and history[-1].get("delay", 0) > 0:
                        alive.add(tag)
            except (OSError, ValueError):
                continue
            if alive:
                break
    finally:
        stop_process(process)
    print(f"Parallel probe: {len(alive)}/{len(nodes)} reachable.", flush=True)
    if not alive:
        return None
    alive_nodes = [nodes[i] for i, (tag, _) in enumerate(tag_map) if tag in alive]
    print("Fetching exit IPs through one bounded probe process...", flush=True)
    ips_list = _fetch_ips_parallel(alive_nodes)
    # classify_ip does one HTTP call each; run them concurrently too.
    ips = list(dict.fromkeys(ip for ip in ips_list if ip))
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(ips)))) as ex:
        kinds = list(ex.map(classify_ip, ips))
    kind_by_ip = dict(zip(ips, kinds))
    reachable = []
    for (name, ob), ip in zip(alive_nodes, ips_list):
        if ip:
            kind, org = kind_by_ip[ip]
            print(f"  {kind:12s} {ip:16s} {name[:34]} ({org[:24]})")
            reachable.append((name, ob, ip, kind, org))
        else:
            print(f"  {'no-ip':12s} {'-':16s} {name[:34]}")
    if not reachable:
        print("\n❌ Alive nodes could not fetch exit IP."); return None

    reachable.sort(key=lambda x: KIND_SCORE.get(x[3], 2))

    # De-duplicate by exit IP: this airport re-uses the same exit for many
    # node entries, and a pool full of aliases of one IP gives retries zero
    # diversity (observed: 3 consecutive attempts all exited 23.237.50.27).
    seen_ips = set()
    deduped = []
    for r in reachable:
        if r[2] in seen_ips:
            continue
        seen_ips.add(r[2])
        deduped.append(r)
    print(f"\n=== {len(reachable)} reachable, {len(deduped)} unique exit IPs ===")

    # Purity check on unique exit IPs. proxycheck free tier: 100/day, so cap.
    check_ips = [r[2] for r in deduped[:PURITY_CHECK_LIMIT]]
    risks = {}
    if check_ips:
        print(f"Purity check (proxycheck.io) on {len(check_ips)} unique exit IPs...")
        with ThreadPoolExecutor(max_workers=min(8, len(check_ips))) as ex:
            risks = dict(zip(check_ips, ex.map(purity_risk, check_ips)))
        unchecked = sum(1 for v in risks.values() if v is None)
        if unchecked == len(risks):
            print("  ⚠️ purity API unreachable/limit hit — ranking by IP type only")

    scored = []
    for name, ob, ip, kind, org in deduped:
        risk = risks.get(ip)
        scored.append((name, ob, ip, kind, org, risk))
        print(f"  {kind:12s} risk={'??' if risk is None else f'{risk:3d}'}  "
              f"{ip:16s} {name[:30]} ({org[:22]})")
    return scored


def prepare_source(source):
    """Prepare exactly one tier. The renewal runner decides when to fail over."""
    if source == "fallback":
        nodes = parse_fallback_proxies(os.environ.get("FALLBACK_PROXIES", ""))
        return write_pool([(name, ob, None, "unknown", "", None) for name, ob in nodes], source)
    if source == "proxy_url":
        url = os.environ.get("PROXY_URL", "").strip()
        if not url:
            raise ProxyConfigurationError("PROXY_URL is empty")
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.username is not None:
            nodes = parse_fallback_proxies(url)
            return write_pool([(name, ob, None, "unknown", "", None) for name, ob in nodes], source)
        if parsed.scheme in {"http", "https"}:
            items = fetch_subscription(url)
        else:
            node = parse_share_link(url)
            items = [("link", node)] if node else []
        return write_single_node_config(items, source)
    if source != "subscription":
        raise ProxyConfigurationError("Unknown proxy source")
    url = os.environ.get("SUB_URL", "").strip()
    if not url:
        raise ProxyConfigurationError("SUB_URL is empty")
    scored = None
    for attempt in range(2):
        scored = _probe_once(url)
        if scored:
            break
        if attempt == 0:
            print("No reachable subscription nodes; retrying once in 5 seconds.", flush=True)
            time.sleep(5)
    if not scored:
        raise ProxyConfigurationError("No reachable nodes after two probe passes")
    ranked = rank_nodes_by_purity(scored)
    return write_pool(ranked[:MAX_POOL], source)


def _fetch_ips_parallel(nodes):
    """One sing-box process, dedicated inbound per node, bounded HTTP threads.

    The old implementation started a process per live node (often hundreds)
    and leaked child processes/configurations if any operation raised.
    """
    if not nodes:
        return []
    outbounds, inbounds, rules = [], [], []
    for index, (_, outbound) in enumerate(nodes):
        tag = f"exit-{index}"
        ob = dict(outbound, tag=tag)
        outbounds.append(ob)
        inbounds.append({"type": "http", "tag": tag, "listen": "127.0.0.1",
                         "listen_port": 18080 + index})
        rules.append({"inbound": [tag], "outbound": tag})
    config = {"log": {"level": "warn"}, "inbounds": inbounds,
              "outbounds": outbounds, "route": {"rules": rules, "final": "exit-0"}}
    write_private_json("ip_probe_cfg.json", config)
    process = spawn_singbox("ip_probe_cfg.json", "probe_sb.log")
    try:
        wait_for_listener(process, 18080)
        def fetch(index):
            return get_exit_ip(f"http://127.0.0.1:{18080 + index}")
        with ThreadPoolExecutor(max_workers=min(12, len(nodes))) as executor:
            return list(executor.map(fetch, range(len(nodes))))
    finally:
        stop_process(process)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-url", action="store_true")
    parser.add_argument("--source", choices=("subscription", "proxy_url", "fallback"))
    args = parser.parse_args(argv)
    if args.source:
        sources = [args.source]
    elif args.proxy_url or os.environ.get("TEST_PROXY_URL_MODE") == "1":
        sources = ["proxy_url"]
    else:
        sources = [source for source, key in (("subscription", "SUB_URL"),
                    ("proxy_url", "PROXY_URL"), ("fallback", "FALLBACK_PROXIES"))
                   if os.environ.get(key, "").strip()]
    if not sources:
        print("No proxy source configured.", flush=True)
        return 2
    for source in sources:
        try:
            prepare_source(source)
            return 0
        except Exception as exc:
            print(f"Proxy source {source} failed ({type(exc).__name__}); trying next source.", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
