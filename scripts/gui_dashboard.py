from __future__ import annotations

import json
import os
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, NamedTuple


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_DIR / "local" / "pipeline.json"
TASK_NAME = "DouyinCreatorMonitor"
VALID_LOGIN_MIN_BYTES = 35_000


class DashboardError(RuntimeError):
    pass


class TaskInfo(NamedTuple):
    state: str
    last_run_time: datetime | None
    next_run_time: datetime | None
    last_result: int | None


class RunInfo(NamedTuple):
    run_id: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    wall_seconds: float
    path: Path | None


class CreatorStatus(NamedTuple):
    key: str
    name: str
    works_count: int
    pending_count: int
    status: str
    detail: str
    latest_publish_time: datetime | None
    works_updated_at: datetime | None


class DashboardSnapshot(NamedTuple):
    task: TaskInfo
    latest_run: RunInfo
    creators: list[CreatorStatus]
    total_creators: int
    total_works: int
    pending_works: int
    account_profiles_total: int
    account_profiles_detected: int
    latest_log: Path | None
    log_tail: str
    refreshed_at: datetime


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def project_path(project_dir: Path, value: Any, default: Path) -> Path:
    raw = str(value or "").strip()
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (project_dir / path).resolve()


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _process_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def query_scheduled_task(
    task_name: str = TASK_NAME,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TaskInfo:
    escaped_name = task_name.replace("'", "''")
    script = (
        f"$task=Get-ScheduledTask -TaskName '{escaped_name}' -ErrorAction Stop;"
        f"$info=Get-ScheduledTaskInfo -TaskName '{escaped_name}' -ErrorAction Stop;"
        "[pscustomobject]@{"
        "State=[string]$task.State;"
        "LastRunTime=$info.LastRunTime.ToString('o');"
        "NextRunTime=$info.NextRunTime.ToString('o');"
        "LastResult=[int64]$info.LastTaskResult"
        "}|ConvertTo-Json -Compress"
    )
    result = runner(
        ["powershell", "-NoProfile", "-Command", script],
        cwd=PROJECT_DIR,
        **_process_options(),
    )
    if result.returncode:
        raise DashboardError((result.stderr or result.stdout or "无法查询定时任务").strip())
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DashboardError("任务计划程序返回了无法识别的数据。") from exc
    return TaskInfo(
        state=str(payload.get("State") or "Unknown"),
        last_run_time=parse_datetime(payload.get("LastRunTime")),
        next_run_time=parse_datetime(payload.get("NextRunTime")),
        last_result=int(payload["LastResult"]) if payload.get("LastResult") is not None else None,
    )


def start_scheduled_task(
    task_name: str = TASK_NAME,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    result = runner(
        ["schtasks", "/run", "/tn", task_name],
        cwd=PROJECT_DIR,
        **_process_options(),
    )
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode:
        raise DashboardError(output or f"启动定时任务失败，退出码 {result.returncode}")
    return output or f"已请求启动 {task_name}"


def latest_file(directory: Path, pattern: str) -> Path | None:
    try:
        files = [path for path in directory.glob(pattern) if path.is_file()]
    except OSError:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime, path.name), default=None)


def load_run_info(state_dir: Path) -> tuple[RunInfo, dict[str, Any]]:
    path = latest_file(state_dir / "runs", "*.json")
    payload = read_json(path) if path else {}
    return (
        RunInfo(
            run_id=str(payload.get("run_id") or ""),
            status=str(payload.get("status") or "never"),
            started_at=parse_datetime(payload.get("started_at")),
            finished_at=parse_datetime(payload.get("finished_at")),
            wall_seconds=float(payload.get("wall_seconds") or 0),
            path=path,
        ),
        payload,
    )


