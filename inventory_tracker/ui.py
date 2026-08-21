from __future__ import annotations

import math
import threading
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd

from .config import ConfigStore
from .importers import preview_beijing, preview_sales, preview_template, preview_xingwang
from .service import InventoryTrackerService, OverlapError, ValidationError


def _parse_date(value: str) -> date:
    return pd.Timestamp(value.strip()).date()


def _format_number(value: object) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, float) and math.isinf(value):
        return "∞"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


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
        self.last_report = None
        self.current_frame = pd.DataFrame()
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

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.dashboard_tab = ttk.Frame(self.notebook)
        self.import_tab = ttk.Frame(self.notebook)
        self.quality_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.dashboard_tab, text="仪表盘")
        self.notebook.add(self.import_tab, text="导入中心")
        self.notebook.add(self.quality_tab, text="数据质量")
        self.notebook.add(self.settings_tab, text="设置")
        self._build_dashboard()
        self._build_import()
        self._build_quality()
        self._build_settings()

    def _build_dashboard(self) -> None:
        header = ttk.Frame(self.dashboard_tab)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="库存跟踪仪表盘", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="快照日").pack(side="left", padx=(28, 4))
        self.dashboard_date = tk.StringVar(value=(date.today() - timedelta(days=1)).isoformat())
        ttk.Entry(header, textvariable=self.dashboard_date, width=14).pack(side="left")
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

        table_frame = ttk.Frame(self.dashboard_tab)
        table_frame.pack(fill="both", expand=True)
        columns = ["groupcode", "product_id", "product_name", "sales", "growth", "stock_total", "sales30", "sales90", "moh30", "moh90", "inventory_status", "alert_labels"]
        self.dashboard_columns = columns
        self.dashboard_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)
        headings = {
            "groupcode": "GROUPCODE",
            "product_id": "货品编号",
            "product_name": "货品名称",
            "sales": "销量",
            "growth": "环比",
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
        self.dashboard_tree.bind("<<TreeviewSelect>>", self._on_product_selected)

        self.chart_frame = ttk.LabelFrame(self.dashboard_tab, text="商品趋势")
        self.chart_frame.pack(fill="x", pady=(8, 0))
        self.chart_message = ttk.Label(self.chart_frame, text="选择商品查看趋势")
        self.chart_message.pack(padx=8, pady=8)

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
        self.snapshot_date = tk.StringVar(value=(date.today() - timedelta(days=1)).isoformat())
        ttk.Entry(form, textvariable=self.snapshot_date, width=20).grid(row=4, column=1, sticky="w", padx=4)
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

    def _build_quality(self) -> None:
        ttk.Label(self.quality_tab, text="数据质量报告", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        self.quality_text = tk.Text(self.quality_tab, height=35, wrap="word")
        self.quality_text.pack(fill="both", expand=True)

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
        self.previews = previews
        self.last_report = report = self._combine_reports(previews)
        self._show_report(report)
        lines = []
        for preview in previews:
            lines.append(f"{preview.kind}: {len(preview.frame)} 行，文件哈希 {preview.file_hash[:12]}…")
            lines.extend(f"  [{issue.level.value}] {issue.message}" for issue in preview.report.issues[:8])
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("end", "\n".join(lines))
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
        report = self.last_report
        if report.blocking:
            messagebox.showerror("无法提交", "存在阻断问题，请先修复输入文件")
            return
        if not messagebox.askyesno("确认导入", "确认写入四类数据并生成库存快照吗？"):
            return
        try:
            snapshot_date = _parse_date(self.snapshot_date.get())
            sales_mode = "replace" if self.replace_sales.get() else "append"

            def work(progress):
                progress(10, "校验并保存原始文件…")
                result = self.service.commit_batch(
                    *self.previews,
                    snapshot_date=snapshot_date,
                    confirmed=True,
                    sales_mode=sales_mode,
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

    def _refresh_dashboard(self) -> None:
        try:
            snapshot_date = _parse_date(self.dashboard_date.get())
            frame = self.service.get_snapshot(snapshot_date, reevaluate=self.reevaluate_var.get())
            if frame.empty:
                self.summary_var.set("当前日期没有已计算快照")
                self.current_frame = frame
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
            self.dashboard_tree.insert("", "end", values=values, tags=(tag,))
        self.dashboard_tree.tag_configure("risk", foreground="#9b1c1c")

    def _on_product_selected(self, _event=None) -> None:
        selection = self.dashboard_tree.selection()
        if not selection:
            return
        values = self.dashboard_tree.item(selection[0], "values")
        if not values:
            return
        groupcode, product_id = values[0], values[1]
        history = self.service.history_for_product(groupcode, product_id)
        for child in self.chart_frame.winfo_children():
            child.destroy()
        if history.empty:
            ttk.Label(self.chart_frame, text="暂无历史快照").pack(padx=8, pady=8)
            return
        if len(history) < 2:
            ttk.Label(self.chart_frame, text="当前只有一个库存快照，暂时无法形成趋势；导入更多日期后会显示历史曲线。").pack(padx=8, pady=8)
            return
        try:
            _configure_matplotlib_chinese_font()
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure

            figure = Figure(figsize=(12, 3.2), dpi=90)
            axis = figure.add_subplot(111)
            axis.plot(history["snapshot_date"], history["sales"], marker="o", label="销量")
            axis.plot(history["snapshot_date"], history["stock_total"], marker="o", label="库存总量")
            axis.plot(history["snapshot_date"], history["in_transit"], marker="o", label="在途库存")
            axis.plot(history["snapshot_date"], history["moh30"], marker="o", label="30天MOH")
            axis.plot(history["snapshot_date"], history["moh90"], marker="o", label="90天MOH")
            axis.set_title(f"{groupcode} / {product_id} 趋势")
            axis.legend()
            axis.grid(alpha=0.25)
            canvas = FigureCanvasTkAgg(figure, master=self.chart_frame)
            canvas.draw()
            canvas.get_tk_widget().pack(fill="x", expand=True)
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
