# 更新说明

本项目的重要变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.5.0] - 2026-11-27

### 新增

- 新增 `mcpmanager context [--no-wsl] [--json]` 命令与 GUI「上下文审计」（`Ctrl+K`）：
  - 区分每个 Agent 的 MCP 路由模式（`routed` 单网关折叠 / `hybrid` / `direct` 直连未折叠）；
  - 检查 Skill 目录折叠状态，识别 `~/.agents/skills` 公共目录导致的上下文泄漏（`leaked_shared`）与目录软链接/Junction（`symlink_shared`），防止误覆盖或误删共享物理目录；
  - 检测同一配置文件中指向同一物理目录的重复插件条目（如 OpenCode 重复插件）；
  - 支持安全只读扫描本机 WSL 发行版中的 Pi、Codex 与 Antigravity 状态及软链接关系。

## [1.4.1] - 2026-11-25

### 修复

- 修复关闭弹窗后点击“刷新”或切换主题时，已销毁输入框回调触发 `TclError` 的问题；统一通过 `_prune_themed()` 捕获 `TclError` 并清理失效回调。

## [1.4.0] - 2026-11-25

### 新增

- 新增 `status` 命令（`--json` 输出 `schemaVersion: 2`），汇总每个 Agent 的检测状态、配置状态、有效路径、junction/符号链接重定向、MCP 数量、来源分布、最近握手状态与多路径/冲突提示。
- 新增 `local/managed.json` 受管标记与 `local/audit.jsonl` 同步审计记录，并提供 `mcpmanager audit` 命令查看同步历史。
- 新增来源与能力分层（`managed` / `magpie_managed` / `user_config` / `plugin_host` / `remote` / `backup` / `candidate` / `market_cache`），同步时自动检测冲突并禁止静默覆盖用户手动配置或 Magpie 管理条目。
- GUI Agent 侧栏改为根据本机实际配置动态显示（含状态色点、MCP 数、健康计数、多来源提示与无 Agent 空状态），主表新增“来源”列并阻止将 Magpie 管理的 MCP 重复登记为普通本地 MCP。

### 修复

- Codex profile 改为跟随 `CODEX_HOME`，支持迁移到 E 盘等自定义目录的安装，并在未设置时回退 `~/.codex/config.toml`。
- 扫描器将 `CODEX_HOME` 纳入有效配置来源，避免把 Codex 误报为未配置。
- `detect_agent()` 将 `plugin_only` 正确标记为 `detected: true`，并逐文件记录多配置路径的生效来源与被隐藏来源。
- 候选差异与日志文本统一对 `Bearer`、`ApiKey`、`github_pat_`、`ghp_`、`sk-`、`XERJ_AUTH` 等凭据脱敏。
- 修复 GUI 刷新侧栏重建后旧控件主题回调残留可能触发 `TclError` 的问题。

### 测试

- 新增 `tests/test_status_and_sync.py`，完整测试套件达到 98 项（Windows 下 97 通过、1 项 POSIX 专用用例按平台跳过）。

## [1.3.3] - 2026-11-15

### 修复

- `install --apply` 改为先展示将写入的差异，确认后才登记注册表、备份并写入 Agent 配置；取消时不再留下注册表变更。`--apply --yes` 仍跳过确认，保持脚本可用。

### 测试

- 新增 4 项 `install` 测试：取消时不留注册表变更、确认后写入、`--yes` 跳过确认、不带 `--apply` 只生成候选。

## [1.3.2] - 2026-11-15

### 新增

- GUI 右键菜单新增“从当前 Agent 卸载…”：生成只删除该 MCP 的候选配置，审阅差异后再应用，写入前自动备份。
- 新增 `mcpmanager uninstall` 命令，用于从指定 Agent 配置中卸载 MCP，支持 `--apply` 和 `--yes`。
- `sync.uninstall_candidate()` 生成卸载候选，复用现有的候选审阅和新鲜度校验机制；注册表定义保留，便于之后重新安装。

### 修复

- 卸载时按 `registry.canonical()` 匹配实际配置键名，支持大小写差异和别名（如注册表 `playwright` 对应配置 `Playwright`，`laya-decision` 对应 `laya`）。
- `adapters.META_KEYS` 补入 `updateCommand`，避免生成安装/替换候选时把更新命令写入 Agent 配置。

