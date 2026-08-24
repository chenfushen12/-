from __future__ import annotations

import math
import threading
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd
from tkcalendar import DateEntry

from .config import ConfigStore
from .charting import ChartMode, ChartSelection, METRIC_LABELS, METRICS_BY_MODE, build_chart_figure, format_hover_text
from .dashboard import highest_alert_label, toggle_alert_filter
from .importers import preview_beijing, preview_sales, preview_template, preview_xingwang
from .service import InventoryTrackerService, OverlapError, OverwriteRequired, ValidationError


IMPORT_KIND_LABELS = {
    "template": "商品主模板",
    "sales": "销售数据",
    "beijing": "北京库存",
    "xingwang": "星望库存",
}

ISSUE_LEVEL_LABELS = {
    "blocking": "阻断",
    "warning": "警告",
    "info": "提示",
}


def _parse_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if hasattr(value, "get_date"):
        return value.get_date()
    return pd.Timestamp(str(value).strip()).date()


def _format_number(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, float) and math.isinf(value):
        return "∞"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def format_import_previews(previews: tuple[object, ...]) -> str:
    lines: list[str] = []
    for preview in previews:
        kind_label = IMPORT_KIND_LABELS.get(preview.kind, preview.kind)
        source_name = Path(preview.source_path).name
        lines.append(f"{kind_label}｜{source_name}：读取 {len(preview.frame)} 行")
        visible_issues = preview.report.issues[:8]
        for issue in visible_issues:
            level_label = ISSUE_LEVEL_LABELS.get(issue.level.value, issue.level.value)
            lines.append(f"  [{level_label}] {issue.message}")
        hidden_count = len(preview.report.issues) - len(visible_issues)
        if hidden_count:
            lines.append(f"  …另有 {hidden_count} 项问题，请在数据质量页查看")
    return "\n".join(lines)


