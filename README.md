# Katabump-Renewal

基于 GitHub Actions + SeleniumBase (UC 模式) 的 Katabump 免费服务器自动续期脚本。支持多账号、自动订阅选节点、失败自动换节点重试、Telegram 通知。

## 工作原理

Katabump 登录页有 Cloudflare Turnstile 人机验证，续期时还有 ALTCHA 验证。GitHub Actions runner 是数据中心 IP，直连通过验证的概率极低，所以流程是：

1. （推荐）`SUB_URL` 模式：`auto_proxy.py` 抓取订阅，用单个 sing-box 并行探测所有节点连通性，再并行取存活节点的出口 IP，按出口 IP 去重后做 IP 纯净度检测（proxycheck.io 风险分，高风险的脏 IP 过不了 Cloudflare Turnstile，会被排到队尾），按 住宅 > ISP > 数据中心、风险分从低到高排序，生成带 selector 的 `config.json` 和排序表 `ranked_pool.json`。
2. 启动 sing-box 作为本机 HTTP 代理（127.0.0.1:8080），SeleniumBase 无头浏览器走代理完成登录 + 续期。
3. 每个续期尝试都会通过 Clash API 把 selector 固定到纯度排名对应的那一个节点（第 1 次用排名 1，第 2 次用排名 2……），保证每次重试都换一个不同的干净出口；排名用尽后回退到 urltest 延迟自动选择。
4. 结果通过 Telegram 推送，截图/日志存入 Actions Artifacts。

## Secrets 配置

`Settings → Secrets and variables → Actions`：

### 账号（必填，二选一）

- `USERS_JSON`（推荐，支持多账号）：
  ```json
  [{"username":"a@example.com","password":"pwd"},{"username":"b@example.com","password":"pwd"}]
  ```
- `KATABUMP_EMAIL` + `KATABUMP_PASSWORD`（单账号）。

### 代理（推荐配 `SUB_URL`）

- `SUB_URL`：机场/自建订阅链接，支持 Clash YAML、sing-box JSON、base64 分享链接（vless/vmess/hy2/trojan/anytls/tuic/ss/socks5）。配了就全自动选节点。
- `PROXY_URL`：`SUB_URL` 未配时的回退，单个节点分享链接。
- `POOL_JSON`（可选，仅配合 `PROXY_URL` 为 anytls 协议时使用）：节点拓扑 JSON（含 server/port/sni，不含密码），用于把单节点链接展开成多节点 urltest 池。格式：
  ```json
  [{"name":"node-a","server":"example.com","port":11000,"sni":"a.example.com"}]
  ```
- 都不配则直连，基本过不了 Turnstile，不推荐。

> 节点信息一律放 Secrets，不要提交到仓库。仓库中不含任何节点地址（早期版本的 `pool.json` 已移除并改写历史）。

### 通知（可选）

- `TG_BOT_TOKEN` / `TG_CHAT_ID`：Telegram 通知。

### 其他（可选）

- `NODE_ATTEMPTS`：每个账号最多换节点重试次数，默认 3。

## 部署

1. Fork 或新建私有仓库，推送本代码。
2. 填好 Secrets（至少 `USERS_JSON` + `SUB_URL`）。
3. Fork 的默认关闭 Actions，需要在 Actions 页面手动启用。
4. `Actions → Katabump Auto Renew → Run workflow` 手动跑一次验证。
5. 排错看 Artifacts 里的截图、`singbox.log`、`config.json`。

定时：每天 UTC 00:00（北京时间 08:00）。

## 仓库结构

- `main.py`：登录、Turnstile/ALTCHA 验证、续期、重试、TG 通知主逻辑
- `auto_proxy.py`：SUB_URL 模式，并行选节点生成 `config.json`
- `proxy_handler.py`：PROXY_URL 单节点/POOL_JSON 池模式回退
- `.github/workflows/renew.yml`：定时工作流
- `login.json.template`：本地运行用的账号示例
- `start_chrome.bat`：本地调试时带远程调试端口启动 Chrome

## 注意事项

- 节点测活必须在 runner 真实环境下进行：很多机场封 GitHub/Azure 的 IP，本地能连的节点在 runner 上未必通，反之亦然。
- IP 纯净度检测走 proxycheck.io 免 key 额度（100 次/天），只对去重后的前 30 个出口 IP 检测；接口不可用时自动降级为仅按 IP 类型排序，不影响主流程。
- 仓库 `.gitignore` 已忽略运行期生成的 `config.json`、`pool.json`、`ranked_pool.json`、`singbox.log` 等含敏感信息的文件。
