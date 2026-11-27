<div align="center">

<img src="assets/icon.png" alt="MCP Manager" width="128" height="128">

# MCP Manager

**跨 Agent 的 MCP 管理工具：扫描、检查、备份、同步和升级 MCP 服务。**

[![Release](https://img.shields.io/github/v/release/1254084397/mcp-manager?include_prereleases&style=flat-square&color=0ea5e9)](https://github.com/1254084397/mcp-manager/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078d4?style=flat-square&logo=windows&logoColor=white)](https://github.com/1254084397/mcp-manager)
[![License](https://img.shields.io/badge/License-MIT-10b981?style=flat-square)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/1254084397/mcp-manager/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/1254084397/mcp-manager/actions)

[更新记录](CHANGELOG.md) · [安全边界](SECURITY.md) · [参与贡献](CONTRIBUTING.md) · [行为准则](CODE_OF_CONDUCT.md)

</div>

---

每个 AI Agent 的 MCP 配置都不一样：

- 路径不同：Trae、VS Code、OpenCode、Codex、Claude Code 各放一处；
- 格式不同：JSON、带注释的 JSONC、TOML；
- 字段不同：VS Code 要 `type: stdio`，OpenCode 要 `command` 数组和 `environment`，Copilot 还会自动加 `tools`。

本项目把这些差异全部收敛到 **声明式 Agent profile** 中，核心代码不认识任何具体 Agent。新增一个 Agent 只需要加一个 TOML 文件，不需要改代码。

## 设计

- `mcpmanager/`：与 Agent 无关的内核（注册表、扫描、格式读写、版本查询、诊断、同步）。
- `mcpmanager/agents/*.toml`：每个 Agent 的路径、格式和字段差异。
- 每个 Agent 可以声明一份主配置和多份附加配置；读取时合并，写入只改主配置，附加配置只读。
- 写入采用 **候选 → 审阅 → 应用**：`sync`、`uninstall` 和不带 `--apply` 的 `install` 先生成候选文件，确认后才写入；带 `--apply` 的 CLI 命令会先展示将写入的差异，确认后再登记注册表、备份并直接写入，取消时注册表和 Agent 配置都不变。
- `install NAME --agent ID --apply --yes` 跳过交互确认，保持适合脚本和 CI 的直接写入语义。
- JSON/JSONC 只替换顶层 MCP 区块，保留注释和无关配置；TOML 只替换 `[mcp_servers*]` 表组。
- 个人数据（注册表、备份、候选、报告）全部在 `local/`，不会进入版本库。

## 平台范围

**Windows 优先。** 内置 Agent profile 主要按 Windows 的用户目录、`%APPDATA%` 和 `%LOCALAPPDATA%` 设计，已在 Windows 上验证扫描、诊断和真实握手。Linux/macOS 目前只保证核心模块、格式读写、扫描和握手逻辑可运行；未覆盖真实 Agent 安装路径，也不代表这些平台上的 Agent 集成已经验证。Ubuntu CI 用于保护内核和 CLI，不改变这一支持范围。

## 快速开始

需要 Python 3.11+。

```powershell
# 列出已支持的 Agent 及其配置路径
python -m mcpmanager agents

# 查看某个 Agent 已配置的 MCP
python -m mcpmanager list --agent trae

# 扫描本机所有 MCP 配置，报告写到 local/reports/
python -m mcpmanager scan

# 检查命令和本地运行时
python -m mcpmanager doctor --agent trae --env

# 真实握手：启动 MCP，执行 initialize 和 tools/list
python -m mcpmanager probe --agent trae

# 只测试一个 MCP，并输出 JSON
python -m mcpmanager probe --agent trae --name context-mode --timeout 15 --json

# 查看上次握手缓存
python -m mcpmanager health --agent trae

# 汇总每个 Agent 的实际配置路径、MCP 数量和最近健康状态
python -m mcpmanager status

# 只读模式：允许扫描、诊断、握手和报告，禁止写入、安装、升级
python -m mcpmanager --read-only scan
python -m mcpmanager --read-only doctor --agent trae

# 查询 npm / PyPI 最新版本
python -m mcpmanager updates

# 生成候选，检查无误后再应用
python -m mcpmanager sync --agent trae
python -m mcpmanager sync --agent trae --apply

# 从 Agent 配置中卸载某个 MCP（生成候选 → 审阅 → 应用）
python -m mcpmanager uninstall --agent trae some-mcp
python -m mcpmanager uninstall --agent trae some-mcp --apply

# 图形界面
python -m mcpmanager gui
```

也可以安装为命令：

```powershell
pip install -e .
mcpmanager agents
```

打包单文件 exe：

```powershell
.\build.ps1
# 产物：dist\MCPManager.exe
```

## 图形界面

`python -m mcpmanager gui` 打开桌面管理器：

- 左侧 Agent 侧栏只显示本机实际检测到的 Agent；未发现配置的内置 profile 不再显示为灰色条目。CLI 仍可通过显式 Agent ID 管理未初始化的 profile；
- 顶部搜索、列排序、右键菜单（诊断、握手测试、改版本、更新、从注册表移除、从当前 Agent 卸载）；
- 可在“操作 → 添加 MCP…”中填写 command/URL、参数、环境变量和版本信息，登记后自动生成当前 Agent 的候选配置；
- 添加流程只针对当前选中的 Agent，应用前可审阅候选和差异，写入时自动备份；管理器不会因此自动安装 MCP 包或浏览器等依赖；
- 右键“从当前 Agent 卸载…”会生成只删除该 MCP 的候选配置，审阅差异后应用，写入前自动备份；注册表中的定义保留，便于之后重新安装。
- 下方详情面板显示启动命令、参数、脱敏后的环境变量、来源文件和上次握手缓存；附加配置中的 MCP 标记为“附加配置·只读”；
- 候选预览可以查看 JSON 和 unified diff，确认后再应用；
- 备份管理器可列出、恢复或打开历史备份；
- 可直接修改当前 Agent 的配置位置（支持 `D:/`、`E:/` 等绝对路径），保存为 `local/agents/<id>.toml` 本机覆盖，并可一键恢复默认；
- 支持浅色/深色主题；窗口大小、所选 Agent、日志状态会记住（`local/settings.json`）；
- 扫描与版本检查在后台执行，状态栏显示进度，运行日志可折叠。

快捷键：`F5` 刷新、`Ctrl+R` 扫描、`Ctrl+U` 检查更新、`Ctrl+F` 搜索、`Ctrl+B` 备份、`Ctrl+D` 诊断、`Esc` 清空搜索。

## CLI

| 命令 | 作用 |
| --- | --- |
| `agents` | 列出 Agent profile、主配置、附加配置及是否存在 |
| `list [--agent ID]` | 列出某个 Agent 已配置的 MCP（合并主配置与附加配置） |
| `scan [--json]` | 扫描本机配置，按来源分级并输出未登记报告 |
| `doctor --agent ID [--network] [--env] [--json]` | 检查配置、command 路径、参数路径和本地运行时，可选探测远程 URL；`--json` 写入 `local/reports/` |
| `probe --agent ID [--name MCP] [--timeout 秒] [--json]` | 真实启动 MCP，执行 initialize 和 tools/list；结果写入握手缓存 |
| `health [--agent ID] [--json]` | 查看上次握手缓存（`local/health.json`） |
| `status [--json]` | 汇总检测状态、有效/重定向路径、MCP 数量、来源分布、冲突与最近健康状态（`--json` 为 `schemaVersion: 2`） |
| `audit [--agent ID] [--limit N] [--json]` | 查看 `local/audit.jsonl` 中的增量同步审计记录 |
| `context [--no-wsl] [--json]` | 审计各 Agent（含 WSL 只读）的 MCP 路由折叠、Skill 上下文开销、公共目录泄漏与重复插件 |
| `updates` | 对比注册表版本与 npm/PyPI 最新版本，区分已是最新、有新版本、当前更高、无法比较 |
| `sync --agent ID [--apply] [--yes]` | 生成候选；`--apply` 备份后写入 |
| `apply 候选文件 [--yes]` | 应用指定候选（含安装候选） |
| `add NAME --command ... [--arg ...] [--env K=V]` | 登记一个 MCP |
| `remove NAME` | 从注册表移除（不改 Agent 配置） |
| `uninstall NAME --agent ID [--apply] [--yes]` | 从 Agent 配置卸载；默认生成卸载候选，`--apply` 确认后备份并写入 |
| `catalog [关键词]` | 查看内置 MCP 目录 |
| `install NAME --agent ID [--apply] [--yes]` | 从目录安装；不带 `--apply` 时生成候选，带 `--apply` 时确认后备份并直接写入 |
| `gui` | 打开桌面界面 |
| `paths` | 显示数据目录 |

全局选项 `--read-only`（或环境变量 `MCP_MANAGER_READ_ONLY=1`）会禁止写入注册表和 Agent 配置、安装、升级、应用候选和删除条目，但保留扫描、诊断、握手、报告和握手缓存读取。

## 目录结构

```text
mcpmanager/                核心代码
  agents/*.toml            Agent profile
  catalog/servers.json     内置可安装 MCP 目录
  formats.py               JSON / JSONC / TOML 读写与局部替换
  adapters.py              标准字段 <-> 各 Agent 字段
  sync.py                  候选生成、增量合并、冲突保护、备份、应用
  provenance.py            受管标记（managed.json）与同步审计记录（audit.jsonl）
  context_audit.py         MCP 路由/Skill 上下文开销、重复插件与 WSL 只读审计
  discovery.py             扫描与配置来源分级
  doctor.py                配置与运行时诊断
  handshake.py             MCP 协议握手（initialize / tools/list）
  health.py                上次握手结果缓存
  versioning.py            npm / PyPI 版本与版本比较
  cli.py, gui.py           CLI 与图形界面
local/                     个人数据（已 gitignore）
  registry.json            主注册表
  backups/                 每次写入前的备份
  generated/               候选配置
  reports/                 扫描与诊断报告
  health.json              上次握手结果
  agents/                  用户私有 profile（同 id 覆盖内置）
scripts/                   随项目发布的 MCP 启动包装器
tests/                     fixture 测试
```

可用环境变量：

- `MCP_MANAGER_HOME`：整个项目根目录；
- `MCP_MANAGER_DATA`：仅数据目录；
- `MCP_MANAGER_PROFILES`：额外的 profile 目录；
- `MCP_MANAGER_READ_ONLY`：设为 `1`/`true`/`yes`/`on` 时启用只读模式。

## 新增一个 Agent

在 `mcpmanager/agents/` 下新增 TOML 即可，例如：

```toml
id = "my-agent"
name = "My Agent"
config = "{appdata}/MyAgent/mcp.json"
format = "json"
schema = "mcpServers"
command = "string"
env = "env"
transport = ""
aliases = ["myagent"]
```

字段说明：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `id` / `name` | 字符串 | 唯一标识和显示名 |
| `config` | 路径模板 | 支持 `{home}`、`{appdata}`、`{localappdata}`、`{project}`、`{data}` |
| `configs` | 数组 | 附加只读配置路径；主配置优先，缺失自动跳过 |
| `auxiliary_optional` | 布尔 | 附加配置是否可选，默认 `true`；设为 `false` 时缺失会标记为“必选” |
| `config_priority` | 字符串 | 目前只支持 `primary-first`：主配置优先，附加配置只读 |
| `format` | `json` / `jsonc` / `toml` | 配置文件格式 |
| `schema` | 字符串 | 存放 MCP 的顶层键，如 `mcpServers`、`servers`、`mcp`、`mcp_servers` |
| `command` | `string` / `array` | `array` 表示写成 `["node", "server.js"]` |
| `env` | 字符串 | 环境变量键名，如 `env` 或 `environment` |
| `transport` | 空 / `type` | 需要显式 `type` 字段时填 `type` |
| `stdio_type` / `http_type` | 字符串 | 例如 `stdio`/`http`、`local`/`remote` |
| `url_key` | 字符串 | 远程 MCP 的地址字段名，默认 `url` |
| `enabled` | 布尔 | 是否补 `enabled = true` |
| `tools` | 布尔 | 是否补 `tools = ["*"]` |
| `readonly` | 布尔 | 只读 profile，禁止写入 |
| `aliases` | 数组 | 命令行可用的别名 |
| `notes` | 字符串 | 备注 |

私有路径或团队内部 Agent 可以放到 `local/agents/`，同 `id` 会覆盖内置 profile。加载时会校验必填字段、字段类型、未知字段和未知路径占位符，错误会指出文件名。

## 注册表与版本

`local/registry.json` 记录期望状态：

```json
{
  "servers": {
    "playwright": {
      "kind": "npm-on-demand",
      "package": "@playwright/mcp",
      "versionPolicy": "pin-before-sync",
      "version": "0.0.85",
      "agents": ["trae", "vscode"]
    }
  }
}
```

- `versionPolicy = "pin-before-sync"`：同步时把 `args` 里的包版本固定为 `version`；
- `kind` 含 `npm` 走 npm 查询，含 `uv`/`python`/`pip` 走 PyPI；
- `agents` 使用 profile 的 `id`，`"*"` 表示全部。

版本比较按数字逐段进行，并识别 `v` 前缀、预发布后缀（`rc`/`beta`/`dev`）和 `post` 版本。结果分为：已是最新、有新版本、当前版本更高、无法比较、未安装、查询失败。`latest`、`next`、commit SHA 等非数字版本会归为“无法比较”，不会提示升级。

## 诊断与握手

两者解决不同层次的问题：

- `doctor`：静态检查。确认命令、脚本、参数路径、运行时和远程 URL 是否存在。`--json` 会同时写入 `local/reports/doctor-<agent>.json`。
- `probe`：动态握手。真正启动 MCP，发送 `initialize`，再请求 `tools/list`，并区分启动失败、初始化超时、协议错误和鉴权失败。`--json` 适合 CI 和脚本使用。
- `health`：读取上次握手缓存。缓存只表示上次 `probe` 的结果，不代表 Agent 已加载或业务工具可用。
- 具体业务工具：握手成功只证明服务可用和工具能列出；工具是否需要 API Key、浏览器、项目目录或特定数据，仍要按 MCP 单独做代表性只读调用。

图形界面中右键某个 MCP，可以分别运行“诊断此 MCP”和“运行握手测试”；候选预览中提供“查看差异”。

## 使用边界

以下四件事不是一回事：

| 状态 | 含义 | 如何确认 |
| --- | --- | --- |
| 配置文件存在 | 扫描到了配置 | `agents` / `scan` |
| MCP 进程能启动 | 命令、参数和运行时可用 | `doctor` |
| 协议能握手 | `initialize` 和 `tools/list` 成功 | `probe` |
| 工具能正常调用 | 业务功能、鉴权、外部依赖正常 | 需要按 MCP 单独调用 |

“当前生效配置”是根据 Agent profile 推断的配置来源，不代表 Agent 内部一定按该优先级加载。插件、工作区配置和 Agent 内置配置的真实优先级由各 Agent 自己决定。

## 安全模型

- 不会静默改 Agent：先写候选，应用时校验源文件哈希，变化则拒绝。
- 每次写入前备份到 `local/backups/`。
- 不把内嵌凭据复制进候选；如果某个 MCP 在 Agent 配置里写了明文密钥，会停止生成，除非注册表里有对应的无密启动方式。
- 只替换 MCP 区块，其余配置和注释保持不变；无法安全替换时中止而不是整文件覆盖。
- profile 标记 `readonly` 时永久跳过写入。
- `--read-only` / `MCP_MANAGER_READ_ONLY=1` 会阻止写入注册表、写入 Agent 配置、安装、升级、应用候选和删除条目，适合首次试用和安全审查。
- 自定义更新命令会在执行前显示完整命令、执行目录和来源，并要求确认；它是确认后执行的任意本地程序，不是沙箱，`shell=False` 只表示管理器不主动调用 shell 解析命令。
- “添加 MCP”中填写的环境变量值会进入本地注册表和候选配置；疑似密钥字段会在保存前提示。优先使用 Agent 或系统提供的密钥注入方式，避免粘贴长期凭据。
- 管理器拒绝 `cmd`、PowerShell、`bash` 等解释器作为更新入口，拒绝任意 `.cmd`/`.bat`，只允许 npm/npx/pnpm/yarn/corepack 的 `.cmd` 包装脚本，并拒绝参数中的 shell 元字符。
- 握手测试只对用户明确选择的 MCP 启动进程，结束后清理进程树，不会在扫描时自动启动服务。

## 开发

```powershell
# 推荐
python -m pytest -q

# 也可以只用标准库
python -m unittest discover -s tests -v
```

GitHub Actions 会在 Windows 和 Ubuntu 上、Python 3.11/3.12 下运行同一套测试。

测试覆盖：profile 加载与 schema 校验、别名解析、Agent 字段转换、多配置合并、版本固定与数字版本比较、只读模式、JSONC 注释保留、TOML 表组替换、候选生成与应用、候选差异、过期候选拒绝、明文密钥拦截，以及 stdio/HTTP 握手、初始化超时、进程提前退出、协议错误、HTTP 401/403、SSE 响应和子进程树清理。

## 从旧版迁移

旧版把数据放在 `registry/`、`backups/`、`generated/`。首次运行时会自动把 `registry/mcp-registry.json` 复制为 `local/registry.json`。建议随后手动把旧备份和候选移到 `local/` 下，旧目录即可删除。

## License

[MIT](LICENSE)
