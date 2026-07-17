# Grok Account Manager

本地 Grok 账号管理应用，围绕现有 [`grok-register-mint`](../grok-register-mint) 注册工具补齐账号归档、批量巡检和 token 失效后的批量登录。

> 仅用于你有权管理的账号。自动化注册与登录可能受目标站点条款、验证码和风控策略限制，请遵守服务条款与当地法律。

## 已实现能力

- 本地 Web 管理端：账号列表、搜索、状态筛选、批量操作和实时任务日志
- 接入 `grok-register-mint/register_cli.py`，支持注册数量、注册并发、CPA mint 并发
- 注册完成后自动导入 `email----password----sso` 和对应 `xai-*.json`
- 在管理端编辑参考项目常用注册配置，也可完整编辑其 `config.json`
- 同时巡检注册 SSO cookie 与 CPA access token：
  - 本地解析两类 JWT 及 CPA `expired` 到期时间
  - SSO 通过只读的 `accounts.x.ai/account` 跳转结果确认会话
  - CPA 在线请求 `cli-chat-proxy.grok.com/v1/models`
  - 区分正常、过期、凭据无效、限流、待登录和网络异常
- 对过期、无效或缺少 SSO 的账号批量登录
  - 复用参考项目的真实 Chrome/DrissionPage 登录与 OAuth mint 流程
  - 同一次浏览器登录刷新 SSO 与 CPA OAuth 凭据
  - 登录结果逐账号回写管理库和 `xai-*.json`，随后自动复核
- 同一套服务同时提供 CLI，方便无界面操作和故障排查

## 设计关系

```text
Grok Account Manager
├── 本地 SQLite：账号索引、状态、巡检/登录时间
├── 注册桥接 ───────────→ grok-register-mint/register_cli.py
├── 配置桥接 ───────────→ grok-register-mint/config.json
├── 巡检服务 ───────────→ accounts.x.ai/account + CPA /models
└── 批量登录 worker ────→ grok_register.cpa_xai.mint
```

管理端不会复制或修改参考项目的注册逻辑。浏览器自动化在独立子进程中运行，使用“配置与环境”里指定的参考项目 Python。管理端自身保持零第三方依赖。本地 HTTP 服务仅绑定回环地址并校验 `Host`，账号、配置、任务详情与全部写操作都要求页面内的随机请求令牌。

## 环境要求

管理端：

- Python 3.9+
- 现代浏览器

注册与批量登录：

- `/Users/puhuan/Desktop/project/mySpace/grok-register-mint`，或在界面中选择其他路径
- 参考项目要求的 Python 3.13、`DrissionPage`、`curl_cffi`
- Chrome/Chromium 与可访问 xAI/Grok、临时邮箱 API 的网络

推荐给参考项目创建独立环境：

```bash
cd /Users/puhuan/Desktop/project/mySpace/grok-register-mint
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

随后按参考项目 README 配好临时邮箱、代理和 CPA。管理端的“配置与环境 → 环境检查”会检查项目结构、Python、依赖和邮箱基础配置。

## 启动

无需安装即可运行：

```bash
cd /Users/puhuan/Desktop/project/mySpace/gork-manager
python3 run.py
```

也可以安装为本地命令：

```bash
python3 -m pip install -e .
grok-manager ui
```

命令会启动 `http://127.0.0.1:8787` 并打开默认浏览器。若不希望自动打开：

```bash
python3 -m grok_manager ui --no-browser
```

首次启动会创建：

```text
data/
├── accounts.sqlite3    # 账号管理库
├── auths/              # 没有原 CPA 目录时的新凭据
└── jobs/               # 任务目录；登录输入中的密码会在任务结束后删除
```

`data/` 与 `config.json` 已被 `.gitignore` 排除。目录权限会尽量设置为 `0700`，数据库和含敏感信息的配置会尽量设置为 `0600`。

## 推荐工作流

1. 打开“配置与环境”，确认参考项目目录和 Python。
2. 在“注册常用配置”填写临时邮箱、域名、代理和 CPA 配置。
3. 运行“环境检查”，所有项目通过后再注册。
4. 在“批量注册”设置数量和并发，启动任务。
5. 注册结束后账号会自动进入“账号与巡检”。历史产物可点“导入产物”。
6. 点“巡检全部”；已过期或在线返回 401/403 的账号会进入异常状态。
7. 点“登录异常账号”，确认后会启动真实浏览器批量重新获取 token。

## CLI

```bash
# 检查参考环境
python3 -m grok_manager config check

# 导入参考项目全部历史产物
python3 -m grok_manager import

# 查看账号（不会输出密码或 token）
python3 -m grok_manager list
python3 -m grok_manager list --json

# 巡检全部；--local 只检查本地到期时间
python3 -m grok_manager inspect --all
python3 -m grok_manager inspect --all --local

# 登录所有巡检判定为过期/无效/待登录的账号
python3 -m grok_manager login --expired

# 调用参考项目注册并自动导入
python3 -m grok_manager register --count 10 --threads 2 --mint-workers 2
```

## 测试

测试通过公开服务接口运行，并用临时目录隔离数据库、任务输入和凭据文件：

```bash
python3 -m unittest discover -v
```

## 状态含义

| 状态 | 含义 | 建议动作 |
| --- | --- | --- |
| 未巡检 | 新导入或刚更新 token | 运行巡检 |
| 正常 | 本地有效期与在线探测通过 | 无 |
| 已过期 | 本地到期或在线返回 401 | 批量登录 |
| 凭据无效 | 缺少必要凭据或在线返回 403 | 检查密码后批量登录 |
| 待登录 | 有邮箱密码但缺少 SSO cookie | 批量登录 |
| 受限但有效 | 在线返回 429 | 等待限流恢复 |
| 巡检异常 | 网络、代理或服务端异常 | 检查配置后重试 |

## 数据边界

- “删除”只删除管理库索引，不修改参考项目原始账号和 CPA 文件。
- 导入同一邮箱时，非空的新密码、SSO、access token 会更新旧记录；空字段不会擦除已有凭据。
- 批量登录后的 SSO 优先于更旧的 `accounts.txt`，启动自动导入不会把新凭据覆盖回旧值。
- CLI 和界面列表不会显示密码、SSO、access token 或 refresh token。
- SSO 在线巡检只把 SSO cookie 发往 `accounts.x.ai/account`，使用 GET 且不修改账号。
- CPA 在线巡检只把 access token 发往 CPA `base_url` 的 `/models`；若凭据文件指定了 `base_url`，优先使用该地址。
- 批量登录会将密码写入权限收紧的临时 JSON，子进程退出后立即删除。

## 当前机器首次运行提示

如果环境检查显示 `No module named 'DrissionPage'`，说明管理端可运行，但参考项目的注册 Python 尚未安装依赖。请给参考项目创建 Python 3.13 虚拟环境并在管理端选择其 `.venv/bin/python`。
