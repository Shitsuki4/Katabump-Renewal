#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import time
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
import requests
from seleniumbase import SB
from selenium.common.exceptions import WebDriverException

# 从环境变量获取账号密码和 TG 配置
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""        # tg通知 chat id(可选)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""      # tg通知bot token(可选)

BASE_URL = "https://dashboard.katabump.com"  # 网站链接


class RenewalNotEligible(Exception):
    """The server explicitly reports that renewal is not available yet."""

# 多账号来源：USERS_JSON 格式 [{"username":"email","password":"pwd"}, ...]
def load_accounts():
    raw = os.environ.get("USERS_JSON", "")
    if not raw:
        # 兼容单账号 env（KATABUMP_EMAIL/KATABUMP_PASSWORD）
        email = os.environ.get("KATABUMP_EMAIL", "")
        pwd   = os.environ.get("KATABUMP_PASSWORD", "")
        if email:
            return [{"email": email, "password": pwd}]
        print("❌ 未配置 USERS_JSON 或 KATABUMP_EMAIL/KATABUMP_PASSWORD")
        return []
    try:
        users = json.loads(raw)
        accounts = []
        for u in users:
            accounts.append({
                "email": u.get("username") or u.get("email") or "",
                "password": u.get("password") or "",
            })
        return [a for a in accounts if a["email"]]
    except Exception as e:
        print(f"❌ USERS_JSON 解析失败: {e}")
        return []

ACCOUNTS = load_accounts()
CURRENT_EMAIL = ""  # 当前正在处理的账号，供 send_tg_message 脱敏

#  Telegram 推送模块
def send_tg_message(status_icon, status_text, time_left=""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    # 获取北京时间 (UTC+8)
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)

    # 邮箱脱敏：保留用户名前2位和后2位，中间用****代替
    email = CURRENT_EMAIL
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = (email[:2] + '****') if email else "未知"

    # time_left 实际承载面板 alert / 失败详情（历史参数名保留）
    detail = (time_left or "").strip()
    text = (
        f"🇫🇷 katabump 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 续期账户: {masked_email}\n"
        f"⏱️ 续期时间: {current_time_str}"
    )
    if detail:
        text += f"\n📋 详情: {detail}"

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text
    }
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            print(f"⚠️ Telegram 通知发送失败: {r.text}")
    except Exception as e:
        print(f"⚠️ Telegram 通知发送异常: {e}")

#  页面注入脚本
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_EXISTS_JS = """
(function(){
    return document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_WININFO_JS = """
