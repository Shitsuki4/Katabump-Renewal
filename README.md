# Katabump Renewal

基于 GitHub Actions、SeleniumBase UC 模式和 sing-box 的 Katabump 服务器自动续期工具。支持多账号、订阅自动选节点、按节点纯度排序重试，以及 Telegram 通知。

## 功能

- 多账号批量续期：使用 `USERS_JSON` 配置账号列表。
- 自动代理选择：`SUB_URL` 支持 Clash YAML、sing-box JSON、Base64 节点链接列表。
- 节点纯度排序：优先住宅 IP，其次 ISP、未知类型、数据中心 IP；同级按 proxycheck 风险从低到高排序。
- 自动重试：每个账号按排序池逐个切换节点尝试。
- Telegram 通知：续期结果、未到续期时间、账号异常和工作流失败均可推送。
- CI 自检：配置校验和回归测试在续期前运行。

## 部署

### 1. Fork 或复制仓库

建议先保持私有仓库运行稳定，确认日志和通知正常后再公开。

### 2. 启用 Actions

进入 `Settings → Actions → General`，选择允许所有 Actions，或至少允许本仓库工作流。

### 3. 配置 Secrets

进入 `Settings → Secrets and variables → Actions → Repository secrets`。

#### 必填：账号

`USERS_JSON` 支持多账号，推荐使用：

```json
[
  {"username":"a@example.com","password":"password-a"},
  {"username":"b@example.com","password":"password-b"}
]
```

单账号可改用：

- `KATABUMP_EMAIL`
- `KATABUMP_PASSWORD`

#### 推荐：订阅

- `SUB_URL`：订阅地址。配置后自动抓取、测活、获取出口 IP、排序并生成节点池。
- `PROXY_URL`：单个节点分享链接；未配置 `SUB_URL` 时作为回退。
- 两者都不配置则直连，但 GitHub Actions 的数据中心 IP 很难通过 Turnstile。

#### Telegram 通知

1. 与 `@BotFather` 创建 Bot，获取 `TG_BOT_TOKEN`。
2. 向你的 Bot 发送任意消息，或使用 `@userinfobot` 获取 `TG_CHAT_ID`。
3. 配置两个 Secrets：
   - `TG_BOT_TOKEN`
   - `TG_CHAT_ID`

#### 可选

- `NODE_ATTEMPTS`：每个账号的最大节点尝试次数，默认 `3`，建议不超过节点池大小。

### 4. 手动验证

进入 `Actions → Katabump Auto Renew → Run workflow`，选择 `main` 分支运行。

配置校验、测试、节点选择、代理启动和续期全部通过后，工作流会显示绿色成功。

## 工作流

1. 校验账号 JSON、密码和重试次数。
2. 运行单元测试。
3. 下载 sing-box。
4. 抓取订阅并并行测活。
5. 获取存活节点出口 IP，识别 IP 类型并查询风险分。
6. 按纯度排序，生成 `config.json` 与 `ranked_pool.json`。
7. 启动本机 HTTP 代理 `127.0.0.1:8080`。
8. 使用浏览器完成登录、Turnstile、ALTCHA 和续期。
9. 失败时按排序切换节点重试，并发送 Telegram 通知。

默认定时：每天 UTC `00:00`，即北京时间 `08:00`。

## 本地运行

需要 Python 3.12 和 Chrome。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
seleniumbase install chromedriver
```

Linux 无显示环境建议使用 `xvfb-run`：

```bash
export USERS_JSON='[{"username":"a@example.com","password":"password"}]'
export SUB_URL='https://example.com/subscription?singbox'
python auto_proxy.py
python proxy_runtime.py &
xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python main.py
```

只校验配置而不启动浏览器：

```bash
python main.py --validate-config
```

## 仓库结构

- `main.py`：登录、验证、续期、重试、Telegram 通知和配置校验。
- `auto_proxy.py`：订阅抓取、协议解析、节点测活、纯度排序和配置生成。
- `proxy_runtime.py`：sing-box 配置检查、启动和就绪验证。
- `.github/workflows/renew.yml`：GitHub Actions 工作流。
- `tests/`：配置校验与节点排序回归测试。
- `requirements.txt`：Python 依赖。

## 诊断

- 查看 Actions 日志中的 `Auto-select proxy node` 步骤，可看到每个节点是否可达、出口 IP、类型和风险分。
- 查看 `Run Renew Script` 步骤，可看到当前出口 IP、节点固定和续期结果。
- 截图上传默认关闭；如需开启，在 `Settings → Secrets and variables → Actions → Variables` 添加 `UPLOAD_SCREENSHOTS=true`。

## 安全提示

- 账号、密码、订阅地址、Telegram Token 只能放在 GitHub Secrets。
- 不要提交 `config.json`、`ranked_pool.json`、日志或截图；`.gitignore` 已忽略这些运行期文件。
- 公开仓库前建议删除包含订阅、账号或出口 IP 的历史 Actions 记录。
- 自动化绕过站点验证可能违反目标网站条款，请确认你有合法授权后再使用。
