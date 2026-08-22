from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from .calculations import calculate_tracking, reevaluate_alerts
from .database import Database
from .importers import file_hash
from .models import CommitResult, DeletionResult, ImportPreview, IssueLevel, QualityReport, TrackerConfig
from .export import export_workbook


class ConfirmationRequired(ValueError):
    pass


class ValidationError(ValueError):
    pass


class DuplicateImportError(ValueError):
    pass


class OverlapError(ValueError):
    pass


class OverwriteRequired(ValueError):
    def __init__(self, summary: dict[str, object]):
        self.summary = summary
        super().__init__(f"快照日期已存在: {summary.get('snapshot_date')}")


class SnapshotNotFound(ValueError):
    pass


@dataclass
class SnapshotResult:
    status: str
    frame: pd.DataFrame
    report: QualityReport


class InventoryTrackerService:
    def __init__(self, database_path: str | Path, *, data_dir: str | Path | None = None, config: TrackerConfig | None = None):
        self.database = Database(database_path)
        self.data_dir = Path(data_dir) if data_dir else Path(database_path).parent / "data"
        self.config = config or TrackerConfig()

    def close(self) -> None:
        self.database.close()

    def _store_raw(self, preview: ImportPreview, snapshot_date: date | None) -> str:
        date_part = snapshot_date.isoformat() if snapshot_date else "undated"
        target_dir = self.data_dir / "raw" / preview.kind / date_part
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{preview.file_hash}.xlsx"
        if not target.exists():
            shutil.copy2(preview.source_path, target)
        return str(target)

    @staticmethod
    def _report_json(report: QualityReport) -> str:
        return json.dumps(
            [
                {
                    "level": issue.level.value,
                    "code": issue.code,
                    "message": issue.message,
                    "row": issue.row,
                    "field": issue.field,
                }
                for issue in report.issues
            ],
            ensure_ascii=False,
        )

    def _validate_previews(self, previews: tuple[ImportPreview, ...], *, ignored_hashes: set[str] | None = None) -> None:
        ignored = ignored_hashes or set()
        blocking = [issue for preview in previews for issue in preview.report.blocking]
        if blocking:
            raise ValidationError("导入包含阻断问题: " + "；".join(issue.message for issue in blocking[:5]))
        for preview in previews:
            if preview.file_hash in ignored:
                continue
            if self.database.has_import_hash(preview.file_hash):
                raise DuplicateImportError(f"{preview.kind} 文件已导入: {preview.source_path}")

    @staticmethod
    def _add_future_date_issue(preview: ImportPreview, snapshot_date: date) -> None:
        future_dates = sorted(value for value in preview.imported_dates if value > snapshot_date)
        if future_dates:
            preview.report.add(
                IssueLevel.INFO,
                "future_sales_date",
                f"销售日期晚于库存快照日，已排除计算: {', '.join(value.isoformat() for value in future_dates)}",
                field="business_date",
            )

    def _record_rejected_previews(self, previews: tuple[ImportPreview, ...], snapshot_date: date | None) -> None:
        stored_paths = {preview.kind: self._store_raw(preview, snapshot_date) for preview in previews}
        with self.database.transaction():
            for preview in previews:
                if self.database.has_import_hash(preview.file_hash):
                    continue
                self.database.insert_import_log(
                    kind=preview.kind,
                    source_path=preview.source_path,
                    stored_path=stored_paths[preview.kind],
                    file_hash=preview.file_hash,
                    business_date=snapshot_date,
                    mode="precheck",
                    status="rejected",
                    report_json=self._report_json(preview.report),
                )

    def _overlap(self, preview: ImportPreview) -> set[date]:
        return set(preview.imported_dates) & self.database.existing_sales_dates()

    @staticmethod
    def _mark_reused(preview: ImportPreview) -> None:
        if not any(issue.code == "reused_file" for issue in preview.report.issues):
            preview.report.add(IssueLevel.INFO, "reused_file", f"已复用此前成功导入的 {preview.kind} 文件")

    def commit_batch(
        self,
        template_preview: ImportPreview,
        sales_preview: ImportPreview,
        beijing_preview: ImportPreview,
        xingwang_preview: ImportPreview,
        *,
        snapshot_date: date,
        confirmed: bool,
        sales_mode: str = "append",
        overwrite: bool = False,
    ) -> SnapshotResult:
        if not confirmed:
            raise ConfirmationRequired("必须在预览后确认导入")
        previews = (template_preview, sales_preview, beijing_preview, xingwang_preview)
        existing_summary = self.database.snapshot_summary(snapshot_date)
        if existing_summary is not None and not overwrite:
            raise OverwriteRequired(existing_summary)
        self._add_future_date_issue(sales_preview, snapshot_date)
        existing_template_version = self.database.template_version_by_hash(template_preview.file_hash)
        reuse_template = existing_template_version is not None
        reused_imports = {
            preview.kind: self.database.committed_import_by_hash(preview.kind, preview.file_hash)
            for preview in previews
        }
        reused_hashes = {
            preview.file_hash
            for preview in previews
            if reused_imports[preview.kind] is not None or (preview.kind == "template" and reuse_template)
        }
        for preview in previews:
            if preview.file_hash in reused_hashes:
                self._mark_reused(preview)
        try:
            self._validate_previews(previews, ignored_hashes=reused_hashes)
        except ValidationError:
            self._record_rejected_previews(previews, snapshot_date)
            raise
        overlap = self._overlap(sales_preview)
        if overwrite and overlap and reused_imports["sales"] is None:
            sales_mode = "replace"
        if overlap and sales_mode == "append" and reused_imports["sales"] is None:
            raise OverlapError("销售日期与已存在数据重叠，必须选择按日期替换")
        if sales_mode not in {"append", "replace"}:
            raise ValueError("sales_mode 必须为 append 或 replace")

        stored_paths = {
            preview.kind: (
                str(reused_imports[preview.kind]["stored_path"])
                if reused_imports[preview.kind] is not None
                else self._store_raw(preview, snapshot_date)
            )
            for preview in previews
        }
        with self.database.transaction():
            if reuse_template:
                assert existing_template_version is not None
                self.database.activate_template_version(existing_template_version)
                template_version_id = existing_template_version
            else:
                self.database.insert_import_log(
                    kind="template",
                    source_path=template_preview.source_path,
                    stored_path=stored_paths["template"],
                    file_hash=template_preview.file_hash,
                    business_date=snapshot_date,
                    mode="activate",
                    status="committed",
                    report_json=self._report_json(template_preview.report),
                )
                template_version_id = self.database.insert_template(
                    template_preview.frame,
                    source_hash=template_preview.file_hash,
                    stored_path=stored_paths["template"],
                )
            sales_reuse = reused_imports["sales"]
            if sales_reuse is None:
                sales_log = self.database.insert_import_log(
                    kind="sales",
                    source_path=sales_preview.source_path,
                    stored_path=stored_paths["sales"],
                    file_hash=sales_preview.file_hash,
                    business_date=snapshot_date,
                    mode=sales_mode,
                    status="committed",
                    report_json=self._report_json(sales_preview.report),
                )
                self.database.insert_sales(
                    sales_preview.frame,
                    sales_preview.imported_dates,
                    import_id=sales_log,
                    replace=sales_mode == "replace",
                    negative_keys=[(value[0], str(value[1]), str(value[2])) for value in sales_preview.metadata.get("negative_keys", [])],
                )
            beijing_reuse = reused_imports["beijing"]
            beijing_log = int(beijing_reuse["id"]) if beijing_reuse is not None else self.database.insert_import_log(
                kind="beijing",
                source_path=beijing_preview.source_path,
                stored_path=stored_paths["beijing"],
                file_hash=beijing_preview.file_hash,
                business_date=snapshot_date,
                mode="replace",
                status="committed",
                report_json=self._report_json(beijing_preview.report),
            )
            beijing_id = self.database.upsert_inventory(
                "beijing",
                snapshot_date,
                beijing_preview.frame,
                source_hash=beijing_preview.file_hash,
                stored_path=stored_paths["beijing"],
                codes=self.config.beijing_codes,
                import_id=beijing_log,
            )
            xingwang_reuse = reused_imports["xingwang"]
            xingwang_log = int(xingwang_reuse["id"]) if xingwang_reuse is not None else self.database.insert_import_log(
                kind="xingwang",
                source_path=xingwang_preview.source_path,
                stored_path=stored_paths["xingwang"],
                file_hash=xingwang_preview.file_hash,
                business_date=snapshot_date,
                mode="replace",
                status="committed",
                report_json=self._report_json(xingwang_preview.report),
            )
            xingwang_id = self.database.upsert_inventory(
                "xingwang",
                snapshot_date,
                xingwang_preview.frame,
                source_hash=xingwang_preview.file_hash,
                stored_path=stored_paths["xingwang"],
                codes=self.config.beijing_codes,
                import_id=xingwang_log,
            )
            calculated = self._calculate_and_save(
                snapshot_date,
                template_version_id=template_version_id,
                beijing_id=beijing_id,
                xingwang_id=xingwang_id,
            )
        combined_report = QualityReport()
        for preview in previews:
            combined_report.extend(preview.report)
        combined_report.extend(calculated.report)
        calculated.report = combined_report
        return calculated

    def commit_template(self, preview: ImportPreview, *, snapshot_date: date | None = None, confirmed: bool) -> CommitResult:
        if not confirmed:
            raise ConfirmationRequired("必须在预览后确认导入")
        try:
            self._validate_previews((preview,))
        except ValidationError:
            self._record_rejected_previews((preview,), snapshot_date)
            raise
        if preview.kind != "template":
            raise ValueError("只能提交 template 预览")
        stored_path = self._store_raw(preview, snapshot_date)
        with self.database.transaction():
            import_id = self.database.insert_import_log(
                kind="template",
                source_path=preview.source_path,
                stored_path=stored_path,
                file_hash=preview.file_hash,
                business_date=snapshot_date,
                mode="activate",
                status="committed",
                report_json=self._report_json(preview.report),
            )
            self.database.insert_template(preview.frame, source_hash=preview.file_hash, stored_path=stored_path)
        return CommitResult(import_id, "商品模板已启用", preview.report)

    def commit_sales(self, preview: ImportPreview, *, mode: str, confirmed: bool) -> CommitResult:
        if not confirmed:
            raise ConfirmationRequired("必须在预览后确认导入")
        try:
            self._validate_previews((preview,))
        except ValidationError:
            self._record_rejected_previews((preview,), None)
            raise
        overlap = self._overlap(preview)
        if overlap and mode == "append":
            raise OverlapError("销售日期重叠，请选择按日期替换")
        stored_path = self._store_raw(preview, None)
        with self.database.transaction():
            import_id = self.database.insert_import_log(
                kind=preview.kind,
                source_path=preview.source_path,
                stored_path=stored_path,
                file_hash=preview.file_hash,
                business_date=None,
                mode=mode,
                status="committed",
                report_json=self._report_json(preview.report),
            )
            self.database.insert_sales(
                preview.frame,
                preview.imported_dates,
                import_id=import_id,
                replace=mode == "replace",
                negative_keys=[(value[0], str(value[1]), str(value[2])) for value in preview.metadata.get("negative_keys", [])],
            )
        return CommitResult(import_id, "销售数据导入成功", preview.report)

    def commit_inventory(
        self,
        preview: ImportPreview,
        *,
        snapshot_date: date,
        confirmed: bool,
    ) -> SnapshotResult:
        """Replace one warehouse snapshot and recalculate the current date."""
        if not confirmed:
            raise ConfirmationRequired("必须在预览后确认导入")
        try:
            self._validate_previews((preview,))
        except ValidationError:
            self._record_rejected_previews((preview,), snapshot_date)
            raise
        if preview.kind not in {"beijing", "xingwang"}:
            raise ValueError("只能提交 beijing 或 xingwang 库存预览")
        stored_path = self._store_raw(preview, snapshot_date)
        with self.database.transaction():
            import_id = self.database.insert_import_log(
                kind=preview.kind,
                source_path=preview.source_path,
                stored_path=stored_path,
                file_hash=preview.file_hash,
                business_date=snapshot_date,
                mode="replace",
                status="committed",
                report_json=self._report_json(preview.report),
            )
            snapshot_id = self.database.upsert_inventory(
                preview.kind,
                snapshot_date,
                preview.frame,
                source_hash=preview.file_hash,
                stored_path=stored_path,
                codes=self.config.beijing_codes,
                import_id=import_id,
            )
            existing_meta = self.database.snapshot_meta(snapshot_date)
            active = self.database.active_template()
            if existing_meta is not None:
                template_version_id = int(existing_meta["template_version_id"])
                calculation_config = TrackerConfig(
                    growth_threshold=float(existing_meta["threshold_growth"]),
                    moh30_threshold=float(existing_meta["threshold_moh30"]),
                    moh90_threshold=float(existing_meta["threshold_moh90"]),
                    beijing_codes=tuple(json.loads(existing_meta["beijing_codes_json"])),
                )
            elif active is not None:
                template_version_id = active[0]
                calculation_config = self.config
            else:
                raise ValidationError("尚未启用商品主模板")
            result = self._calculate_and_save(
                snapshot_date,
                template_version_id=template_version_id,
                beijing_id=self.database.inventory_snapshot_id("beijing", snapshot_date),
                xingwang_id=self.database.inventory_snapshot_id("xingwang", snapshot_date),
                config=calculation_config,
            )
        return result

    def _calculate_and_save(
        self,
        snapshot_date: date,
        *,
        template_version_id: int,
        beijing_id: int | None,
        xingwang_id: int | None,
        config: TrackerConfig | None = None,
    ) -> SnapshotResult:
        products = self.database.template_by_id(template_version_id)
        if products.empty:
            raise ValidationError(f"模板版本不存在: {template_version_id}")
        calculation_config = config or self.config
        sales = self.database.load_sales()
        beijing = self.database.load_inventory("beijing", snapshot_date)
        xingwang = self.database.load_inventory("xingwang", snapshot_date)
        inventory_complete = beijing_id is not None and xingwang_id is not None
        result = calculate_tracking(
            products,
            sales,
            beijing,
            xingwang,
            snapshot_date=snapshot_date,
            imported_sales_dates=self.database.existing_sales_dates(),
            inventory_complete=inventory_complete,
            config=calculation_config,
            negative_sales_keys=self.database.load_negative_sales_keys(),
        )
        status = "complete" if inventory_complete else "partial"
        self.database.save_snapshot(
            snapshot_date,
            result,
            template_version_id=template_version_id,
            beijing_snapshot_id=beijing_id,
            xingwang_snapshot_id=xingwang_id,
            status=status,
            threshold_growth=calculation_config.growth_threshold,
            threshold_moh30=calculation_config.moh30_threshold,
            threshold_moh90=calculation_config.moh90_threshold,
            beijing_codes=calculation_config.beijing_codes,
        )
        report = QualityReport()
        if not inventory_complete:
            report.add(IssueLevel.INFO, "partial_inventory", "库存快照缺少一个仓库")
        return SnapshotResult(status, result, report)

    def get_snapshot(self, snapshot_date: date, *, reevaluate: bool = False) -> pd.DataFrame:
        frame = self.database.load_snapshot(snapshot_date)
        return reevaluate_alerts(frame, self.config) if reevaluate else frame

    def snapshot_summary(self, snapshot_date: date) -> dict[str, object] | None:
        return self.database.snapshot_summary(snapshot_date)

    def list_snapshots(self) -> list[dict[str, object]]:
        return self.database.list_snapshots()

    def deletion_logs(self) -> list[dict[str, object]]:
        return self.database.deletion_logs()

    def quality_details(
        self,
        snapshot_date: date,
        *,
        groupcode: str | None = None,
        product_id: str | None = None,
    ) -> dict[str, object]:
        frame = self.database.load_snapshot(snapshot_date)
        if groupcode is not None and product_id is not None:
            frame = frame.loc[(frame["groupcode"] == groupcode) & (frame["product_id"] == product_id)]
        product_issues: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            for label in row.get("quality_labels", []) or []:
                product_issues.append(
                    {
                        "snapshot_date": snapshot_date,
                        "groupcode": row.get("groupcode"),
                        "product_id": row.get("product_id"),
                        "product_name": row.get("product_name"),
                        "reason": label,
                        "source": "追踪结果",
                    }
                )
        return {
            "product_issues": product_issues,
            "import_issues": self.database.import_quality_issues(snapshot_date),
            "is_product_located": groupcode is not None and product_id is not None,
        }

    def delete_snapshots(self, dates: list[date], *, confirmed: bool) -> DeletionResult:
        if not confirmed:
            raise ConfirmationRequired("必须在摘要确认后删除快照")
        if not dates:
            return DeletionResult((), ())
        missing = [value for value in dates if self.database.snapshot_summary(value) is None]
        if missing:
            formatted = ", ".join(value.strftime("%Y/%m/%d") for value in missing)
            raise SnapshotNotFound(f"以下日期没有可删除的快照: {formatted}")
        with self.database.transaction():
            deleted, skipped = self.database.delete_snapshots(dates)
        return DeletionResult(deleted, skipped)

    def export_snapshot(self, snapshot_date: date, output_path: str | Path) -> None:
        frame = self.get_snapshot(snapshot_date)
        metadata = self.database.snapshot_meta(snapshot_date) or {"snapshot_date": snapshot_date.isoformat()}
        logs = self.database.import_logs()
        report = QualityReport()
        for log in logs:
            try:
                issues = json.loads(str(log.get("report_json", "[]")))
            except json.JSONDecodeError:
                issues = []
            for issue in issues:
                try:
                    level = IssueLevel(issue.get("level", IssueLevel.INFO.value))
                except ValueError:
                    level = IssueLevel.INFO
                report.add(level, str(issue.get("code", "import")), str(issue.get("message", "")), row=issue.get("row"), field=issue.get("field"))
        for _, row in frame.iterrows():
            for label in row.get("quality_labels", []) or []:
                report.add(IssueLevel.WARNING, "snapshot_quality", str(label), field=str(row.get("product_id", "")))
        export_workbook(
            output_path,
            tracking=frame,
            quality_report=report,
            import_logs=logs,
            metadata=metadata,
        )

    def history_for_product(self, groupcode: str, product_id: str) -> pd.DataFrame:
        return self.database.history_for_product(groupcode, product_id)