def _configure_matplotlib_chinese_font() -> None:
    from matplotlib import font_manager, rcParams

    available = {font.name for font in font_manager.fontManager.ttflist}
    preferred = [name for name in ("Microsoft YaHei", "SimHei", "Microsoft JhengHei", "Noto Sans CJK SC") if name in available]
    rcParams["font.sans-serif"] = preferred or ["DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False


class InventoryApp:
    def __init__(self, root: tk.Tk, *, app_dir: str | Path | None = None):
        self.root = root
        self.root.title("库存跟踪监控")
        self.root.geometry("1420x860")
        self.app_dir = Path(app_dir) if app_dir else Path(__file__).resolve().parent.parent
        self.data_dir = self.app_dir / "data"
        self.config_store = ConfigStore(self.data_dir / "config.json")
        self.service = InventoryTrackerService(
            self.data_dir / "inventory.sqlite3",
            data_dir=self.data_dir,
            config=self.config_store.load(),
        )
        self.previews = None
        self._preview_snapshot_date: date | None = None
        self.last_report = None
        self.current_frame = pd.DataFrame()
        self._trend_product_key: tuple[str, str] | None = None
        self._pending_trend_after_id: str | None = None
        self.chart_selection = ChartSelection()
        self._build_style()
        self._build_tabs()
        self._refresh_dashboard()

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"), foreground="#16324f")
        style.configure("Danger.Treeview", foreground="#9b1c1c")

    def _confirm_action(self, title: str, message: str, confirm_text: str) -> bool:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        ttk.Label(dialog, text=message, justify="left", wraplength=620).pack(padx=20, pady=18)
        buttons = ttk.Frame(dialog)
        buttons.pack(pady=(0, 16))
        result = {"confirmed": False}

        def close(value: bool) -> None:
            result["confirmed"] = value
            dialog.destroy()

        ttk.Button(buttons, text="取消", command=lambda: close(False)).pack(side="left", padx=6)
        ttk.Button(buttons, text=confirm_text, command=lambda: close(True)).pack(side="left", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
        self.root.wait_window(dialog)
        return result["confirmed"]

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.dashboard_tab = ttk.Frame(self.notebook)
        self.import_tab = ttk.Frame(self.notebook)
        self.quality_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.dashboard_tab, text="仪表盘")
        self.notebook.add(self.import_tab, text="导入中心")
        self.history_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.history_tab, text="历史快照")
        self.notebook.add(self.quality_tab, text="数据质量")
        self.notebook.add(self.settings_tab, text="设置")
        self._build_dashboard()
        self._build_import()
        self._build_history()
        self._build_quality()
        self._build_settings()

    def _build_dashboard(self) -> None:
        header = ttk.Frame(self.dashboard_tab)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="库存跟踪仪表盘", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="快照日").pack(side="left", padx=(28, 4))
        self.dashboard_date = DateEntry(header, date_pattern="yyyy/mm/dd", locale="zh_CN", maxdate=date.today(), width=12)
        self.dashboard_date.set_date(date.today() - timedelta(days=1))
        self.dashboard_date.pack(side="left")
        ttk.Button(header, text="刷新", command=self._refresh_dashboard).pack(side="left", padx=6)
        ttk.Button(header, text="导出当前结果", command=self._export_current).pack(side="left", padx=6)
        self.reevaluate_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="按当前设置重新评估", variable=self.reevaluate_var, command=self._refresh_dashboard).pack(side="left", padx=12)

        filters = ttk.Frame(self.dashboard_tab)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="搜索").pack(side="left")
        self.search_text = tk.StringVar()
        ttk.Entry(filters, textvariable=self.search_text, width=24).pack(side="left", padx=4)
        ttk.Label(filters, text="预警").pack(side="left", padx=(16, 4))
        self.alert_filter = tk.StringVar(value="全部")
        ttk.Combobox(filters, textvariable=self.alert_filter, values=["全部", "无库存预警", "增长型缺货风险", "常规低库存", "滞销品预警", "数据质量异常"], state="readonly", width=16).pack(side="left")
        ttk.Label(filters, text="分类").pack(side="left", padx=(16, 4))
        self.category_filter = tk.StringVar(value="全部")
        self.category_combo = ttk.Combobox(filters, textvariable=self.category_filter, values=["全部"], state="readonly", width=16)
        self.category_combo.pack(side="left")
        self.quality_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(filters, text="只看数据质量", variable=self.quality_only, command=self._refresh_dashboard).pack(side="left", padx=10)
        ttk.Button(filters, text="应用筛选", command=self._refresh_dashboard).pack(side="left", padx=6)

        summary = ttk.Frame(self.dashboard_tab)
        summary.pack(fill="x", pady=(0, 8))
        self.summary_var = tk.StringVar(value="尚未加载快照")
        ttk.Label(summary, textvariable=self.summary_var).pack(side="left")
        self.alert_cards_frame = ttk.Frame(self.dashboard_tab)
        self.alert_cards_frame.pack(fill="x", pady=(0, 8))
        self.alert_card_buttons: dict[str, ttk.Button] = {}
        for label in ("全部预警", "无库存预警", "增长型缺货风险", "常规低库存", "滞销品预警", "数据质量异常"):
            button = ttk.Button(self.alert_cards_frame, text=f"{label}：0", command=lambda value=label: self._on_alert_card_clicked(value))
            button.pack(side="left", padx=(0, 8))
            self.alert_card_buttons[label] = button

        table_frame = ttk.Frame(self.dashboard_tab)
        table_frame.pack(fill="both", expand=True)
        columns = ["groupcode", "product_id", "product_name", "sales", "growth", "beijing_available", "xingwang_available", "in_transit", "stock_total", "sales30", "sales90", "moh30", "moh90", "inventory_status", "alert_labels"]
        self.dashboard_columns = columns
        self.dashboard_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)
        headings = {
            "groupcode": "GROUPCODE",
            "product_id": "货品编号",
            "product_name": "货品名称",
            "sales": "销量",
            "growth": "环比",
            "beijing_available": "北京可用库存",
            "xingwang_available": "星望可用库存",
            "in_transit": "在途库存",
            "stock_total": "库存总量",
            "sales30": "近30天销量",
            "sales90": "近90天销量",
            "moh30": "30天MOH",
            "moh90": "90天MOH",
            "inventory_status": "库存状态",
            "alert_labels": "预警标签",
        }
        widths = {"groupcode": 110, "product_id": 150, "product_name": 220, "alert_labels": 230}
        for column in columns:
            self.dashboard_tree.heading(column, text=headings[column])
            self.dashboard_tree.column(column, width=widths.get(column, 100), anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.dashboard_tree.yview)
        self.dashboard_tree.configure(yscrollcommand=scrollbar.set)
        self.dashboard_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.dashboard_tree.bind("<Button-1>", self._on_dashboard_click)
        self.dashboard_tree.bind("<Double-1>", self._on_dashboard_double_click)

        self.chart_frame = ttk.LabelFrame(self.dashboard_tab, text="商品趋势")
        self.chart_frame.pack(fill="x", pady=(8, 0))
        self.chart_window_var = tk.StringVar(value="近7天")
        self.chart_mode_controls = ttk.Frame(self.chart_frame)
        self.chart_mode_controls.pack(fill="x", padx=8, pady=(6, 0))
        self.chart_mode_var = tk.StringVar(value=self.chart_selection.mode.value)
        ttk.Label(self.chart_mode_controls, text="查看").pack(side="left")
        ttk.Radiobutton(self.chart_mode_controls, text="库存/销量", value=ChartMode.QUANTITY.value, variable=self.chart_mode_var, command=self._on_chart_mode_changed, style="Toolbutton").pack(side="left", padx=2)
        ttk.Radiobutton(self.chart_mode_controls, text="MOH", value=ChartMode.MOH.value, variable=self.chart_mode_var, command=self._on_chart_mode_changed, style="Toolbutton").pack(side="left", padx=2)
        self.chart_controls = ttk.Frame(self.chart_frame)
        self.chart_controls.pack(fill="x", padx=8, pady=(2, 2))
        self.chart_start_date = DateEntry(self.chart_controls, date_pattern="yyyy/mm/dd", locale="zh_CN", maxdate=date.today(), width=12)
        self.chart_end_date = DateEntry(self.chart_controls, date_pattern="yyyy/mm/dd", locale="zh_CN", maxdate=date.today(), width=12)
        self.chart_start_date.set_date(date.today() - timedelta(days=6))
        self.chart_end_date.set_date(date.today())
        ttk.Label(self.chart_controls, text="时间范围").pack(side="left")
        ttk.Combobox(self.chart_controls, textvariable=self.chart_window_var, values=("近7天", "近30天", "全部快照", "自定义"), state="readonly", width=10).pack(side="left", padx=5)
        ttk.Label(self.chart_controls, text="开始").pack(side="left")
        self.chart_start_date.pack(side="left", padx=3)
        ttk.Label(self.chart_controls, text="结束").pack(side="left")
        self.chart_end_date.pack(side="left", padx=3)
        ttk.Button(self.chart_controls, text="应用", command=self._apply_chart_window).pack(side="left", padx=6)
        self.chart_metric_controls = ttk.Frame(self.chart_frame)
        self.chart_metric_controls.pack(fill="x", padx=8, pady=(2, 6))
        self.chart_metric_vars: dict[str, tk.BooleanVar] = {}
        self.chart_metric_buttons: dict[str, ttk.Checkbutton] = {}
        self.chart_metric_label: ttk.Label | None = None
        self.chart_select_all_button: ttk.Button | None = None
        self.chart_reset_button: ttk.Button | None = None
        self.chart_metric_controls.bind("<Configure>", lambda _event: self._layout_chart_metric_controls())
        self._rebuild_chart_metric_controls()
        self.chart_plot_frame = ttk.Frame(self.chart_frame)
        self.chart_plot_frame.pack(fill="x", expand=True)
        self.chart_message = ttk.Label(self.chart_plot_frame, text="选择商品查看趋势")
        self.chart_message.pack(padx=8, pady=8)

    def _rebuild_chart_metric_controls(self) -> None:
        for child in self.chart_metric_controls.winfo_children():
            child.destroy()
        self.chart_metric_vars = {}
        self.chart_metric_buttons = {}
        self.chart_metric_label = ttk.Label(self.chart_metric_controls, text="指标")
        for metric in METRICS_BY_MODE[self.chart_selection.mode]:
            variable = tk.BooleanVar(value=metric in self.chart_selection.selected)
            self.chart_metric_vars[metric] = variable
            button = ttk.Checkbutton(
                self.chart_metric_controls,
                text=METRIC_LABELS[metric],
                variable=variable,
                command=lambda name=metric: self._on_chart_metric_changed(name),
            )
            self.chart_metric_buttons[metric] = button
        self.chart_select_all_button = ttk.Button(self.chart_metric_controls, text="全选", command=self._select_all_chart_metrics)
        self.chart_reset_button = ttk.Button(self.chart_metric_controls, text="恢复默认", command=self._reset_chart_metrics)
        self._layout_chart_metric_controls()

    def _layout_chart_metric_controls(self) -> None:
        if self.chart_metric_label is None or self.chart_select_all_button is None or self.chart_reset_button is None:
            return
        widgets = [self.chart_metric_label, *self.chart_metric_buttons.values(), self.chart_select_all_button, self.chart_reset_button]
        for widget in widgets:
            widget.grid_forget()
        available_width = max(self.chart_metric_controls.winfo_width(), 420)
        row = 0
        column = 0
        used_width = 0
        for widget in widgets:
            requested = widget.winfo_reqwidth() + 10
            if column and used_width + requested > available_width:
                row += 1
                column = 0
                used_width = 0
            widget.grid(row=row, column=column, sticky="w", padx=5, pady=2)
            column += 1
            used_width += requested
        self.chart_metric_controls.rowconfigure(row, weight=1)

    def _on_chart_mode_changed(self) -> None:
        self.chart_selection.set_mode(ChartMode(self.chart_mode_var.get()))
        self._rebuild_chart_metric_controls()
        self._render_current_trend()

    def _on_chart_metric_changed(self, metric: str) -> None:
        changed = self.chart_selection.set_selected(metric, self.chart_metric_vars[metric].get())
        if not changed:
            self.chart_metric_vars[metric].set(metric in self.chart_selection.selected)
        self._render_current_trend()

    def _select_all_chart_metrics(self) -> None:
        self.chart_selection.select_all()
        self._rebuild_chart_metric_controls()
        self._render_current_trend()

    def _reset_chart_metrics(self) -> None:
        self.chart_selection.reset_defaults()
        self._rebuild_chart_metric_controls()
        self._render_current_trend()

    def _update_chart_metric_labels(self, history: pd.DataFrame) -> None:
        for metric in self.chart_metric_vars:
            label = METRIC_LABELS[metric]
            series = history.get(metric)
            if series is None or not series.notna().any():
                label += "（暂无数据）"
            self.chart_metric_buttons[metric].configure(text=label)

    def _build_import(self) -> None:
        ttk.Label(self.import_tab, text="导入中心", style="Title.TLabel").pack(anchor="w", pady=(0, 12))
        form = ttk.Frame(self.import_tab)
        form.pack(fill="x")
        self.file_vars: dict[str, tk.StringVar] = {}
        fields = [("template", "商品主模板"), ("sales", "销售数据"), ("beijing", "北京库存"), ("xingwang", "星望库存")]
        for row, (key, label) in enumerate(fields):
            ttk.Label(form, text=label, width=14).grid(row=row, column=0, sticky="w", pady=5)
            variable = tk.StringVar()
            self.file_vars[key] = variable
            ttk.Entry(form, textvariable=variable, width=90).grid(row=row, column=1, sticky="ew", padx=4)
            ttk.Button(form, text="选择文件", command=lambda name=key: self._choose_file(name)).grid(row=row, column=2, padx=4)
        ttk.Label(form, text="库存快照日", width=14).grid(row=4, column=0, sticky="w", pady=5)
        self.snapshot_date = DateEntry(form, date_pattern="yyyy/mm/dd", locale="zh_CN", maxdate=date.today(), width=14)
        self.snapshot_date.set_date(date.today() - timedelta(days=1))
        self.snapshot_date.grid(row=4, column=1, sticky="w", padx=4)
        self.snapshot_date.bind("<<DateEntrySelected>>", self._invalidate_preview_for_date)
        self.snapshot_date.bind("<FocusOut>", self._invalidate_preview_for_date)
        form.columnconfigure(1, weight=1)
        options = ttk.Frame(self.import_tab)
        options.pack(fill="x", pady=10)
        self.replace_sales = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="销售日期重叠时按日期替换", variable=self.replace_sales).pack(side="left")
        self.preview_button = ttk.Button(options, text="预检查并预览", command=self._preview_import)
        self.preview_button.pack(side="left", padx=8)
        self.commit_button = ttk.Button(options, text="确认导入并计算", command=self._commit_import)
        self.commit_button.pack(side="left")
        self.import_progress = ttk.Progressbar(options, orient="horizontal", mode="determinate", maximum=100, length=260)
        self.import_progress.pack(side="left", padx=12)
        self.progress_var = tk.StringVar(value="")
        ttk.Label(options, textvariable=self.progress_var, width=24).pack(side="left")
        self.import_status = tk.StringVar(value="请选择四个 Excel 文件并执行预检查")
        ttk.Label(self.import_tab, textvariable=self.import_status, foreground="#375a7f").pack(anchor="w", pady=8)
        self.preview_text = tk.Text(self.import_tab, height=22, wrap="word")
        self.preview_text.pack(fill="both", expand=True)

    def _build_history(self) -> None:
        ttk.Label(self.history_tab, text="历史快照", style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        controls = ttk.Frame(self.history_tab)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="定位日期").pack(side="left")
        self.history_date = DateEntry(controls, date_pattern="yyyy/mm/dd", locale="zh_CN", maxdate=date.today(), width=14)
        self.history_date.set_date(date.today() - timedelta(days=1))
        self.history_date.pack(side="left", padx=5)
        ttk.Button(controls, text="定位", command=self._select_history_date).pack(side="left")
        ttk.Button(controls, text="刷新", command=self._refresh_history).pack(side="left", padx=5)
        self.partial_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="只看 partial", variable=self.partial_only, command=self._refresh_history).pack(side="left", padx=10)
        self.delete_history_button = ttk.Button(controls, text="删除所选日期快照", command=self._delete_selected_snapshots, state="disabled")
        self.delete_history_button.pack(side="left", padx=10)
        self.history_progress = ttk.Progressbar(controls, orient="horizontal", mode="indeterminate", length=180)
        self.history_progress.pack(side="left", padx=5)
        self.history_status = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.history_status).pack(side="left")

        table_frame = ttk.Frame(self.history_tab)
        table_frame.pack(fill="both", expand=True)
        columns = ("snapshot_date", "status", "product_count", "has_beijing", "has_xingwang")
        self.history_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        for column, heading, width in (
            ("snapshot_date", "快照日期", 150),
            ("status", "状态", 100),
            ("product_count", "商品数", 100),
            ("has_beijing", "北京库存", 120),
            ("has_xingwang", "星望库存", 120),
        ):
            self.history_tree.heading(column, text=heading)
            self.history_tree.column(column, width=width, anchor="w")
        history_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=history_scroll.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        history_scroll.pack(side="right", fill="y")
        self.history_tree.bind("<<TreeviewSelect>>", self._update_history_delete_state)

        ttk.Label(self.history_tab, text="删除记录", style="Title.TLabel").pack(anchor="w", pady=(10, 5))
        self.deletion_log_text = tk.Text(self.history_tab, height=7, wrap="word")
        self.deletion_log_text.pack(fill="x")
        self._refresh_history()

    def _refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        snapshots = self.service.list_snapshots()
        if self.partial_only.get():
            snapshots = [item for item in snapshots if item["status"] == "partial"]
        for item in snapshots:
            self.history_tree.insert(
                "",
                "end",
                values=(
                    item["snapshot_date"].strftime("%Y/%m/%d"),
                    item["status"],
                    item["product_count"],
                    "已存在" if item["has_beijing"] else "缺失",
                    "已存在" if item["has_xingwang"] else "缺失",
                ),
            )
        logs = self.service.deletion_logs()
        self.deletion_log_text.delete("1.0", "end")
        if logs:
            for log in logs:
                self.deletion_log_text.insert(
                    "end",
                    f"{log['snapshot_date']} | {log['deleted_at']} | {log['previous_status']} | 商品 {log['product_count']}\n",
                )
        else:
            self.deletion_log_text.insert("end", "暂无删除记录")
        self._update_history_delete_state()

    def _update_history_delete_state(self, _event=None) -> None:
        if hasattr(self, "delete_history_button"):
            state = "normal" if self.history_tree.selection() else "disabled"
            self.delete_history_button.configure(state=state)

    def _select_history_date(self) -> None:
        target = _parse_date(self.history_date)
        for item in self.history_tree.get_children():
            values = self.history_tree.item(item, "values")
            if values and values[0] == target.strftime("%Y/%m/%d"):
                self.history_tree.selection_set(item)
                self.history_tree.focus(item)
                self.history_tree.see(item)
                return
        messagebox.showinfo("没有快照", "该日期没有库存快照")

    def _delete_selected_snapshots(self) -> None:
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showwarning("未选择日期", "请先选择一个或多个历史快照日期")
            return
        dates = [_parse_date(self.history_tree.item(item, "values")[0]) for item in selected]
        summaries = [self.service.snapshot_summary(value) for value in dates]
        details = "\n".join(
            f"{summary['snapshot_date'].strftime('%Y/%m/%d')} | {summary['status']} | 商品 {summary['product_count']} | 北京 {'有' if summary['has_beijing'] else '无'} | 星望 {'有' if summary['has_xingwang'] else '无'}"
            for summary in summaries
            if summary is not None
        )
        title = "删除该日期快照" if len(dates) == 1 else "删除所选日期快照"
        confirm_text = "删除该日期快照" if len(dates) == 1 else "删除所选日期快照"
        if not self._confirm_action(title, f"以下快照将被永久删除：\n\n{details}\n\n销售数据、原始 Excel 和导入日志不会删除。", confirm_text):
            return
        self.history_progress.start(12)
        self.history_status.set("正在删除…")

        def worker() -> None:
            try:
                result = self.service.delete_snapshots(dates, confirmed=True)
            except Exception as error:
                self.root.after(0, lambda error=error: (self.history_progress.stop(), self.history_status.set("删除失败"), messagebox.showerror("删除失败", str(error))))
            else:
                self.root.after(0, lambda result=result: (self.history_progress.stop(), self.history_status.set(f"已删除 {len(result.deleted_dates)} 个快照"), self._refresh_history(), self._refresh_dashboard()))

        threading.Thread(target=worker, name="snapshot-delete", daemon=True).start()

    def _build_quality(self) -> None:
        ttk.Label(self.quality_tab, text="数据质量报告", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        self.quality_locator_var = tk.StringVar(value="当前定位：无")
        ttk.Label(self.quality_tab, textvariable=self.quality_locator_var, foreground="#375a7f").pack(anchor="w", pady=(0, 5))
        quality_actions = ttk.Frame(self.quality_tab)
        quality_actions.pack(fill="x", pady=(0, 5))
        ttk.Button(quality_actions, text="显示全部质量问题", command=self._show_all_quality).pack(side="left")
        ttk.Label(self.quality_tab, text="商品级异常原因").pack(anchor="w")
        detail_frame = ttk.Frame(self.quality_tab)
        detail_frame.pack(fill="x", pady=(0, 8))
        detail_columns = ("level", "product", "date", "reason", "source")
        self.quality_detail_tree = ttk.Treeview(detail_frame, columns=detail_columns, show="headings", height=6)
        for column, heading, width in (("level", "级别", 80), ("product", "商品", 230), ("date", "日期", 110), ("reason", "异常原因", 260), ("source", "来源", 120)):
            self.quality_detail_tree.heading(column, text=heading)
            self.quality_detail_tree.column(column, width=width, anchor="w")
        self.quality_detail_tree.pack(fill="x", expand=True)
        ttk.Label(self.quality_tab, text="导入级异常（无法可靠归属到单个商品）").pack(anchor="w")
        import_frame = ttk.Frame(self.quality_tab)
        import_frame.pack(fill="both", expand=True)
        import_columns = ("level", "kind", "date", "row", "field", "message", "source_path")
        self.quality_import_tree = ttk.Treeview(import_frame, columns=import_columns, show="headings", height=12)
        for column, heading, width in (("level", "级别", 80), ("kind", "类型", 90), ("date", "日期", 110), ("row", "行号", 70), ("field", "字段", 110), ("message", "原因", 420), ("source_path", "来源文件", 260)):
            self.quality_import_tree.heading(column, text=heading)
            self.quality_import_tree.column(column, width=width, anchor="w")
        quality_scroll = ttk.Scrollbar(import_frame, orient="vertical", command=self.quality_import_tree.yview)
        self.quality_import_tree.configure(yscrollcommand=quality_scroll.set)
        self.quality_import_tree.pack(side="left", fill="both", expand=True)
        quality_scroll.pack(side="right", fill="y")
        self.quality_text = tk.Text(self.quality_tab, height=4, wrap="word")
        self.quality_text.pack(fill="x", pady=(6, 0))

    def _build_settings(self) -> None:
        ttk.Label(self.settings_tab, text="设置", style="Title.TLabel").pack(anchor="w", pady=(0, 12))
        form = ttk.Frame(self.settings_tab)
        form.pack(anchor="w")
        config = self.service.config
        self.growth_var = tk.StringVar(value=f"{config.growth_threshold:.1%}")
        self.moh30_var = tk.StringVar(value=str(config.moh30_threshold))
        self.moh90_var = tk.StringVar(value=str(config.moh90_threshold))
        self.codes_var = tk.StringVar(value=", ".join(config.beijing_codes))
        for row, (label, variable) in enumerate((("环比阈值", self.growth_var), ("30天MOH阈值", self.moh30_var), ("90天MOH阈值", self.moh90_var), ("北京库房代码", self.codes_var))):
            ttk.Label(form, text=label, width=18).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(form, textvariable=variable, width=42).grid(row=row, column=1, sticky="w", pady=6)
        ttk.Button(form, text="保存设置", command=self._save_settings).grid(row=4, column=1, sticky="w", pady=12)

    def _choose_file(self, key: str) -> None:
        path = filedialog.askopenfilename(filetypes=[("Excel 文件", "*.xlsx *.xls"), ("所有文件", "*.*")])
        if path:
            self.file_vars[key].set(path)

    def _invalidate_preview_for_date(self, _event=None) -> None:
        if self._preview_snapshot_date is None:
            return
        try:
            current_date = _parse_date(self.snapshot_date)
        except Exception:
            current_date = None
        if current_date != self._preview_snapshot_date:
            self.previews = None
            self._preview_snapshot_date = None
            self.import_status.set("库存日期已改变，请重新预检查")

    def _set_import_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.preview_button.configure(state=state)
        self.commit_button.configure(state=state)
        if not busy:
            self.import_progress.stop()
            self.import_progress.configure(mode="determinate", value=0)
            self.progress_var.set("")

    def _start_background_task(self, work, on_success, *, indeterminate: bool = False) -> None:
        self._set_import_busy(True)
        self.import_progress.configure(mode="indeterminate" if indeterminate else "determinate")
        if indeterminate:
            self.import_progress.start(12)

        def update_progress(value: int, message: str) -> None:
            self.root.after(0, lambda: (self.import_progress.configure(value=value), self.progress_var.set(message)))

        def worker() -> None:
            try:
                result = work(update_progress)
            except Exception as error:
                self.root.after(0, lambda error=error: (self._set_import_busy(False), self._show_background_error(error)))
            else:
                self.root.after(0, lambda: (self._set_import_busy(False), on_success(result)))

        threading.Thread(target=worker, name="inventory-import", daemon=True).start()

    @staticmethod
    def _show_background_error(error: Exception) -> None:
        if isinstance(error, OverlapError):
            messagebox.showwarning("销售日期重叠", str(error) + "；勾选替换选项后重试")
        elif isinstance(error, OverwriteRequired):
            messagebox.showwarning("快照已存在", "目标日期已有快照，请重新点击确认导入并确认覆盖")
        else:
            messagebox.showerror("导入失败", str(error))

    def _preview_import(self) -> None:
        try:
            snapshot_date = _parse_date(self.snapshot_date.get())
            paths = {key: variable.get().strip() for key, variable in self.file_vars.items()}
            if not all(paths.values()):
                raise ValueError("四个 Excel 文件都必须选择")
            def work(progress):
                progress(5, "读取商品主模板…")
                template = preview_template(paths["template"])
                progress(30, "读取销售数据…")
                sales = preview_sales(paths["sales"])
                progress(55, "读取北京库存…")
                beijing = preview_beijing(paths["beijing"], codes=self.service.config.beijing_codes)
                progress(80, "读取星望库存…")
                xingwang = preview_xingwang(paths["xingwang"])
                progress(100, "预检查完成")
                return snapshot_date, (template, sales, beijing, xingwang)

            self.import_status.set("后台读取中，窗口仍可操作…")
            self._start_background_task(work, self._apply_preview_result)
        except Exception as error:
            messagebox.showerror("预检查失败", str(error))

    def _apply_preview_result(self, result) -> None:
        snapshot_date, previews = result
        if _parse_date(self.snapshot_date) != snapshot_date:
            self.import_status.set("预检查完成时库存日期已改变，请重新预检查")
            self.previews = None
            self._preview_snapshot_date = None
            return
        self.previews = previews
        self._preview_snapshot_date = snapshot_date
        self.last_report = report = self._combine_reports(previews)
        self._show_report(report)
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("end", format_import_previews(previews))
        self.import_status.set(f"预检查完成：{len(report.blocking)} 个阻断，{len(report.warnings)} 个警告，{len(report.infos)} 个提示；快照日 {snapshot_date}")

    @staticmethod
    def _combine_reports(previews) -> object:
        from .models import QualityReport

        report = QualityReport()
        for preview in previews:
            report.extend(preview.report)
        return report

    def _commit_import(self) -> None:
        if not self.previews:
            messagebox.showwarning("尚未预检查", "请先执行预检查并预览")
            return
        if self._preview_snapshot_date != _parse_date(self.snapshot_date):
            self.previews = None
            self._preview_snapshot_date = None
            messagebox.showwarning("预览已失效", "库存日期已改变，请重新执行预检查")
            return
        report = self.last_report
        if report.blocking:
            messagebox.showerror("无法提交", "存在阻断问题，请先修复输入文件")
            return
        try:
            snapshot_date = _parse_date(self.snapshot_date.get())
            sales_mode = "replace" if self.replace_sales.get() else "append"
            overwrite = False
            existing = self.service.snapshot_summary(snapshot_date)
            if existing is not None:
                summary = (
                    f"目标日期：{snapshot_date.strftime('%Y/%m/%d')}\n"
                    f"当前状态：{existing['status']}\n"
                    f"商品数：{existing['product_count']}\n"
                    f"北京库存：{'已存在' if existing['has_beijing'] else '缺失'}\n"
                    f"星望库存：{'已存在' if existing['has_xingwang'] else '缺失'}\n\n"
                    "覆盖后将替换当前日期结果和两仓库存快照；销售数据只替换新文件中实际重叠的日期。"
                )
                if not self._confirm_action("覆盖已有快照", summary, "覆盖已有快照"):
                    return
                overwrite = True
            elif not self._confirm_action("确认导入", "确认写入四类数据并生成库存快照吗？", "确认导入并计算"):
                return

            def work(progress):
                progress(10, "校验并保存原始文件…")
                result = self.service.commit_batch(
                    *self.previews,
                    snapshot_date=snapshot_date,
                    confirmed=True,
                    sales_mode=sales_mode,
                    overwrite=overwrite,
                )
                progress(100, "快照计算完成")
                return result

            self.import_status.set("后台提交中，窗口仍可操作…")
            self._start_background_task(work, self._apply_commit_result, indeterminate=True)
        except OverlapError as error:
            messagebox.showwarning("销售日期重叠", str(error) + "；勾选替换选项后重试")
        except Exception as error:
            messagebox.showerror("提交失败", str(error))

    def _apply_commit_result(self, result) -> None:
        reused_count = sum(issue.code == "reused_file" for issue in result.report.infos)
        reused_text = f"，复用 {reused_count} 个已导入文件" if reused_count else ""
        self.import_status.set(f"导入成功：{result.status}，已生成 {len(result.frame)} 个商品结果{reused_text}")
        self._show_report(result.report)
        self._refresh_dashboard()
        self.notebook.select(self.dashboard_tab)

    def _show_report(self, report) -> None:
        self.quality_text.delete("1.0", "end")
        if not report.issues:
            self.quality_text.insert("end", "没有发现数据质量问题。")
            return
        for issue in report.issues:
            self.quality_text.insert("end", f"[{issue.level.value}] {issue.code}: {issue.message}\n")

    def _show_quality_for_product(self, snapshot_date: date, groupcode: str, product_id: str, product_name: str) -> None:
        details = self.service.quality_details(snapshot_date, groupcode=groupcode, product_id=product_id)
        self.quality_locator_var.set(
            f"当前定位：{snapshot_date.strftime('%Y/%m/%d')} / {groupcode} / {product_id} / {product_name}"
        )
        self._render_quality_details(details, highlight_product=(groupcode, product_id))
        self.quality_text.delete("1.0", "end")
        self.quality_text.insert("end", "该问题属于导入批次，无法归属到当前商品；请查看全部导入异常。")
        if not details["import_issues"]:
            self.quality_text.insert("end", "\n当前快照没有独立的导入级异常记录。")

    def _show_all_quality(self) -> None:
        try:
            snapshot_date = _parse_date(self.dashboard_date)
        except Exception:
            snapshot_date = date.today() - timedelta(days=1)
        details = self.service.quality_details(snapshot_date)
        filter_text = self._dashboard_filter_text()
        self.quality_locator_var.set(f"当前定位：{snapshot_date.strftime('%Y/%m/%d')} / 全部商品{filter_text}")
        allowed = set(zip(self.current_frame.get("groupcode", []), self.current_frame.get("product_id", [])))
        details["product_issues"] = [
            issue for issue in details["product_issues"]
            if (issue["groupcode"], issue["product_id"]) in allowed
        ]
        self._render_quality_details(details)

    def _dashboard_filter_text(self) -> str:
        parts = []
        if self.search_text.get().strip():
            parts.append(f"搜索={self.search_text.get().strip()}")
        if self.category_filter.get() != "全部":
            parts.append(f"分类={self.category_filter.get()}")
        if self.alert_filter.get() != "全部":
            parts.append(f"预警={self.alert_filter.get()}")
        if self.quality_only.get():
            parts.append("只看数据质量")
        return f"（筛选：{'；'.join(parts)}）" if parts else ""

    def _render_quality_details(self, details: dict[str, object], *, highlight_product: tuple[str, str] | None = None) -> None:
        for item in self.quality_detail_tree.get_children():
            self.quality_detail_tree.delete(item)
        for item in self.quality_import_tree.get_children():
            self.quality_import_tree.delete(item)
        product_items = details["product_issues"]
        selected_iid = None
        for index, issue in enumerate(product_items):
            iid = f"quality-{index}"
            is_located = highlight_product and (issue["groupcode"], issue["product_id"]) == highlight_product
            self.quality_detail_tree.insert(
                "", "end", iid=iid,
                values=("商品级", f"{issue['groupcode']} / {issue['product_id']} / {issue['product_name'] or '-'}", issue["snapshot_date"].strftime("%Y/%m/%d"), issue["reason"], issue["source"]),
                tags=("located",) if is_located else (),
            )
            if is_located:
                selected_iid = iid
        self.quality_detail_tree.tag_configure("located", background="#fff2a8")
        if selected_iid:
            self.quality_detail_tree.selection_set(selected_iid)
            self.quality_detail_tree.see(selected_iid)
        import_items = details["import_issues"]
        for index, issue in enumerate(import_items):
            self.quality_import_tree.insert(
                "", "end", iid=f"import-{index}",
                values=(issue.get("level"), issue.get("kind"), issue.get("business_date") or "-", issue.get("row") or "-", issue.get("field") or "-", issue.get("message"), issue.get("source_path")),
            )
        self.quality_text.delete("1.0", "end")
        if not product_items and not import_items:
            self.quality_text.insert("end", "当前定位没有数据质量异常。")
        elif not import_items:
            self.quality_text.insert("end", "当前快照没有可关联的导入级质量问题；商品级异常已在上方列出。")

    def _refresh_dashboard(self) -> None:
        try:
            snapshot_date = _parse_date(self.dashboard_date.get())
            frame = self.service.get_snapshot(snapshot_date, reevaluate=self.reevaluate_var.get())
            if frame.empty:
                self.summary_var.set("当前日期没有已计算快照")
                self.current_frame = frame
                self._update_alert_cards(frame)
                self._fill_dashboard(frame)
                return
            search = self.search_text.get().strip().lower()
            if search:
                mask = frame.apply(lambda row: search in " ".join(str(value).lower() for value in row.tolist()), axis=1)
                frame = frame.loc[mask]
            selected_alert = self.alert_filter.get()
            if selected_alert != "全部":
                frame = frame.loc[frame["alert_labels"].map(lambda labels: selected_alert in labels)]
            categories = sorted(value for value in frame.get("category", pd.Series(dtype=object)).dropna().unique())
            self.category_combo["values"] = ["全部", *categories]
            selected_category = self.category_filter.get()
            if selected_category != "全部" and "category" in frame:
                frame = frame.loc[frame["category"] == selected_category]
            if self.quality_only.get():
                frame = frame.loc[frame["quality_labels"].map(bool)]
            severity_order = {"无库存预警": 0, "增长型缺货风险": 1, "常规低库存": 2, "滞销品预警": 3, "数据质量异常": 4}
            frame = frame.assign(
                _severity=frame["alert_labels"].map(lambda labels: min((severity_order.get(label, 99) for label in labels), default=99)),
                _low_moh=frame.apply(lambda row: min((value for value in (row.get("moh30"), row.get("moh90")) if value is not None and not pd.isna(value)), default=float("inf")), axis=1),
                _growth_sort=frame["growth"].fillna(float("-inf")),
            ).sort_values(["_severity", "_low_moh", "_growth_sort"], ascending=[True, True, False]).drop(columns=["_severity", "_low_moh", "_growth_sort"])
            self.current_frame = frame
            self.summary_var.set(f"快照日 {snapshot_date}：{len(frame)} 个商品，{sum(bool(labels) for labels in frame['alert_labels'])} 个含预警标签")
            self._update_alert_cards(frame)
            self._fill_dashboard(frame)
        except Exception as error:
            self.summary_var.set(f"加载失败：{error}")

    def _fill_dashboard(self, frame: pd.DataFrame) -> None:
        for item in self.dashboard_tree.get_children():
            self.dashboard_tree.delete(item)
        if frame.empty:
            return
        for _, row in frame.iterrows():
            values = []
            for column in self.dashboard_columns:
                value = row.get(column)
                if column == "growth" and value is not None and not pd.isna(value):
                    value = f"{float(value):.1%}"
                elif column == "growth":
                    reasons = {"base_zero": "基数为0", "not_positive": "销量非正", "history_missing": "历史缺失"}
                    value = f"—（{reasons.get(row.get('growth_status'), '不可计算')}）"
                elif column == "alert_labels":
                    value = "、".join(value or [])
                else:
                    value = _format_number(value)
                values.append(value)
            tag = "risk" if row.get("alert_labels") else ""
            iid = f"{row.get('groupcode')}::{row.get('product_id')}"
            self.dashboard_tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        self.dashboard_tree.tag_configure("risk", foreground="#9b1c1c")

    def _update_alert_cards(self, frame: pd.DataFrame) -> None:
        counts = {label: 0 for label in self.alert_card_buttons}
        counts["全部预警"] = len(frame)
        if not frame.empty and "alert_labels" in frame:
            for labels in frame["alert_labels"]:
                for label in labels or []:
                    if label in counts:
                        counts[label] += 1
        for label, button in self.alert_card_buttons.items():
            button.configure(text=f"{label}：{counts[label]}")

    def _on_alert_card_clicked(self, label: str) -> None:
        if label == "全部预警":
            self.alert_filter.set("全部")
            self._refresh_dashboard()
            return
        if label == "数据质量异常":
            self._show_all_quality()
            self.notebook.select(self.quality_tab)
            return
        self.alert_filter.set(toggle_alert_filter(self.alert_filter.get(), label))
        self._refresh_dashboard()

    def _on_dashboard_click(self, event) -> str | None:
        row_id = self.dashboard_tree.identify_row(event.y)
        column_id = self.dashboard_tree.identify_column(event.x)
        if not row_id:
            return None
        row = self.dashboard_tree.item(row_id, "values")
        if not row:
            return None
        if column_id == f"#{self.dashboard_columns.index('alert_labels') + 1}":
            labels = [label.strip() for label in str(row[-1]).split("、") if label.strip()]
            if "数据质量异常" in labels:
                self._cancel_pending_trend()
                self._show_quality_for_product(
                    _parse_date(self.dashboard_date),
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                )
                self.notebook.select(self.quality_tab)
                return "break"
            selected_label = highest_alert_label(labels)
            if selected_label:
                self._cancel_pending_trend()
                self.alert_filter.set(toggle_alert_filter(self.alert_filter.get(), selected_label))
                self._refresh_dashboard()
                return "break"
        self._schedule_trend_selection(row_id)
        return "break"

    def _schedule_trend_selection(self, row_id: str) -> None:
        self._cancel_pending_trend()
        self._pending_trend_after_id = self.root.after(220, lambda: self._select_trend_row(row_id))

    def _cancel_pending_trend(self) -> None:
        if self._pending_trend_after_id is not None:
            try:
                self.root.after_cancel(self._pending_trend_after_id)
            except tk.TclError:
                pass
            self._pending_trend_after_id = None

    def _select_trend_row(self, row_id: str) -> None:
        self._pending_trend_after_id = None
        if not self.dashboard_tree.exists(row_id):
            return
        self.dashboard_tree.selection_set(row_id)
        self.dashboard_tree.focus(row_id)
        self._trend_product_key = tuple(row_id.split("::", 1))
        self._show_trend_for_product(*self._trend_product_key)

    def _on_dashboard_double_click(self, event) -> str:
        row_id = self.dashboard_tree.identify_row(event.y)
        column_id = self.dashboard_tree.identify_column(event.x)
        self._cancel_pending_trend()
        if not row_id:
            return "break"
        row = self.dashboard_tree.item(row_id, "values")
        if not row:
            return "break"
        if column_id == f"#{self.dashboard_columns.index('alert_labels') + 1}":
            labels = [label.strip() for label in str(row[-1]).split("、") if label.strip()]
            if "数据质量异常" in labels:
                self._show_quality_for_product(_parse_date(self.dashboard_date), str(row[0]), str(row[1]), str(row[2]))
                self.notebook.select(self.quality_tab)
            elif (selected_label := highest_alert_label(labels)):
                self.alert_filter.set(toggle_alert_filter(self.alert_filter.get(), selected_label))
                self._refresh_dashboard()
            return "break"
        product_key = tuple(row_id.split("::", 1))
        if self._trend_product_key is None or self._trend_product_key == product_key:
            self._clear_trend()
        else:
            self.dashboard_tree.selection_set(row_id)
            self.dashboard_tree.focus(row_id)
            self._trend_product_key = product_key
            self._show_trend_for_product(*product_key)
        return "break"

    def _clear_trend(self) -> None:
        self._trend_product_key = None
        self.dashboard_tree.selection_remove(self.dashboard_tree.selection())
        if self.chart_frame.winfo_ismapped():
            self.chart_frame.pack_forget()

    def _show_trend_for_product(self, groupcode: str, product_id: str) -> None:
        if not self.chart_frame.winfo_ismapped():
            self.chart_frame.pack(fill="x", pady=(8, 0))
        self._trend_product_key = (groupcode, product_id)
        self._trend_history = self.service.history_for_product(groupcode, product_id)
        self._render_current_trend()

    def _apply_chart_window(self) -> None:
        try:
            if self.chart_window_var.get() == "自定义":
                start_date = self.chart_start_date.get_date()
                end_date = self.chart_end_date.get_date()
                if start_date > end_date:
                    raise ValueError("开始日期不能晚于结束日期")
            self._render_current_trend()
        except Exception as error:
            messagebox.showerror("趋势范围无效", str(error))

    def _render_current_trend(self) -> None:
        if not hasattr(self, "_trend_history"):
            return
        history = self._trend_history
        groupcode, product_id = self._trend_product_key or ("", "")
        from .trends import filter_history_window

        try:
            filtered = filter_history_window(
                history,
                self.chart_window_var.get(),
                start_date=self.chart_start_date.get_date(),
                end_date=self.chart_end_date.get_date(),
            )
        except ValueError as error:
            for child in self.chart_plot_frame.winfo_children():
                child.destroy()
            ttk.Label(self.chart_plot_frame, text=str(error)).pack(padx=8, pady=8)
            return
        for child in self.chart_plot_frame.winfo_children():
            child.destroy()
        self._update_chart_metric_labels(filtered)
        if filtered.empty:
            self._draw_product_trend(filtered, groupcode, product_id, "当前条件下暂无趋势数据")
            return
        message = "仅有 1 个数据点，无法形成趋势" if len(filtered) == 1 else None
        self._draw_product_trend(filtered, groupcode, product_id, message)

    def _draw_product_trend(self, history: pd.DataFrame, groupcode: str, product_id: str, message: str | None = None) -> None:
        try:
            _configure_matplotlib_chinese_font()
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            import matplotlib.dates as mdates
            figure = build_chart_figure(
                history,
                self.chart_selection.selected,
                self.chart_selection.mode,
                title=("库存与销量趋势" if self.chart_selection.mode is ChartMode.QUANTITY else "MOH趋势") + f" — {groupcode} / {product_id}",
                moh30_threshold=self.service.config.moh30_threshold,
                moh90_threshold=self.service.config.moh90_threshold,
                empty_message=message,
            )
            canvas = FigureCanvasTkAgg(figure, master=self.chart_plot_frame)
            axis = figure.axes[0]
            hover = axis.annotate(
                "",
                xy=(0, 0),
                xytext=(12, 12),
                textcoords="offset points",
                bbox={"boxstyle": "round", "fc": "white", "ec": "#9ca3af", "alpha": 0.95},
                arrowprops={"arrowstyle": "->", "color": "#6b7280"},
            )
            hover.set_visible(False)
            dates = pd.to_datetime(history.get("snapshot_date", pd.Series(dtype="datetime64[ns]")))
            date_numbers = mdates.date2num(dates.dt.to_pydatetime()) if not dates.empty else []

            def on_motion(event) -> None:
                if event.inaxes is not axis or event.xdata is None or not len(date_numbers):
                    if hover.get_visible():
                        hover.set_visible(False)
                        canvas.draw_idle()
                    return
                nearest = min(range(len(date_numbers)), key=lambda index: abs(date_numbers[index] - event.xdata))
                if abs(date_numbers[nearest] - event.xdata) > max(2.0, (max(date_numbers) - min(date_numbers)) * 0.08):
                    hover.set_visible(False)
                    canvas.draw_idle()
                    return
                hover.xy = (dates.iloc[nearest], axis.get_ylim()[1] * 0.78)
                hover.set_text(format_hover_text(history, self.chart_selection.selected, nearest))
                hover.set_visible(True)
                canvas.draw_idle()

            canvas.mpl_connect("motion_notify_event", on_motion)
            canvas.draw()
            canvas.get_tk_widget().pack(in_=self.chart_plot_frame, fill="x", expand=True)
        except Exception as error:
            ttk.Label(self.chart_frame, text=f"图表加载失败：{error}").pack(padx=8, pady=8)

    def _save_settings(self) -> None:
        try:
            from .models import TrackerConfig

            codes = tuple(code.strip() for code in self.codes_var.get().split(",") if code.strip())
            if not codes:
                raise ValueError("至少需要一个北京库房代码")
            growth_raw = self.growth_var.get().strip()
            growth_value = float(growth_raw.replace("%", ""))
            if "%" in growth_raw or growth_value > 1:
                growth_value /= 100
            config = TrackerConfig(growth_value, float(self.moh30_var.get()), float(self.moh90_var.get()), codes)
            self.config_store.save(config)
            self.service.config = config
            self._refresh_dashboard()
            messagebox.showinfo("设置已保存", "新设置将用于后续导入和计算")
        except Exception as error:
            messagebox.showerror("设置保存失败", str(error))

    def _export_current(self) -> None:
        try:
            snapshot_date = _parse_date(self.dashboard_date.get())
            if self.service.get_snapshot(snapshot_date).empty:
                messagebox.showwarning("没有结果", "当前日期没有可导出的快照")
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".xlsx",
                initialfile=f"库存追踪_{snapshot_date.isoformat()}.xlsx",
                filetypes=[("Excel 文件", "*.xlsx")],
            )
            if path:
                self.service.export_snapshot(snapshot_date, path)
                messagebox.showinfo("导出完成", f"结果已导出到\n{path}")
        except Exception as error:
            messagebox.showerror("导出失败", str(error))


def launch() -> None:
    root = tk.Tk()
    app = InventoryApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.service.close(), root.destroy()))
    root.mainloop()
