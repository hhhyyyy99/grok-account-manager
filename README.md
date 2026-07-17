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

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Chrome/Chromium
- 可访问 xAI/Grok、临时邮箱 API 的网络

项目通过 `.python-version` 固定 Python 3.13，并通过 `uv.lock` 锁定全部 Python 依赖。首次运行执行：

```bash
cd grok-account-manager
uv python install 3.13
uv sync --locked
uv run --locked python run.py
```

`uv sync` 会自动创建根目录下的 `.venv` 并安装项目、`DrissionPage`、`curl_cffi`、`requests` 等全部依赖，不需要手工执行 `venv` 或 `pip install`。随后在“配置与环境”中设置临时邮箱、代理和 CPA；环境检查会验证内置运行时、Python、依赖和邮箱基础配置。

## 启动

日常启动直接通过 `uv` 运行根目录启动器：

```bash
cd grok-account-manager
uv run --locked python run.py
```

启动后会打开 `http://127.0.0.1:8787`。不自动打开浏览器或更换端口时直接传参数，不需要再写 `ui`：

```bash
uv run --locked python run.py --no-browser
uv run --locked python run.py --port 9000
```

Linux/macOS 也可以这样运行：

```bash
uv run --locked ./run.py
```

如果 `.venv` 尚不存在，直接执行 `python3 run.py` 或 `./run.py` 时，启动器也会自动转交给已安装的 `uv` 创建环境。CLI 子命令继续通过同一个启动器使用：

```bash
uv run --locked python run.py config check
uv run --locked python run.py list
uv run --locked python run.py login --expired
```

源码目录运行时，首次启动会创建：

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

通过 wheel 安装时，可变数据不会写进 `site-packages`：macOS 默认使用 `~/Library/Application Support/grok-account-manager`，Linux 使用 `$XDG_DATA_HOME/grok-account-manager`（未设置时为 `~/.local/share/grok-account-manager`），Windows 使用本地 AppData。可用 `GROK_MANAGER_DATA_DIR` 覆盖。

从旧版升级时，应用会在首次启动时一次性迁移旧安装目录中的账号库和管理配置，并复制旧注册项目中的 `config.json` 和 `output/`。迁移完成后只使用本应用数据目录，后续启动不再读取旧位置；旧目录不存在也不影响运行。

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
uv run --locked python run.py config check

# 导入本应用全部历史产物
uv run --locked python run.py import

# 查看账号（不会输出密码或 token）
uv run --locked python run.py list
uv run --locked python run.py list --json

# 巡检全部；--local 只检查本地到期时间
uv run --locked python run.py inspect --all
uv run --locked python run.py inspect --all --local

# 登录所有巡检判定为过期/无效/待登录的账号
uv run --locked python run.py login --expired

# 调用内置运行时注册并自动导入
uv run --locked python run.py register --count 10 --threads 2 --mint-workers 2
```

## 测试

测试通过公开服务接口运行，并用临时目录隔离数据库、任务输入和凭据文件：

```bash
uv run --locked python -m unittest discover -v
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

如果环境检查显示 `No module named 'DrissionPage'`，说明项目环境尚未同步。运行 `uv sync --locked`，然后用 `uv run --locked python run.py` 启动管理端。

## 上游许可

内置注册运行时基于 MIT 许可的 `grok-register-mint`，上游版权与许可原文保存在 `grok_register/LICENSE.upstream`。运行时源码和扩展已包含在本仓库及构建产物中，使用时不访问或导入上游项目目录。
