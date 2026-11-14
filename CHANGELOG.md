# 更新说明

本项目的重要变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.2.0] - 2026-11-14

### 新增

- **数字版本比较**：`updates` 和 GUI 不再只判断“不相等”，而是按数字逐段比较，识别 `v` 前缀、`rc`/`beta`/`dev` 预发布和 `post` 版本。
- **版本状态**：已是最新、有新版本、当前版本更高、无法比较、未安装、查询失败；当前版本更高时不会提示升级或降级。
- **只读模式**：`--read-only` 或 `MCP_MANAGER_READ_ONLY=1` 禁止写入注册表、写入 Agent 配置、安装、升级、应用候选和删除条目，保留扫描、诊断、握手和报告。
- **Profile schema 校验**：加载时检查必填字段、字段类型、未知字段和未知路径占位符，错误会指出文件名。
- **可选附加配置**：新增 `auxiliary_optional` 和 `config_priority` 字段；`agents` 和 GUI 会区分“可选未发现”和“必选缺失”。
- **握手 JSON**：`probe --json` 输出 agent、每个 MCP 的状态、阶段、工具数量和工具列表。
- **握手缓存**：`probe` 结果写入 `local/health.json`，新增 `health` 命令，GUI 详情显示上次握手状态。
- **诊断报告导出**：`doctor --json` 输出机器可读报告，并写入 `local/reports/doctor-<agent>.json`。
- **候选差异**：GUI 候选预览新增“查看差异”，显示当前配置与候选配置的 unified diff。
- **开源协作文件**：新增 `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、Issue 模板和 PR 模板。

### 改进

- `agents` 对附加配置显示“只读，存在 / 可选未发现 / 必选缺失”。
- README 补充只读模式、版本状态、JSON 输出、握手缓存和候选差异说明。
- 测试覆盖版本比较、只读模式、profile 校验、候选差异和握手缓存。

### 测试

- 自动测试从 29 项增加到 45 项，全部通过。

## [1.1.1] - 2026-11-14

### 安全

- 移除自定义更新命令中的 `shell=True`。`updateCommand` 现在按参数数组执行，也支持在注册表中直接写成数组。
- 执行前在图形界面显示完整命令、执行目录和来源，并要求确认。
- 拒绝 `&&`、`||`、`|`、`;`、`>`、`<` 等 shell 操作符，避免把注入内容当作命令解释。
- 增加 [SECURITY.md](SECURITY.md)，说明本地数据、网络请求、进程执行和漏洞报告方式。

### 改进

- 握手结束时清理整个进程树：Windows 使用 `taskkill /T /F`，POSIX 使用独立进程组。
- README 新增“使用边界”，明确配置存在、进程启动、协议握手、业务工具可用是四种不同状态；并说明“当前生效配置”是根据 profile 推断，不代表 Agent 实际加载优先级。
- README 的开发命令同时列出 `pytest` 和 `unittest`。

### 测试

- 新增 `tests/test_handshake.py` 及 stdio / HTTP fixture，覆盖 initialize、tools/list、无工具、初始化超时、进程提前退出、HTTP 401、SSE 响应和子进程树清理。
- 新增更新命令解析测试：Windows 路径引号、shell 操作符拒绝、参数数组执行。
- 自动测试从 17 项增加到 29 项。

## [1.1.0] - 2026-11-14

### 新增

- **多配置源合并**：Agent profile 支持 `configs` 声明附加配置。主配置优先，附加配置只读合并；附加文件缺失时自动跳过，不会报错。
- **Antigravity 双配置模型**：主配置为 `~/.gemini/config/mcp_config.json`，并把 `~/.gemini/antigravity/mcp_config.json` 声明为可选只读附加来源。
- **真实 MCP 握手测试**：新增 `handshake.py` 和 `probe` 命令。会真正启动 stdio MCP（或请求远程 HTTP MCP），依次执行 `initialize`、`notifications/initialized`、`tools/list`，返回服务信息和工具列表。
- **握手错误分级**：区分配置缺失、启动失败、初始化超时、协议错误和鉴权失败；GUI 右键菜单新增“运行握手测试”。
- **扫描来源分级**：扫描结果区分当前生效配置、用户配置、插件配置、市场缓存、历史备份、候选配置。未登记警告只针对当前生效配置，缓存/插件/备份单独列出。
- **宿主级识别**：由 Agent 运行时提供的 MCP 不计入“未登记”警告。
- **右键登记**：从当前 Agent 配置中发现、但主注册表没有的 MCP，可直接登记，随后参与版本、更新和移除管理。
- **自定义更新命令**：注册表条目可以配置自己的更新命令，不局限于 npm / PyPI。

### 改进

- `mcpmanager agents` 会展示附加只读配置路径，并标注“存在”或“缺失，自动跳过”。
- `mcpmanager list` 会显示每个 MCP 的来源文件，附加配置来源标记为“附加配置·只读”。
- `doctor` 使用合并后的配置进行诊断，不再只看主配置文件。
- `scan` 报告新增 `effectiveServers`、`userServers`、`cachedServers` 和对应的未登记分组。
- PyPI 版本查询增加重试，并支持 `PIP_INDEX_URL` / `UV_INDEX_URL` 和清华镜像回退。
- 注册表已登记但当前 Agent 未安装的 MCP 会明确显示为“注册表已登记（未安装）”。

### 验证

- `python -m unittest discover -s tests -v`：17 项全部通过。
- `python -m mcpmanager probe --agent antigravity --timeout 15`：laya、playwright、context-mode、xerj 均握手成功。
- `python -m mcpmanager scan`：32 个有效配置、29 个 MCP 名称、17 个当前生效 MCP，当前生效但未登记的 MCP 为 0。

### 说明

握手成功只表示服务能启动、协议能握手、工具能列出，不代表每个工具的业务功能、鉴权或外部依赖都正常。具体工具仍需按 MCP 做代表性调用。

## [1.0.0] - 初始版本

- 声明式 Agent profile，支持 JSON、JSONC、TOML。
- 配置扫描、静态诊断、候选生成、备份与应用。
- npm / PyPI 版本查询。
- 命令行与桌面图形界面。
