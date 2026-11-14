"""Tkinter shell over the agent-agnostic core.

Every button delegates to :mod:`mcpmanager` modules; no Agent-specific paths or
schemas live here.
"""

from __future__ import annotations

import datetime as dt
import os
import queue
import shutil
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import adapters, discovery, doctor, formats, paths, profiles, registry, sync, versioning

SCAN_RESULT: queue.Queue = queue.Queue()
SCAN_RUNNING = {"value": False}


class App:
    def __init__(self) -> None:
        paths.ensure_layout()
        self.profiles = profiles.load_profiles()
        if not self.profiles:
            raise RuntimeError("没有找到任何 agents/*.toml，无法启动。")
        self.root = tk.Tk()
        self.root.title("MCP Manager")
        self.root.geometry("1180x780")
        self.root.minsize(980, 640)
        self.root.report_callback_exception = self._report_exception

        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="MCP Manager", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="跨 Agent 扫描、检查、备份与同步 MCP。每个 Agent 的路径和字段差异由 agents/*.toml 描述。\n",
        ).pack(anchor="w")

        agent_row = ttk.Frame(frame)
        agent_row.pack(fill="x", pady=(0, 6))
        ttk.Label(agent_row, text="目标 Agent：").pack(side="left")
        self.agent_choice = tk.StringVar(value="trae" if "trae" in self.profiles else next(iter(self.profiles)))
        chooser = ttk.Combobox(
            agent_row,
            textvariable=self.agent_choice,
            values=list(self.profiles),
            state="readonly",
            width=20,
        )
        chooser.pack(side="left")
        chooser.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        self.agent_status = tk.StringVar(value="")
        ttk.Label(agent_row, textvariable=self.agent_status).pack(side="left", padx=(12, 0))

        columns = ("MCP", "类型", "版本", "状态", "适用 Agent")
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=15)
        for column, width in zip(columns, (200, 150, 120, 250, 340)):
            self.tree.heading(column, text=column)
            self.tree.column(column, width=width, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        actions = (
            ("刷新状态", self.refresh),
            ("扫描全部 MCP", self.scan_all),
            ("添加扫描目录", self.add_scan_folder),
            ("打开发现报告", self.open_report),
            ("检查可升级包", self.check_updates),
            ("更新选中 MCP", self.update_selected),
            ("修改选中版本", self.edit_version),
            ("列出 Agent 配置", self.list_agent_configs),
            ("备份当前配置", self.backup_configs),
            ("恢复配置备份", self.restore_backup),
            ("诊断当前 Agent", self.diagnose_current),
            ("检查运行时", self.check_runtime),
            ("打开主注册表", self.open_registry),
            ("打开候选目录", self.open_generated),
            ("打开数据目录", self.open_data),
            ("打开目标配置", self.open_selected_config),
            ("生成候选配置", self.generate_candidate),
            ("预览当前候选", self.show_candidate_preview),
        )
        for index, (label, command) in enumerate(actions):
            ttk.Button(buttons, text=label, command=command).grid(
                row=index // 6, column=index % 6, padx=(0, 6), pady=2, sticky="w"
            )

        self.output = tk.Text(frame, height=9, state="disabled", wrap="word")
        self.output.pack(fill="both", expand=False)

        self.refresh()
        self.root.mainloop()

    # ------------------------------------------------------------------ ui
    def log(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _report_exception(self, exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            self.log(f"操作失败：{exc_type.__name__}: {exc_value}")
        except Exception:
            pass
        messagebox.showerror("操作失败", f"{exc_type.__name__}: {exc_value}\n\n{detail.strip().splitlines()[-1]}")

    def current_profile(self):
        return self.profiles.get(self.agent_choice.get()) or next(iter(self.profiles.values()))

    def selected_name(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("选择 MCP", "请先在列表中选择一个 MCP。")
            return None
        return self.tree.item(selection[0], "values")[0]

    @staticmethod
    def tagged_for(profile, spec) -> bool:
        agents = [str(item).lower() for item in spec.get("agents", [])]
        if "*" in agents:
            return True
        keys = {profile.id.lower(), profile.name.lower(), *(alias.lower() for alias in profile.aliases)}
        return any(agent in keys for agent in agents)

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        profile = self.current_profile()
        try:
            data = registry.load()
        except Exception as exc:
            self.log(f"注册表读取失败：{exc}")
            return
        configured: dict[str, str] = {}
        section: dict = {}
        if profile.path.exists():
            try:
                section = formats.read_section(profile.path, profile.format, profile.schema)
            except Exception as exc:
                self.log(f"{profile.name} 配置解析失败：{exc}")
        for name in section:
            configured.setdefault(registry.canonical(name), name)

        rows = []
        registered: set[str] = set()
        for name, spec in registry.servers(data).items():
            canonical = registry.canonical(name)
            here = canonical in configured
            if not (self.tagged_for(profile, spec) or here):
                continue
            registered.add(canonical)
            if spec.get("kind") == "copilot-plugin-host":
                state = "宿主/插件级"
            elif here:
                state = "已配置"
            else:
                state = "注册表已登记"
            rows.append((name, spec, state))
        for name in section:
            if registry.canonical(name) not in registered:
                rows.append((name, {"kind": "仅配置中发现", "agents": [profile.id]}, "已配置（未登记）"))

        for name, spec, state in rows:
            live = section.get(name, spec)
            command = adapters.command_of(live)
            if command and (Path(command).exists() or shutil.which(command)):
                state += " / 路径正常"
            field = registry.version_field(spec)
            version = spec.get(field, "") if field else ""
            agents = ", ".join(str(item) for item in spec.get("agents", []))
            self.tree.insert("", "end", values=(name, spec.get("kind", ""), version, state, agents))

        self.agent_status.set(
            f"{profile.name} | 配置：{profile.path} | 已配置 {len(section)} 项；显示 {len(rows)} 项"
        )
        self.log(f"已刷新 {profile.name}：显示 {len(rows)} 项，当前配置 {len(section)} 项。")

    # --------------------------------------------------------------- scan
    def scan_all(self) -> None:
        if SCAN_RUNNING["value"]:
            self.log("扫描仍在进行，请稍候……")
            return
        SCAN_RUNNING["value"] = True
        self.log("开始扫描 MCP 配置……")

        def worker():
            try:
                data = registry.load()
                profile_paths = [item.path for item in self.profiles.values()]
                report, unknown = discovery.scan_report(data, profile_paths)
                target = paths.REPORT_DIR / "discovery-report.json"
                import json

                target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                SCAN_RESULT.put(("ok", (report, unknown, target)))
            except Exception as exc:  # noqa: BLE001 - forwarded to UI
                SCAN_RESULT.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(200, self.poll_scan)

    def poll_scan(self) -> None:
        try:
            status, payload = SCAN_RESULT.get_nowait()
        except queue.Empty:
            self.root.after(200, self.poll_scan)
            return
        SCAN_RUNNING["value"] = False
        if status == "error":
            self.log(f"扫描失败：{payload}")
            messagebox.showerror("扫描失败", str(payload))
            return
        report, unknown, target = payload
        self.log(f"扫描到 {len(report['configFiles'])} 个有效配置，{len(report['discoveredServers'])} 个 MCP 名称。")
        self.log("未登记 MCP：" + (", ".join(unknown) if unknown else "无"))
        self.log("发现报告：" + str(target))
        if unknown:
            messagebox.showwarning("发现未登记 MCP", "发现未登记 MCP：\n" + "\n".join(unknown))
        else:
            messagebox.showinfo("扫描完成", "未发现主注册表之外的 MCP。")

    def add_scan_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择要纳入 MCP 扫描的目录")
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
            self.log("还没有发现报告，请先点击“扫描全部 MCP”。")

    # ------------------------------------------------------------ versions
    def check_updates(self) -> None:
        data = registry.load()
        targets = [(name, spec) for name, spec in registry.servers(data).items() if spec.get("package")]
        if not targets:
            self.log("主注册表里没有登记 package 的 MCP。")
            return
        self.log("正在查询版本（npm / PyPI），请稍候……")
        self.root.update_idletasks()
        updatable = []
        for name, spec in targets:
            field = registry.version_field(spec)
            current = spec.get(field, "") if field else ""
            try:
                latest = versioning.latest_version(spec)
            except Exception as exc:  # noqa: BLE001 - reported inline
                self.log(f"{name}（{spec['package']}）：查询失败 —— {exc}")
                continue
            if registry.normalize_version(latest) == registry.normalize_version(current):
                self.log(f"{name}（{spec['package']}）：已是最新 {current}")
            else:
                updatable.append(name)
                self.log(f"{name}（{spec['package']}）：当前 {current or '未登记'} -> 最新 {latest}")
        self.log("可更新：" + ("、".join(updatable) if updatable else "无"))

    def update_selected(self) -> None:
        name = self.selected_name()
        if not name:
            return
        data = registry.load()
        spec = registry.entry(data, name)
        if spec is None:
            messagebox.showerror("未登记", f"{name} 只在 Agent 配置中发现，不在主注册表中。")
            return
        package, field = spec.get("package"), registry.version_field(spec)
        if not package or not field:
            messagebox.showinfo("无法自动更新", f"{name} 不是包管理器安装的 MCP。")
            return
        try:
            latest = versioning.latest_version(spec)
        except Exception as exc:  # noqa: BLE001 - reported inline
            self.log(f"{name}：版本查询失败 —— {exc}")
            messagebox.showerror("版本查询失败", str(exc))
            return
        current = spec.get(field, "")
        if registry.normalize_version(latest) == registry.normalize_version(current):
            messagebox.showinfo("已是最新", f"{name} 当前版本 {current} 已是官方最新版本。")
            return
        if spec.get("kind") == "npm-global":
            if not messagebox.askyesno("全局安装", f"将执行：\n\nnpm install -g {package}@{latest}\n\n继续吗？"):
                return
            self.log(f"正在执行 npm install -g {package}@{latest} ……")
            self.root.update_idletasks()
            ok, detail = versioning.npm_install_global(package, latest)
            if detail:
                self.log(detail[-1500:])
            if not ok:
                messagebox.showerror("安装失败", "npm install 失败，详情见日志区。")
                return
        elif not messagebox.askyesno(
            "更新版本登记",
            f"{name}\n当前 {current or '未登记'} -> {latest}\n\n写入主注册表并重新生成候选，不会直接改 Agent。继续吗？",
        ):
            return
        spec[field] = latest
        registry.save(data)
        self.log(f"{name}：注册表版本已更新为 {latest}。")
        try:
            candidate = sync.build_candidate(self.current_profile(), data)
            self.refresh()
            self.log(f"已重新生成候选：{candidate.path}")
            messagebox.showinfo("更新完成", f"{name} 已更新到 {latest}。\n候选：{candidate.path}\n确认后再应用。")
        except Exception as exc:  # noqa: BLE001 - reported inline
            self.refresh()
            messagebox.showinfo("更新完成", f"{name} 已更新到 {latest}。候选生成失败：{exc}")

    def edit_version(self) -> None:
        name = self.selected_name()
        if not name:
            return
        data = registry.load()
        spec = registry.entry(data, name)
        if spec is None:
            messagebox.showinfo("未登记", f"{name} 不在主注册表中，请先登记。")
            return
        field = registry.version_field(spec)
        if not field:
            messagebox.showinfo("无版本字段", f"{name} 没有可编辑的版本字段。")
            return
        value = simpledialog.askstring("设置版本", f"输入 {name} 的版本：", initialvalue=spec.get(field, ""))
        if not value or not value.strip():
            return
        spec[field] = value.strip()
        registry.save(data)
        self.refresh()
        self.log(f"已更新 {name} 的版本登记为 {value.strip()}。尚未应用到 Agent。")

    # ---------------------------------------------------------- candidates
    def generate_candidate(self) -> None:
        try:
            candidate = sync.build_candidate(self.current_profile(), registry.load())
        except Exception as exc:  # noqa: BLE001 - reported inline
            self.log(f"生成候选失败：{exc}")
            messagebox.showerror("生成候选失败", str(exc))
            return
        self.log(f"已生成候选：{candidate.path}")
        self.show_preview(candidate.path)

    def show_candidate_preview(self) -> None:
        self.show_preview(sync.candidate_path(self.current_profile()))

    def show_preview(self, path: Path) -> None:
        if not path.exists():
            messagebox.showinfo("没有候选配置", "请先点击“生成候选配置”。")
            return
        candidate = sync.load_candidate(path)
        fresh = sync.is_fresh(candidate)
        window = tk.Toplevel(self.root)
        window.title(f"{candidate.get('agentName', candidate['agent'])} 候选预览")
        window.geometry("820x560")
        ttk.Label(window, text=f"目标：{candidate['targetPath']}", wraplength=780).pack(anchor="w", padx=10, pady=8)
        ttk.Label(
            window,
            text="源配置未变化，可应用" if fresh else "源配置已变化，请重新生成",
            foreground=("green" if fresh else "red"),
        ).pack(anchor="w", padx=10)
        box = tk.Text(window, wrap="none")
        box.pack(fill="both", expand=True, padx=10, pady=8)
        import json

        box.insert("end", json.dumps(candidate["servers"], ensure_ascii=False, indent=2))
        box.configure(state="disabled")
        ttk.Button(
            window, text="应用到此 Agent…", command=lambda: self.apply_candidate(path, window)
        ).pack(anchor="e", padx=10, pady=(0, 10))

    def apply_candidate(self, path: Path, window: tk.Toplevel | None = None) -> None:
        candidate = sync.load_candidate(path)
        if not sync.is_fresh(candidate):
            messagebox.showwarning("配置已变化", "目标配置在生成候选后发生变化，请重新生成。")
            return
        if not messagebox.askyesno(
            "确认应用",
            f"将先备份，再仅替换 MCP 区块。\n\nAgent：{candidate.get('agentName', candidate['agent'])}\n"
            f"配置：{candidate['targetPath']}\nMCP 数量：{len(candidate['servers'])}\n\n继续吗？",
        ):
            return
        try:
            backup, mode = sync.apply_candidate(path)
        except Exception as exc:  # noqa: BLE001 - reported inline
            messagebox.showerror("应用失败", str(exc))
            self.log(f"应用失败：{exc}")
            return
        self.log(f"已应用（{mode}）；备份：{backup}")
        messagebox.showinfo("应用完成", f"配置已写入。\n备份：{backup}")
        if window is not None:
            window.destroy()
        self.refresh()

    # ------------------------------------------------------------- backups
    def backup_configs(self) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        count = 0
        for profile in self.profiles.values():
            if profile.path.exists():
                suffix = profile.path.suffix or ".json"
                shutil.copy2(profile.path, paths.BACKUP_DIR / f"{profile.id}-{stamp}{suffix}")
                count += 1
        self.log(f"已备份 {count} 份配置，时间戳：{stamp}")
        messagebox.showinfo("备份完成", f"已备份 {count} 份配置。")

    def restore_backup(self) -> None:
        file = filedialog.askopenfilename(
            title="选择要恢复的备份", initialdir=str(paths.BACKUP_DIR), filetypes=[("配置文件", "*.json *.toml *.jsonc")]
        )
        if not file:
            return
        backup = Path(file)
        name = backup.stem.lower()
        matches = [
            profile
            for profile in self.profiles.values()
            if name.startswith(profile.id.lower()) or profile.name.lower().replace(" ", "-") in name
        ]
        if len(matches) != 1:
            messagebox.showerror("无法识别", f"备份文件名无法唯一对应到 Agent：\n{backup.name}")
            return
        profile = matches[0]
        if not messagebox.askyesno("确认恢复", f"将用备份覆盖：\n{profile.path}\n\n恢复前会再备份当前文件。继续吗？"):
            return
        if profile.path.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(profile.path, paths.BACKUP_DIR / f"{profile.id}-before-restore-{stamp}{profile.path.suffix}")
        profile.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, profile.path)
        self.log(f"已恢复：{profile.path}")
        messagebox.showinfo("恢复完成", str(profile.path))
        self.refresh()

    # ---------------------------------------------------------- diagnostics
    def diagnose_current(self) -> None:
        profile = self.current_profile()
        try:
            rows = doctor.diagnose(profile)
        except Exception as exc:  # noqa: BLE001 - reported inline
            messagebox.showerror("诊断失败", str(exc))
            return
        lines = [f"{profile.name}  {profile.path}"]
        for row in rows:
            state = "OK" if row["available"] else "FAIL"
            lines.append(f"{row['name']:22} {state:5} {row['detail']}")
            for problem in row["problems"]:
                lines.append(f"{'':22}  ! {problem}")
        self._show_text("诊断结果", "\n".join(lines))
        self.log("诊断完成。")

    def check_runtime(self) -> None:
        rows = doctor.environment_report()
        lines = [f"{row['name']:10} {'OK' if row['available'] else '--':3} {row['path']}" for row in rows]
        lines += ["", "数据目录：" + str(paths.DATA_DIR)]
        self._show_text("本机运行时", "\n".join(lines))
        self.log("运行时检查完成。")

    def list_agent_configs(self) -> None:
        for profile in self.profiles.values():
            try:
                section = formats.read_section(profile.path, profile.format, profile.schema)
                self.log(f"{profile.name}: {', '.join(section) or '无'}")
            except Exception as exc:  # noqa: BLE001 - reported inline
                self.log(f"{profile.name}: 解析失败：{exc}")

    def _show_text(self, title: str, text: str) -> None:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("760x420")
        box = tk.Text(window, wrap="none")
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("end", text)
        box.configure(state="disabled")

    # --------------------------------------------------------------- open
    def _open(self, target: Path) -> None:
        if os.name == "nt":
            os.startfile(str(target))
        else:
            self.log(f"请手动打开：{target}")

    def open_registry(self) -> None:
        self._open(paths.REGISTRY_PATH)

    def open_generated(self) -> None:
        paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        self._open(paths.GENERATED_DIR)

    def open_data(self) -> None:
        self._open(paths.DATA_DIR)

    def open_selected_config(self) -> None:
        profile = self.current_profile()
        if not profile.path.exists():
            messagebox.showerror("配置不存在", str(profile.path))
            return
        self._open(profile.path)


def main() -> None:
    App()
