#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import re
import time
import subprocess
from datetime import datetime
import argparse
import requests

from fallback_proxy import parse_fallback_proxies
from renewal_runner import int_setting, run_renewals
from secrets_runtime import mask_workflow_secrets

# 从环境变量获取账号密码和 TG 配置
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""        # tg通知 chat id(可选)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""      # tg通知bot token(可选)

BASE_URL = "https://dashboard.katabump.com"  # 网站链接


class RenewalNotEligible(Exception):
    """The server explicitly reports that renewal is not available yet."""

# 多账号来源：USERS_JSON 格式 [{"username":"email","password":"pwd"}, ...]
class AccountConfigurationError(ValueError):
    pass


def load_accounts():
    raw = os.environ.get("USERS_JSON", "").strip()
    if not raw:
        email = os.environ.get("KATABUMP_EMAIL", "").strip()
        password = os.environ.get("KATABUMP_PASSWORD", "")
        return [{"email": email, "password": password}] if email else []
    try:
        users = json.loads(raw)
    except ValueError:
        raise AccountConfigurationError("USERS_JSON 不是有效 JSON") from None
    if not isinstance(users, list) or not users:
        raise AccountConfigurationError("USERS_JSON 必须是非空账号数组")
    accounts = []
    for index, user in enumerate(users, 1):
        if not isinstance(user, dict):
            raise AccountConfigurationError(f"账号 {index} 必须是对象")
        email = user.get("username") or user.get("email")
        password = user.get("password", "")
        if not isinstance(email, str) or not email.strip():
            raise AccountConfigurationError(f"账号 {index} 缺少有效邮箱")
        if not isinstance(password, str):
            raise AccountConfigurationError(f"账号 {index} 的密码必须是字符串")
        accounts.append({"email": email.strip(), "password": password})
    return accounts

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
            print(f"⚠️ Telegram 通知发送失败: HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ Telegram 通知发送异常: {type(e).__name__}")

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

# 注意：轮询用的 JS 用 "return ..." 语句形式，不用 IIFE 表达式。
# 实测部分 chromedriver/selenium 组合（Chrome 153 + selenium 4.49）对
# IIFE 表达式语句一律返回 None，会被误判为"未检测到"。
_EXISTS_JS = ("var i=document.querySelector('input[name=\"cf-turnstile-response\"]');"
              "if(i)return 'input';"
              "var f=document.querySelectorAll('iframe');"
              "for(var k=0;k<f.length;k++){var s=f[k].src||'';"
              "if(s.indexOf('challenges.cloudflare.com')>-1||s.indexOf('/turnstile/')>-1)return 'iframe';}"
              "return '';")

# 页面 HTML 里服务端渲染的 Turnstile 容器；与出口 IP 无关
_TURNSTILE_CONFIGURED_JS = "return document.querySelector('.cf-turnstile, [data-sitekey*=\"0x\"]') !== null;"

_SOLVED_JS = ("var i=document.querySelector('input[name=\"cf-turnstile-response\"]');"
              "return !!(i && i.value && i.value.length > 20);")

_WININFO_JS = """
return {
    sx: window.screenX || 0,
    sy: window.screenY || 0,
    oh: window.outerHeight,
    ih: window.innerHeight
};
"""

# Turnstile 复选框 iframe 的可见包围盒（用于 xdotool 物理点击）
_TURNSTILE_BBOX_JS = """
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
"""

# 在 Turnstile 尚未加载时，尝试点击“启动验证”的入口控件
_TURNSTILE_LAUNCH_CLICK_JS = """
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
"""

# 页面所有 iframe 的 src + 矩形（诊断用）
_IFRAME_MAP_JS = """
var out=[];
var frames=document.querySelectorAll('iframe');
for (var i=0;i<frames.length;i++){
    var f=frames[i], r=f.getBoundingClientRect();
    out.push({ src:(f.src||'').slice(0,80),
               x:Math.round(r.left), y:Math.round(r.top),
               w:Math.round(r.width), h:Math.round(r.height) });
}
return JSON.stringify(out);
"""

# ===== 自动续期相关 =====
# ALTCHA 为 PoW 验证(auto=onsubmit),点击 Renew 提交后由页面自动完成,
# 无需主动点击求解 —— 沿用 wszpwu1 参考仓库的被动等待方案。


