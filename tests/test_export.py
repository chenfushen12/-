from pathlib import Path

import pandas as pd

from inventory_tracker.export import export_workbook
from inventory_tracker.models import IssueLevel, QualityReport


def test_export_writes_four_static_sheets(tmp_path: Path) -> None:
    output = tmp_path / "result.xlsx"
    tracking = pd.DataFrame(
        [
            {
                "groupcode": "G1",
                "product_id": "001",
                "alert_labels": ["常规低库存"],
                "quality_labels": [],
                "moh30": 2.5,
            }
        ]
    )
    report = QualityReport()
    report.add(IssueLevel.WARNING, "example", "示例警告")

    export_workbook(
        output,
        tracking=tracking,
        quality_report=report,
        import_logs=[{"kind": "sales", "status": "committed"}],
        metadata={"snapshot_date": "2026-08-10", "template_version_id": 1},
    )

    workbook = pd.ExcelFile(output)
    assert workbook.sheet_names == ["追踪结果", "预警清单", "数据质量报告", "导入日志"]
    alerts = pd.read_excel(output, sheet_name="预警清单")
    assert alerts.iloc[0]["alert_labels"] == "常规低库存"