### 测试

- 新增 3 项卸载测试：应用后条目消失、保留其他 MCP、不存在时抛错。
- 新增大小写、别名匹配和 `updateCommand` 剥离测试。

## [1.3.1] - 2026-11-15

### 视觉

- 新增应用图标：深蓝底 + 青色 M 字母 + 五个连接节点，象征 Manager 连接多个 MCP/Agent。
- GUI 窗口和 Windows EXE 均使用该图标；`assets/icon.svg` 为设计源文件，`icon.ico`/`icon.png` 为产物。

## [1.3.0] - 2026-11-14

### 新增

- GUI 新增「操作 → 添加 MCP…」入口，可填写 command/URL、参数、环境变量、包名和版本，登记后自动为当前选中的 Agent 生成候选配置。
- 环境变量改为逐行 key/value 输入，值可包含分号和等号，不再被错误拆分。
- 已在注册表中的 MCP 可直接为新 Agent 生成候选，无需重复填写定义；自动把新 Agent 追加到该 MCP 的 `agents` 列表。
- 新增 `creation.build_server_spec` 校验模块，覆盖名称必填、command/URL 二选一、URL 格式、参数解析、环境变量非空与去重、版本关联包名等规则。
- 新增 `sync.attach_agent_and_install`，封装「追加 Agent → 生成候选」事务并返回是否修改注册表。

### 安全

- 新建 MCP 和复用已有 MCP 两条分支都会检测疑似密钥环境变量，只提示变量名不显示值，并建议改用系统环境变量或密钥文件。
- 登记流程使用注册表副本生成候选，候选成功后才保存注册表，避免候选失败留下半完成状态。
- 候选文件名对 MCP 名称做 URL 编码，防止特殊字符导致文件名非法。

### 测试

- 新增 `tests/test_creation.py`，覆盖输入校验、密钥检测、环境变量值含分号/等号等场景。
- `tests/test_core.py` 新增 `attach_agent_and_install` 的三项测试：追加 Agent、Agent 已在列表时跳过、MCP 不存在时报错。
- 全量测试通过：68 passed、1 skipped。

## [1.2.2] - 2026-11-14

### 安全

- 拒绝把 `cmd`、PowerShell、`bash`、`sh`、WSL 等 shell 解释器作为自定义更新命令入口。
- 拒绝直接执行任意 `.cmd`/`.bat`，只允许 `npm`、`npx`、`pnpm`、`yarn`、`corepack` 的包装脚本。
- 对允许的 `.cmd` 包装脚本，额外拒绝 `^`、`()` 等 cmd 元字符；对所有命令参数拒绝 `&`、`|`、`;`、`<`、`>`，避免 `safe&whoami` 这类内容被 `cmd /c` 解释。
- 修正安全说明：更新命令是用户确认后执行的任意本地程序，不是沙箱；`shell=False` 只表示管理器不主动调用 shell，不限制被启动程序再启动 shell。

### 文档

- README 增加“平台范围”：Windows 优先，Linux/macOS 仅保证核心模块和通用配置逻辑，未覆盖真实 Agent 安装路径。
- SECURITY.md 和 GUI 提示同步更新更新命令的安全边界表述。

### 测试

- 新增 5 项更新命令安全测试：嵌入元字符、Windows/POSIX shell 入口、任意 `.cmd`、允许脚本中的 cmd 元字符。
- 全量测试通过：Windows 下 58 passed、1 skipped；Linux CI 覆盖 POSIX shell 入口。

## [1.2.1] - 2026-11-14

### 修复

- 握手结束后等待读取线程退出，并显式关闭 stdin/stdout/stderr，消除测试中的 `ResourceWarning`。
- 测试中的 HTTP fixture 在结束时关闭子进程管道。

### 改进

- CI 新增打包任务：构建 wheel 和 sdist，安装 wheel 后运行 `mcpmanager --help` 和 `mcpmanager agents`。
- `pyproject.toml` 补充 Homepage、Repository、Issues、Changelog、关键词、分类器，以及 Python 3.11/3.12 元数据。
- 将 `tests/test_p2_features.py` 纳入版本控制，确保 CI 执行 P2 测试。

### 测试

- 全量测试保持 54 项通过，并在 `-W error::ResourceWarning` 下通过。

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