#  底层输入工具
def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    sb.execute_script(f"""
    var el = document.querySelector('{selector}');
    if (!el) return;
    var descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value") ||
                     Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value");
    if (descriptor && descriptor.set) {{
        descriptor.set.call(el, "{safe_text}");
    }} else {{
        el.value = "{safe_text}";
    }}
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
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


def _human_warmup(sb):
    """Turnstile 静默不渲染时模拟人类交互：鼠标轨迹 + 页面滚动。

    Cloudflare 的 managed challenge 在判定自动化特征后可能连交互式
    widget 都不渲染；真实鼠标事件（X11 层，非 JS 合成）与滚动有时能
    促使 challenge-platform 重新评估并注入 token。仅在有 DISPLAY 的
    Linux/本机 GUI 环境生效，失败则安静跳过。"""
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        w, h = pyautogui.size()
        # 从中心出发的贝塞尔式鼠标轨迹（多段小步移动）
        cx, cy = w // 2, h // 2
        pyautogui.moveTo(cx, cy, duration=0.3)
        for dx, dy in ((120, -60), (-90, 40), (60, 80), (-100, -50)):
            pyautogui.moveRel(dx, dy, duration=0.25)
        pyautogui.click(cx + 80, cy - 30)  # 页面空白处
    except Exception:
        pass
    # 滚动页面制造 wheel 事件
    try:
        sb.execute_script("""
            window.scrollBy(0, 150);
            setTimeout(function(){ window.scrollBy(0, -150); }, 400);
        """)
    except Exception:
        pass


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


def _switch_to_turnstile_frame(sb):
    """切入页面上的 Turnstile iframe，返回是否成功。"""
    try:
        el = sb.driver.execute_script("""
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
    """点击 Turnstile 并等待 token。容器在 HTML 里但 iframe 未渲染时，
    用回车提交触发 Turnstile 显式 execute（flexible 尺寸初始不渲染），
    页面跳转证明 token 由 JS 自动注入并随表单提交。"""
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    try:
        fm = sb.execute_script(_IFRAME_MAP_JS)
        print(f"  📄 页面 iframe: {fm}")
    except Exception:
        pass

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
                var cands = document.querySelectorAll(
                    '[role="checkbox"], input[type="checkbox"],'+
                    '[class*="checkbox"], [class*="btn-check"]'
                );
                for (var i = 0; i < cands.length; i++){
                    var e = cands[i]; var r = e.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return e;
                }
                return null;
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

    # 等待 Turnstile 渲染（最多 12 秒）。
    # flexible 尺寸下 iframe 可能永不出现，但 HTML 中的 .cf-turnstile 容器
    # 一定存在；容器在而 widget 不渲染通常是 CF 判定自动化嫌疑 ——
    # 先模拟人类交互（真实鼠标轨迹 + 滚动），再看 token 是否被动注入。
    print("⏳ 等待 Turnstile 验证框出现...")
    ts_found = False
    configured = False
    warmed = False
    for i in range(14):
        state = sb.execute_script(_EXISTS_JS) or ""
        if state:
            ts_found = True
            print(f"✅ 检测到 Turnstile（{state}，{i+1}s）")
            break
        if i == 4 and not configured:
            configured = bool(sb.execute_script(_TURNSTILE_CONFIGURED_JS))
        if i == 6:
            if configured and not warmed:
                print("  👤 容器存在但 widget 未渲染；模拟人类交互")
                _human_warmup(sb)
                warmed = True
            elif not configured:
                # 容器都没有：页面可能仍在 CF challenge 或加载中
                try:
                    sb.sleep(1)
                except Exception:
                    pass
        _nudge_turnstile_launcher(sb)
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(1)

    # warmup 后 token 可能已静默注入（invisible 通过）
    if not ts_found and sb.execute_script(_SOLVED_JS):
        print("✅ Turnstile 已静默通过（warmup 后 token 注入）")
        ts_found = True
        state = "silent"

    if ts_found and state != "silent":
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            sb.save_screenshot("login_turnstile_fail.png")
            return False
    elif configured:
        # 容器在但始终未渲染：token 由 Turnstile JS 自动注入 hidden input，
        # 直接提交（首次提交若缺 token 会重定向 error=captcha，再走 handle_turnstile）。
        print("ℹ️ Turnstile 容器存在但未渲染，直接提交观察")
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 敲击回车提交表单...")
    sb.press_keys('input[name="password"]', '\n')

    # 提交后若被 error=captcha 打回且 Turnstile 这时才渲染，补一次处理
    time.sleep(2)
    if "error=" in (sb.get_current_url() or ""):
        err_kind = "captcha" if "captcha" in (sb.get_current_url() or "").lower() else "other"
        if err_kind == "captcha":
            if sb.execute_script(_EXISTS_JS):
                print("↩️ 提交被 captcha 拒绝且 Turnstile 已渲染，重试验证...")
                if not handle_turnstile(sb):
                    sb.save_screenshot("login_turnstile_fail.png")
                    return False
                print("🖱️ 重新提交表单...")
                sb.press_keys('input[name="password"]', '\n')
            elif configured:
                # widget 仍不渲染：再 warmup 一轮后重试一次
                print("↩️ captcha 拒绝且 widget 仍未渲染；再次模拟人类交互后重试")
                _human_warmup(sb)
                time.sleep(3)
                if sb.execute_script(_SOLVED_JS):
                    print("✅ 第二轮 warmup 后 token 注入，重新提交")
                    sb.press_keys('input[name="password"]', '\n')
                elif sb.execute_script(_EXISTS_JS):
                    if handle_turnstile(sb):
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
    if alert_text and _renew_feedback_outcome(alert_text) == "not_due":
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
    if alert_text and _renew_feedback_outcome(alert_text) == "not_due":
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
        var btn = document.querySelector('button[data-bs-target="#renew-modal"]')
                 || document.querySelector('button.btn.btn-outline-primary');
        if (btn) btn.scrollIntoView({behavior:'smooth',block:'center'});
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


def _read_expiry(sb) -> str:
    """Read the current server expiry, used as a submit-independent result check."""
    try:
        return sb.execute_script(r"""
            var m = (document.body.innerText || '').match(
                /Expiry\s*(?:\n\s*|:\s*)(\d{4}-\d{2}-\d{2})/
            );
            return m ? m[1] : '';
        """) or ""
    except Exception:
        return ""


def _visible_renew_feedback(sb):
    """Return visible success/error feedback, excluding stale background alerts."""
    try:
        return sb.execute_script("""
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
        """) or ""
    except Exception:
        return ""


def _renew_feedback_outcome(feedback):
    """Negative feedback takes precedence over the substring 'renewed'."""
    low = feedback.lower()
    if ("can't renew" in low or "cannot renew" in low) and "will be able to" in low:
        return "not_due"
    if any(text in low for text in (
        "can't renew", "cannot renew", "unable to renew", "not eligible",
        "already renewed", "not renewed", "not been renewed", "not successfully renewed",
        "failed", "unsuccessful", "error",
    )):
        return "failure"
    if re.search(r"\b(?:renewed|renewal successful|renew success|extended successfully)\b", low):
        return "success"
    return None


def _expiry_advanced(before, after):
    try:
        return datetime.strptime(after, "%Y-%m-%d") > datetime.strptime(before, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False


def _check_renew_result(sb, expiry_before: str = "") -> bool:
    """Wait for and verify the actual renewal result; return success/failure."""
    print("\n📋 等待并检查真实续期结果...")
    deadline = time.time() + 60
    last_feedback = ""
    while time.time() < deadline:
        feedback = _visible_renew_feedback(sb)
        if feedback:
            last_feedback = feedback
            print(f"  页面状态: {feedback[:180]}")
            if _renew_feedback_outcome(feedback) == "not_due":
                sb.save_screenshot("renew_result.png")
                print("ℹ️ 服务器当前不在可续期窗口内，按无需操作处理")
                send_tg_message("⏳", "未到续期时间", feedback[:500])
                return True
            outcome = _renew_feedback_outcome(feedback)
            if outcome == "failure":
                sb.save_screenshot("renew_result.png")
                send_tg_message("❌", "未能续期", feedback[:500])
                return False
            if outcome == "success":
                sb.save_screenshot("renew_result.png")
                send_tg_message("✅", "续期成功", feedback[:500])
                return True
        time.sleep(2)

    sb.save_screenshot("renew_result.png")
    if expiry_before:
        sb.execute_script("location.reload();")
        time.sleep(6)
        expiry_after = _read_expiry(sb)
        if _expiry_advanced(expiry_before, expiry_after):
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


def _submit_first_renew(sb):
    """点击模态框内第一次 Renew 按钮（ALTCHA auto=onsubmit 会随后自动验证）。"""
    print("🖱️  点击第一次 Renew 按钮...")
    try:
        submit = sb.find_element('div.modal.show button.btn-primary', timeout=5)
        submit.click()
    except Exception:
        sb.execute_script("""
            var m = document.querySelector('div.modal.show');
            if (!m) return;
            var bs = m.querySelectorAll('button');
            for (var i = 0; i < bs.length; i++)
                if (/renew/i.test(bs[i].textContent)) { bs[i].click(); break; }
        """)
    time.sleep(3)


def _confirm_second_renew(sb):
    """处理二次确认弹窗：点第二次 Renew，然后等待被动 ALTCHA 自动完成。"""
    print("\n🔄 检查是否有二次确认弹窗...")
    alert_text = _read_alert(sb)
    if alert_text and ("changing the server type" in alert_text.lower()
                       or "startup command" in alert_text.lower()):
        print("⚠️ 检测到确认弹窗，点击第二次 Renew...")
        clicked = False
        try:
            confirm_btn = sb.find_element('div.modal.show button.btn-primary', timeout=5)
            confirm_btn.click()
            clicked = True
            print("✅ 第二次点击 btn-primary")
        except Exception:
            pass
        if not clicked:
            sb.execute_script("""
                var m = document.querySelector('div.modal.show') || document.body;
                var bs = m.querySelectorAll('button');
                for (var i = 0; i < bs.length; i++) {
                    var t = (bs[i].textContent || '').toLowerCase();
                    if (t.includes('renew') || t.includes('confirm') ||
                        t.includes('ok') || t.includes('continue'))
                        { bs[i].click(); break; }
                }
            """)
            print("✅ JS 第二次点击确认按钮")
    else:
        print("ℹ️ 无二次确认弹窗，继续等待...")
    print("⏳ 等待 30 秒（被动 ALTCHA 自动验证中）...")
    time.sleep(30)


def renew_server(sb) -> bool:
    """登录成功后调用：进入详情页 -> 打开 Renew 模态框 -> 被动 ALTCHA 流程。"""
    print("\n" + "#" * 25)
    print("  开始自动续期流程")
    print("#" * 25)

    if not _goto_server_detail(sb):
        return False

    if not _open_renew_modal(sb):
        return False

    expiry_before = _read_expiry(sb)

    _submit_first_renew(sb)
    _confirm_second_renew(sb)
    return _check_renew_result(sb, expiry_before)


def _run_account(sb_kwargs, email, pwd) -> bool:
    """单个账号：启动浏览器 -> 登录 -> 自动续期。返回是否成功。"""
    from seleniumbase import SB
    from selenium.common.exceptions import WebDriverException

    global CURRENT_EMAIL
    CURRENT_EMAIL = email
    print("🚀 启动浏览器...")
    try:
        with SB(**sb_kwargs) as sb:
            sb.driver.set_page_load_timeout(45)
            sb.driver.set_script_timeout(30)

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
        print(f"\n❌ 账号处理异常: {type(e).__name__}")
        send_tg_message("❌", f"处理异常: {type(e).__name__}", "未知")
        return False


#  脚本执行入口 (可选代理)
def main(argv=None):
    parser = argparse.ArgumentParser(description="Katabump automatic renewal runner")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--notify-failure", action="store_true")
    args = parser.parse_args(argv)
    if args.validate_config:
        return validate_config()
    if args.notify_failure:
        return notify_workflow_failure()
    if validate_config():
        return 1
    accounts = load_accounts()
    mask_workflow_secrets(accounts)
    result = run_renewals(accounts, _run_account, node_attempts=parse_node_attempts())
    if result.failed:
        global CURRENT_EMAIL
        CURRENT_EMAIL = ""
        send_tg_message("❌", "所有可用线路尝试后仍有账号失败",
                        f"{len(result.failed)}/{result.total} 个账号；请查看 Actions 日志")
        return 1
    return 0


def parse_node_attempts():
    return int_setting("NODE_ATTEMPTS", 3, 25)


def validate_config():
    try:
        accounts = load_accounts()
        attempts = parse_node_attempts()
        int_setting("FALLBACK_ATTEMPTS", 10, 50)
        int_setting("RUN_BUDGET_SECONDS", 2400, 3000)
        backups = parse_fallback_proxies(os.environ.get("FALLBACK_PROXIES", ""))
    except ValueError as exc:
        print(f"❌ 配置校验失败：{exc}")
        return 1
    if not accounts:
        print("❌ 配置校验失败：没有可用账号")
        return 1
    if any(not account["password"] for account in accounts):
        print("❌ 配置校验失败：账号缺少密码")
        return 1
    print(f"✅ 配置校验通过：{len(accounts)} 个账号，主线路最多 {attempts} 次尝试，{len(backups)} 个保底代理")
    return 0


def notify_workflow_failure():
    send_tg_message("❌", "GitHub Actions 工作流失败", "请打开 Actions 日志查看失败步骤")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
