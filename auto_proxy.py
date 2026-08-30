#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_proxy.py — Auto-select working proxy nodes from ANY subscription link.

Adapts to multiple subscription formats:
  - sing-box JSON config (starts with '{'): read outbounds directly.
  - Clash YAML (proxies:): parse with PyYAML.
  - base64 node-link list (vmess://, vless://, hy2://, ...): decode + parse each.

Fast parallel liveness test (seconds, not minutes):
  - Build ONE sing-box config with all node outbounds + a urltest group +
    an external clash_api. Start it once; sing-box probes all nodes in
    parallel. Read /proxies via the clash API to learn which are alive.
  - Only for alive nodes: fetch exit IP through each node (temporary HTTP
    inbound per query) and classify residential/ISP/datacenter via ip-api.com.

Output:
  config.json — sing-box config with HTTP inbound on 127.0.0.1:8080 and a
  urltest POOL of the alive, best-scored nodes wrapped in a selector "proxy";
  main.py pins a specific purity-ranked node per retry via the Clash API.
  ranked_pool.json — the pool order (tag/name/ip/kind/risk) main.py follows.

  If no low-risk exit survives two probe passes (dirty = proxycheck risk>=66
  or datacenter), the script exits 3 without writing a pool: Cloudflare
  refuses to render Turnstile on such exits, so browser renewal is doomed.

Env: SUB_URL (required).
"""

import os, sys, json, time, base64, subprocess, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse, parse_qs, unquote

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8080
CLASH_API_PORT = 9099
TEST_URL = "https://api.ip.sb/ip"
PROBE_URL = "https://www.gstatic.com/generate_204"
NODE_TIMEOUT = 8
ASN_TIMEOUT = 6
MAX_POOL = 25
# Cap proxycheck.io free tier usage (100/day no key). We only purity-check
# this many unique exit IPs; anything beyond is left "unchecked" and ranked
# by IP type alone.
PURITY_CHECK_LIMIT = 30
# proxycheck risk score (0-100) at/above which a node is demoted. 66 is their
# "high risk" threshold; heavily-abused exits typically score 75+.
PURITY_RISK_REJECT = 66
# Exit-kind ranking (lower is better) used to order the pool.
KIND_SCORE = {"residential": 0, "isp": 1, "unknown": 2, "datacenter": 3}
# Checked pool order is written here; main.py pins one node per retry attempt
# via the Clash API instead of relying on urltest's latency-only choice.
RANKED_POOL_FILE = "ranked_pool.json"

SKIP_KEYWORDS = ["剩余流量", "距离下次", "套餐到期", "流量剩余", "重置剩余"]

# uTLS fingerprints sing-box 1.13.x actually knows. Subscriptions circulate
# made-up values like fp=unsafe; a single such node makes `sing-box check`
# reject the WHOLE probe config (run 31: 495 nodes lost to one bad one).
KNOWN_UTLS_FINGERPRINTS = {
    "", "chrome", "firefox", "safari", "ios", "android", "edge", "360",
    "qq", "random", "randomized", "chrome_psk", "chrome_pske",
    "chrome_padding_psk", "chrome_padding_pske", "chrome_psk_shuffle",
    "chrome_pades_padding_psk", "chrome_final_psk", "chrome_final_pske",
}

# ==========================================================================
# Format sniffing + parsing
# ==========================================================================

def _strip(s):
    return s.strip().lstrip("﻿")

def fetch_subscription(url):
    """Return a list of node dicts. Auto-detects sing-box json / clash yaml /
    base64 link list by content sniffing, regardless of query (?clash/?singbox/...).
    Sends a neutral UA so the server returns its default format; a ?singbox /
    ?base64 query in the URL takes precedence at the converter.

    Retries up to 3 times on transient network errors (timeout, connection
    reset, 5xx). 15s per attempt is plenty for a healthy subscription host;
    a 40s hang (the previous default) is almost always a stuck peer we should
    abort early and retry rather than wait out."""
    req = urllib.request.Request(url, headers={"User-Agent": "sing-box/1.10"})
    last_err = None
    for attempt in range(1, 4):
        try:
            raw = _strip(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            if attempt > 1:
                print(f"  ✓ fetch_subscription succeeded on attempt {attempt}", flush=True)
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < 3:
                wait = 2 ** attempt  # 2s, 4s
                print(f"  ⚠ fetch_subscription attempt {attempt}/3 failed: {e!r} — retrying in {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"  ✗ fetch_subscription failed after 3 attempts: {e!r}", flush=True)
    else:
        raise last_err

    # 1) sing-box JSON config
    if raw.startswith("{"):
        return _from_singbox(raw)
    # 2) Clash YAML
    if raw.startswith("mixed-port") or raw.startswith("port:") or "\nproxies:" in raw or raw.startswith("proxies:"):
        return _from_clash(raw)
    # 3) base64 link list
    return _from_base64(raw)

def _from_singbox(raw):
    """Extract node outbounds from a sing-box config JSON."""
    cfg = json.loads(raw)
    out = []
    for ob in cfg.get("outbounds", []):
        t = ob.get("type")
        if t in ("direct", "block", "dns", "selector", "urltest"):
            continue
        if not (ob.get("server") and ob.get("server_port")):
            continue
        # Normalize sing-box outbound -> clash-ish dict for to_outbound()
        # Easier: keep sing-box form directly. We detect by 'type'.
        ob2 = dict(ob)
        ob2["name"] = ob.get("tag", "")
        out.append(("singbox", ob2))
    return out

def _from_clash(raw):
    import yaml
    cfg = yaml.safe_load(raw)
    return [("clash", n) for n in cfg.get("proxies", [])
            if n.get("server") and n.get("port")
            and n.get("server") not in ("127.0.0.1", "localhost")]

def _from_base64(raw):
    """Decode base64 (possibly multi-chunk) and parse vmess/vless/hy2/trojan/tuic/anytls/ss/socks5 links."""
    raw = raw.replace(" ", "").replace("\n", "").replace("\r", "")
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
        if "://" in decoded:
            raw = decoded
    except Exception:
        pass
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if "://" not in line:
            continue
        node = parse_share_link(line)
        if node:
            out.append(("link", node))
    return out

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
        if scheme in ("socks5", "socks"):
            return _parse_socks_link(parsed)
    except Exception:
        return None
    return None

def _name(link):
    return unquote(urlparse(link).fragment) or link[:20]

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
    if n["network"] == "ws":
        n["ws-opts"] = {"path": unquote(q.get("path", ["/"])[0]), "headers": {"Host": q.get("host", [""])[0]}}
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
    # ss://base64(method:password)@host:port#name  OR  ss://method:password@host:port
    userinfo = p.username or ""
    if "@" in userinfo or not p.hostname:
        # legacy: whole thing base64
        enc = link[len("ss://"):].split("#")[0].split("?")[0]
        enc += "=" * (-len(enc) % 4)
        try:
            dec = base64.b64decode(enc).decode("utf-8")
            if "@" in dec:
                mp, hostport = dec.rsplit("@", 1)
                method, password = mp.split(":", 1)
                host, port = hostport.rsplit(":", 1)
                return {"name": _name(link), "type": "ss", "server": host, "port": int(port),
                        "cipher": method, "password": password}
        except Exception:
            pass
    method = unquote(userinfo.split(":")[0]) if ":" in userinfo else ""
    password = unquote(userinfo.split(":", 1)[1]) if ":" in userinfo else ""
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
    """Accept clash-ish dict OR sing-box outbound dict (marker 'singbox').
    Clash uses 'port'; sing-box uses 'server_port'. Normalize both."""
    t = n.get("type")
    port = n.get("port") or n.get("server_port")
    if not (n.get("server") and port):
        return None
    base = {"server": n["server"], "server_port": int(port), "tag": tag}
    if t == "vless":
        ob = {"type": "vless", **base, "uuid": n["uuid"]}
        if n.get("flow"): ob["flow"] = n["flow"]
        sec = "reality" if n.get("reality-opts") else ("tls" if n.get("tls") else "")
        if sec: ob["tls"] = _tls(n, sec)
        net = n.get("network", "tcp")
        if net == "ws":
            wso = n.get("ws-opts") or {}
            tr = {"type": "ws"}
            if wso.get("path"): tr["path"] = wso["path"]
            h = (wso.get("headers") or {}).get("Host", "")
            if h: tr["headers"] = {"Host": h}
            ob["transport"] = tr
        elif net == "grpc":
            tr = {"type": "grpc"}
            sn = (n.get("grpc-opts") or {}).get("grpc-service-name", "")
            if sn: tr["service_name"] = sn
            ob["transport"] = tr
        return ob
    if t == "hysteria2":
        ob = {"type": "hysteria2", **base, "password": n.get("password", "")}
        ob["tls"] = _tls(n, "tls")
        obfs = n.get("obfs") or n.get("obfs-param")
        if obfs: ob["obfs"] = {"type": "salamander", "password": n.get("obfs-password", obfs)}
        return ob
    if t == "trojan":
        ob = {"type": "trojan", **base, "password": n.get("password", "")}
        if n.get("tls") or n.get("sni"): ob["tls"] = _tls(n, "tls")
        return ob
    if t == "anytls":
        ob = {"type": "anytls", **base, "password": n.get("password", "")}
        ob["tls"] = _tls(n, "tls")
        return ob
    if t == "ss":
        return {"type": "shadowsocks", **base, "method": n.get("cipher"), "password": n.get("password", "")}
    if t == "vmess":
        ob = {"type": "vmess", **base, "uuid": n.get("uuid", ""),
              "alter_id": int(n.get("alterId", 0)), "security": n.get("cipher", "auto")}
        if n.get("tls"): ob["tls"] = _tls(n, "tls")
        net = n.get("network", "tcp")
        if net == "ws":
            wso = n.get("ws-opts") or {}
            tr = {"type": "ws"}
            if wso.get("path"): tr["path"] = wso["path"]
            h = (wso.get("headers") or {}).get("Host", "")
            if h: tr["headers"] = {"Host": h}
            ob["transport"] = tr
        return ob
    if t == "tuic":
        ob = {"type": "tuic", **base, "uuid": n.get("uuid", ""), "password": n.get("password", "")}
        ob["tls"] = _tls(n, "tls")
        return ob
    if t in ("socks5", "socks"):
        ob = {"type": "socks", **base, "version": "5"}
        if n.get("username"): ob["username"] = n["username"]
        if n.get("password"): ob["password"] = n["password"]
        return ob
    return None

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
            f"http://proxycheck.io/v2/{ip}?vpn=3&risk=1&seen=1&days=7&tag=renew",
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

# ==========================================================================
# Main: parallel probe via one sing-box + clash_api
# ==========================================================================

def _probe_once(sub_url):
    """One pass: fetch subscription -> parallel liveness probe -> exit-IP
    fetch -> classify -> purity check. Returns scored tuples
    (name, ob, ip, kind, org, risk), or None when nothing probed alive
    (transient failure the caller may retry)."""
    print(f"Fetching subscription: {sub_url}")
    items = fetch_subscription(sub_url)
    print(f"Parsed {len(items)} candidate nodes.\n")

    # Build (name, outbound) for every node; collect unique tags.
    nodes = []
    for src, n in items:
        name = n.get("name") or n.get("tag", "")
        if any(k in name for k in SKIP_KEYWORDS):
            continue
        if src == "singbox":
            # n IS already a sing-box outbound dict (has server/server_port/tls/transport).
            # Strip the tag (will be reassigned) and drop name-only keys.
            ob = dict(n)
            ob.pop("name", None)
            ob.pop("tag", None)
            # sanity: needs server + server_port
            if not (ob.get("server") and ob.get("server_port")):
                continue
            # CRITICAL: sing-box 1.13.16 nil-panics in TLS handshake when a
            # tls block lacks `enabled: true` (singbox-format nodes from many
            # converters only set server_name/utls). Without this fix the whole
            # sing-box process segfaults and ALL probes report 0 alive.
            tls = ob.get("tls")
            if isinstance(tls, dict) and tls.get("enabled") is None:
                tls["enabled"] = True
                ob["tls"] = tls
        else:
            ob = to_outbound(n, "proxy")
        if not ob:
            print(f"  skip: {name[:40]} (type={n.get('type')})")
            continue
        # Drop nodes whose transport type is unknown to this sing-box build
        # (xhttp / httpupgrade etc. need a newer sing-box). One bad outbound
        # would make sing-box refuse the whole config, killing the probe.
        tr = (ob.get("transport") or {}).get("type", "")
        if tr and tr not in ("ws", "grpc", "http", "quic", "h2mux"):
            continue
        # Same for unknown uTLS fingerprints (fp=unsafe is circulating).
        tls = ob.get("tls") or {}
        utls_fp = ((tls.get("utls") or {}).get("fingerprint") or "")
        if utls_fp not in KNOWN_UTLS_FINGERPRINTS:
            print(f"  skip: {name[:40]} (unknown uTLS fingerprint '{utls_fp}')")
            continue
        nodes.append((name, ob))
    print(f"{len(nodes)} nodes to probe in parallel.\n")

    if not nodes:
        print("❌ No usable nodes."); return None

    # Build ONE config: all outbounds as node-N + urltest + clash_api.
    outbounds = []
    tag_map = []  # tag -> name
    for i, (name, ob) in enumerate(nodes, 1):
        tag = f"node-{i}"
        ob = dict(ob); ob["tag"] = tag
        outbounds.append(ob); tag_map.append((tag, name))
    outbounds.append({"type": "urltest", "tag": "proxy",
                      "outbounds": [t for t, _ in tag_map],
                      "url": PROBE_URL, "interval": "30s"})
    outbounds.append({"type": "direct", "tag": "direct"})

    probe_cfg = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{"type": "http", "tag": "http-in",
                      "listen": "127.0.0.1", "listen_port": LISTEN_PORT}],
        "outbounds": outbounds,
        "route": {"final": "proxy"},
        "experimental": {"clash_api": {"external_controller": f"127.0.0.1:{CLASH_API_PORT}"}},
    }
    open("probe_cfg.json", "w").write(json.dumps(probe_cfg, ensure_ascii=False))

    # Validate config BEFORE running — sing-box check prints the exact error
    # (e.g. unknown transport / invalid field) that would otherwise kill the
    # process silently. We validate node-by-node isn't feasible, so check the
    # whole config; if it fails, progressively strip suspicious outbounds.
    print("Starting sing-box (parallel probe of all nodes)...")
    check = subprocess.run(["./sing-box", "check", "-c", "probe_cfg.json"],
                            capture_output=True, text=True, timeout=15)
    if check.returncode != 0:
        print(f"⚠️ sing-box check failed: {check.stderr.strip()[:300]}")
        # Try to identify the bad outbound by binary-stripping. Simple approach:
        # re-validate with only the first half, then narrow. For now, log + abort
        # so we can see the error; a re-run often succeeds with a fresh subscription.
        # As a fallback, try dropping nodes whose outbound has any non-standard keys.
        print("Attempting fallback: drop nodes with non-core outbound fields...")
        CORE = {"type", "tag", "server", "server_port", "uuid", "password",
                "method", "flow", "tls", "transport", "alter_id", "security",
                "username", "version", "obfs", "multihop"}
        cleaned = []
        for nm, ob in nodes:
            cob = {k: v for k, v in ob.items() if k in CORE}
            cleaned.append((nm, cob))
        outbounds2 = []
        for i, (nm, ob) in enumerate(cleaned, 1):
            ob = dict(ob); ob["tag"] = f"node-{i}"; outbounds2.append(ob)
        outbounds2.append({"type": "urltest", "tag": "proxy",
                           "outbounds": [f"node-{i}" for i in range(1, len(cleaned)+1)],
                           "url": PROBE_URL, "interval": "30s"})
        outbounds2.append({"type": "direct", "tag": "direct"})
        probe_cfg["outbounds"] = outbounds2
        open("probe_cfg.json", "w").write(json.dumps(probe_cfg, ensure_ascii=False))
        tag_map = [(f"node-{i}", nm) for i, (nm, _) in enumerate(cleaned, 1)]
        check2 = subprocess.run(["./sing-box", "check", "-c", "probe_cfg.json"],
                                capture_output=True, text=True, timeout=15)
        print(f"fallback check: rc={check2.returncode} {check2.stderr.strip()[:300]}")
        if check2.returncode != 0:
            print("❌ sing-box still rejects config; aborting.")
            print(check2.stderr.strip()[:500])
            sys.exit(3)

    p = subprocess.Popen(["./sing-box", "run", "-c", "probe_cfg.json"],
                         stdout=open("probe_sb.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(3)
    if p.poll() is not None:
        print(f"❌ sing-box exited early (code {p.returncode}). probe_sb.log:")
        try:
            print(open("probe_sb.log", encoding="utf-8", errors="ignore").read()[:500])
        except Exception:
            pass
        sys.exit(3)
    # urltest probes ALL nodes in parallel automatically. Just wait + read once.
    alive = set()
    for wait_s in (8, 12, 15):   # ~35s total; urltest probes are parallel
        time.sleep(wait_s)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{CLASH_API_PORT}/proxies")
            data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            for pname, pinfo in data.get("proxies", {}).items():
                if not pname.startswith("node-"):
                    continue
                hist = pinfo.get("history") or []
                if hist and hist[-1].get("delay", 0) > 0:
                    alive.add(pname)
        except Exception:
            pass
        if alive:
            break

    try: p.terminate(); p.wait(timeout=3)
    except Exception: p.kill()

    print(f"\n=== Parallel probe done: {len(alive)}/{len(tag_map)} alive ===")
    for tag, name in tag_map:
        print(f"  {'OK  ' if tag in alive else 'FAIL'} {tag:8s} {name[:40]}")

    if not alive:
        print("\n❌ No reachable node."); return None

    # For alive nodes, fetch exit IP via per-node sing-box. All instances run
    # in PARALLEL (each on its own port) and IP queries go out concurrently,
    # so the whole phase costs ~one node startup+probe round (~8s) instead of
    # ~10-12s per node sequentially.
    print("\nFetching exit IPs for alive nodes (parallel)...")
    alive_nodes = []
    for tag, name in tag_map:
        if tag not in alive:
            continue
        idx = int(tag.split("-")[1]) - 1
        ob = dict(nodes[idx][1]); ob["tag"] = "proxy"
        alive_nodes.append((name, ob))

    ips_list = _fetch_ips_parallel(alive_nodes)
    # classify_ip does one HTTP call each; run them concurrently too.
    ips = [ip for ip in ips_list if ip]
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


def main():
    sub_url = os.environ.get("SUB_URL", "").strip()
    if not sub_url:
        print("SUB_URL not set, cannot auto-select proxy.")
        sys.exit(2)

    # Residential/ISP exits flap: run 29 (2026-08-30) probed a subscription
    # snapshot where every reachable exit scored proxycheck risk=66
    # (Turnstile-bait) while the clean residential lines were merely down at
    # probe time. Retry the whole probe once before declaring the pool doomed.
    clean = []
    scored = []
    for attempt in (1, 2):
        scored = _probe_once(sub_url)
        if scored is None:
            clean = []
        else:
            clean = [s for s in scored
                     if s[3] != "datacenter"
                     and (s[5] is None or s[5] < PURITY_RISK_REJECT)]
        if clean:
            break
        if attempt == 1:
            print(f"\n⚠️ 第 1 轮探测没有发现任何低风险出口"
                  f"（全是 risk>={PURITY_RISK_REJECT} 的脏 IP / 机房 IP，或全部探活失败）"
                  f"— 5 秒后重试一轮...")
            time.sleep(5)

    if not clean:
        print(f"\n❌ 两轮探测后仍无低风险出口（全是 risk>={PURITY_RISK_REJECT} 的脏 IP / 机房 IP）。")
        print("   这些出口打开登录页只会得到 1x1 隐身 Turnstile（拒绝渲染），续期必然失败，")
        print("   因此直接退出，不烧掉整轮浏览器尝试。建议检查订阅里住宅/ISP 节点是否下线。")
        sys.exit(3)

    # IMPORTANT: the urltest group selects the LOWEST-LATENCY node, not the
    # best-scored one. Datacenter IPs and high-risk ("dirty") exits fail
    # Cloudflare Turnstile, so keep them OUT of the pool whenever cleaner
    # nodes exist. Final order: kind first, then lowest risk. Dirty nodes are
    # still appended after the clean ones (urltest fallback / late attempts),
    # but an all-dirty pool can no longer happen: main() exits first.
    rest = [s for s in scored if s not in clean]
    ranked = sorted(clean, key=lambda s: (KIND_SCORE.get(s[3], 2), s[5] if s[5] is not None else 50))
    ranked += sorted(rest, key=lambda s: (KIND_SCORE.get(s[3], 2), s[5] if s[5] is not None else 50))
    pool = ranked[:MAX_POOL]

    outbounds = []
    for i, (name, ob, ip, kind, org, _risk) in enumerate(pool, 1):
        ob = dict(ob); ob["tag"] = f"node-{i}"
        outbounds.append(ob)
    # "urltest auto" picks lowest latency (fallback), "proxy" selector lets
    # main.py pin a specific purity-ranked node per retry via the Clash API.
    outbounds.append({"type": "urltest", "tag": "auto",
                      "outbounds": [f"node-{i}" for i in range(1, len(pool) + 1)],
                      "url": PROBE_URL, "interval": "30s"})
    outbounds.append({"type": "selector", "tag": "proxy",
                      "outbounds": ["auto"] + [f"node-{i}" for i in range(1, len(pool) + 1)],
                      "default": "auto"})
    outbounds.append({"type": "direct", "tag": "direct"})

    config = {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [{"type": "http", "tag": "http-in",
                      "listen": LISTEN_HOST, "listen_port": LISTEN_PORT}],
        "outbounds": outbounds,
        "route": {"final": "proxy"},
        "experimental": {"clash_api": {"external_controller": f"127.0.0.1:{CLASH_API_PORT}"}},
    }
    with open("config.json", "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    # Ranked pool metadata for main.py's per-attempt node pinning.
    with open(RANKED_POOL_FILE, "w") as f:
        json.dump([{"tag": f"node-{i}", "name": name, "ip": ip,
                    "kind": kind, "risk": risk}
                   for i, (name, ob, ip, kind, org, risk) in enumerate(pool, 1)],
                  f, ensure_ascii=False, indent=1)

    best = pool[0]
    print(f"\n✅ config.json written: {len(pool)}-node urltest pool.")
    print(f"   best: {best[0]} (ip={best[2]}, {best[3]})")
    print(f"   inbound: http://{LISTEN_HOST}:{LISTEN_PORT}")

def _fetch_ips_parallel(nodes):
    """Start one sing-box per alive node at the same time (each on its own
    port), query all exit IPs concurrently via threads, then shut everything
    down. Returns a list of IPs aligned with `nodes` (None on failure).

    Sequential version cost ~10-12s per node; this costs one startup round
    plus the slowest single probe, i.e. ~10s for the whole batch."""
    if not nodes:
        return []
    procs = []
    cfg_files = []
    for i, (_, ob) in enumerate(nodes):
        port = 18080 + i
        cfg = {"log": {"level": "warn", "timestamp": True},
               "inbounds": [{"type": "http", "tag": "in",
                             "listen": "127.0.0.1", "listen_port": port}],
               "outbounds": [ob, {"type": "direct", "tag": "direct"}],
               "route": {"final": "proxy"}}
        path = f"tc_{i}.json"
        cfg_files.append(path)
        open(path, "w").write(json.dumps(cfg, ensure_ascii=False))
        procs.append(subprocess.Popen(["./sing-box", "run", "-c", path],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.STDOUT))
    time.sleep(3)  # let all sing-box instances start once, in parallel

    def fetch(i):
        port = 18080 + i
        try:
            ph = urllib.request.ProxyHandler({
                "http": f"http://127.0.0.1:{port}",
                "https": f"http://127.0.0.1:{port}"})
            op = urllib.request.build_opener(ph)
            r = op.open(urllib.request.Request(
                TEST_URL, headers={"User-Agent": "curl/8"}), timeout=NODE_TIMEOUT)
            return r.read().decode().strip()
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as ex:
        ips = list(ex.map(fetch, range(len(nodes))))

    for p in procs:
        try: p.terminate(); p.wait(timeout=3)
        except Exception: p.kill()
    for path in cfg_files:
        try: os.remove(path)
        except OSError: pass
    return ips
if __name__ == "__main__":
    main()