(function(){
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight
    };
})()
"""

# Turnstile 复选框 iframe 的可见包围盒（用于 xdotool 物理点击）
_TURNSTILE_BBOX_JS = """
(function(){
    function expand(f){
        f.style.width='300px'; f.style.height='80px';
        f.style.minWidth='300px'; f.style.minHeight='80px';
        f.style.visibility='visible'; f.style.opacity='1';
        f.style.zIndex='9999';
        var p=f.parentElement, guard=0;
        while(p && guard<14){ p.style.overflow='visible'; p=p.parentElement; guard++; }
        var r=f.getBoundingClientRect();
        return { x: Math.round(r.left), y: Math.round(r.top),
                 w: Math.round(r.width), h: Math.round(r.height) };
    }
    if (!window.frames) return null;
    var frames = document.querySelectorAll('iframe');
    for (var i=0;i<frames.length;i++){
        var f=frames[i]; var src=f.src||'';
        if (src.indexOf('challenges.cloudflare.com')>-1 || src.indexOf('/turnstile/')>-1){
            var r=f.getBoundingClientRect();
            if (r.width>0 && r.height>0) return expand(f);
        }
    }
    // 兜底：Turnstile 组件容器内部的 iframe
    var q = document.querySelector(
        '[class*="cf-turnstile"] iframe, [id*="turnstile"] iframe, '+
        '[class*="turnstile"] iframe, .cf-turnstile-wrapper iframe'
    );
    if (q) return expand(q);
    return null;
})()
"""

# 在 Turnstile 尚未加载时，尝试点击“启动验证”的入口控件
_TURNSTILE_LAUNCH_CLICK_JS = """
(function(){
    if (document.querySelector('input[name="cf-turnstile-response"]')) return 'turnstile-ready';
    function isVisible(el){
        if (!el) return false;
        var r = el.getBoundingClientRect();
        var s = window.getComputedStyle(el);
        return r.width > 8 && r.height > 8 && s.display !== 'none' &&
               s.visibility !== 'hidden' && s.opacity !== '0';
    }
    function fireClick(el){
        if (!isVisible(el)) return false;
        var r = el.getBoundingClientRect();
        var cx = r.left + Math.min(30, Math.max(10, r.width / 2));
        var cy = r.top + r.height / 2;
        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(tp){
            el.dispatchEvent(new MouseEvent(tp, {
                bubbles: true, cancelable: true, composed: true,
                clientX: cx, clientY: cy, button: 0
            }));
        });
        try { el.click(); } catch(e) {}
        return true;
    }

    var f = document.querySelector(
        'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
    );
    if (f && fireClick(f)) return 'clicked-iframe';

    var launchers = document.querySelectorAll(
        '[class*="cf-turnstile"], [id*="turnstile"], [class*="turnstile"], ' +
        'label[for*="turnstile"], div[role="button"], button'
    );
    for (var i = 0; i < launchers.length; i++){
        var e = launchers[i];
        if (!isVisible(e)) continue;
        var hint = ((e.className || '') + ' ' + (e.id || '') + ' ' +
                    (e.getAttribute('aria-label') || '') + ' ' +
                    (e.textContent || '')).toLowerCase();
        if (hint.indexOf('turnstile') > -1 || hint.indexOf('verify') > -1 ||
            hint.indexOf('captcha') > -1 || hint.indexOf('robot') > -1){
            if (fireClick(e)) return 'clicked-launcher';
        }
    }
    return 'no-launcher';
})()
"""

# 页面所有 iframe 的 src + 矩形（诊断用）
_IFRAME_MAP_JS = """
(function(){
    var out=[];
    var frames=document.querySelectorAll('iframe');
    for (var i=0;i<frames.length;i++){
        var f=frames[i], r=f.getBoundingClientRect();
        out.push({ src:(f.src||'').slice(0,80),
                   x:Math.round(r.left), y:Math.round(r.top),
                   w:Math.round(r.width), h:Math.round(r.height) });
    }
    return JSON.stringify(out);
})()
"""

# ===== 自动续期相关 =====

# 在模态框内查找 iframe 并展开，返回点击坐标
_ALTCHA_EXPAND_JS = """
(function() {
    var modal = document.querySelector('div.modal.show') || document;
    var iframes = modal.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var r = iframes[i].getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            iframes[i].style.width  = '300px';
            iframes[i].style.height = '150px';
            iframes[i].style.minWidth  = '300px';
            iframes[i].style.minHeight = '150px';
            iframes[i].style.visibility = 'visible';
            iframes[i].style.opacity = '1';
            var el = iframes[i];
            for (var j = 0; j < 10; j++) {
                el = el.parentElement;
                if (!el) break;
                el.style.overflow = 'visible';
            }
            var r2 = iframes[i].getBoundingClientRect();
            return { cx: Math.round(r2.x + 30), cy: Math.round(r2.y + r2.height / 2) };
        }
    }
    return null;
})()
"""

# 检测 ALTCHA 是否已验证通过
_ALTCHA_SOLVED_JS = r"""
(function(){
    var modal = document.querySelector('div.modal.show') || document;
    // hidden input 有值
    var inputs = modal.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < inputs.length; i++) {
        var n = (inputs[i].name || '').toLowerCase();
        if ((n.includes('altcha') || n.includes('captcha')) &&
            inputs[i].value && inputs[i].value.length > 20) return {ok:true, why:'hidden-input'};
    }
    var w = modal.querySelector('[data-state="verified"],.altcha--verified,.altcha-verified');
    if (w) return {ok:true, why:'data-state'};
    var state = modal.querySelector('altcha-widget [data-state], .altcha[data-state], [data-state]');
    return {ok:false, why:(state && state.getAttribute('data-state')) || ''};
})()
"""

# 输出模态框内 ALTCHA 相关的 DOM 诊断信息
_ALTCHA_DIAG_JS = r"""
(function(){
    var modal = document.querySelector('div.modal.show') || document;
    var out = {hidden:[], buttons:[], state:null, widget:null, forms:0};
    modal.querySelectorAll('input[type="hidden"]').forEach(function(i){
        var n = (i.name || '').toLowerCase();
        if (n.indexOf('altcha') > -1 || n.indexOf('captcha') > -1)
            out.hidden.push({name:i.name || i.id || '?', len:(i.value || '').length});
    });
    modal.querySelectorAll('button').forEach(function(b){
        out.buttons.push({t:(b.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
                          dis:!!b.disabled, type:(b.type || '')});
    });
    var w = modal.querySelector('altcha-widget, [data-state], .altcha');
    if (w) {
        out.state = w.getAttribute('data-state');
        out.widget = (w.outerHTML || '').replace(/\s+/g, ' ').slice(0, 500);
    }
    out.forms = modal.querySelectorAll('form').length;
    return out;
})()
"""


#  底层输入工具
def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    sb.execute_script(f"""
    (function(){{
        var el = document.querySelector('{selector}');
        if (!el) return;
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        if (nativeInputValueSetter) {{
            nativeInputValueSetter.call(el, "{safe_text}");
        }} else {{
            el.value = "{safe_text}";
        }}
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()
    """)

def _activate_window():
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")


def dump_driver_log(path="chromedriver.log"):
    """Persist the chromedriver service log (if any) into an artifact path.

    SeleniumBase writes chromedriver.log under the latest_savedfile logged
    sessions dir; when absent this is a no-op. Artifact upload picks the
    file up via the *.log globs, so crashes leave a post-mortem behind."""
    import glob
    cands = (glob.glob(os.path.join("latest_logs", "**", "chromedriver.log"), recursive=True)
             + glob.glob(os.path.join("*", "chromedriver.log"))
             + glob.glob("chromedriver.log"))
    for src in cands:
        try:
            with open(src, "rb") as f:
                data = f.read()
            with open(path, "wb") as f:
                f.write(data)
            print(f"  📎 chromedriver 日志已保存到 {path}（来源 {src}, {len(data)} bytes）")
            return True
        except Exception:
            continue
    print("  （未找到 chromedriver.log）")
    return False


def _restart_proxy():
    """重启 sing-box，让 urltest 重新探测，可能选中池子里另一个节点。

    仅在 GitHub Actions 环境生效（本地无 sing-box 可执行文件则跳过）。
    就绪检测改为轮询：代理能连上外网就立即返回，不再固定等 26 秒。
    """
    if not os.path.exists("sing-box"):
        print("  （本环境无 sing-box 可执行文件，跳过代理节点切换）")
        return
    print("\n🔄 重启 sing-box 以切换代理节点...")
    subprocess.run(["pkill", "-9", "-f", "sing-box"], capture_output=True)
    time.sleep(2)
    log = open("singbox.log", "ab")
    try:
        subprocess.Popen(
            ["./sing-box", "run", "-c", "config.json"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    finally:
        log.close()
    # 轮询等待 urltest 组选出可用节点（最长 40s，通则秒回）
    import urllib.request
    for _ in range(20):
        time.sleep(2)
        try:
            ph = urllib.request.ProxyHandler({"http": "http://127.0.0.1:8080",
                                              "https": "http://127.0.0.1:8080"})
            urllib.request.build_opener(ph).open(
                urllib.request.Request("https://api.ip.sb/ip",
                                       headers={"User-Agent": "curl/8"}),
                timeout=8).read()
            print("   代理已恢复")
            break
        except Exception:
            pass
    else:
        print("   ⚠️ 40 秒内代理未恢复，继续尝试...")
    try:
        with open("singbox.log", "rb") as f:
            lines = f.read().decode("utf-8", "ignore").splitlines()
        shown = 0
        for ln in lines[-40:]:
            if ("urltest" in ln or "selected" in ln or "node-" in ln) and shown < 5:
                print("   sing-box:", ln.strip())
                shown += 1
    except Exception:
        pass

def _pin_pool_node(attempt: int) -> None:
    """Pin the selector "proxy" to the purity-ranked node for this attempt.

    auto_proxy.py ranks unique exit IPs by type (residential > isp > dc) and
    proxycheck.io risk score into ranked_pool.json. Attempt N uses rank N;
    beyond the pool we fall back to the latency-based urltest group "auto".
    This replaces blind urltest re-rolls that kept picking the same
    Turnstile-blocked exit IP."""
    if not os.path.exists("ranked_pool.json"):
        print("   ⚠️ ranked_pool.json 缺失，无法固定节点（沿用 urltest 自动选择）")
        return
    try:
        pool = json.load(open("ranked_pool.json", encoding="utf-8"))
    except Exception as e:
        print(f"   ⚠️ 读取 ranked_pool.json 失败: {e}")
        return
    if not pool:
        return
    i = min(attempt - 1, len(pool) - 1) if attempt <= len(pool) else None
    if i is None:
        target, expect = "auto", ""
        print(f"   第 {attempt} 次尝试超出节点池，回退到 urltest 自动选节点")
    else:
        target, expect = pool[i]["tag"], pool[i].get("ip", "")
        print(f"   📌 固定使用节点 {target}（排名 {i + 1}/{len(pool)}: "
              f"{pool[i].get('name', '?')[:40]}, {pool[i].get('kind')}, "
              f"risk={pool[i].get('risk')}）")
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:9099/proxies/proxy",
            data=json.dumps({"name": target}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"   ⚠️ Clash API 固定节点失败（{e}），沿用当前选择")
        return
    # 确认 selector 已指向目标节点；住宅节点的出口 IP 可能按连接轮换，
    # 所以不能拿探测时的 IP 做严格比对，仅打印当前实际出口做参考。
    try:
        cur = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:9099/proxies/proxy", timeout=5).read().decode())
        now = (cur.get("now") or "")
        if now and now != target:
            print(f"   ⚠️ selector 当前指向 {now}，与预期 {target} 不一致")
            return
    except Exception:
        pass
    try:
        ph = urllib.request.ProxyHandler({"http": "http://127.0.0.1:8080",
                                          "https": "http://127.0.0.1:8080"})
        ip = urllib.request.build_opener(ph).open(
            urllib.request.Request("https://api.ip.sb/ip",
                                   headers={"User-Agent": "curl/8"}),
            timeout=8).read().decode().strip()
        if ip and expect and ip != expect:
            print(f"   实际出口 {ip}（探测时 {expect}，住宅线路出口按连接轮换，属正常）")
        elif ip == expect:
            print(f"   出口 IP 确认: {ip}")
    except Exception:
        pass

def _switch_to_turnstile_frame(sb):
    """切入页面上的 Turnstile iframe，返回是否成功。"""
    try:
        el = sb.driver.execute_script("""
        (function(){
            var frames = document.querySelectorAll('iframe');
            for (var i = 0; i < frames.length; i++){
                var f = frames[i], s = f.src || '';
                if (s.indexOf('challenges.cloudflare.com') > -1 ||
                    s.indexOf('turnstile') > -1) return f;
            }
            var q = document.querySelector(
                '[class*="cf-turnstile"], [id*="turnstile"]');
            if (q){ var qf = q.querySelector('iframe'); if (qf) return qf; }
            return null;
        })()
        """)
        if el is None:
            return False
        sb.driver.switch_to.frame(el)
        return True
    except Exception:
        return False

def _nudge_turnstile_launcher(sb):
    """Turnstile 尚未出现时，尝试点击入口触发加载。"""
    try:
        ret = sb.execute_script(_TURNSTILE_LAUNCH_CLICK_JS)
        if ret and ret not in ("turnstile-ready", "no-launcher"):
            print(f"🖱️ 预触发 Turnstile: {ret}")
    except Exception:
        pass


#  人机验证处理（多策略：SeleniumBase UC GUI 点击 + xdotool 物理点击 + iframe 内 JS 点击）
def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    # 检查是否已静默通过
    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    # 记录页面 iframe 布局（诊断用）
    try:
        fm = sb.execute_script(_IFRAME_MAP_JS)
        print(f"  📄 页面 iframe: {fm}")
    except Exception:
        pass

    # 展开 Turnstile 验证框（防止被父容器 overflow:hidden 裁剪）
    for _ in range(3):
        try: sb.execute_script(_EXPAND_JS)
        except Exception: pass
        time.sleep(0.5)

    # ── 策略 A：SeleniumBase UC 内置 GUI 点击 ──
    for attempt in range(4):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过（A 第 {attempt + 1} 次）")
            return True
        print(f"🖱️ [A] 第 {attempt + 1}/4 次调用 uc_gui_click_captcha...")
        try:
            if attempt < 2:
                sb.uc_gui_click_captcha()
            else:
                sb.uc_gui_click_cf(frame="iframe", retry=True, blind=True)
        except Exception as e:
            print(f"⚠️ [A] 调用异常: {e}")
        solved = False
        for _ in range(8):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                solved = True
                break
        if solved:
            print(f"✅ Turnstile 通过（A 第 {attempt + 1} 次）")
            return True

    # ── 策略 B：xdotool 物理点击复选框坐标（与 ALTCHA 同机制） ──
    for attempt in range(4):
        if sb.execute_script(_SOLVED_JS):
            print("✅ Turnstile 通过（B 前缀检查）")
            return True
        bbox = None
        try:
            bbox = sb.execute_script(_TURNSTILE_BBOX_JS)
        except Exception:
            bbox = None
        if not bbox:
            print("⚠️ [B] 未定位到 Turnstile iframe，稍等重试...")
            time.sleep(2)
            continue
        try:
            wi = sb.execute_script(_WININFO_JS)
        except Exception:
            wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}
        bar = wi.get("oh", 800) - wi.get("ih", 768)
        cx = bbox["x"] + wi.get("sx", 0) + 30          # 复选框在 iframe 左侧约 30px
        cy = bbox["y"] + wi.get("sy", 0) + bar + max(28, int(bbox["h"]) // 2)
        print(f"🖱️ [B] xdotool 点击复选框 ({cx}, {cy})  bbox={bbox}")
        _xdotool_click(cx, cy)
        solved = False
        for _ in range(8):
            time.sleep(0.5)
            if sb.execute_script(_SOLVED_JS):
                solved = True
                break
        if solved:
            print(f"✅ Turnstile 通过（B 第 {attempt + 1} 次）")
            return True
        print(f"  ⚠️ [B] 第 {attempt + 1} 次未通过")

    # ── 策略 C：切入 iframe 直接点击复选框元素 ──
    for attempt in range(3):
        if sb.execute_script(_SOLVED_JS):
            print("✅ Turnstile 通过（C 前缀检查）")
            return True
        print(f"🖱️ [C] 第 {attempt + 1}/3 切入 iframe 尝试...")
        if not _switch_to_turnstile_frame(sb):
            print("  ⚠️ [C] 未找到 Turnstile iframe")
            sb.driver.switch_to.default_content()
            time.sleep(2)
            continue
        try:
            cb = sb.driver.execute_script("""
            (function(){
                var cands = document.querySelectorAll(
                    '[role="checkbox"], input[type="checkbox"],'+
                    '[class*="checkbox"], [class*="btn-check"]'
                );
                for (var i = 0; i < cands.length; i++){
                    var e = cands[i]; var r = e.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return e;
                }
                return null;
            })()
            """)
            if cb is not None:
                sb.driver.execute_script(
                    "arguments[0].focus(); arguments[0].click();", cb)
                print("    [C] 已 click 复选框元素")
            else:
                # 没找到复选框：尝试空格键触发
                sb.driver.switch_to.active_element.send_keys(" ")
                print("    [C] 未找到复选框元素，发送空格键")
        except Exception as e:
            print(f"    ⚠️ [C] 异常: {e}")
        finally:
            sb.driver.switch_to.default_content()
        solved = False
        for _ in range(6):
            time.sleep(1)
            if sb.execute_script(_SOLVED_JS):
                solved = True
                break
        if solved:
            print(f"✅ Turnstile 通过（C 第 {attempt + 1} 次）")
            return True

    print("  ❌ Turnstile A/B/C 策略均失败")
    return False

#  账户登录
def login(sb, email, password) -> bool:
    print(f"🌐 打开登录页面: {BASE_URL}/auth/login")
    sb.uc_open_with_reconnect(BASE_URL + "/auth/login", reconnect_time=8)
    time.sleep(6)

    # 先等待 Cloudflare 验证通过（最多等 30 秒）
    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过（{i+1}s）")
            break
        time.sleep(1)
    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[name="email"]', timeout=15)
    except Exception:
        # 尝试大写选择器作为后备
        try:
            sb.wait_for_element('input[name="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            cur_url = sb.get_current_url()
            page_title = sb.get_title() or ""
            print(f"  当前 URL: {cur_url}")
            print(f"  当前标题: {page_title}")
            sb.save_screenshot("login_load_fail.png")
            return False

    print("🍪 关闭可能的 Cookie 弹窗...")
    try:
        for btn in sb.find_elements("button"):
            if "Accept" in (btn.text or ""):
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    print(f"📧 填写邮箱...")
    js_fill_input(sb, 'input[name="email"]', email)
    time.sleep(0.3)

    print("🔑 填写密码...")
    js_fill_input(sb, 'input[name="password"]', password)
    time.sleep(1)

    # 等待 Turnstile 验证框出现（最多 10 秒）
    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    for i in range(10):
        if sb.execute_script(_EXISTS_JS):
            ts_found = True
            print(f"✅ 检测到 Turnstile（{i+1}s）")
            break
        _nudge_turnstile_launcher(sb)
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(1)

    if ts_found:
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            sb.save_screenshot("login_turnstile_fail.png")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 敲击回车提交表单...")
    sb.press_keys('input[name="password"]', '\n')

    print("⏳ 等待登录跳转...")
    for _ in range(12):
        time.sleep(1)
        cur_url = sb.get_current_url().split('?')[0].lower()
        page_title = sb.get_title() or ""
        if cur_url.startswith(f"{BASE_URL}/dashboard") or "Dashboard | KataBump" in page_title.lower():
            break

    cur_url = sb.get_current_url().split('?')[0].lower()
    page_title = sb.get_title() or ""
    if cur_url.startswith(f"{BASE_URL}/dashboard") or "Dashboard | KataBump" in page_title.lower():
        print(f"✅ 登录成功！(URL: {sb.get_current_url()}, Title: {page_title})")
        return True
        
    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {sb.get_current_url()}, Title: {page_title})")
    sb.save_screenshot("login_failed.png")
    return False

# ===== 自动续期流程 =====

def _read_alert(sb):
    """读取页面第一个 Bootstrap alert 的文本，找不到返回空串"""
    try:
        el = sb.find_element("div.alert", timeout=4)
        return (el.text or "").strip()
    except Exception:
        return ""


def _goto_server_detail(sb) -> bool:
    """在 Dashboard 首页查找并点击 See 进入服务器详情页"""
    print("\n🖥️  正在进入服务器续期页...")
    time.sleep(5)

    # 检查页面顶部是否已有"还无法续期"全局提示
    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️  页面顶部提示: {alert_text}")
        sb.save_screenshot("renew_not_eligible.png")
        raise RenewalNotEligible(alert_text)

    # 多种选择器尝试查找 See 链接
    selectors = [
        'a[href*="/servers/edit?id="]',
        'td a[href*="/servers/edit"]',
        'table a[href*="/servers/edit"]',
        'table td a',
    ]

    see_link = None
    for sel in selectors:
        try:
            see_link = sb.find_element(sel, timeout=8)
            print(f"✅ 通过选择器找到链接: {sel}")
            break
        except Exception:
            continue

    # 选择器全部失败，尝试通过文本内容查找
    if see_link is None:
        print("⚠️ 选择器未命中，尝试文本匹配...")
        try:
            for a in sb.find_elements("a"):
                if (a.text or "").strip().lower() == "see":
                    see_link = a
                    print("✅ 通过文本 'See' 找到链接")
                    break
        except Exception:
            pass

    if see_link is None:
        # 打印调试信息帮助排查
        cur_url = sb.get_current_url()
        title = sb.get_title() or ""
        print(f"❌ 未找到 'See' 链接")
        print(f"当前 URL: {cur_url}")
        print(f"页面标题: {title}")
        try:
            links = sb.find_elements("a")
            print(f"     页面共 {len(links)} 个链接:")
            for a in links[:20]:
                href = a.get_attribute("href") or ""
                txt  = (a.text or "").strip()[:30]
                if href:
                    print(f"       - [{txt}] -> {href}")
        except Exception:
            pass
        sb.save_screenshot("servers_page_fail.png")
        return False

    print("🖱️  点击 'See' 进入服务器详情页...")
    see_link.click()
    time.sleep(5)
    print(f"📄 当前页面: {sb.get_current_url()}")

    # The server edit page renders the eligibility error above the form.
    alert_text = _read_alert(sb)
    if alert_text and "can't renew" in alert_text.lower():
        print(f"ℹ️  页面提示: {alert_text}")
        sb.save_screenshot("renew_not_eligible.png")
        raise RenewalNotEligible(alert_text)

    return True


def _open_renew_modal(sb) -> bool:
    """滚动到 Renew 按钮并点击，打开模态框"""
    print("\n🔄 查找 Renew 按钮...")
    try:
        renew_btn = sb.find_element('button[data-bs-target="#renew-modal"]', timeout=10)
    except Exception:
        try:
            renew_btn = sb.find_element('button.btn.btn-outline-primary', timeout=5)
        except Exception:
            print("  ❌ 未找到 Renew 按钮")
            return False

    sb.execute_script("""
        (function(){
            var btn = document.querySelector('button[data-bs-target="#renew-modal"]')
                     || document.querySelector('button.btn.btn-outline-primary');
            if (btn) btn.scrollIntoView({behavior:'smooth',block:'center'});
        })()
    """)
    time.sleep(0.8)
    renew_btn.click()
    print("🖱️ 已点击 Renew 按钮，等待 ALTCHA 验证框...")
    time.sleep(3)

    try:
        sb.find_element('div.modal.show', timeout=5)
        print("✅ Renew 模态框已弹出")
        return True
    except Exception:
        print("⚠️ 模态框未弹出")
        return False


def _renew_not_due(sb) -> bool:
    """The site exposes the renewal window even when its modal can be opened."""
    try:
        body = sb.execute_script(r"""
            (function(){
                return document.body.innerText || '';
            })()
        """) or ""

        low = body.lower()
        if "can't renew" in low and (
            "will be able to" in low or "not eligible" in low
        ):
            return True

        # Observed on the free plan: expiry 08-31 opens renewal on 08-30.
        match = re.search(
            r"Expiry\s*(?:\n\s*|:\s*)(\d{4}-\d{2}-\d{2})", body
        )
        if match:
            expiry = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            opens_on = expiry - timedelta(days=1)
            today = datetime.now(timezone.utc).date()
            if today < opens_on:
                print(
                    f"ℹ️ 按 Expiry 预判续期窗口: {opens_on.isoformat()} "
                    f"(today UTC: {today.isoformat()})"
                )
                return True
    except Exception:
        return False

    return False


def _altcha_wait_strong_signal(sb, timeout_s: int = 20) -> bool:
    """等待 PoW 结果写入隐藏 input 或 ALTCHA 进入 verified 状态。"""
    for _ in range(timeout_s):
        time.sleep(1)
        strong = sb.execute_script(_ALTCHA_SOLVED_JS) or {}
        if strong.get("ok") and strong.get("why") == "hidden-input":
            print("  ✅ 隐藏 input 已写入 PoW 结果")
            return True
    return False


def _altcha_dump_diag(sb):
    try:
        print("  🔍", sb.execute_script(_ALTCHA_DIAG_JS))
    except Exception:
        pass


def _solve_altcha(sb) -> bool:
    """Solve ALTCHA and require a real token or verified state."""
    print("\n🔐 处理 ALTCHA 人机验证...")
    time.sleep(2)

    # 先检查是否已自动通过
    solved = sb.execute_script(_ALTCHA_SOLVED_JS) or {}
    if solved.get("ok"):
        print(f"✅ ALTCHA 已自动通过（信号: {solved.get('why')}）")
        return True

    # 展开模态框内 iframe 并获取坐标
    coords = None
    try:
        coords = sb.execute_script(_ALTCHA_EXPAND_JS)
    except Exception:
        pass

    if coords:
        print(f"  📍 找到模态框内 iframe 坐标: ({coords['cx']}, {coords['cy']})")

    # 最多尝试 3 轮
    for attempt in range(3):
        solved = sb.execute_script(_ALTCHA_SOLVED_JS) or {}
        if solved.get("ok"):
            print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮，信号: {solved.get('why')}）")
            return True

        # 策略 1: xdotool 物理点击 iframe 坐标
        if coords:
            try:
                wi = sb.execute_script(_WININFO_JS)
            except Exception:
                wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768}
            bar = wi["oh"] - wi["ih"]
            ax  = coords["cx"] + wi["sx"]
            ay  = coords["cy"] + wi["sy"] + bar
            print(f"🖱️  ALTCHA点击复选框  ({ax}, {ay})")
            _xdotool_click(ax, ay)

        # 策略 2: SeleniumBase 原生点击模态框内 iframe 元素
        try:
            iframes = sb.find_elements('div.modal.show iframe')
            for iframe in iframes:
                try:
                    iframe.click()
                    print("🖱️  SeleniumBase 点击模态框 iframe")
                except Exception:
                    pass
        except Exception:
            pass

        # 策略 3: JS 遍历模态框内所有可点击元素
        sb.execute_script("""
            (function(){
                var modal = document.querySelector('div.modal.show');
                if (!modal) return;
                // 点击 iframe
                var iframes = modal.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    iframes[i].click();
                    iframes[i].dispatchEvent(new MouseEvent('click', {bubbles:true}));
                }
                // 点击含 checkbox 的 label
                var labels = modal.querySelectorAll('label');
                for (var j = 0; j < labels.length; j++) {
                    var txt = (labels[j].textContent || '').toLowerCase();
                    if (txt.includes('robot') || txt.includes('captcha') || txt.includes('verify'))
                        labels[j].click();
                }
                // 点击 checkbox
                var cbs = modal.querySelectorAll('input[type="checkbox"]');
                for (var k = 0; k < cbs.length; k++) {
                    if (!cbs[k].disabled) {
                        cbs[k].click();
                        cbs[k].dispatchEvent(new MouseEvent('click', {bubbles:true}));
                    }
                }
            })()
        """)

        # 等待验证结果
        for _ in range(6):
            time.sleep(1)
            solved = sb.execute_script(_ALTCHA_SOLVED_JS) or {}
            if solved.get("ok"):
                print(f"✅ ALTCHA 验证通过（第 {attempt + 1} 轮，信号: {solved.get('why')}）")
                return True

        print(f"  ⚠️ 第 {attempt + 1} 轮未通过，重试...")
        # 重新获取坐标（iframe 可能已重新渲染）
        try:
            new_coords = sb.execute_script(_ALTCHA_EXPAND_JS)
            if new_coords:
                coords = new_coords
        except Exception:
            pass

    print("  ❌ ALTCHA 3 轮均失败")
    return False


def _read_expiry(sb) -> str:
    """Read the current server expiry, used as a submit-independent result check."""
    try:
        return sb.execute_script(r"""
            (function(){
                var m = (document.body.innerText || '').match(
                    /Expiry\s*(?:\n\s*|:\s*)(\d{4}-\d{2}-\d{2})/
                );
                return m ? m[1] : '';
            })()
        """) or ""
    except Exception:
        return ""


def _submit_renew(sb) -> bool:
    """Submit the renewal form without causing ALTCHA to restart.

    With auto=onsubmit, form.requestSubmit() emits another submit event. If a
    token already exists, that second event makes ALTCHA reset and verify again
    instead of reaching the server with the current token. HTMLFormElement's
    submit() deliberately bypasses submit handlers and preserves that token.
    """
    print("🖱️  提交模态框中的 Renew 表单...")
    time.sleep(2)
    _altcha_dump_diag(sb)

    action = ""
    try:
        state = sb.execute_script(r"""
            (function(){
                var m = document.querySelector('div.modal.show');
                if (!m) return {modal:false};
                var f = m.querySelector('form');
                var token = m.querySelector(
                    'input[type="hidden"][name="altcha"], input[name="altcha"]'
                );
                var button = m.querySelector('button[type="submit"]') ||
                             m.querySelector('button.btn-primary');
                return {
                    modal:true,
                    form:!!f,
                    token:!!(token && token.value),
                    button:!!button,
                    enabled:!!(button && !button.disabled),
                    text:((button && button.textContent) || '').trim()
                };
            })()
        """) or {}

        if state.get("modal") and state.get("form") and state.get("token"):
            sb.execute_script(r"""
                (function(){
                    var m = document.querySelector('div.modal.show');
                    var f = m && m.querySelector('form');
                    if (f) f.submit();
                })()
            """)
            action = "submitted-token"
        elif state.get("modal") and state.get("button") and state.get("enabled"):
            sb.execute_script(r"""
                (function(){
                    var m = document.querySelector('div.modal.show');
                    var b = m.querySelector('button[type="submit"]') ||
                            m.querySelector('button.btn-primary');
                    if (b) b.click();
                })()
            """)
            action = "clicked-submit"
            # Let the ALTCHA auto=onsubmit handler verify and resubmit itself.
            for _ in range(20):
                time.sleep(1)
                now = sb.execute_script(r"""
                    (function(){
                        var m = document.querySelector('div.modal.show');
                        if (!m) return {modal:false};
                        var f = m.querySelector('form');
                        var token = m.querySelector('input[name="altcha"]');
                        return {modal:true, form:!!f, token:!!(token && token.value)};
                    })()
                """) or {}
                if not now.get("modal"):
                    break
                if now.get("form") and now.get("token"):
                    sb.execute_script(r"""
                        (function(){
                            var m = document.querySelector('div.modal.show');
                            var f = m && m.querySelector('form');
                            if (f) f.submit();
                        })()
                    """)
                    action = "submitted-after-altcha"
                    break
        else:
            print(f"  ⚠️ 表单状态异常: {state}")
            sb.save_screenshot("renew_submit_state.png")
            return False
    except Exception as e:
        print(f"  ❌ 提交异常: {e}")
        sb.save_screenshot("renew_submit_error.png")
        return False

    if action:
        print(f"  ✅ Renew 提交动作完成（{action}）")
        time.sleep(3)
        _altcha_dump_diag(sb)
        return True

    # Failure: report honestly, don't fake success.
    sb.save_screenshot("renew_submit_timeout.png")
    print("  ❌ Renew 表单未能提交")
    return False


def _confirm_server_type_warning(sb) -> bool:
    """Handle only a visible confirmation dialog, never a background alert.

    The page may retain an old Bootstrap alert from the server edit page. It
    must not be used as evidence that a confirmation dialog is open.
    """
    try:
        state = sb.execute_script("""
            (function(){
                var m = document.querySelector('div.modal.show, div[role="dialog"]');
                if (!m) return {visible:false, text:''};
                var r = m.getBoundingClientRect();
                return {visible:r.width > 0 && r.height > 0, text:(m.innerText||'').trim()};
            })()
        """)
    except Exception:
        return False
    text = (state or {}).get("text", "")
    if not (state or {}).get("visible") or "server type" not in text.lower():
        return False

    print("⚠️ 检测到可见的 server type 确认框...")
    sb.save_screenshot("renew_warn_before_confirm.png")
    clicked = sb.execute_script("""
        (function(){
            var m = document.querySelector('div.modal.show, div[role="dialog"]');
            if (!m) return false;
            var re = /confirm|continue|yes|ok|确定|确认|继续|更换/;
            for (var b of m.querySelectorAll('button')) {
                var t = (b.textContent||'').trim().toLowerCase();
                if (!b.disabled && re.test(t) && !/^renew$/.test(t)) {
                    b.click(); return true;
                }
            }
            return false;
        })()
    """)
    if clicked:
        print("  ✅ 已点击 server type 确认按钮")
        time.sleep(3)
        sb.save_screenshot("renew_warn_after_confirm.png")
        return True
    print("  ❌ 未找到明确的 server type 确认按钮")
    return False


def _visible_renew_feedback(sb):
    """Return visible success/error feedback, excluding stale background alerts."""
    try:
        return sb.execute_script("""
            (function(){
                var selectors = ['div.modal.show', 'div[role="dialog"]',
                                 '.alert', '.toast', '.swal2-container'];
                var out=[];
                selectors.forEach(function(sel){
                    document.querySelectorAll(sel).forEach(function(el){
                        var r=el.getBoundingClientRect(), s=getComputedStyle(el);
                        if (r.width && r.height && s.visibility !== 'hidden' && s.display !== 'none') {
                            var t=(el.innerText||'').trim();
                            if (t && !out.includes(t)) out.push(t);
                        }
                    });
                });
                return out.join('\\n');
            })()
        """) or ""
    except Exception:
        return ""


def _check_renew_result(sb, expiry_before: str = "") -> bool:
    """Wait for and verify the actual renewal result; return success/failure."""
    print("\n📋 等待并检查真实续期结果...")
    deadline = time.time() + 60
    last_feedback = ""
    while time.time() < deadline:
        feedback = _visible_renew_feedback(sb)
        if feedback:
            last_feedback = feedback
            low = feedback.lower()
            print(f"  页面状态: {feedback[:180]}")
            # The stale server-type warning is not a success result.
            if "server type" in low and "verifying" not in low:
                _confirm_server_type_warning(sb)
            if "can't renew" in low and "will be able to" in low:
                sb.save_screenshot("renew_result.png")
                print("ℹ️ 服务器当前不在可续期窗口内，按无需操作处理")
                send_tg_message("⏳", "未到续期时间", feedback[:500])
                return True
            # Only explicit result text counts as success.
            if any(kw in low for kw in ("renewed", "renewal successful", "renew success",
                                        "server renewed", "extended successfully")):
                sb.save_screenshot("renew_result.png")
                send_tg_message("✅", "续期成功", feedback[:500])
                return True
            if any(kw in low for kw in ("can't renew", "cannot renew", "unable to renew",
                                        "not eligible", "already renewed")):
                sb.save_screenshot("renew_result.png")
                send_tg_message("⏳", "未能续期", feedback[:500])
                return False
        time.sleep(2)

    sb.save_screenshot("renew_result.png")
    if expiry_before:
        sb.execute_script("location.reload();")
        time.sleep(6)
        expiry_after = _read_expiry(sb)
        if expiry_after and expiry_after != expiry_before:
            print(f"✅ 可见提示缺失，但 Expiry 已由 {expiry_before} 更新为 {expiry_after}")
            send_tg_message("✅", "续期成功", f"Expiry: {expiry_before} -> {expiry_after}")
            return True
    if last_feedback:
        print(f"❌ 续期超时，最后页面状态: {last_feedback[:300]}")
        send_tg_message("❌", "续期未确认", last_feedback[:500])
    else:
        print("❌ 续期超时，未检测到真实结果")
        send_tg_message("❌", "续期未确认", "60 秒内没有成功/失败结果")
    return False


def renew_server(sb) -> bool:
    """登录成功后调用：自动进入详情页 -> Renew -> ALTCHA -> 提交。"""
    print("\n" + "#" * 25)
    print("  开始自动续期流程")
    print("#" * 25)

    if not _goto_server_detail(sb):
        return False

    if _renew_not_due(sb):
        sb.save_screenshot("renew_not_due.png")
        print("ℹ️ 服务器尚未进入可续期窗口，跳过 ALTCHA 和提交")
        send_tg_message("⏳", "未到续期时间", "服务器当前不在可续期窗口内")
        return True

    if not _open_renew_modal(sb):
        return False

    expiry_before = _read_expiry(sb)

    altcha_ok = _solve_altcha(sb)
    if not altcha_ok:
        print("⚠️ ALTCHA 验证未通过，终止本次续期，不伪报成功")
        return False

    if not _submit_renew(sb):
        return False
    _confirm_server_type_warning(sb)
    return _check_renew_result(sb, expiry_before)


def _run_account(sb_kwargs, email, pwd) -> bool:
    """单个账号：启动浏览器 -> 登录 -> 自动续期。返回是否成功。"""
    global CURRENT_EMAIL
    CURRENT_EMAIL = email
    print("🚀 启动浏览器...")
    try:
        with SB(**sb_kwargs) as sb:
            try:
                sb.open("https://api.ip.sb/ip")
                print(f"📍  当前出口IP: {sb.get_text('body')}")
            except Exception:
                pass

            if login(sb, email, pwd):
                try:
                    return renew_server(sb)
                except RenewalNotEligible as e:
                    print(f"⏳ 未到续期时间: {e}")
                    send_tg_message("⏳", "未到续期时间", str(e))
                    return True
            else:
                print("\n❌ 登录失败，终止该账号续期操作。")
                send_tg_message("❌", "登录失败", "未知")
                return False
    except WebDriverException as e:
        # chromedriver died mid-run (run 29: Turnstile never rendered on
        # risk>=66 exits, driver refused the next command and every later
        # call failed with Connection refused, losing all diagnostics).
        # Grab what we can before the browser context is gone for good.
        print(f"\n💀 浏览器/驱动会话中断: {type(e).__name__}: {str(e)[:300]}")
        try:
            dump_driver_log("driver_crash.log")
        except Exception:
            pass
        send_tg_message("💀", "浏览器会话中断",
                        f"{type(e).__name__}: {str(e)[:150]}")
        return False
    except Exception as e:
        print(f"\n❌ 账号 {email} 处理异常: {e}")
        send_tg_message("❌", f"处理异常: {e}", "未知")
        return False


#  脚本执行入口 (可选代理)
def main():
    print("#" * 25)
    print("   katabump 自动登录续期")
    print("#" * 25)

    if not ACCOUNTS:
        print("❌ 没有可用的账号，退出。")
        raise SystemExit(1)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:8080"
    sb_kwargs = {"uc": True, "headless": False}

    if IS_PROXY:
        print(f"🔗 挂载代理: {proxy_str}")
        sb_kwargs["proxy"] = proxy_str
    else:
        print("🌐 未使用代理，直连访问")

    print(f"👥 共 {len(ACCOUNTS)} 个账号待处理")

    ok_count = 0
    max_attempts = int(os.environ.get("NODE_ATTEMPTS", "3"))
    for idx, acc in enumerate(ACCOUNTS, 1):
        email = acc["email"]
        pwd   = acc["password"]
        print("\n" + "=" * 25)
        print(f"  处理账号 {idx}/{len(ACCOUNTS)}: {email}")
        print("=" * 25)

        acc_ok = False
        for attempt in range(1, max_attempts + 1):
            print(f"  ── 节点尝试 {attempt}/{max_attempts} ──")
            if attempt > 1:
                _restart_proxy()   # 换池子里另一个节点再试
            if IS_PROXY:
                _pin_pool_node(attempt)  # 按纯度排名固定本次使用的节点
            if _run_account(sb_kwargs, email, pwd):
                acc_ok = True
                break
        if acc_ok:
            ok_count += 1
        else:
            print(f"❌ 账号 {email} 所有节点尝试均失败")
            send_tg_message("❌", "节点尝试均失败", f"{max_attempts} 次不同代理节点")

    print("\n" + "#" * 25)
    print(f"  全部账号处理完毕: {ok_count}/{len(ACCOUNTS)} 成功")
    print("#" * 25)
    if ok_count < len(ACCOUNTS):
        raise SystemExit(1)

if __name__ == "__main__":
    main()
