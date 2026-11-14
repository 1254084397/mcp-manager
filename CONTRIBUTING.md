# 贡献指南

感谢参与 MCP Manager。项目以 Windows 为主要平台，同时保持核心模块可在 Linux/macOS 上测试。

## 开发环境

- Python 3.11 或 3.12
- 不需要运行时第三方依赖
- 图形界面使用标准库 `tkinter`

```powershell
git clone https://github.com/1254084397/mcp-manager.git
cd mcp-manager
python -m pytest -q
python -m mcpmanager agents
```

如果只使用标准库：

```powershell
python -m unittest discover -s tests -v
```

## 目录约定

- `mcpmanager/`：核心逻辑，不依赖 GUI。
- `mcpmanager/agents/*.toml`：Agent 差异，只声明路径和字段映射，不写代码。
- `mcpmanager/catalog/servers.json`：内置 MCP 目录。
- `local/`：个人数据（注册表、备份、候选、报告、握手缓存），不要提交。
- `tests/`：标准库 unittest 风格，pytest 也能直接收集。

## 修改原则

- 写入 Agent 配置必须走“候选 → 审阅 → 应用”，应用前备份，并保留源文件哈希校验。
- 不要直接覆盖整个配置文件；JSON/JSONC 只替换 MCP 区块，TOML 只替换 MCP 表组。
- 不要执行不可信的命令，不要恢复 `shell=True`。
- 新增 Agent 时优先改 `agents/*.toml`；只有字段结构无法用声明表达时才改 `adapters.py`。
- 不把密钥、令牌、真实用户路径写入测试、示例或文档。
- 新增依赖前先说明理由；当前项目目标是零运行时依赖。

## 测试要求

- 修复 bug 时补充能复现问题的测试。
- 修改握手、版本比较、配置写入、只读模式或注册表时，必须补测试。
- 提交前运行：

```powershell
python -m compileall -q mcpmanager tests
python -m pytest -q
```

## 提交与 Pull Request

- 一个 PR 聚焦一件事，避免把无关重构混进来。
- 提交信息使用 `feat:`、`fix:`、`docs:`、`test:`、`chore:` 前缀。
- PR 描述中说明：改了什么、为什么改、如何验证、是否影响已发布配置。
- 如果改动涉及安全问题，请先阅读 [SECURITY.md](SECURITY.md)，不要公开敏感细节。

## Agent profile 检查清单

- `id`、`config` 非空，路径使用 `{home}`、`{appdata}`、`{localappdata}`、`{project}`、`{data}`。
- 附加配置写在 `configs`，默认只读；可选来源标记 `auxiliary_optional = true`。
- `format` 只能是 `json`、`jsonc`、`toml`。
- `command` 只能是 `string` 或 `array`。
- 不要声明不存在或无法验证的路径顺序；不清楚时在 `notes` 中说明。
