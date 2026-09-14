# Katabump Renewal

GitHub Actions 驱动的 Katabump 多账号续期工具，使用 SeleniumBase 和 sing-box。支持订阅节点排序、逐节点重试、**失败后才启用的认证代理保底**，以及 Telegram 通知。

## 配置

在 `Settings → Secrets and variables → Actions → Secrets` 添加：

| Secret | 用途 |
| --- | --- |
| `USERS_JSON` | 必填账号数组，示例见下方；也支持单账号 `KATABUMP_EMAIL` + `KATABUMP_PASSWORD` |
| `SUB_URL` | 首选订阅；支持 sing-box JSON、Clash YAML、明文或 Base64 分享链接列表 |
| `PROXY_URL` | 可选第二线路；支持分享链接、订阅 URL、含认证信息的 HTTP/HTTPS 代理 URL |
| `FALLBACK_PROXIES` | 可选保底代理列表；只有前面的线路失败才启用 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 可选 Telegram 通知 |

账号格式（`username` 与 `email` 都支持）：

```json
[
  {"email": "a@example.com", "password": "password-a"},
  {"email": "b@example.com", "password": "password-b"}
]
```

账号配置必须是非空数组，每项都需要邮箱和密码；错误条目不会再被静默丢弃。配置校验失败会返回非零退出码，Actions 会明确显示失败。

### 保底代理

将代理列表粘贴到 **`FALLBACK_PROXIES` Secret**，每行一个，不要写入仓库：

```text
192.0.2.10:8080:username:password
http://username:password@192.0.2.11:8080
https://username:password@proxy.example.com:443
socks5://username:password@192.0.2.12:1080
```

- `host:port:username:password` 默认是 HTTP CONNECT 代理。
- 也支持 JSON 字符串数组、IPv6 和 URL 编码的用户名/密码；URL 中的 `@`、`:` 等特殊字符请百分号编码。
- 精确重复项会去重，其他节点按配置顺序尝试，最多配置 50 个。
- 认证在本机 sing-box 中完成，浏览器只连接 `127.0.0.1:8080`，不依赖 Chrome 的代理认证弹窗。
- **主线路正常时，不准备、不测活、不使用保底节点。**

### 重试设置

在 Actions 的 **Variables** 页添加可选参数：

| Variable | 默认值 | 说明 |
| --- | --- | --- |
| `NODE_ATTEMPTS` | `3` | 每个主线路来源最多尝试多少个不同节点，范围 1–25；兼容同名 Secret，Secret 优先 |
| `FALLBACK_ATTEMPTS` | `10` | 保底最多尝试多少个不同节点，范围 1–50；不受主线路重试上限截断 |
| `RUN_BUDGET_SECONDS` | `2400` | 达到时间预算后不再启动新尝试，范围 1–3000；当前已开始的浏览器操作会完成有界等待 |
| `UPLOAD_SCREENSHOTS` | 关闭 | 设置为 `true` 才上传诊断截图，保留 1 天；截图可能包含账号信息 |

## 执行顺序

```text
SUB_URL 订阅节点（有配置时）
    ↓ 获取失败 / 无可用节点 / 连接或续期重试失败
PROXY_URL（有配置时）
    ↓ 仍有失败账号
FALLBACK_PROXIES（有配置时）
    ↓ 所有线路失败或耗尽时间预算
非零退出码 + 失败通知
```

没有 `SUB_URL` / `PROXY_URL` 时先直连，直连失败后再用保底。配置了主代理时，不会在失败后擅自降级直连。

- 已成功续期或被明确确认“尚未到续期时间”的账号，不会在保底阶段重复执行。
- 每次切换必须经本机控制接口确认，不会偷偷沿用失败节点或回到延迟优先的随机节点。
- 坏的订阅条目单独隔离，不会让一个不支持的 `flow` / 字段拖垮整个节点池。
- 代理进程就绪检测不依赖首个出口是否在线；节点不可用时仍能继续切换。
- 只停止本次运行自己创建的 sing-box，不使用全局 `pkill`。
- “成功”要求页面明确成功提示或到期日向后推进；错误提示里出现 `renewed` 不会误报成功。

## 运行与检查

进入 `Actions → Katabump Auto Renew → Run workflow`，选择 `main`。

定时任务每 6 小时执行一次：UTC `00:17 / 06:17 / 12:17 / 18:17`，即北京时间 `08:17 / 14:17 / 20:17 / 02:17`。GitHub 定时任务可能延迟；增加检查频率可以降低一次订阅故障就错过续期窗口的风险，但不能保证第三方服务永远可用。

关注 `Run renewal with automatic failover` 步骤：

- `Route source: subscription`：正在使用首选订阅。
- `Source ... failed ... advancing to next tier`：准备或启动失败，进入下一线路。
- `Route source: fallback`：已经进入保底代理。
- 作业摘要展示完成/失败账号数与实际使用的线路层级，不包含密码。

正常续期工作流不会因为代码 push 自动运行；push/PR 会运行独立的 Linux/Windows 回归测试，不读取账号或代理 Secrets。

## 本地运行

需要 Python 3.12、Chrome。Linux 无显示环境需要 Xvfb；Windows 直接启动浏览器。

```bash
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/install_singbox.py
seleniumbase install chromedriver
```

通过环境变量设置账号和所需代理，**不要把真实凭据写进脚本或 Git 历史**。

```bash
python main.py --validate-config
python -m unittest discover -s tests -v
# Linux:
xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python main.py
# Windows:
python main.py
```

`main.py` 现在负责整个代理生命周期，无需另外后台启动 sing-box。高级用法仍支持：

```bash
# 仅准备配置；默认在来源准备失败时自动尝试下一来源
python auto_proxy.py
# 只准备某一层，不会执行续期
python auto_proxy.py --source fallback
```

已有外部代理可以通过 `IS_PROXY=true` 和 `PROXY_SERVER` 使用；不要同时让外部进程与本程序占用 `8080` / `9099`。

## 回归测试与安全

- 测试包含订阅超时、单个坏节点隔离、主线路成功不触发保底、10 个保底节点不被主重试上限截断、多账号只重试失败者、进程清理及失败退出码。
- 安装 sing-box 后，测试还会用真实二进制验证 VLESS flow 兼容性，并通过本机模拟 HTTP CONNECT 服务验证代理认证，不使用任何真实账号。
- sing-box 固定为 `1.13.16`，下载后校验固定 SHA-256；不执行未校验的下载文件。
- Secrets 只在运行时读取；配置文件设置为仅所有者可读写，忽略 Git 并在 Actions 结束时清理。日志不输出订阅 URL、原始节点名或代理认证信息。
- GitHub token 只用于仓库维护，**不需要**添加为续期工作的 Secret。曾公开发送的 token、密码应撤销/轮换。
- 请确认你有权自动化管理对应账号，并遵守目标平台条款。代理可达不代表一定能通过 Cloudflare/Turnstile，也无法恢复已经被平台永久删除的服务器。
