# Grok Account Manager

自包含的本地 Grok 账号管理应用，提供账号注册、配置、归档、批量巡检和 token 失效后的批量登录，不需要另行下载其他项目。

> 仅用于你有权管理的账号。自动化注册与登录可能受目标站点条款、验证码和风控策略限制，请遵守服务条款与当地法律。

## 已实现能力

- 本地 Web 管理端：账号列表、搜索、状态筛选、批量操作和实时任务日志
- 内置注册与 OAuth mint 运行时，支持注册数量、注册并发、CPA mint 并发
- 注册完成后自动导入 `email----password----sso` 和对应 `xai-*.json`
- 在管理端编辑常用注册配置，也可完整编辑注册 JSON
- 同时巡检注册 SSO cookie 与 CPA access token：
  - 本地解析两类 JWT 及 CPA `expired` 到期时间
  - SSO 通过只读的 `accounts.x.ai/account` 跳转结果确认会话
  - CPA 在线请求 `cli-chat-proxy.grok.com/v1/models`
  - 区分正常、过期、凭据无效、限流、待登录和网络异常
- 对过期、无效或缺少 SSO 的账号批量登录
  - 复用内置的真实 Chrome/DrissionPage 登录与 OAuth mint 流程
  - 同一次浏览器登录刷新 SSO 与 CPA OAuth 凭据
  - 登录结果逐账号回写管理库和 `xai-*.json`，随后自动复核
- 同一套服务同时提供 CLI，方便无界面操作和故障排查

## 设计关系

```text
Grok Account Manager
├── 本地 SQLite：账号索引、状态、巡检/登录时间
├── 内置注册运行时 ─────→ grok_register.cli
├── 本地注册配置 ───────→ data/registration-config.json
├── 巡检服务 ───────────→ accounts.x.ai/account + CPA /models
└── 批量登录 worker ────→ grok_register.cpa_xai.mint
```

注册运行时代码和 Turnstile 扩展随本项目一起安装。浏览器自动化仍在独立子进程中运行，避免阻塞管理端。本地 HTTP 服务仅绑定回环地址并校验 `Host`，账号、配置、任务详情与全部写操作都要求页面内的随机请求令牌。

## 环境要求

- Python 3.9+
- 现代浏览器
- 通过本项目安装的 `DrissionPage`、`curl_cffi`、`requests`
- Chrome/Chromium 与可访问 xAI/Grok、临时邮箱 API 的网络

推荐为本项目创建虚拟环境并一次性安装全部依赖：

```bash
cd /Users/puhuan/Desktop/project/mySpace/gork-manager
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/grok-manager ui
```

随后在“配置与环境”中设置临时邮箱、代理和 CPA。环境检查会验证内置运行时、Python、依赖和邮箱基础配置。

## 启动

只查看管理端时可以直接运行；执行注册或批量登录前仍需安装本项目依赖：

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
├── manager-config.json       # 管理端配置
├── registration-config.json  # 注册、邮箱、代理和 CPA 配置
├── accounts.sqlite3          # 账号管理库
├── registration-output/      # 注册账号与 CPA 产物
├── auths/                    # 批量登录生成的新凭据
└── jobs/                     # 临时任务；登录输入会在任务结束后删除
```

`data/` 已被 `.gitignore` 排除。目录权限会尽量设置为 `0700`，数据库和含敏感信息的配置会尽量设置为 `0600`。

## 推荐工作流

1. 在“注册常用配置”填写临时邮箱、域名、代理和 CPA 配置。
2. 运行“环境检查”，所有项目通过后再注册。
3. 在“批量注册”设置数量和并发，启动任务。
4. 注册结束后账号会自动进入“账号与巡检”。历史产物可点“导入产物”。
5. 点“巡检全部”；已过期或在线返回 401/403 的账号会进入异常状态。
6. 点“登录异常账号”，确认后会启动真实浏览器批量重新获取 token。

## CLI

```bash
# 检查内置注册环境
python3 -m grok_manager config check

# 导入本应用全部历史产物
python3 -m grok_manager import

# 查看账号（不会输出密码或 token）
python3 -m grok_manager list
python3 -m grok_manager list --json

# 巡检全部；--local 只检查本地到期时间
python3 -m grok_manager inspect --all
python3 -m grok_manager inspect --all --local

# 登录所有巡检判定为过期/无效/待登录的账号
python3 -m grok_manager login --expired

# 调用内置运行时注册并自动导入
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

- “删除”只删除管理库索引，不修改注册账号和 CPA 产物文件。
- 导入同一邮箱时，非空的新密码、SSO、access token 会更新旧记录；空字段不会擦除已有凭据。
- 批量登录后的 SSO 优先于更旧的 `accounts.txt`，启动自动导入不会把新凭据覆盖回旧值。
- CLI 和界面列表不会显示密码、SSO、access token 或 refresh token。
- SSO 在线巡检只把 SSO cookie 发往 `accounts.x.ai/account`，使用 GET 且不修改账号。
- CPA 在线巡检只把 access token 发往 CPA `base_url` 的 `/models`；若凭据文件指定了 `base_url`，优先使用该地址。
- 批量登录会将密码写入权限收紧的临时 JSON，子进程退出后立即删除。

## 当前机器首次运行提示

如果环境检查显示 `No module named 'DrissionPage'`，说明源码模式下尚未安装本项目依赖。运行 `python3 -m pip install -e .`，然后用同一个 Python 启动管理端。

## 上游许可

内置注册运行时基于 MIT 许可的 `grok-register-mint`，上游版权与许可原文保存在 `grok_register/LICENSE.upstream`。运行时源码和扩展已包含在本仓库及构建产物中，使用时不访问或导入上游项目目录。
