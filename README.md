# MCP Manager

跨 Agent 的 MCP 管理工具：扫描、检查、备份、同步和升级 MCP 服务。

每个 AI Agent 的 MCP 配置都不一样：

- 路径不同：Trae、VS Code、OpenCode、Codex、Claude Code 各放一处；
- 格式不同：JSON、带注释的 JSONC、TOML；
- 字段不同：VS Code 要 `type: stdio`，OpenCode 要 `command` 数组和 `environment`，Copilot 还会自动加 `tools`。

本项目把这些差异全部收敛到 **声明式 Agent profile** 中，核心代码不认识任何具体 Agent。新增一个 Agent 只需要加一个 TOML 文件，不需要改代码。

## 设计

- `mcpmanager/`：与 Agent 无关的内核（注册表、扫描、格式读写、版本查询、诊断、同步）。
- `mcpmanager/agents/*.toml`：每个 Agent 的路径、格式和字段差异。
- 写入采用 **候选 → 审阅 → 应用**：先生成候选文件，确认后才写入，写入前自动备份。
- JSON/JSONC 只替换顶层 MCP 区块，保留注释和无关配置；TOML 只替换 `[mcp_servers*]` 表组。
- 个人数据（注册表、备份、候选、报告）全部在 `local/`，不会进入版本库。

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

# 查询 npm / PyPI 最新版本
python -m mcpmanager updates

# 生成候选，检查无误后再应用
python -m mcpmanager sync --agent trae
python -m mcpmanager sync --agent trae --apply

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

- 左侧 Agent 侧栏，绿点表示该 Agent 的配置文件已存在；
- 顶部搜索、列排序、右键菜单（诊断、改版本、更新、移除登记）；
- 下方详情面板显示启动命令、参数、脱敏后的环境变量和配置来源；
- 备份管理器可列出、恢复或打开历史备份；
- 可直接修改当前 Agent 的配置位置（支持 `D:/`、`E:/` 等绝对路径），保存为 `local/agents/<id>.toml` 本机覆盖，并可一键恢复默认；
- 支持浅色/深色主题；窗口大小、所选 Agent、日志状态会记住（`local/settings.json`）；
- 扫描与版本检查在后台执行，状态栏显示进度，运行日志可折叠。

快捷键：`F5` 刷新、`Ctrl+R` 扫描、`Ctrl+U` 检查更新、`Ctrl+F` 搜索、`Ctrl+B` 备份、`Ctrl+D` 诊断、`Esc` 清空搜索。

## CLI

| 命令 | 作用 |
| --- | --- |
| `agents` | 列出 Agent profile、配置路径和是否存在 |
| `list [--agent ID]` | 列出某个 Agent 已配置的 MCP |
| `scan [--json]` | 扫描本机配置，输出未登记 MCP 报告 |
| `doctor --agent ID [--network] [--env]` | 检查 command 路径、参数路径，可选探测远程 URL |
| `updates` | 对比注册表版本与 npm/PyPI 最新版本 |
| `sync --agent ID [--apply] [--yes]` | 生成候选；`--apply` 备份后写入 |
| `apply 候选文件 [--yes]` | 应用指定候选（含安装候选） |
| `add NAME --command ... [--arg ...] [--env K=V]` | 登记一个 MCP |
| `remove NAME` | 从注册表移除（不改 Agent 配置） |
| `catalog [关键词]` | 查看内置 MCP 目录 |
| `install NAME --agent ID [--apply]` | 从目录安装 |
| `gui` | 打开桌面界面 |
| `paths` | 显示数据目录 |

## 目录结构

```text
mcpmanager/                核心代码
  agents/*.toml            Agent profile
  catalog/servers.json     内置可安装 MCP 目录
  formats.py               JSON / JSONC / TOML 读写与局部替换
  adapters.py              标准字段 <-> 各 Agent 字段
  sync.py                  候选生成、备份、应用
  discovery.py             扫描
  doctor.py                诊断
  versioning.py            npm / PyPI 版本
  cli.py, gui.py           CLI 与图形界面
local/                     个人数据（已 gitignore）
  registry.json            主注册表
  backups/                 每次写入前的备份
  generated/               候选配置
  reports/                 扫描报告
  agents/                  用户私有 profile（同 id 覆盖内置）
scripts/                   随项目发布的 MCP 启动包装器
tests/                     fixture 测试
```

可用环境变量：

- `MCP_MANAGER_HOME`：整个项目根目录；
- `MCP_MANAGER_DATA`：仅数据目录；
- `MCP_MANAGER_PROFILES`：额外的 profile 目录。

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

私有路径或团队内部 Agent 可以放到 `local/agents/`，同 `id` 会覆盖内置 profile。

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

## 安全模型

- 不会静默改 Agent：先写候选，应用时校验源文件哈希，变化则拒绝。
- 每次写入前备份到 `local/backups/`。
- 不把内嵌凭据复制进候选；如果某个 MCP 在 Agent 配置里写了明文密钥，会停止生成，除非注册表里有对应的无密启动方式。
- 只替换 MCP 区块，其余配置和注释保持不变；无法安全替换时中止而不是整文件覆盖。
- profile 标记 `readonly` 时永久跳过写入。

## 开发

```powershell
python -m unittest discover -s tests -v
```

GitHub Actions 会在 Windows 和 Ubuntu 上、Python 3.11/3.12 下运行同一套测试。

测试覆盖：profile 加载、别名解析、Agent 字段转换、版本固定、JSONC 注释保留、TOML 表组替换、候选生成与应用、过期候选拒绝、明文密钥拦截。

## 从旧版迁移

旧版把数据放在 `registry/`、`backups/`、`generated/`。首次运行时会自动把 `registry/mcp-registry.json` 复制为 `local/registry.json`。建议随后手动把旧备份和候选移到 `local/` 下，旧目录即可删除。

## License

[MIT](LICENSE)
