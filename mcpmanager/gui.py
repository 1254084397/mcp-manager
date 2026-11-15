"""Desktop shell over the agent-agnostic core.

The UI intentionally keeps all Agent-specific knowledge in ``agents/*.toml``;
this module only renders whatever the core reports.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
import queue
import shutil
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__, adapters, creation, discovery, doctor, formats, handshake, health, paths, profiles, registry, sync, versioning

FONT = "Microsoft YaHei UI"
MONO = "Consolas"
REPO_URL = "https://github.com/1254084397/mcp-manager"


def _package_token(args: list[str], separator: str) -> tuple[str | None, str | None]:
    """Pick the package (and inline version) from a package-runner argument list."""
    for item in args:
        token = str(item)
        if not token or token.startswith("-"):
            continue
        if separator == "@":
            if token.startswith("@"):
                scope, slash, rest = token.partition("/")
                if not slash:
                    return token, None
                package, _, version = rest.partition("@")
                return f"{scope}/{package}", version or None
            package, _, version = token.partition("@")
            return package, version or None
        package, _, version = token.partition(separator)
        return package, version or None
    return None, None


LIGHT = {
    "bg": "#f1f5f9",
    "surface": "#ffffff",
    "surface2": "#f8fafc",
    "text": "#0f172a",
    "muted": "#64748b",
    "border": "#e2e8f0",
    "sidebar": "#0f172a",
    "sidebar_text": "#cbd5e1",
    "sidebar_active": "#1e293b",
    "header": "#0f172a",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "ok": "#16a34a",
    "warn": "#d97706",
    "danger": "#dc2626",
    "info": "#2563eb",
    "plugin": "#7c3aed",
}

DARK = {
    "bg": "#0b1220",
    "surface": "#111c2e",
    "surface2": "#0f172a",
    "text": "#e2e8f0",
    "muted": "#94a3b8",
    "border": "#243247",
    "sidebar": "#070d18",
    "sidebar_text": "#94a3b8",
    "sidebar_active": "#152238",
    "header": "#070d18",
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "danger": "#f87171",
    "info": "#60a5fa",
    "plugin": "#c084fc",
}


class App:
    def __init__(self) -> None:
        paths.ensure_layout()
        self.profiles = profiles.load_profiles()
        if not self.profiles:
            raise RuntimeError("没有找到任何 Agent profile，无法启动。")

        self.settings = self._load_settings()
        self.dark = bool(self.settings.get("dark"))
        self.agent_id = self.settings.get("agent") if self.settings.get("agent") in self.profiles else (
            "trae" if "trae" in self.profiles else next(iter(self.profiles))
        )
        self.sort_column = "MCP"
        self.sort_reverse = False
        self.busy = False
        self.rows: dict[str, dict] = {}
        self._themed: list = []
        self._buttons: list[tuple[tk.Button, str]] = []
        self._sidebar_rows: dict[str, dict] = {}

        self._enable_dpi()
        self.root = tk.Tk()
        self.root.title(f"MCP Manager {__version__}")
        if paths.ICON_PATH.exists():
            try:
                self.root.iconbitmap(str(paths.ICON_PATH))
            except tk.TclError:
                pass
        self._apply_geometry()
        self.root.minsize(980, 620)
        self.root.report_callback_exception = self._report_exception
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_menu()
        self._build_header()
        self._build_status()
        self._build_body()
        self._bind_keys()
        self.apply_theme()
        self.refresh_agents()
        self.refresh_servers()
        self.set_status("就绪（只读模式）" if paths.read_only() else "就绪")
        self._set_details("在左侧选择 Agent，再选择一条 MCP 查看详情。")
        self.root.after(250, self._init_sash)
        if self.settings.get("log_visible", True) is False:
            self._toggle_log()
        self.root.after(300, lambda: self.log("就绪。选择左侧 Agent 查看其 MCP。"))
        self.root.mainloop()

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _enable_dpi() -> None:
        if os.name != "nt":
            return
        try:
            from ctypes import windll

            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    def _load_settings(self) -> dict:
        file = paths.DATA_DIR / "settings.json"
        try:
            return json.loads(file.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def _apply_geometry(self) -> None:
        width, height = 1280, 800
        saved = str(self.settings.get("geometry", ""))
        try:
            size = saved.split("+")[0]
            width, height = (int(part) for part in size.split("x"))
        except Exception:
            pass
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = max(980, min(width, screen_w - 80))
        height = max(620, min(height, screen_h - 160))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _save_settings(self) -> None:
        data = {
            "dark": self.dark,
            "agent": self.agent_id,
            "geometry": self.root.geometry(),
            "log_visible": bool(self.log_box.winfo_manager()),
        }
        try:
            (paths.DATA_DIR / "settings.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            pass

    def palette(self) -> dict:
        return DARK if self.dark else LIGHT

    def _theme(self, widget, bg: str | None = None, fg: str | None = None,
               active_bg: str | None = None, insert: str | None = None) -> None:
        def apply() -> None:
            colors = self.palette()
            options = {}
            if bg:
                options["bg"] = colors[bg]
            if fg:
                options["fg"] = colors[fg]
            if active_bg:
                options["activebackground"] = colors[active_bg]
            if insert:
                options["insertbackground"] = colors[insert]
            if options:
                widget.configure(**options)

        self._themed.append(apply)
        apply()

    def _button(self, parent, text: str, command, kind: str = "ghost") -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(FONT, 9),
            padx=12,
            pady=6,
            highlightthickness=0,
        )
        self._buttons.append((button, kind))
        self._paint_button(button, kind)
        return button

    def _paint_button(self, button: tk.Button, kind: str) -> None:
        colors = self.palette()
        if kind == "accent":
            button.configure(bg=colors["accent"], fg="#ffffff", activebackground=colors["accent_hover"],
                             activeforeground="#ffffff")
        elif kind == "header":
            button.configure(bg=colors["header"], fg=colors["sidebar_text"], activebackground=colors["sidebar_active"],
                             activeforeground="#ffffff")
        else:
            button.configure(bg=colors["surface"], fg=colors["text"], activebackground=colors["surface2"],
                             activeforeground=colors["text"])

    # ------------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        self.menubar = tk.Menu(self.root, tearoff=0)
        file_menu = tk.Menu(self.menubar, tearoff=0)
        file_menu.add_command(label="打开数据目录", command=lambda: self._open(paths.DATA_DIR))
        file_menu.add_command(label="打开主注册表", command=lambda: self._open(paths.REGISTRY_PATH))
        file_menu.add_command(label="打开候选目录", command=lambda: self._open(paths.GENERATED_DIR))
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        self.menubar.add_cascade(label="文件", menu=file_menu)

        action_menu = tk.Menu(self.menubar, tearoff=0)
        action_menu.add_command(label="刷新", accelerator="F5", command=self.refresh_all)
        action_menu.add_command(label="扫描全部 MCP", accelerator="Ctrl+R", command=self.scan_all)
        action_menu.add_command(label="添加扫描目录…", command=self.add_scan_folder)
        action_menu.add_command(label="打开扫描报告", command=self.open_report)
        action_menu.add_command(label="检查可升级包", accelerator="Ctrl+U", command=self.check_updates)
        action_menu.add_separator()
        action_menu.add_command(label="诊断当前 Agent", accelerator="Ctrl+D", command=self.diagnose_current)
        action_menu.add_command(label="检查本机运行时", command=self.check_runtime)
        action_menu.add_separator()
        action_menu.add_command(label="备份全部配置", command=self.backup_all)
        action_menu.add_command(label="备份管理…", command=self.open_backup_manager)
        action_menu.add_separator()
        action_menu.add_command(label="初始化当前 Agent 配置", command=self.init_config)
        action_menu.add_command(label="添加 MCP…", command=self.add_mcp)
        action_menu.add_command(label="修改当前 Agent 配置位置…", command=self.edit_profile_path)
        action_menu.add_command(label="生成候选配置", command=self.generate_candidate)
        action_menu.add_command(label="应用候选…", command=self.show_candidate_preview)
        self.menubar.add_cascade(label="操作", menu=action_menu)

        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="快捷键", command=self.show_shortcuts)
        help_menu.add_command(label="关于", command=self.show_about)
        self.menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.configure(menu=self.menubar)
        self._menus = [self.menubar, file_menu, action_menu, help_menu]

    # ----------------------------------------------------------------- header
    def _build_header(self) -> None:
        self.header = tk.Frame(self.root, height=70)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self._theme(self.header, bg="header")

        self.brand = tk.Label(self.header, text="MCP Manager", font=(FONT, 18, "bold"))
        self.brand.pack(side="left", padx=(22, 12), pady=16)
        self._theme(self.brand, bg="header", fg="sidebar_text")

        self.brand_sub = tk.Label(self.header, text="跨 Agent 的 MCP 配置管理", font=(FONT, 9))
        self.brand_sub.pack(side="left", pady=20)
        self._theme(self.brand_sub, bg="header", fg="muted")

        self.header_buttons = tk.Frame(self.header)
        self.header_buttons.pack(side="right", padx=18)
        self._theme(self.header_buttons, bg="header")
        for text, command in (
            ("检查更新", self.check_updates),
            ("扫描全部", self.scan_all),
            ("刷新", self.refresh_all),
            ("主题", self.toggle_theme),
        ):
            button = self._button(self.header_buttons, text, command, "header")
            button.pack(side="right", padx=4)

    # ----------------------------------------------------------------- status
    def _build_status(self) -> None:
        self.status = tk.Frame(self.root, height=30)
        self.status.pack(side="bottom", fill="x")
        self.status.pack_propagate(False)
        self._theme(self.status, bg="surface2")

        self.status_left = tk.Label(self.status, text="", font=(FONT, 9), anchor="w")
        self.status_left.pack(side="left", padx=16)
        self._theme(self.status_left, bg="surface2", fg="muted")

        self.status_right = tk.Label(self.status, text="", font=(FONT, 9), anchor="e")
        self.status_right.pack(side="right", padx=16)
        self._theme(self.status_right, bg="surface2", fg="muted")

        self.progress = ttk.Progressbar(
            self.status, mode="indeterminate", length=110, style="App.Horizontal.TProgressbar"
        )

    def set_status(self, text: str) -> None:
        self.status_left.configure(text=text)

    # ------------------------------------------------------------------- body
    def _build_body(self) -> None:
        self.body = tk.Frame(self.root)
        self.body.pack(fill="both", expand=True)
        self._theme(self.body, bg="bg")

        self._build_sidebar()

        self.content = tk.Frame(self.body)
        self.content.pack(side="left", fill="both", expand=True, padx=(18, 18), pady=(14, 8))
        self._theme(self.content, bg="bg")

        toolbar = tk.Frame(self.content)
        toolbar.pack(fill="x")
        self._theme(toolbar, bg="bg")

        tk.Label(toolbar, text="搜索", font=(FONT, 9)).pack(side="left", padx=(0, 6))
        self._theme(toolbar.winfo_children()[-1], bg="bg", fg="muted")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_servers())
        self.search_entry = tk.Entry(
            toolbar, textvariable=self.search_var, relief="flat", font=(FONT, 9), width=26,
            highlightthickness=1,
        )
        self.search_entry.pack(side="left", ipady=5)
        self._theme(self.search_entry, bg="surface", fg="text", insert="text")
        self._themed.append(
            lambda: self.search_entry.configure(highlightbackground=self.palette()["border"],
                                                highlightcolor=self.palette()["accent"])
        )

        for text, command, kind in (
            ("恢复备份", self.open_backup_manager, "ghost"),
            ("备份全部", self.backup_all, "ghost"),
            ("添加 MCP", self.add_mcp, "ghost"),
            ("应用候选…", self.show_candidate_preview, "ghost"),
            ("生成候选", self.generate_candidate, "accent"),
            ("配置位置", self.edit_profile_path, "ghost"),
        ):
            button = self._button(toolbar, text, command, kind)
            button.pack(side="right", padx=(6, 0))

        self.hint = tk.Label(self.content, text="", font=(FONT, 9), anchor="w")
        self.hint.pack(fill="x", pady=(8, 6))
        self._theme(self.hint, bg="bg", fg="muted")

        self.paned = ttk.Panedwindow(self.content, orient="vertical", style="App.TPanedwindow")
        self.paned.pack(fill="both", expand=True)

        tree_card = tk.Frame(self.paned)
        self.paned.add(tree_card, weight=4)
        self._theme(tree_card, bg="surface")

        columns = ("MCP", "类型", "版本", "状态")
        self.tree = ttk.Treeview(
            tree_card, columns=columns, show="headings", selectmode="browse", style="App.Treeview", height=9
        )
        for column, width in zip(columns, (240, 170, 120, 260)):
            self.tree.heading(column, text=column, anchor="w", command=lambda c=column: self.sort_by(c))
            self.tree.column(column, width=width, anchor="w", stretch=(column == "状态"))
        scroll = ttk.Scrollbar(tree_card, orient="vertical", command=self.tree.yview, style="App.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", pady=6, padx=(0, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._on_context)
        self.tree.bind("<Double-1>", lambda _event: self.open_selected_config())

        details_card = tk.Frame(self.paned)
        self.paned.add(details_card, weight=2)
        self._theme(details_card, bg="surface")

        details_head = tk.Frame(details_card)
        details_head.pack(fill="x", padx=12, pady=(8, 0))
        self._theme(details_head, bg="surface")
        title = tk.Label(details_head, text="服务详情", font=(FONT, 9, "bold"), anchor="w")
        title.pack(side="left")
        self._theme(title, bg="surface", fg="text")
        copy_button = self._button(details_head, "复制启动命令", self.copy_command, "ghost")
        copy_button.pack(side="right")

        self.details = tk.Text(
            details_card, height=5, wrap="word", relief="flat", bd=0, padx=12, pady=8, font=(MONO, 9),
            state="disabled", highlightthickness=0,
        )
        self.details.pack(fill="both", expand=True, padx=6, pady=(4, 8))
        self._theme(self.details, bg="surface", fg="text", insert="text")

        self._build_log()

    def _build_sidebar(self) -> None:
        self.sidebar = tk.Frame(self.body, width=238)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._theme(self.sidebar, bg="sidebar")

        heading = tk.Label(self.sidebar, text="AGENTS", font=(FONT, 8, "bold"), anchor="w")
        heading.pack(fill="x", padx=18, pady=(16, 8))
        self._theme(heading, bg="sidebar", fg="muted")

        for profile in self.profiles.values():
            row = tk.Frame(self.sidebar, cursor="hand2")
            row.pack(fill="x", padx=10, pady=1)
            dot = tk.Label(row, text="●", font=(FONT, 9), width=2)
            dot.pack(side="left", padx=(6, 0))
            name = tk.Label(row, text=profile.name, font=(FONT, 9), anchor="w")
            name.pack(side="left", fill="x", expand=True, pady=6)
            self._sidebar_rows[profile.id] = {"row": row, "dot": dot, "name": name}
            for widget in (row, dot, name):
                widget.bind("<Button-1>", lambda _event, pid=profile.id: self.select_agent(pid))
            row.bind("<Enter>", lambda _event, pid=profile.id: self._hover_agent(pid, True))
            row.bind("<Leave>", lambda _event, pid=profile.id: self._hover_agent(pid, False))

        footer = tk.Label(self.sidebar, text=f"数据目录\n{paths.DATA_DIR}", font=(FONT, 8),
                          justify="left", anchor="w", wraplength=200)
        footer.pack(side="bottom", fill="x", padx=18, pady=14)
        self._theme(footer, bg="sidebar", fg="muted")

    def _build_log(self) -> None:
        self.log_card = tk.Frame(self.content)
        self.log_card.pack(fill="x", pady=(10, 0))
        self._theme(self.log_card, bg="bg")

        head = tk.Frame(self.log_card)
        head.pack(fill="x")
        self._theme(head, bg="bg")
        label = tk.Label(head, text="运行日志", font=(FONT, 9, "bold"))
        label.pack(side="left")
        self._theme(label, bg="bg", fg="text")
        self.log_toggle_button = self._button(head, "收起", self._toggle_log, "ghost")
        self.log_toggle_button.pack(side="right")
        clear = self._button(head, "清空", self.clear_log, "ghost")
        clear.pack(side="right", padx=(0, 6))

        self.log_box = tk.Text(
            self.log_card, height=4, wrap="word", relief="flat", bd=0, padx=12, pady=8,
            font=(MONO, 9), state="disabled", highlightthickness=0,
        )
        self.log_box.pack(fill="x", pady=(6, 0))
        self._theme(self.log_box, bg="surface2", fg="muted")

    def _hover_agent(self, profile_id: str, active: bool) -> None:
        if profile_id == self.agent_id:
            return
        colors = self.palette()
        row = self._sidebar_rows[profile_id]
        color = colors["sidebar_active"] if active else colors["sidebar"]
        for widget in (row["row"], row["dot"], row["name"]):
            widget.configure(bg=color)

    def _paint_agent_rows(self) -> None:
        colors = self.palette()
        for profile_id, row in self._sidebar_rows.items():
            selected = profile_id == self.agent_id
            background = colors["sidebar_active"] if selected else colors["sidebar"]
            row["row"].configure(bg=background)
            row["dot"].configure(bg=background, fg=colors["ok"] if any(p.exists() for p in self.profiles[profile_id].paths) else colors["muted"])
            row["name"].configure(bg=background, fg="#ffffff" if selected else colors["sidebar_text"],
                                  font=(FONT, 9, "bold") if selected else (FONT, 9))

    def _init_sash(self) -> None:
        try:
            height = self.paned.winfo_height()
            if height > 200:
                self.paned.sashpos(0, int(height * 0.6))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ theme
    def apply_theme(self) -> None:
        colors = self.palette()
        self.root.configure(bg=colors["bg"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "App.Treeview",
            background=colors["surface"],
            fieldbackground=colors["surface"],
            foreground=colors["text"],
            borderwidth=0,
            rowheight=31,
            font=(FONT, 9),
        )
        style.map(
            "App.Treeview",
            background=[("selected", colors["accent"])],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "App.Treeview.Heading",
            background=colors["surface2"],
            foreground=colors["muted"],
            relief="flat",
            font=(FONT, 9, "bold"),
            padding=(8, 8),
        )
        style.map("App.Treeview.Heading", background=[("active", colors["surface2"])])
        style.configure("App.Horizontal.TProgressbar", troughcolor=colors["surface2"], background=colors["accent"],
                        borderwidth=0, thickness=6)
        style.configure("App.TPanedwindow", background=colors["bg"], sashwidth=8, sashrelief="flat")
        style.configure("App.Vertical.TScrollbar", background=colors["border"], troughcolor=colors["surface"],
                        bordercolor=colors["surface"], arrowcolor=colors["muted"])

        for apply in self._themed:
            apply()
        for button, kind in self._buttons:
            self._paint_button(button, kind)
        for menu in self._menus:
            menu.configure(bg=colors["surface"], fg=colors["text"], activebackground=colors["accent"],
                           activeforeground="#ffffff", bd=0)

        self.tree.tag_configure("ok", foreground=colors["ok"])
        self.tree.tag_configure("registered", foreground=colors["info"])
        self.tree.tag_configure("missing", foreground=colors["muted"])
        self.tree.tag_configure("plugin", foreground=colors["plugin"])
        self.tree.tag_configure("warn", foreground=colors["warn"])
        self.tree.tag_configure("odd", background=colors["surface2"])
        self.tree.tag_configure("even", background=colors["surface"])
        self._paint_agent_rows()

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self.apply_theme()
        self.refresh_servers()
        self._save_settings()
        self.log("已切换到" + ("深色" if self.dark else "浅色") + "主题。")

    # ------------------------------------------------------------------ agents
    def current(self):
        return self.profiles[self.agent_id]

    def select_agent(self, profile_id: str) -> None:
        self.agent_id = profile_id
        self._paint_agent_rows()
        self.refresh_servers()
        self._save_settings()

    def refresh_agents(self) -> None:
        for profile_id, profile in self.profiles.items():
            exists = any(path.exists() for path in profile.paths)
            self._sidebar_rows[profile_id]["dot"].configure(fg=self.palette()["ok"] if exists else self.palette()["muted"])
        self._paint_agent_rows()

    def refresh_all(self) -> None:
        self.refresh_agents()
        self.refresh_servers()
        self.log("已刷新。")

    # ------------------------------------------------------------------ table
    @staticmethod
    def _tagged_for(profile, spec) -> bool:
        agents = [str(item).lower() for item in spec.get("agents", [])]
        if "*" in agents:
            return True
        keys = {profile.id.lower(), profile.name.lower(), *(alias.lower() for alias in profile.aliases)}
        return any(agent in keys for agent in agents)

    def refresh_servers(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.rows.clear()

        profile = self.current()
        data = registry.load()
        section: dict = {}
        sources: dict[str, Path] = {}
        error = ""
        try:
            section, sources = formats.read_merged_section(profile.paths, profile.format, profile.schema)
        except Exception as exc:  # noqa: BLE001 - shown in the hint
            error = f"配置解析失败：{exc}"
            self.log(error)

        configured = {registry.canonical(name): name for name in section}
        rows = []
        registered = set()
        for name, spec in registry.servers(data).items():
            canonical = registry.canonical(name)
            here = canonical in configured
            if not (self._tagged_for(profile, spec) or here):
                continue
            registered.add(canonical)
            if spec.get("kind") == "copilot-plugin-host":
                state, tag = "宿主/插件级", "plugin"
            elif here:
                state, tag = "已配置", "ok"
            else:
                state, tag = "注册表已登记（未安装）", "registered"
            rows.append((name, spec, state, tag, here))

        ignored = 0
        for name in section:
            if registry.canonical(name) not in registered:
                if adapters.is_container(section[name]):
                    ignored += 1
                    continue
                rows.append((name, {"kind": "仅配置中发现", "agents": [profile.id]}, "已配置（未登记）", "warn", True))

        for name, spec, state, tag, here in rows:
            canonical = registry.canonical(name)
            live = section.get(configured[canonical], spec) if here else spec
            level = adapters.level_of(live, profile)
            if spec.get("kind") == "copilot-plugin-host":
                level = "host"
            if level == "host":
                registered_here = registry.entry(data, name) is not None
                state = "宿主/插件级" if registered_here else "宿主/插件级（未登记）"
                tag = "plugin"
            elif level == "invalid":
                if here:
                    state = "配置不完整" if registry.entry(data, name) is not None else "配置不完整（未登记）"
                    tag = "warn"
                elif spec.get("package"):
                    # Registry-only package definition; this Agent just doesn't
                    # have it installed yet.
                    level = "manageable"
                else:
                    state, tag = "配置不完整", "warn"
            command = adapters.command_of(live)
            source = sources.get(configured[canonical]) if here else None
            aux = bool(here and source and source != profile.path)
            if level == "manageable" and aux:
                state, tag = "已配置（附加配置·只读）", "plugin"
            if level == "manageable" and command and (Path(command).exists() or shutil.which(command)):
                state += " · 路径正常"
            elif level == "manageable" and command:
                tag = "warn"
                state += " · 路径缺失"
            field = registry.version_field(spec)
            version = str(spec.get(field, "")) if field else ""
            self.rows[name] = {
                "name": name,
                "spec": spec,
                "live": live,
                "state": state,
                "tag": tag,
                "version": version,
                "kind": str(spec.get("kind", "")),
                "level": level,
                "here": here,
                "source": str(source) if source else "",
                "aux": aux,
            }

        needle = self.search_var.get().strip().lower()
        ordered = list(self.rows.values())
        key = {
            "MCP": lambda row: row["name"].lower(),
            "类型": lambda row: row["kind"].lower(),
            "版本": lambda row: row["version"],
            "状态": lambda row: row["state"],
        }.get(self.sort_column, lambda row: row["name"].lower())
        ordered.sort(key=key, reverse=self.sort_reverse)

        for index, row in enumerate(ordered):
            if needle and needle not in f"{row['name']} {row['kind']} {row['state']}".lower():
                continue
            self.tree.insert(
                "", "end", iid=row["name"], values=(row["name"], row["kind"], row["version"], row["state"]),
                tags=(row["tag"], "odd" if index % 2 else "even"),
            )

        if error:
            self.hint.configure(text=error, fg=self.palette()["danger"])
        elif not any(path.exists() for path in profile.paths):
            self.hint.configure(
                text=f"{profile.name} 还没有配置文件：{profile.path}  ·  可在“操作”菜单中初始化配置。",
                fg=self.palette()["warn"],
            )
        else:
            extra = [str(path) for path in profile.paths[1:] if path.exists()]
            missing = [path for path in profile.paths[1:] if not path.exists()]
            text = f"{profile.name}  ·  {profile.path}  ·  已配置 {len(section)} 项"
            if extra:
                text += "  ·  附加只读：" + "，".join(extra)
            if missing:
                text += ("  ·  附加来源未发现（可选）" if profile.auxiliary_optional
                         else "  ·  附加来源缺失（必选）")
            self.hint.configure(text=text, fg=self.palette()["muted"])
        if ignored and not error:
            self.hint.configure(text=self.hint.cget("text") + f"  ·  已忽略 {ignored} 个非 MCP 条目")
        self.status_right.configure(text=f"{profile.name} · 显示 {len(self.tree.get_children())} / {len(self.rows)} 项")

    def sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self.refresh_servers()

    def selected_name(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("选择 MCP", "请先在列表中选择一个 MCP。", parent=self.root)
            return None
        return self.tree.item(selection[0], "values")[0]

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            self._set_details("")
            return
        row = self.rows.get(str(selection[0]))
        if row:
            self._set_details(self._describe(row))

    def _describe(self, row: dict) -> str:
        profile = self.current()
        live = row["live"]
        spec = row["spec"]
        level = row.get("level", "manageable")
        level_text = {"manageable": "可管理", "host": "宿主/插件级（不可登记）",
                      "invalid": "配置不完整"}.get(level, "可管理")
        lines = [
            f"名称        {row['name']}",
            f"状态        {row['state']}",
            f"注册表      {'已登记' if spec.get('kind') != '仅配置中发现' else '未登记'}",
            f"管理级别    {level_text}",
            f"本机配置    {'已写入当前 Agent' if row.get('here') else '未安装（仅注册表登记）'}",
            f"类型        {row['kind'] or '-'}",
        ]
        command = adapters.command_of(live)
        args = adapters.args_of(live)
        url = adapters.url_of(live, profile)
        if command:
            lines.append(f"命令        {command}")
        if args:
            lines.append(f"参数        {' '.join(args)}")
        if url:
            lines.append(f"地址        {url}")
        env = live.get(profile.env) or live.get("env") or live.get("environment") or {}
        if isinstance(env, dict) and env:
            lines.append("环境变量")
            for key, value in env.items():
                secret = any(marker in str(key).lower() for marker in adapters.SECRET_MARKERS)
                lines.append(f"  {key} = {'***' if secret else value}")
        update_command = versioning.format_command(spec.get("updateCommand"))
        if update_command:
            lines.append(f"更新命令    {update_command}")
        source = row.get("source") or str(profile.path)
        lines.append(f"来源文件    {source}" + ("（附加配置·只读）" if row.get("aux") else ""))
        cached = health.get(profile.id, row["name"])
        if cached:
            state = "OK" if cached.get("ok") else "FAIL"
            lines.append(
                f"上次握手    {state} · {cached.get('toolCount', 0)} 个工具 · {cached.get('checkedAt', '')}"
            )
        return "\n".join(lines)

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self.details.configure(state="disabled")

    def _style_table(self, table: ttk.Treeview) -> None:
        colors = self.palette()
        for tag, role in (("ok", "ok"), ("registered", "info"), ("missing", "muted"),
                          ("plugin", "plugin"), ("warn", "warn")):
            table.tag_configure(tag, foreground=colors[role])

    # ----------------------------------------------------------- context menu
    def _on_context(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        name = self.tree.item(item, "values")[0]
        profile = self.current()
        row = self.rows.get(name) or {}
        level = str(row.get("level", "invalid"))
        manageable = level == "manageable"
        live = row.get("live") or {}
        has_target = bool(adapters.command_of(live) or adapters.url_of(live, profile))
        if manageable:
            register_label = "登记到主注册表…"
        elif level == "host":
            register_label = "宿主/插件级（不可登记）"
        else:
            register_label = "配置不完整（无法登记）"
        registered = registry.entry(registry.load(), name) is not None
        menu = tk.Menu(self.root, tearoff=0)
        colors = self.palette()
        menu.configure(bg=colors["surface"], fg=colors["text"], activebackground=colors["accent"],
                       activeforeground="#ffffff", bd=0)
        menu.add_command(label="复制启动命令", command=self.copy_command,
                         state="normal" if manageable and has_target else "disabled")
        menu.add_command(label="诊断此 MCP", command=lambda: self.diagnose_one(name),
                         state="normal" if has_target or level == "host" else "disabled")
        menu.add_command(label="运行握手测试", command=lambda: self.handshake_one(name),
                         state="normal" if manageable and has_target else "disabled")
        menu.add_separator()
        menu.add_command(label=register_label, command=lambda: self.register_server(name),
                         state="normal" if manageable and not registered else "disabled")
        menu.add_command(label="修改版本登记", command=self.edit_version,
                         state="normal" if manageable and registered else "disabled")
        menu.add_command(label="设置更新命令…", command=self.edit_update_command,
                         state="normal" if manageable and registered else "disabled")
        menu.add_command(label="更新此 MCP", command=self.update_selected,
                         state="normal" if manageable and registered else "disabled")
        menu.add_command(label="从注册表移除", command=lambda: self.remove_server(name),
                         state="normal" if registered else "disabled")
        menu.add_command(label="从当前 Agent 卸载…", command=lambda: self.uninstall_from_agent(name),
                         state="normal" if row.get("here") and not row.get("aux") else "disabled")
        menu.add_separator()
        menu.add_command(label="打开 Agent 配置", command=self.open_selected_config)
        menu.add_command(label="打开所在目录", command=lambda: self._open(profile.path.parent))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    @staticmethod
    def _registration_meta(command: str | None, args: list[str],
                           url: str | None) -> tuple[str, str | None, str | None]:
        """Infer registry ``kind``/``package``/``version`` from a live entry."""
        if not command:
            return ("remote-http" if url else "local-binary"), None, None
        exe = Path(command).name.lower()
        for suffix in (".exe", ".cmd", ".bat"):
            if exe.endswith(suffix):
                exe = exe[: -len(suffix)]
                break
        if exe in {"npx", "bunx"}:
            package, version = _package_token(args, "@")
            return "npm-on-demand", package, version
        if exe == "uvx":
            package, version = _package_token(args, "==")
            return "uv-on-demand", package, version
        if exe.startswith("python"):
            return "local-python", None, None
        if exe == "node":
            return "local-node", None, None
        return "local-binary", None, None

    def _agents_with_server(self, name: str) -> list[str]:
        wanted = registry.canonical(name)
        agents = []
        for profile in self.profiles.values():
            try:
                section, _sources = formats.read_merged_section(profile.paths, profile.format, profile.schema)
            except Exception:  # noqa: BLE001 - unreadable profiles are skipped
                continue
            if any(registry.canonical(item) == wanted for item in section):
                agents.append(profile.id)
        return agents or [self.current().id]

    def register_server(self, name: str) -> None:
        data = registry.load()
        if registry.entry(data, name) is not None:
            messagebox.showinfo("已登记", f"{name} 已经在主注册表中。", parent=self.root)
            return
        row = self.rows.get(name)
        if not row or not isinstance(row["live"], dict):
            messagebox.showerror("无法登记", f"没有读到 {name} 的配置内容。", parent=self.root)
            return
        level = row.get("level") or adapters.level_of(row["live"], self.current())
        if level == "host":
            messagebox.showinfo(
                "宿主/插件级",
                f"{name} 由 Agent 运行时提供，不在本地安装，无法通过注册表管理。",
                parent=self.root,
            )
            return
        if level != "manageable":
            messagebox.showerror(
                "无法登记", f"{name} 的配置缺少有效的 command 或 url，无法登记。", parent=self.root
            )
            return
        profile = self.current()
        live = row["live"]
        command = adapters.command_of(live)
        args = adapters.args_of(live)
        url = adapters.url_of(live, profile)
        kind, package, version = self._registration_meta(command, args, url)
        env = live.get(profile.env) or live.get("env") or live.get("environment")

        spec: dict = {"kind": kind}
        if command:
            spec["command"] = command
            if args:
                spec["args"] = args
        if url:
            spec["url"] = url
        if isinstance(env, dict) and env:
            spec["env"] = env
        secret_keys = (
            [str(key) for key in env if adapters.has_inline_secret(env[key], str(key))]
            if isinstance(env, dict) else []
        )
        if package:
            spec["package"] = package
            spec["versionPolicy"] = "pin-before-sync"
            spec["version"] = version or ""
        spec["agents"] = self._agents_with_server(name)

        lines = [f"将 {name} 登记到主注册表：", "", f"类型      {kind}"]
        if command:
            lines.append(f"命令      {command}")
        if args:
            lines.append(f"参数      {' '.join(args)}")
        if url:
            lines.append(f"地址      {url}")
        if isinstance(env, dict) and env:
            lines.append(f"环境变量  {len(env)} 项（{', '.join(env)}）")
        if secret_keys:
            lines.append(f"注意      {'、'.join(secret_keys)} 疑似凭据，将以明文写入 {paths.REGISTRY_PATH}")
        if package:
            lines.append(f"包        {package}{'@' + version if version else '（版本未登记）'}")
        lines.append(f"适用      {', '.join(spec['agents'])}")
        lines.append("")
        lines.append("登记后即可使用更新、版本、移除等管理功能；不会修改 Agent 配置。")
        if not messagebox.askyesno("登记到主注册表", "\n".join(lines), parent=self.root):
            return
        registry.add_server(data, name, spec)
        registry.save(data)
        self.log(f"已登记 {name}（{kind}），适用 Agent：{', '.join(spec['agents'])}。")
        self.refresh_servers()
        messagebox.showinfo("登记完成", f"{name} 已登记到主注册表，现在可通过右键菜单管理。", parent=self.root)

    def copy_command(self) -> None:
        name = self.tree.selection()
        if not name:
            return
        row = self.rows.get(str(self.tree.item(name[0], "values")[0]))
        if not row:
            return
        command = adapters.command_of(row["live"])
        parts = [command] if command else []
        parts += adapters.args_of(row["live"])
        if not parts:
            url = adapters.url_of(row["live"], self.current())
            parts = [url] if url else []
        text = " ".join(part for part in parts if part)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log("已复制：" + text)

    def diagnose_one(self, name: str) -> None:
        row = self.rows.get(name)
        if not row:
            return
        try:
            result = doctor.diagnose_server(self.current(), name, row["live"])
        except Exception as exc:  # noqa: BLE001 - shown to the user
            messagebox.showerror("诊断失败", str(exc), parent=self.root)
            return
        state = "正常" if result["available"] else "异常"
        text = f"{name}\n状态：{state}\n详情：{result['detail']}"
        if result["problems"]:
            text += "\n问题：\n- " + "\n- ".join(result["problems"])
        messagebox.showinfo("诊断结果", text, parent=self.root)

    def handshake_one(self, name: str) -> None:
        row = self.rows.get(name)
        if not row:
            return
        profile = self.current()
        live = row["live"]

        def work():
            return handshake.probe(profile, name, live)

        def done(result) -> None:
            health.record(profile.id, name, result)
            if result["ok"]:
                state = "成功"
            elif result.get("skipped"):
                state = "跳过"
            else:
                state = "失败"
            stage = {"config": "配置", "start": "启动", "initialize": "初始化",
                     "tools": "工具列表", "ready": "就绪"}.get(result["stage"], result["stage"])
            text = f"{name}\n状态：{state}\n阶段：{stage}\n详情：{result['detail']}"
            info = result.get("serverInfo") or {}
            if info:
                text += f"\n服务：{info.get('name', '')} {info.get('version', '')}".rstrip()
            if result.get("protocolVersion"):
                text += f"\n协议：{result['protocolVersion']}"
            if result["tools"]:
                text += "\n工具：\n- " + "\n- ".join(result["tools"])
            self.log(f"握手测试 {name}：{state}（{result['detail']}）")
            if result["ok"]:
                messagebox.showinfo("握手测试", text, parent=self.root)
            else:
                messagebox.showwarning("握手测试", text, parent=self.root)

        self.run_task(f"正在握手测试 {name}…", work, done)

    def remove_server(self, name: str) -> None:
        data = registry.load()
        if registry.entry(data, name) is None:
            messagebox.showinfo("未登记", f"{name} 不在主注册表中。", parent=self.root)
            return
        if not messagebox.askyesno("确认移除", f"从主注册表移除 {name}？\n不会修改 Agent 配置文件。", parent=self.root):
            return
        registry.remove_server(data, name)
        registry.save(data)
        self.refresh_servers()
        self.log(f"已从注册表移除 {name}。")

    def uninstall_from_agent(self, name: str) -> None:
        profile = self.current()
        if profile.readonly:
            messagebox.showinfo("只读 Agent", f"{profile.name} 被标记为只读，不能卸载。", parent=self.root)
            return
        try:
            path = sync.uninstall_candidate(profile, name)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.log(f"生成卸载候选失败：{exc}")
            messagebox.showerror("无法卸载", str(exc), parent=self.root)
            return
        self.log(f"已生成卸载候选：{path}")
        self.show_preview(path)

    # ------------------------------------------------------------- candidates
    def generate_candidate(self) -> None:
        profile = self.current()
        if not profile.path.exists():
            if messagebox.askyesno(
                "初始化配置", f"{profile.name} 还没有配置文件：\n{profile.path}\n\n现在创建吗？", parent=self.root
            ):
                self.init_config()
            return
        try:
            candidate = sync.build_candidate(profile, registry.load())
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.log(f"生成候选失败：{exc}")
            messagebox.showerror("生成候选失败", str(exc), parent=self.root)
            return
        self.log(f"已生成候选：{candidate.path}")
        self.show_preview(candidate.path)

    def show_candidate_preview(self) -> None:
        self.show_preview(sync.candidate_path(self.current()))

    def show_preview(self, path: Path) -> None:
        if not path.exists():
            messagebox.showinfo("没有候选配置", "请先点击“生成候选”。", parent=self.root)
            return
        candidate = sync.load_candidate(path)
        fresh = sync.is_fresh(candidate)
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title(f"候选预览 - {candidate.get('agentName', candidate['agent'])}")
        window.geometry("860x600")
        window.configure(bg=colors["bg"])

        header = tk.Frame(window, height=54, bg=colors["header"])
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="候选配置预览", bg=colors["header"], fg="#ffffff",
                 font=(FONT, 12, "bold")).pack(side="left", padx=16)
        tk.Label(header, text=f"{candidate.get('agentName', candidate['agent'])} · "
                              f"{len(candidate['servers'])} 个 MCP", bg=colors["header"],
                 fg=colors["sidebar_text"], font=(FONT, 9)).pack(side="left", padx=8)

        info = tk.Frame(window, bg=colors["bg"])
        info.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(info, text=f"目标：{candidate['targetPath']}", bg=colors["bg"], fg=colors["muted"],
                 font=(FONT, 9), wraplength=800, justify="left").pack(anchor="w")
        tk.Label(info, text="源配置未变化，可以应用" if fresh else "源配置已变化，请重新生成",
                 bg=colors["bg"], fg=colors["ok"] if fresh else colors["danger"],
                 font=(FONT, 9, "bold")).pack(anchor="w", pady=(4, 0))

        box = tk.Text(window, wrap="none", relief="flat", bd=0, padx=12, pady=10,
                      font=(MONO, 9), bg=colors["surface"], fg=colors["text"], highlightthickness=0)
        box.pack(fill="both", expand=True, padx=16, pady=8)
        box.insert("end", json.dumps(candidate["servers"], ensure_ascii=False, indent=2))
        box.configure(state="disabled")

        actions = tk.Frame(window, bg=colors["bg"])
        actions.pack(fill="x", padx=16, pady=(0, 14))
        tk.Button(actions, text="关闭", command=window.destroy, relief="flat", bd=0, padx=14, pady=7,
                  bg=colors["surface"], fg=colors["text"], font=(FONT, 9), cursor="hand2").pack(side="right")
        tk.Button(actions, text="查看差异", command=lambda: self.show_diff(path), relief="flat", bd=0,
                  padx=14, pady=7, bg=colors["surface"], fg=colors["text"], font=(FONT, 9),
                  cursor="hand2").pack(side="right", padx=8)
        tk.Button(actions, text="应用到 Agent", relief="flat", bd=0, padx=14, pady=7,
                  bg=colors["accent"], fg="#ffffff", activebackground=colors["accent_hover"],
                  activeforeground="#ffffff", font=(FONT, 9), cursor="hand2",
                  command=lambda: self.apply_candidate(path, window)).pack(side="right", padx=8)

    def show_diff(self, path: Path) -> None:
        try:
            text = sync.candidate_diff(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            messagebox.showerror("无法生成差异", str(exc), parent=self.root)
            return
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title("候选差异")
        window.geometry("960x640")
        window.configure(bg=colors["bg"])
        tk.Label(window, text="当前配置 → 候选配置（unified diff）", bg=colors["bg"],
                 fg=colors["text"], font=(FONT, 11, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        box = tk.Text(window, wrap="none", relief="flat", bd=0, padx=12, pady=10,
                      font=(MONO, 9), bg=colors["surface"], fg=colors["text"], highlightthickness=0)
        box.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        box.insert("end", text or "没有差异。")
        box.tag_configure("add", foreground=colors["ok"])
        box.tag_configure("remove", foreground=colors["danger"])
        for index, line in enumerate(text.splitlines(), start=1):
            if line.startswith("+") and not line.startswith("+++"):
                box.tag_add("add", f"{index}.0", f"{index}.end")
            elif line.startswith("-") and not line.startswith("---"):
                box.tag_add("remove", f"{index}.0", f"{index}.end")
        box.configure(state="disabled")

    def apply_candidate(self, path: Path, window: tk.Toplevel | None = None) -> None:
        parent = window or self.root
        candidate = sync.load_candidate(path)
        if not sync.is_fresh(candidate):
            messagebox.showwarning("配置已变化", "目标配置在生成候选后发生变化，请重新生成。", parent=parent)
            return
        if not messagebox.askyesno(
            "确认应用",
            f"将先备份，再仅替换 MCP 区块。\n\nAgent：{candidate.get('agentName', candidate['agent'])}\n"
            f"配置：{candidate['targetPath']}\nMCP 数量：{len(candidate['servers'])}\n\n继续吗？",
            parent=parent,
        ):
            return
        try:
            backup, mode = sync.apply_candidate(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            messagebox.showerror("应用失败", str(exc), parent=parent)
            self.log(f"应用失败：{exc}")
            return
        self.log(f"已应用（{mode}）；备份：{backup or '无'}")
        messagebox.showinfo("应用完成", f"配置已写入。\n备份：{backup or '目标为新建文件'}", parent=parent)
        if window is not None:
            window.destroy()
        self.refresh_all()

    def init_config(self) -> None:
        profile = self.current()
        if profile.path.exists():
            messagebox.showinfo("配置已存在", f"{profile.name} 已有配置文件：\n{profile.path}", parent=self.root)
            self._open(profile.path)
            return
        profile.path.parent.mkdir(parents=True, exist_ok=True)
        if profile.format == "toml":
            text = f"[{profile.schema}]\n"
        else:
            text = json.dumps({profile.schema: {}}, ensure_ascii=False, indent=2) + "\n"
        profile.path.write_text(text, encoding="utf-8")
        self.log(f"已创建 {profile.name} 配置：{profile.path}")
        self.refresh_all()
        self._open(profile.path)

    def add_mcp(self) -> None:
        """Register a new MCP and prepare an install candidate for this Agent."""
        if paths.read_only():
            messagebox.showwarning("只读模式", "当前处于只读模式，不能添加 MCP。", parent=self.root)
            return
        profile = self.current()
        if profile.readonly:
            messagebox.showwarning("Agent 只读", f"{profile.name} 的 profile 被标记为只读，不能添加 MCP。", parent=self.root)
            return
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title(f"添加 MCP 到 {profile.name}")
        window.geometry("760x700")
        window.configure(bg=colors["bg"])
        window.transient(self.root)
        window.grab_set()

        tk.Label(
            window,
            text=f"添加 MCP · 目标 Agent：{profile.name}",
            bg=colors["bg"], fg=colors["text"], font=(FONT, 13, "bold"),
        ).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(
            window,
            text="填写 command 或 URL。保存后只登记到本地注册表，并生成候选配置；确认差异后才会写入 Agent。",
            bg=colors["bg"], fg=colors["muted"], font=(FONT, 9), justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        form = tk.Frame(window, bg=colors["bg"])
        form.pack(fill="both", expand=True, padx=20)
        fields: dict[str, tk.Entry] = {}

        def add_field(key: str, label: str, value: str = "") -> tk.Entry:
            tk.Label(form, text=label, bg=colors["bg"], fg=colors["text"],
                     font=(FONT, 9), anchor="w").pack(fill="x", pady=(6, 2))
            entry = tk.Entry(
                form, relief="flat", font=(MONO, 9), bg=colors["surface"], fg=colors["text"],
                insertbackground=colors["text"], highlightthickness=1,
            )
            entry.insert(0, value)
            entry.pack(fill="x", ipady=5)
            self._themed.append(
                lambda e=entry: e.configure(
                    bg=self.palette()["surface"], fg=self.palette()["text"],
                    insertbackground=self.palette()["text"],
                    highlightbackground=self.palette()["border"],
                    highlightcolor=self.palette()["accent"],
                )
            )
            fields[key] = entry
            return entry

        add_field("name", "MCP 名称（必填）")
        add_field("command", "启动命令（stdio，与 URL 二选一）")
        add_field("args", "参数（可选，使用空格和引号分隔，例如：-y @playwright/mcp@0.0.85）")
        add_field("url", "远程 URL（http MCP，与启动命令二选一）")

        tk.Label(form, text="环境变量（可选，逐项填写，值可含分号和等号）", bg=colors["bg"],
                 fg=colors["text"], font=(FONT, 9), anchor="w").pack(fill="x", pady=(6, 2))
        env_container = tk.Frame(form, bg=colors["bg"])
        env_container.pack(fill="x")
        env_rows: list[tuple[tk.Frame, tk.Entry, tk.Entry]] = []

        def themed_entry(parent: tk.Misc) -> tk.Entry:
            entry = tk.Entry(
                parent, relief="flat", font=(MONO, 9), bg=colors["surface"], fg=colors["text"],
                insertbackground=colors["text"], highlightthickness=1,
            )
            self._themed.append(
                lambda e=entry: e.configure(
                    bg=self.palette()["surface"], fg=self.palette()["text"],
                    insertbackground=self.palette()["text"],
                    highlightbackground=self.palette()["border"],
                    highlightcolor=self.palette()["accent"],
                )
            )
            return entry

        def add_env_row() -> None:
            row = tk.Frame(env_container, bg=colors["bg"])
            row.pack(fill="x", pady=1)
            key_entry = themed_entry(row)
            key_entry.pack(side="left", fill="x", expand=True, ipady=3)
            value_entry = themed_entry(row)
            value_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(4, 4))
            remove_btn = tk.Button(
                row, text="×", relief="flat", bd=0, padx=6, pady=0,
                bg=colors["surface"], fg=colors["muted"], activebackground=colors["border"],
                activeforeground=colors["text"], font=(FONT, 10), cursor="hand2",
                command=lambda: remove_env_row(row),
            )
            remove_btn.pack(side="left")
            env_rows.append((row, key_entry, value_entry))
            key_entry.focus_set()

        def remove_env_row(row: tk.Frame) -> None:
            for i, (r, _k, _v) in enumerate(env_rows):
                if r is row:
                    env_rows.pop(i)
                    break
            row.destroy()

        add_env_row()
        tk.Button(
            form, text="+ 添加环境变量", relief="flat", bd=0, padx=8, pady=3,
            bg=colors["surface"], fg=colors["text"], activebackground=colors["border"],
            activeforeground=colors["text"], font=(FONT, 8), cursor="hand2",
            command=add_env_row,
        ).pack(anchor="w", pady=(2, 0))

        add_field("kind", "类型（可选，例如：npm-on-demand、uv-on-demand、remote-http、local-binary）")
        add_field("package", "包名（可选，例如：@playwright/mcp）")
        add_field("version", "版本（可选；填写后同步时可固定版本）")

        actions = tk.Frame(window, bg=colors["bg"])
        actions.pack(fill="x", padx=20, pady=16)

        def save() -> None:
            env: dict[str, str] = {}
            for _row, key_entry, value_entry in env_rows:
                key = key_entry.get().strip()
                value = value_entry.get()
                if not key and not value:
                    continue
                env[key] = value
            try:
                name, spec, secret_keys = creation.build_server_spec(
                    name=fields["name"].get(),
                    command=fields["command"].get(),
                    args_text=fields["args"].get(),
                    url=fields["url"].get(),
                    env=env,
                    kind=fields["kind"].get(),
                    package=fields["package"].get(),
                    version=fields["version"].get(),
                    agent_id=profile.id,
                )
            except ValueError as exc:
                messagebox.showerror("输入无效", str(exc), parent=window)
                return
            command = str(spec.get("command", ""))
            url = str(spec.get("url", ""))
            args = spec.get("args", [])
            env = spec.get("env", {})
            data = registry.load()
            try:
                existing_section, _sources = formats.read_merged_section(
                    profile.paths, profile.format, profile.schema
                )
            except Exception as exc:
                messagebox.showerror("配置读取失败", str(exc), parent=window)
                return
            if any(registry.canonical(existing) == registry.canonical(name)
                   for existing in existing_section):
                messagebox.showerror(
                    "MCP 已配置",
                    f"{name} 已存在于 {profile.name} 的主配置或附加配置中，不能重复添加。",
                    parent=window,
                )
                return
            existing = registry.entry(data, name)
            if existing is not None:
                candidate_data = copy.deepcopy(data)
                existing = registry.entry(candidate_data, name)
                existing_command = str(existing.get("command", ""))
                existing_url = str(existing.get("url", ""))
                existing_args = existing.get("args", []) or []
                existing_env = existing.get("env", {}) or {}
                version_policy = existing.get("versionPolicy")
                secret_keys = [k for k, v in existing_env.items() if adapters.has_inline_secret(v, k)]
                preview = [
                    f"名称：{name}",
                    f"来源：本地注册表（不修改已有定义）",
                    f"目标 Agent：{profile.name}",
                    f"类型：{existing.get('kind', 'manual')}",
                ]
                if existing_command:
                    preview.append(f"实际命令：{existing_command}")
                if existing_url:
                    preview.append(f"远程地址：{existing_url}")
                preview.append(f"实际参数：{' '.join(existing_args) if existing_args else '-'}")
                preview.append(f"环境变量：{len(existing_env)} 项")
                if version_policy:
                    preview.append(f"版本策略：{version_policy}")
                if existing.get("package"):
                    preview.append(f"包名：{existing.get('package')}"
                                   + (f"@{existing.get('version')}" if existing.get("version") else ""))
                preview += [
                    "",
                    f"将为 {profile.name} 生成候选配置。",
                ]
                if secret_keys:
                    preview += [
                        "",
                        "警告：以下环境变量看起来包含凭据：" + ", ".join(secret_keys),
                        "它们会随候选进入目标 Agent 配置。更安全的做法是让 Agent 从系统环境变量或密钥文件读取。",
                    ]
                if not messagebox.askyesno("确认生成候选", "\n".join(preview), parent=window):
                    return
                try:
                    candidate, agent_added = sync.attach_agent_and_install(profile, candidate_data, name)
                    if agent_added:
                        registry.save(candidate_data)
                except Exception as exc:
                    messagebox.showerror("生成候选失败", str(exc), parent=window)
                    return
                window.destroy()
                self.refresh_servers()
                self.log(f"已为 {profile.name} 生成 {name} 的候选配置：{candidate}")
                messagebox.showinfo(
                    "候选已生成",
                    f"{name} 已在注册表中，已为 {profile.name} 生成候选配置。\n\n候选配置：\n{candidate}\n\n请在候选预览中确认差异后应用。",
                    parent=self.root,
                )
                self.show_preview(candidate)
                return
            preview = [
                f"名称：{name}",
                f"Agent：{profile.name}",
                f"类型：{spec['kind']}",
                f"命令：{command or url}",
                f"参数：{' '.join(args) if args else '-'}",
                f"环境变量：{len(env)} 项",
                "",
                "将写入本地注册表并生成候选配置；不会直接覆盖 Agent 配置。",
            ]
            if secret_keys:
                preview += [
                    "",
                    "警告：以下环境变量看起来包含凭据：" + ", ".join(secret_keys),
                    "它们会写入本地注册表。更安全的做法是让 Agent 从系统环境变量或密钥文件读取。",
                ]
            if not messagebox.askyesno("确认添加 MCP", "\n".join(preview), parent=window):
                return
            try:
                candidate_data = copy.deepcopy(data)
                registry.add_server(candidate_data, name, spec)
                candidate = sync.install_candidate(profile, candidate_data, name)
                registry.save(candidate_data)
            except Exception as exc:
                messagebox.showerror("添加失败", str(exc), parent=window)
                return
            window.destroy()
            self.refresh_servers()
            self.log(f"已登记 {name}，候选配置：{candidate}")
            messagebox.showinfo("候选已生成", f"{name} 已登记。\n\n候选配置：\n{candidate}\n\n请在候选预览中确认差异后应用。", parent=self.root)
            self.show_preview(candidate)

        tk.Button(
            actions, text="取消", command=window.destroy, relief="flat", bd=0, padx=14, pady=7,
            bg=colors["surface"], fg=colors["text"], font=(FONT, 9), cursor="hand2",
        ).pack(side="right")
        tk.Button(
            actions, text="登记并生成候选", command=save, relief="flat", bd=0, padx=16, pady=7,
            bg=colors["accent"], fg="#ffffff", activebackground=colors["accent_hover"],
            activeforeground="#ffffff", font=(FONT, 9), cursor="hand2",
        ).pack(side="right", padx=8)
        fields["name"].focus_set()

    def edit_profile_path(self) -> None:
        profile = self.current()
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title(f"配置位置 - {profile.name}")
        window.geometry("760x310")
        window.resizable(False, False)
        window.configure(bg=colors["bg"])
        window.transient(self.root)
        window.grab_set()

        tk.Label(window, text="配置文件位置", bg=colors["bg"], fg=colors["text"],
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=20, pady=(16, 2))
        source = "本机覆盖" if Path(profile.source).resolve().parent == paths.USER_PROFILE_DIR.resolve() else "内置配置"
        tk.Label(window, text=f"{profile.name} · {profile.format} / {profile.schema} · {source}",
                 bg=colors["bg"], fg=colors["muted"], font=(FONT, 9)).pack(anchor="w", padx=20)

        row = tk.Frame(window, bg=colors["bg"])
        row.pack(fill="x", padx=20, pady=(14, 8))
        value = tk.StringVar(value=str(profile.path))
        entry = tk.Entry(
            row, textvariable=value, relief="flat", font=(MONO, 9), bg=colors["surface"],
            fg=colors["text"], insertbackground=colors["text"], highlightthickness=1,
            highlightbackground=colors["border"], highlightcolor=colors["accent"],
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6)

        def browse() -> None:
            parent_dir = profile.path.parent
            chosen = filedialog.askopenfilename(
                title="选择配置文件", parent=window,
                initialdir=str(parent_dir if parent_dir.exists() else paths.HOME),
                filetypes=[("JSON", "*.json"), ("JSONC", "*.jsonc"), ("TOML", "*.toml"), ("所有文件", "*.*")],
            )
            if chosen:
                value.set(chosen.replace("\\", "/"))

        tk.Button(
            row, text="浏览…", command=browse, relief="flat", bd=0, padx=12, pady=6,
            bg=colors["surface"], fg=colors["text"], activebackground=colors["surface2"],
            font=(FONT, 9), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        tk.Label(
            window,
            text="可填绝对路径（如 E:/CodexHome/config.toml），也可用 {home}、{appdata} 占位符。\n"
                 "保存会写入 local/agents/<id>.toml，只影响本机，不改内置配置。",
            bg=colors["bg"], fg=colors["muted"], font=(FONT, 9), justify="left",
        ).pack(anchor="w", padx=20)

        actions = tk.Frame(window, bg=colors["bg"])
        actions.pack(fill="x", padx=20, side="bottom", pady=16)

        def save() -> None:
            new_config = value.get().strip().replace("\\", "/")
            if not new_config:
                messagebox.showerror("路径为空", "请填写配置文件路径。", parent=window)
                return
            target = paths.expand_path(new_config)
            if not target.exists() and not messagebox.askyesno(
                "文件不存在", f"{target}\n\n该文件当前不存在，仍要保存吗？", parent=window
            ):
                return
            fmt = {".json": "json", ".jsonc": "jsonc", ".toml": "toml"}.get(target.suffix.lower(), profile.format)
            updated = dataclasses.replace(profile, config=new_config, format=fmt)
            try:
                file = self._write_profile_override(updated)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                messagebox.showerror("保存失败", str(exc), parent=window)
                return
            self.profiles = profiles.load_profiles(reload=True)
            if profile.id not in self.profiles:
                messagebox.showerror("加载失败", f"覆盖文件写入后无法加载：\n{file}", parent=window)
                return
            window.destroy()
            self.refresh_agents()
            self.refresh_servers()
            self.log(f"{profile.name} 配置位置已改为 {updated.path}（覆盖：{file}）")
            messagebox.showinfo("已保存", f"配置位置已更新：\n{updated.path}", parent=self.root)

        def restore() -> None:
            file = paths.USER_PROFILE_DIR / f"{profile.id}.toml"
            if not file.exists():
                messagebox.showinfo("已是内置配置", f"{profile.name} 当前没有被本机覆盖。", parent=window)
                return
            if not messagebox.askyesno(
                "恢复默认",
                f"删除本机覆盖并回到内置路径？\n\n{file}\n\n删除前请确认没有自定义别名或备注需要保留。",
                parent=window,
            ):
                return
            file.unlink()
            self.profiles = profiles.load_profiles(reload=True)
            window.destroy()
            self.refresh_agents()
            self.refresh_servers()
            self.log(f"已删除 {profile.name} 的本机覆盖，恢复内置配置。")

        tk.Button(
            actions, text="保存", command=save, relief="flat", bd=0, padx=16, pady=7,
            bg=colors["accent"], fg="#ffffff", activebackground=colors["accent_hover"],
            activeforeground="#ffffff", font=(FONT, 9), cursor="hand2",
        ).pack(side="right")
        tk.Button(
            actions, text="取消", command=window.destroy, relief="flat", bd=0, padx=14, pady=7,
            bg=colors["surface"], fg=colors["text"], activebackground=colors["surface2"],
            font=(FONT, 9), cursor="hand2",
        ).pack(side="right", padx=8)
        tk.Button(
            actions, text="恢复默认", command=restore, relief="flat", bd=0, padx=14, pady=7,
            bg=colors["surface"], fg=colors["muted"], activebackground=colors["surface2"],
            font=(FONT, 9), cursor="hand2",
        ).pack(side="left")
        entry.focus_set()
        entry.icursor("end")

    @staticmethod
    def _write_profile_override(profile) -> Path:
        paths.USER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        file = paths.USER_PROFILE_DIR / f"{profile.id}.toml"

        def quote(value) -> str:
            return json.dumps(str(value), ensure_ascii=False)

        lines = [
            f"id = {quote(profile.id)}",
            f"name = {quote(profile.name)}",
            f"config = {quote(profile.config)}",
            f"format = {quote(profile.format)}",
            f"schema = {quote(profile.schema)}",
            f"command = {quote(profile.command)}",
            f"env = {quote(profile.env)}",
            f"transport = {quote(profile.transport)}",
            f"stdio_type = {quote(profile.stdio_type)}",
            f"http_type = {quote(profile.http_type)}",
            f"url_key = {quote(profile.url_key)}",
            f"enabled = {'true' if profile.enabled else 'false'}",
            f"tools = {'true' if profile.tools else 'false'}",
            f"readonly = {'true' if profile.readonly else 'false'}",
            "aliases = [" + ", ".join(quote(alias) for alias in profile.aliases) + "]",
            f"notes = {quote(profile.notes)}",
        ]
        file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return file

    # ---------------------------------------------------------------- versions
    def check_updates(self) -> None:
        data = registry.load()
        targets = [(name, spec) for name, spec in registry.servers(data).items() if spec.get("package")]
        if not targets:
            messagebox.showinfo("没有可检查项", "主注册表里没有登记 package 的 MCP。", parent=self.root)
            return

        def work():
            results = []
            for name, spec in targets:
                field = registry.version_field(spec)
                current = str(spec.get(field, "")) if field else ""
                try:
                    latest = versioning.latest_version(spec)
                    status, _cmp = versioning.compare_versions(current, latest)
                    results.append((name, str(spec.get("package")), current, latest, status,
                                    status == versioning.STATUS_NEWER))
                except Exception as exc:  # noqa: BLE001 - shown in the table
                    results.append((name, str(spec.get("package")), current,
                                    f"查询失败：{exc}", versioning.STATUS_FAILED, False))
            return results

        self.run_task("正在查询 npm / PyPI 版本…", work, self._show_updates)

    def _show_updates(self, results: list) -> None:
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title("可升级 MCP")
        window.geometry("760x460")
        window.configure(bg=colors["bg"])
        tk.Label(window, text="版本检查结果", bg=colors["bg"], fg=colors["text"],
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        updatable = [item for item in results if item[5]]
        tk.Label(window, text=f"发现 {len(updatable)} 个可更新项，共检查 {len(results)} 个包。",
                 bg=colors["bg"], fg=colors["muted"], font=(FONT, 9)).pack(anchor="w", padx=16, pady=(0, 8))

        frame = tk.Frame(window, bg=colors["bg"])
        frame.pack(fill="both", expand=True, padx=16)
        columns = ("MCP", "包", "当前", "最新", "状态")
        table = ttk.Treeview(frame, columns=columns, show="headings", style="App.Treeview")
        for column, width in zip(columns, (150, 220, 110, 150, 120)):
            table.heading(column, text=column)
            table.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview, style="App.Vertical.TScrollbar")
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for name, package, current, latest, status, is_new in results:
            table.insert("", "end", values=(name, package, current or "-", latest, status),
                         tags=("warn",) if is_new else ("missing",))
        self._style_table(table)

        tk.Button(window, text="关闭", command=window.destroy, relief="flat", bd=0, padx=14, pady=7,
                  bg=colors["surface"], fg=colors["text"], font=(FONT, 9), cursor="hand2").pack(
            side="right", padx=16, pady=12)
        for item in results:
            self.log(f"{item[0]}：{item[2] or '未登记'} -> {item[3]}")
        if updatable:
            self.log("可更新：" + "、".join(item[0] for item in updatable))

    def update_selected(self) -> None:
        name = self.selected_name()
        if not name:
            return
        data = registry.load()
        spec = registry.entry(data, name)
        if spec is None:
            messagebox.showerror("未登记", f"{name} 只在 Agent 配置中发现，不在主注册表中。", parent=self.root)
            return
        command = spec.get("updateCommand")
        shown = versioning.format_command(command)
        if shown:
            cwd = os.getcwd()
            if not messagebox.askyesno(
                "执行更新命令",
                f"{name}\n\n命令：\n{shown}\n\n执行目录：\n{cwd}\n\n"
                f"来源：\n{paths.REGISTRY_PATH} 的 updateCommand\n\n"
                "该命令由注册表内容决定，确认后将作为本地程序执行，不是沙箱。"
                "请确认来源可信后继续。",
                parent=self.root,
            ):
                return
            self.run_task(
                f"正在更新 {name}…",
                lambda: versioning.run_custom_update(command, cwd=cwd),
                lambda result: self._after_custom_update(name, result),
            )
            return
        package, field = spec.get("package"), registry.version_field(spec)
        if not package or not field:
            messagebox.showinfo(
                "没有更新方式",
                f"{name} 没有可自动更新的版本登记。\n\n"
                "可在右键菜单选择“设置更新命令…”，填入 git pull、pip install -U 等命令后再更新。",
                parent=self.root,
            )
            return
        self.run_task(
            f"正在查询 {name} 的最新版本…",
            lambda: versioning.latest_version(spec),
            lambda latest: self._confirm_update(name, str(latest)),
        )

    def _confirm_update(self, name: str, latest: str) -> None:
        data = registry.load()
        spec = registry.entry(data, name)
        field = registry.version_field(spec) if spec else None
        if spec is None or not field:
            messagebox.showerror("未登记", f"{name} 已不在主注册表中。", parent=self.root)
            return
        package = spec.get("package")
        current = str(spec.get(field, ""))
        status, _cmp = versioning.compare_versions(current, latest)
        if status == versioning.STATUS_LATEST:
            messagebox.showinfo("已是最新", f"{name} 当前版本 {current} 已是官方最新版本。", parent=self.root)
            return
        if status == versioning.STATUS_CURRENT_HIGHER:
            messagebox.showinfo(
                "无需升级",
                f"{name} 当前登记版本 {current} 高于查询到的最新版本 {latest}，不会降级。",
                parent=self.root,
            )
            return
        if status == versioning.STATUS_UNCOMPARABLE:
            if not messagebox.askyesno(
                "版本无法比较",
                f"{name} 的版本 {current or '未登记'} 与 {latest} 无法按数字规则比较。\n"
                f"是否仍将注册表版本写为 {latest}？",
                parent=self.root,
            ):
                return
        if spec.get("kind") == "npm-global":
            if not messagebox.askyesno(
                "全局安装", f"将执行：\n\nnpm install -g {package}@{latest}\n\n继续吗？", parent=self.root
            ):
                return

            def work():
                return versioning.npm_install_global(package, latest)

            self.run_task(
                f"正在安装 {package}@{latest}…",
                work,
                lambda result: self._after_install(name, latest, result),
            )
            return
        if not messagebox.askyesno(
            "更新版本登记",
            f"{name}\n当前 {current or '未登记'} -> {latest}\n\n写入主注册表并重新生成候选，不会直接改 Agent。继续吗？",
            parent=self.root,
        ):
            return
        self._finish_update(name, latest)

    def _after_install(self, name: str, latest: str, result) -> None:
        ok, detail = result
        if detail:
            self.log(detail[-1500:])
        if not ok:
            messagebox.showerror("安装失败", "npm install 失败，详情见日志。", parent=self.root)
            return
        self._finish_update(name, latest)

    def _finish_update(self, name: str, latest: str) -> None:
        data = registry.load()
        spec = registry.entry(data, name)
        field = registry.version_field(spec) if spec else None
        if spec is None or not field:
            messagebox.showerror("未登记", f"{name} 已不在主注册表中。", parent=self.root)
            return
        spec[field] = latest
        registry.save(data)
        self.log(f"{name}：注册表版本已更新为 {latest}。")
        try:
            candidate = sync.build_candidate(self.current(), data)
            self.log(f"已重新生成候选：{candidate.path}")
            messagebox.showinfo("更新完成", f"{name} 已更新到 {latest}。\n候选：{candidate.path}", parent=self.root)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            messagebox.showinfo("更新完成", f"{name} 已更新到 {latest}。候选生成失败：{exc}", parent=self.root)
        self.refresh_all()

    def _after_custom_update(self, name: str, result) -> None:
        ok, detail = result
        if detail:
            self.log(detail[-2000:])
        if not ok:
            messagebox.showerror("更新失败", f"{name} 的更新命令执行失败，详情见日志。", parent=self.root)
            return
        messagebox.showinfo(
            "更新完成",
            f"{name} 的更新命令已执行完成。\n如路径或版本有变化，可用“修改版本登记”记录。",
            parent=self.root,
        )
        self.refresh_all()

    def edit_update_command(self) -> None:
        name = self.selected_name()
        if not name:
            return
        data = registry.load()
        spec = registry.entry(data, name)
        if spec is None:
            messagebox.showinfo("未登记", f"{name} 不在主注册表中，请先登记。", parent=self.root)
            return
        current = versioning.format_command(spec.get("updateCommand"))
        command = simpledialog.askstring(
            "设置更新命令",
            f"输入 {name} 的升级命令（留空清除）：\n"
            "例如：git -C D:/AI/laya pull\n"
            "或：D:/AI/laya/.venv/Scripts/python.exe -m pip install -U 包名\n\n"
            "命令会在确认后作为本地程序执行，不是沙箱；不支持 &&、|、> 等操作符，"
            "也不能使用 cmd、PowerShell、bash 作为入口。",
            initialvalue=current,
            parent=self.root,
        )
        if command is None:
            return
        command = command.strip()
        if command:
            spec["updateCommand"] = command
        else:
            spec.pop("updateCommand", None)
        registry.save(data)
        self.refresh_servers()
        if command:
            self.log(f"{name} 的更新命令已设置：{command}")
        else:
            self.log(f"{name} 的更新命令已清除。")

    def edit_version(self) -> None:
        name = self.selected_name()
        if not name:
            return
        data = registry.load()
        spec = registry.entry(data, name)
        if spec is None:
            messagebox.showinfo("未登记", f"{name} 不在主注册表中，请先登记。", parent=self.root)
            return
        field = registry.version_field(spec)
        if not field:
            if spec.get("package"):
                messagebox.showinfo("无版本字段", f"{name} 没有可编辑的版本字段。", parent=self.root)
                return
            field = "installedVersion"
        value = simpledialog.askstring("设置版本", f"输入 {name} 的版本：", initialvalue=spec.get(field, ""),
                                       parent=self.root)
        if not value or not value.strip():
            return
        spec[field] = value.strip()
        registry.save(data)
        self.refresh_servers()
        self.log(f"已更新 {name} 的版本登记为 {value.strip()}。尚未应用到 Agent。")

    # ---------------------------------------------------------------- backups
    def backup_all(self) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        count = 0
        for profile in self.profiles.values():
            if profile.path.exists():
                suffix = profile.path.suffix or ".json"
                shutil.copy2(profile.path, paths.BACKUP_DIR / f"{profile.id}-{stamp}{suffix}")
                count += 1
        self.log(f"已备份 {count} 份配置，时间戳：{stamp}")
        messagebox.showinfo("备份完成", f"已备份 {count} 份配置到：\n{paths.BACKUP_DIR}", parent=self.root)

    def open_backup_manager(self) -> None:
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title("备份管理")
        window.geometry("760x480")
        window.configure(bg=colors["bg"])
        tk.Label(window, text="配置备份", bg=colors["bg"], fg=colors["text"],
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(window, text=str(paths.BACKUP_DIR), bg=colors["bg"], fg=colors["muted"],
                 font=(FONT, 9)).pack(anchor="w", padx=16, pady=(0, 8))

        frame = tk.Frame(window, bg=colors["bg"])
        frame.pack(fill="both", expand=True, padx=16)
        columns = ("文件", "大小", "时间")
        table = ttk.Treeview(frame, columns=columns, show="headings", style="App.Treeview")
        for column, width in zip(columns, (420, 90, 180)):
            table.heading(column, text=column)
            table.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview, style="App.Vertical.TScrollbar")
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def reload() -> None:
            for item in table.get_children():
                table.delete(item)
            files = sorted(paths.BACKUP_DIR.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True)
            for file in files:
                if not file.is_file():
                    continue
                stat = file.stat()
                table.insert("", "end", iid=str(file), values=(
                    file.name,
                    f"{stat.st_size / 1024:.1f} KB",
                    dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                ))

        def restore() -> None:
            selection = table.selection()
            if not selection:
                messagebox.showinfo("选择备份", "请先选择一个备份文件。", parent=window)
                return
            self._restore_backup(Path(selection[0]))
            reload()

        actions = tk.Frame(window, bg=colors["bg"])
        actions.pack(fill="x", padx=16, pady=12)
        for text, command, kind in (
            ("恢复选中", restore, "accent"),
            ("打开目录", lambda: self._open(paths.BACKUP_DIR), "ghost"),
            ("刷新", reload, "ghost"),
        ):
            tk.Button(actions, text=text, command=command, relief="flat", bd=0, padx=14, pady=7,
                      bg=colors["accent"] if kind == "accent" else colors["surface"],
                      fg="#ffffff" if kind == "accent" else colors["text"],
                      activebackground=colors["accent_hover"] if kind == "accent" else colors["surface2"],
                      activeforeground="#ffffff" if kind == "accent" else colors["text"],
                      font=(FONT, 9), cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(actions, text="关闭", command=window.destroy, relief="flat", bd=0, padx=14, pady=7,
                  bg=colors["surface"], fg=colors["text"], font=(FONT, 9), cursor="hand2").pack(side="right")
        reload()

    def _restore_backup(self, backup: Path) -> None:
        stem = backup.stem.lower()
        candidates = [
            profile for profile in self.profiles.values()
            if stem.startswith(profile.id.lower()) or profile.name.lower().replace(" ", "-") in stem
        ]
        candidates.sort(key=lambda profile: len(profile.id), reverse=True)
        if not candidates or (len(candidates) > 1 and len(candidates[0].id) == len(candidates[1].id)):
            messagebox.showerror("无法识别", f"备份文件名无法唯一对应到 Agent：\n{backup.name}", parent=self.root)
            return
        profile = candidates[0]
        if not messagebox.askyesno(
            "确认恢复", f"将用备份覆盖：\n{profile.path}\n\n恢复前会再备份当前文件。继续吗？", parent=self.root
        ):
            return
        if profile.path.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(profile.path, paths.BACKUP_DIR / f"{profile.id}-before-restore-{stamp}{profile.path.suffix}")
        profile.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, profile.path)
        self.log(f"已恢复：{profile.path}")
        self.refresh_all()

    # ------------------------------------------------------------- diagnostics
    def diagnose_current(self) -> None:
        profile = self.current()
        try:
            rows = doctor.diagnose(profile)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            messagebox.showerror("诊断失败", str(exc), parent=self.root)
            return
        colors = self.palette()
        window = tk.Toplevel(self.root)
        window.title(f"诊断 - {profile.name}")
        window.geometry("760x460")
        window.configure(bg=colors["bg"])
        tk.Label(window, text=f"{profile.name} 诊断", bg=colors["bg"], fg=colors["text"],
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(window, text=str(profile.path), bg=colors["bg"], fg=colors["muted"],
                 font=(FONT, 9)).pack(anchor="w", padx=16, pady=(0, 8))
        frame = tk.Frame(window, bg=colors["bg"])
        frame.pack(fill="both", expand=True, padx=16)
        columns = ("MCP", "状态", "详情")
        table = ttk.Treeview(frame, columns=columns, show="headings", style="App.Treeview")
        for column, width in zip(columns, (160, 80, 460)):
            table.heading(column, text=column)
            table.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview, style="App.Vertical.TScrollbar")
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for row in rows:
            state = "正常" if row["available"] else "异常"
            table.insert("", "end", values=(row["name"], state, row["detail"]),
                         tags=("ok",) if row["available"] else ("warn",))
        self._style_table(table)
        tk.Button(window, text="关闭", command=window.destroy, relief="flat", bd=0, padx=14, pady=7,
                  bg=colors["surface"], fg=colors["text"], font=(FONT, 9), cursor="hand2").pack(
            side="right", padx=16, pady=12)
        self.log(f"{profile.name} 诊断完成：{len(rows)} 项。")

    def check_runtime(self) -> None:
        rows = doctor.environment_report()
        text = "\n".join(f"{row['name']:10} {'正常' if row['available'] else '缺失':4} {row['path']}" for row in rows)
        text += f"\n\n数据目录：{paths.DATA_DIR}"
        messagebox.showinfo("本机运行时", text, parent=self.root)
        self.log("运行时检查完成。")

    # ------------------------------------------------------------------- scan
    def scan_all(self) -> None:
        def work():
            data = registry.load()
            profile_paths = [path for profile in self.profiles.values() for path in profile.paths]
            report, unknown = discovery.scan_report(data, profile_paths)
            target = paths.REPORT_DIR / "discovery-report.json"
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return report, unknown, target

        self.run_task("正在扫描本机 MCP 配置…", work, self._show_scan)

    def _show_scan(self, payload) -> None:
        report, unknown, target = payload
        effective = report.get("effectiveServers", {})
        cached = report.get("cachedServers", {})
        self.log(f"扫描到 {len(report['configFiles'])} 个有效配置，{len(report['discoveredServers'])} 个 MCP 名称。")
        self.log(f"当前生效：{len(effective)} 个；缓存/插件/备份：{len(cached)} 个。")
        self.log("未登记（当前生效）：" + (", ".join(unknown) if unknown else "无"))
        if report.get("unknownCached"):
            self.log("未登记（缓存/插件/备份）：" + ", ".join(report["unknownCached"]))
        self.log("报告：" + str(target))

        lines = [
            f"配置文件：{len(report['configFiles'])} 个",
            f"当前生效配置：{len(effective)} 个 MCP",
            f"用户配置：{len(report.get('userServers', {}))} 个 MCP",
            f"缓存/插件/备份：{len(cached)} 个 MCP",
        ]
        counts: dict[str, int] = {}
        for item in report["configFiles"]:
            counts[item["level"]] = counts.get(item["level"], 0) + 1
            if item["effective"]:
                self.log(f"生效配置：{item['path']}")
        if counts:
            lines += ["", "配置来源分级：", *[f"  {level}：{count} 个" for level, count in counts.items()]]
        if unknown:
            lines += ["", "当前生效但未登记：", *[f"  - {name}" for name in unknown]]
        if report.get("unknownCached"):
            lines += ["", "仅在缓存/插件/备份中发现（仅供参考）：",
                      *[f"  - {name}" for name in report["unknownCached"]]]
        lines += ["", f"报告：{target}"]
        if unknown:
            messagebox.showwarning("扫描完成", "\n".join(lines), parent=self.root)
        else:
            messagebox.showinfo("扫描完成", "\n".join(lines), parent=self.root)

    def add_scan_folder(self) -> None:
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="选择要纳入 MCP 扫描的目录", parent=self.root)
        if not folder:
            return
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        file = paths.DISCOVERY_ROOTS_FILE
        existing = file.read_text(encoding="utf-8-sig").splitlines() if file.exists() else []
        if folder not in existing:
            with file.open("a", encoding="utf-8") as handle:
                if existing and existing[-1].strip():
                    handle.write("\n")
                handle.write(folder + "\n")
        self.log("已加入扫描目录：" + folder)

    def open_report(self) -> None:
        target = paths.REPORT_DIR / "discovery-report.json"
        if target.exists():
            self._open(target)
        else:
            messagebox.showinfo("没有报告", "还没有扫描报告，请先执行“扫描全部 MCP”。", parent=self.root)

    # -------------------------------------------------------------- async task
    def run_task(self, label: str, work, on_done) -> None:
        if self.busy:
            self.log("已有任务在执行，请稍候。")
            return
        self.busy = True
        self.set_status(label)
        self.progress.pack(side="right", padx=(0, 8), pady=6)
        self.progress.start(12)
        result: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                result.put(("ok", work()))
            except Exception as exc:  # noqa: BLE001 - forwarded to the UI thread
                result.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            try:
                status, payload = result.get_nowait()
            except queue.Empty:
                self.root.after(120, poll)
                return
            self.progress.stop()
            self.progress.pack_forget()
            self.busy = False
            self.set_status("就绪")
            if status == "error":
                self.log(f"任务失败：{payload}")
                messagebox.showerror("任务失败", str(payload), parent=self.root)
                return
            on_done(payload)

        self.root.after(120, poll)

    # ------------------------------------------------------------------- misc
    def log(self, text: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _toggle_log(self) -> None:
        if self.log_box.winfo_manager():
            self.log_box.pack_forget()
            self.log_toggle_button.configure(text="展开")
        else:
            self.log_box.pack(fill="x", pady=(6, 0))
            self.log_toggle_button.configure(text="收起")

    def _open(self, target: Path) -> None:
        if not Path(target).exists():
            messagebox.showinfo("不存在", str(target), parent=self.root)
            return
        if os.name == "nt":
            os.startfile(str(target))
        else:
            self.log(f"请手动打开：{target}")

    def open_selected_config(self) -> None:
        profile = self.current()
        if not profile.path.exists():
            messagebox.showerror("配置不存在", str(profile.path), parent=self.root)
            return
        self._open(profile.path)

    def show_shortcuts(self) -> None:
        messagebox.showinfo(
            "快捷键",
            "F5            刷新\n"
            "Ctrl+R        扫描全部 MCP\n"
            "Ctrl+U        检查可升级包\n"
            "Ctrl+F        聚焦搜索框\n"
            "Ctrl+B        备份全部配置\n"
            "Ctrl+D        诊断当前 Agent\n"
            "Esc           清空搜索\n"
            "双击          打开 Agent 配置\n"
            "右键          更多操作",
            parent=self.root,
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于 MCP Manager",
            f"MCP Manager {__version__}\n\n"
            "跨 Agent 的 MCP 配置管理工具。\n"
            "Agent 差异由 mcpmanager/agents/*.toml 声明，无需改代码。\n\n"
            f"数据目录：{paths.DATA_DIR}\n"
            f"项目主页：{REPO_URL}",
            parent=self.root,
        )

    def _bind_keys(self) -> None:
        self.root.bind("<F5>", lambda _event: self.refresh_all())
        self.root.bind("<Control-r>", lambda _event: self.scan_all())
        self.root.bind("<Control-u>", lambda _event: self.check_updates())
        self.root.bind("<Control-f>", lambda _event: self.search_entry.focus_set())
        self.root.bind("<Control-b>", lambda _event: self.backup_all())
        self.root.bind("<Control-d>", lambda _event: self.diagnose_current())
        self.root.bind("<Escape>", lambda _event: self.search_var.set(""))

    def _report_exception(self, exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            self.log(f"操作失败：{exc_type.__name__}: {exc_value}")
        except Exception:
            pass
        messagebox.showerror("操作失败", f"{exc_type.__name__}: {exc_value}\n\n{detail.strip().splitlines()[-1]}",
                             parent=self.root)

    def _on_close(self) -> None:
        self._save_settings()
        self.root.destroy()


def main() -> None:
    App()