def work_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("works")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def summarize_error(value: Any, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    if "account blocked" in lowered:
        return "抖音账号被风控（account blocked）"
    if "hermes context detected" in lowered or "not_configured" in lowered:
        return "飞书 CLI 未绑定当前运行身份"
    if "cookie" in lowered and any(word in text for word in ("失效", "过期", "登录")):
        return "抖音登录态失效，请重新登录"
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def creator_statuses(
    project_dir: Path,
    config: dict[str, Any],
    run_payload: dict[str, Any],
) -> list[CreatorStatus]:
    run_creators = {
        str(item.get("key") or ""): item
        for item in run_payload.get("creators", [])
        if isinstance(item, dict)
    }
    result: list[CreatorStatus] = []
    for creator in config.get("creators", []):
        if not isinstance(creator, dict) or not creator.get("enabled", True):
            continue
        key = str(
            creator.get("key")
            or creator.get("creator_dir_name")
            or creator.get("creator_name")
            or "?"
        )
        name = str(creator.get("creator_name") or creator.get("creator_dir_name") or key)
        works_file = project_path(
            project_dir,
            creator.get("works_file"),
            project_dir / "runtime" / f"{key}-works-from-mediacrawler.json",
        )
        works_payload = read_json(works_file)
        works = work_items(works_payload)
        pending_ids = works_payload.get("pending_aweme_ids")
        pending_count = len(pending_ids) if isinstance(pending_ids, list) else int(
            works_payload.get("pending_count") or 0
        )
        latest_epoch = max(
            (int(item.get("create_time") or 0) for item in works),
            default=0,
        )
        run_item = run_creators.get(key, {})
        status = str(run_item.get("status") or ("ready" if works_file.exists() else "not_run"))
        detail = str(
            run_item.get("collection_error")
            or run_item.get("profile_sync_error")
            or run_item.get("error")
            or ""
        ).strip()
        if not detail and run_item.get("account_profile_key"):
            detail = f"账号槽位：{run_item['account_profile_key']}"
        result.append(
            CreatorStatus(
                key=key,
                name=name,
                works_count=len(works),
                pending_count=pending_count,
                status=status,
                detail=summarize_error(detail),
                latest_publish_time=datetime.fromtimestamp(latest_epoch) if latest_epoch else None,
                works_updated_at=datetime.fromtimestamp(works_file.stat().st_mtime)
                if works_file.exists()
                else None,
            )
        )
    return result


def account_profile_status(project_dir: Path, config: dict[str, Any]) -> tuple[int, int]:
    collection = config.get("collection")
    collection = collection if isinstance(collection, dict) else {}
    profiles: list[str] = []
    for value in collection.get("account_profiles", []):
        if isinstance(value, str) and value.strip() and value not in profiles:
            profiles.append(value.strip())
    for creator in config.get("creators", []):
        if not isinstance(creator, dict):
            continue
        for value in creator.get("account_profiles", []):
            if isinstance(value, str) and value.strip() and value not in profiles:
                profiles.append(value.strip())
    media_crawler_dir = project_path(
        project_dir,
        collection.get("media_crawler_dir"),
        project_dir / "MediaCrawler",
    )
    detected = 0
    for profile in profiles:
        cookie_file = (
            media_crawler_dir
            / "browser_data"
            / f"cdp_{profile}_dy_user_data_dir"
            / "Default"
            / "Network"
            / "Cookies"
        )
        try:
            if cookie_file.stat().st_size > VALID_LOGIN_MIN_BYTES:
                detected += 1
        except OSError:
            continue
    return len(profiles), detected


def read_log_tail(path: Path | None, lines: int = 100) -> str:
    if path is None:
        return "暂无流水线日志。"
    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        return f"无法读取日志：{exc}"
    return "\n".join(content[-lines:]) or "日志文件为空。"


def build_dashboard_snapshot(
    project_dir: Path = PROJECT_DIR,
    *,
    task_provider: Callable[[str], TaskInfo] = query_scheduled_task,
) -> DashboardSnapshot:
    project_dir = project_dir.resolve()
    config_path = project_dir / "local" / "pipeline.json"
    config = read_json(config_path)
    if not config:
        raise DashboardError(f"找不到或无法解析配置：{config_path}")
    state_dir = project_path(
        project_dir,
        config.get("state_dir"),
        project_dir / "runtime" / "pipeline",
    )
    log_dir = project_path(project_dir, config.get("log_dir"), project_dir / "logs")
    latest_run, run_payload = load_run_info(state_dir)
    creators = creator_statuses(project_dir, config, run_payload)
    profiles_total, profiles_detected = account_profile_status(project_dir, config)
    latest_log = latest_file(log_dir, "pipeline-*.log")
    return DashboardSnapshot(
        task=task_provider(TASK_NAME),
        latest_run=latest_run,
        creators=creators,
        total_creators=len(creators),
        total_works=sum(item.works_count for item in creators),
        pending_works=sum(item.pending_count for item in creators),
        account_profiles_total=profiles_total,
        account_profiles_detected=profiles_detected,
        latest_log=latest_log,
        log_tail=read_log_tail(latest_log),
        refreshed_at=datetime.now().astimezone(),
    )


COLORS = {
    "bg": "#0B1120",
    "panel": "#111A2E",
    "panel_alt": "#162138",
    "border": "#263552",
    "text": "#F4F7FC",
    "muted": "#94A3B8",
    "accent": "#35D6B4",
    "accent_hover": "#22B99A",
    "blue": "#60A5FA",
    "warning": "#FBBF24",
    "danger": "#FB7185",
    "success": "#34D399",
}


def format_time(value: datetime | None, fallback: str = "暂无") -> str:
    if value is None:
        return fallback
    return value.astimezone().strftime("%m-%d %H:%M:%S") if value.tzinfo else value.strftime("%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    minutes, remaining = divmod(int(seconds), 60)
    return f"{minutes}分{remaining}秒" if minutes else f"{remaining}秒"


def status_text(status: str) -> str:
    return {
        "Ready": "待命",
        "Running": "运行中",
        "Disabled": "已禁用",
        "success": "成功",
        "partial_failure": "部分失败",
        "failed": "失败",
        "planned": "计划",
        "never": "尚未运行",
        "ready": "已有数据",
        "not_run": "无数据",
    }.get(status, status or "未知")


def status_color(status: str) -> str:
    if status in {"Ready", "success", "ready"}:
        return COLORS["success"]
    if status in {"Running", "planned"}:
        return COLORS["blue"]
    if status in {"partial_failure"}:
        return COLORS["warning"]
    if status in {"failed", "Disabled", "not_run"}:
        return COLORS["danger"]
    return COLORS["muted"]


class StatCard(tk.Frame):
    def __init__(self, parent: tk.Misc, title: str, accent: str) -> None:
        super().__init__(
            parent,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=18,
            pady=14,
        )
        tk.Frame(self, bg=accent, width=4).pack(side="left", fill="y", padx=(0, 14))
        body = tk.Frame(self, bg=COLORS["panel"])
        body.pack(side="left", fill="both", expand=True)
        tk.Label(
            body,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w")
        self.value = tk.Label(
            body,
            text="—",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 19, "bold"),
        )
        self.value.pack(anchor="w", pady=(5, 0))
        self.note = tk.Label(
            body,
            text="",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        self.note.pack(anchor="w", pady=(3, 0))

    def set(self, value: str, note: str = "", color: str | None = None) -> None:
        self.value.configure(text=value, fg=color or COLORS["text"])
        self.note.configure(text=note)


class DashboardApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.snapshot: DashboardSnapshot | None = None
        self.refreshing = False
        self.root.title("抖音达人监控中心")
        self.root.geometry("1240x820")
        self.root.minsize(1040, 700)
        self.root.configure(bg=COLORS["bg"])
        self._configure_styles()
        self._build()
        self.refresh()
        self.root.after(12_000, self._auto_refresh)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dashboard.Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["text"],
            rowheight=35,
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Dashboard.Treeview",
            background=[("selected", "#1E3A5F")],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "Dashboard.Treeview.Heading",
            background=COLORS["panel_alt"],
            foreground=COLORS["muted"],
            relief="flat",
            padding=(10, 9),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Accent.Horizontal.TProgressbar",
            background=COLORS["accent"],
            troughcolor=COLORS["panel_alt"],
            bordercolor=COLORS["panel_alt"],
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool = False,
    ) -> tk.Button:
        bg = COLORS["accent"] if primary else COLORS["panel_alt"]
        fg = COLORS["bg"] if primary else COLORS["text"]
        active = COLORS["accent_hover"] if primary else "#22304B"
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief="flat",
            bd=0,
            padx=18,
            pady=9,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _build(self) -> None:
        container = tk.Frame(self.root, bg=COLORS["bg"], padx=26, pady=22)
        container.pack(fill="both", expand=True)

        header = tk.Frame(container, bg=COLORS["bg"])
        header.pack(fill="x")
        heading = tk.Frame(header, bg=COLORS["bg"])
        heading.pack(side="left")
        tk.Label(
            heading,
            text="抖音达人监控中心",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 23, "bold"),
        ).pack(anchor="w")
        tk.Label(
            heading,
            text="采集、账号池与下游同步的一站式本地控制台",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(5, 0))

        actions = tk.Frame(header, bg=COLORS["bg"])
        actions.pack(side="right")
        self._button(actions, "打开配置", self.open_config).pack(side="left", padx=5)
        self._button(actions, "打开日志", self.open_log).pack(side="left", padx=5)
        self.refresh_button = self._button(actions, "刷新", self.refresh)
        self.refresh_button.pack(side="left", padx=5)
        self.run_button = self._button(actions, "▶ 立即运行", self.run_now, primary=True)
        self.run_button.pack(side="left", padx=(5, 0))

        status_row = tk.Frame(container, bg=COLORS["bg"])
        status_row.pack(fill="x", pady=(20, 14))
        self.status_dot = tk.Label(
            status_row,
            text="●",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 12),
        )
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(
            status_row,
            text="正在读取状态…",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        self.status_label.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(
            status_row,
            style="Accent.Horizontal.TProgressbar",
            mode="indeterminate",
            length=150,
        )

        cards = tk.Frame(container, bg=COLORS["bg"])
        cards.pack(fill="x")
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1, uniform="cards")
        self.task_card = StatCard(cards, "定时任务", COLORS["accent"])
        self.run_card = StatCard(cards, "最近运行", COLORS["blue"])
        self.creator_card = StatCard(cards, "达人数据", COLORS["warning"])
        self.account_card = StatCard(cards, "账号池", COLORS["danger"])
        for index, card in enumerate(
            (self.task_card, self.run_card, self.creator_card, self.account_card)
        ):
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0 if index == 3 else 6))

        main = tk.PanedWindow(
            container,
            orient=tk.VERTICAL,
            bg=COLORS["bg"],
            sashwidth=8,
            sashrelief="flat",
            bd=0,
        )
        main.pack(fill="both", expand=True, pady=(18, 0))

        creators_panel = tk.Frame(
            main,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        panel_head = tk.Frame(creators_panel, bg=COLORS["panel"], padx=16, pady=12)
        panel_head.pack(fill="x")
        tk.Label(
            panel_head,
            text="达人运行状态",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left")
        self.creator_summary = tk.Label(
            panel_head,
            text="",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        self.creator_summary.pack(side="right")

        columns = ("name", "works", "pending", "latest", "status", "detail")
        self.creator_tree = ttk.Treeview(
            creators_panel,
            columns=columns,
            show="headings",
            style="Dashboard.Treeview",
        )
        headings = {
            "name": "达人",
            "works": "作品数",
            "pending": "待处理",
            "latest": "数据更新时间",
            "status": "最近状态",
            "detail": "说明",
        }
        widths = {
            "name": 190,
            "works": 75,
            "pending": 75,
            "latest": 145,
            "status": 90,
            "detail": 430,
        }
        for column in columns:
            self.creator_tree.heading(column, text=headings[column])
            self.creator_tree.column(
                column,
                width=widths[column],
                minwidth=60,
                anchor="w" if column in {"name", "detail"} else "center",
                stretch=column == "detail",
            )
        self.creator_tree.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        main.add(creators_panel, minsize=260)

        log_panel = tk.Frame(
            main,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        log_head = tk.Frame(log_panel, bg=COLORS["panel"], padx=16, pady=11)
        log_head.pack(fill="x")
        tk.Label(
            log_head,
            text="最新流水线日志",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left")
        self.log_path_label = tk.Label(
            log_head,
            text="",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 8),
        )
        self.log_path_label.pack(side="right")
        self.log_text = tk.Text(
            log_panel,
            bg="#0A1020",
            fg="#C7D2E5",
            insertbackground=COLORS["text"],
            selectbackground="#24476E",
            relief="flat",
            bd=0,
            padx=14,
            pady=10,
            wrap="none",
            height=12,
            font=("Consolas", 9),
        )
        self.log_text.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self.log_text.configure(state="disabled")
        main.add(log_panel, minsize=190)

        self.footer = tk.Label(
            container,
            text="",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        )
        self.footer.pack(anchor="e", pady=(10, 0))

    def _auto_refresh(self) -> None:
        self.refresh()
        self.root.after(12_000, self._auto_refresh)

    def _show_progress(self) -> None:
        if not self.progress.winfo_ismapped():
            self.progress.pack(side="right")
        self.progress.start(12)

    def _hide_progress(self) -> None:
        self.progress.stop()
        self.progress.pack_forget()

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.refresh_button.configure(state="disabled")
        self._show_progress()
        self.status_label.configure(text="正在刷新本地状态…")

        def worker() -> None:
            try:
                snapshot = build_dashboard_snapshot(PROJECT_DIR)
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._refresh_failed(exc))
            else:
                self.root.after(0, lambda: self._apply_snapshot(snapshot))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_failed(self, exc: Exception) -> None:
        self.refreshing = False
        self.refresh_button.configure(state="normal")
        self._hide_progress()
        self.status_dot.configure(fg=COLORS["danger"])
        self.status_label.configure(text=f"刷新失败：{exc}")

    def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot
        self.refreshing = False
        self.refresh_button.configure(state="normal")
        self._hide_progress()
        task_running = snapshot.task.state.lower() == "running"
        self.status_dot.configure(fg=COLORS["blue"] if task_running else COLORS["success"])
        self.status_label.configure(
            text="任务正在运行，页面会自动刷新" if task_running else "状态已更新"
        )
        self.run_button.configure(state="disabled" if task_running else "normal")
        if task_running:
            self._show_progress()

        self.task_card.set(
            status_text(snapshot.task.state),
            f"下次：{format_time(snapshot.task.next_run_time)}",
            status_color(snapshot.task.state),
        )
        self.run_card.set(
            status_text(snapshot.latest_run.status),
            f"{format_time(snapshot.latest_run.finished_at)} · {format_duration(snapshot.latest_run.wall_seconds)}",
            status_color(snapshot.latest_run.status),
        )
        self.creator_card.set(
            f"{snapshot.total_creators} 位 / {snapshot.total_works} 条",
            f"待处理 {snapshot.pending_works} 条",
        )
        account_value = (
            f"{snapshot.account_profiles_detected}/{snapshot.account_profiles_total} 已检测"
            if snapshot.account_profiles_total
            else "未启用"
        )
        self.account_card.set(
            account_value,
            "本地登录文件（非在线校验）",
            COLORS["success"]
            if snapshot.account_profiles_total
            and snapshot.account_profiles_detected == snapshot.account_profiles_total
            else COLORS["warning"],
        )

        self.creator_tree.delete(*self.creator_tree.get_children())
        failures = 0
        for item in snapshot.creators:
            if item.status in {"failed", "partial_failure", "not_run"}:
                failures += 1
            self.creator_tree.insert(
                "",
                "end",
                values=(
                    item.name,
                    item.works_count,
                    item.pending_count,
                    format_time(item.works_updated_at),
                    status_text(item.status),
                    item.detail or "—",
                ),
                tags=(item.status,),
            )
        for state in ("success", "ready"):
            self.creator_tree.tag_configure(state, foreground=COLORS["success"])
        self.creator_tree.tag_configure("partial_failure", foreground=COLORS["warning"])
        self.creator_tree.tag_configure("failed", foreground=COLORS["danger"])
        self.creator_tree.tag_configure("not_run", foreground=COLORS["muted"])
        self.creator_summary.configure(
            text=f"{snapshot.total_creators - failures} 正常 · {failures} 异常"
        )

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", snapshot.log_tail)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.log_path_label.configure(
            text=snapshot.latest_log.name if snapshot.latest_log else "暂无日志"
        )
        result_text = (
            f"退出码 {snapshot.task.last_result}"
            if snapshot.task.last_result is not None
            else "无退出码"
        )
        self.footer.configure(
            text=f"最后刷新：{format_time(snapshot.refreshed_at)} · Windows 任务最近结果：{result_text}"
        )

    def run_now(self) -> None:
        if self.snapshot and self.snapshot.task.state.lower() == "running":
            messagebox.showinfo("任务正在运行", "DouyinCreatorMonitor 当前已经在运行。")
            return
        self.run_button.configure(state="disabled")
        self._show_progress()
        self.status_label.configure(text="正在请求 Windows 任务计划程序启动…")

        def worker() -> None:
            try:
                message = start_scheduled_task()
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._run_failed(exc))
            else:
                self.root.after(0, lambda: self._run_started(message))

        threading.Thread(target=worker, daemon=True).start()

    def _run_failed(self, exc: Exception) -> None:
        self._hide_progress()
        self.run_button.configure(state="normal")
        self.status_dot.configure(fg=COLORS["danger"])
        self.status_label.configure(text=f"启动失败：{exc}")
        messagebox.showerror("启动失败", str(exc))

    def _run_started(self, message: str) -> None:
        self.status_dot.configure(fg=COLORS["blue"])
        self.status_label.configure(text="启动请求已发送，等待任务进入运行状态…")
        self.root.after(1500, self.refresh)
        self.root.after(15_000, self.refresh)
        if message:
            self.footer.configure(text=message)

    def open_config(self) -> None:
        self._open_path(DEFAULT_CONFIG)

    def open_log(self) -> None:
        path = self.snapshot.latest_log if self.snapshot else PROJECT_DIR / "logs"
        self._open_path(path or PROJECT_DIR / "logs")

    @staticmethod
    def _open_path(path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("无法打开", f"{path}\n\n{exc}")


def main() -> int:
    root = tk.Tk()
    DashboardApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
