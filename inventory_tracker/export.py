from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from .models import QualityReport


def _display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in ("alert_labels", "quality_labels"):
        if column in result:
            result[column] = result[column].map(lambda value: "、".join(value) if isinstance(value, list) else value)
    return result


def _quality_frame(report: QualityReport) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "level": issue.level.value,
                "code": issue.code,
                "message": issue.message,
                "row": issue.row,
                "field": issue.field,
            }
            for issue in report.issues
        ]
    )


def export_workbook(
    output_path: str | Path,
    *,
    tracking: pd.DataFrame,
    quality_report: QualityReport,
    import_logs: Iterable[Mapping[str, object]],
    metadata: Mapping[str, object],
) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    display = _display_frame(tracking)
    if "alert_labels" in tracking:
        has_alert = tracking["alert_labels"].map(bool)
        alerts = display.loc[has_alert].copy()
    else:
        alerts = display.iloc[0:0].copy()
    logs = pd.DataFrame(list(import_logs))
    if metadata:
        metadata_rows = pd.DataFrame([dict(metadata)])
        logs = pd.concat([metadata_rows, logs], ignore_index=True, sort=False)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        display.to_excel(writer, sheet_name="追踪结果", index=False)
        alerts.to_excel(writer, sheet_name="预警清单", index=False)
        _quality_frame(quality_report).to_excel(writer, sheet_name="数据质量报告", index=False)
        logs.to_excel(writer, sheet_name="导入日志", index=False)
