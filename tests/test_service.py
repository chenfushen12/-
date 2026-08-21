from datetime import date

import pandas as pd
import pytest

from inventory_tracker.importers import preview_beijing, preview_sales, preview_template, preview_xingwang
from inventory_tracker.service import InventoryTrackerService, OverlapError, ConfirmationRequired, OverwriteRequired, SnapshotNotFound


def _write_inputs(tmp_path):
    template = tmp_path / "template.xlsx"
    sales = tmp_path / "sales.xlsx"
    beijing = tmp_path / "beijing.xlsx"
    xingwang = tmp_path / "xingwang.xlsx"
    pd.DataFrame(
        [{"货品分类": "彩片", "GROUPCODE": "G1", "货品编号": "001", "货品名称": "商品1"}]
    ).to_excel(template, index=False)
    pd.DataFrame(
        [{"时间": "2026-08-10", "GROUP CODE": "G1", "货品编号": "001", "数量": 11}]
    ).to_excel(sales, index=False)
    pd.DataFrame(
        [{"库房": "CB", "产品组": "G1", "产品": "001", "可用数": 20}]
    ).to_excel(beijing, index=False)
    pd.DataFrame(
        [{"GROUPCODE(货)": "G1", "货品编号": "001", "可用库存": 10, "采购在途": 5, "近90天销量(库存公式)": 10, "近30天销量": 10}]
    ).to_excel(xingwang, index=False)
    return template, sales, beijing, xingwang


def test_batch_commit_calculates_and_persists_snapshot(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )

    result = service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)

    assert result.status == "complete"
    stored = service.get_snapshot(date(2026, 8, 10))
    assert len(stored) == 1
    assert stored.iloc[0]["stock_total"] == 35
    assert (tmp_path / "data" / "raw").exists()


def test_batch_requires_confirmation_and_rejects_overlapping_sales(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )

    with pytest.raises(ConfirmationRequired):
        service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=False)

    service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)
    overlapping_sales = tmp_path / "overlapping-sales.xlsx"
    pd.DataFrame(
        [{"时间": "2026-08-10", "GROUP CODE": "G1", "货品编号": "001", "数量": 10}]
    ).to_excel(overlapping_sales, index=False)
    overlapping_preview = preview_sales(overlapping_sales)
    with pytest.raises(OverlapError):
        service.commit_sales(overlapping_preview, mode="append", confirmed=True)


def test_single_warehouse_commit_persists_partial_snapshot(tmp_path) -> None:
    template, _sales, beijing, _xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    template_preview = preview_template(template)
    service.commit_template(template_preview, snapshot_date=date(2026, 8, 10), confirmed=True)

    result = service.commit_inventory(
        preview_beijing(beijing, codes=("CB",)),
        snapshot_date=date(2026, 8, 10),
        confirmed=True,
    )

    assert result.status == "partial"
    row = service.get_snapshot(date(2026, 8, 10)).iloc[0]
    assert row["snapshot_status"] == "partial"
    assert row["inventory_status"] == "待补全"
    assert "北京库存未匹配" not in row["quality_labels"]
    assert "星望库存未匹配" not in row["quality_labels"]


def test_rejected_batch_keeps_raw_files_and_rejected_logs(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    broken_sales = tmp_path / "broken-sales.xlsx"
    pd.DataFrame([{"时间": "not-a-date", "GROUP CODE": "G1", "货品编号": "001", "数量": 1}]).to_excel(broken_sales, index=False)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(broken_sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )

    with pytest.raises(ValueError):
        service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)

    assert list((tmp_path / "data" / "raw").rglob("*.xlsx"))
    assert any(log["status"] == "rejected" for log in service.database.import_logs())


def test_future_sales_dates_are_reported_and_excluded(tmp_path) -> None:
    template, _sales, beijing, xingwang = _write_inputs(tmp_path)
    future_sales = tmp_path / "future-sales.xlsx"
    pd.DataFrame(
        [{"时间": "2026-08-20", "GROUP CODE": "G1", "货品编号": "001", "数量": 10}]
    ).to_excel(future_sales, index=False)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(future_sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )

    result = service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)

    assert any(issue.code == "future_sales_date" for issue in result.report.infos)
    assert result.frame.iloc[0]["sales"] is None


def test_same_active_template_can_be_reused_for_another_snapshot_date(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    first = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )
    service.commit_batch(*first, snapshot_date=date(2026, 8, 10), confirmed=True)

    sales_second = tmp_path / "sales-second.xlsx"
    beijing_second = tmp_path / "beijing-second.xlsx"
    xingwang_second = tmp_path / "xingwang-second.xlsx"
    pd.DataFrame([{"时间": "2026-08-11", "GROUP CODE": "G1", "货品编号": "001", "数量": 12}]).to_excel(sales_second, index=False)
    pd.DataFrame([{"库房": "CB", "产品组": "G1", "产品": "001", "可用数": 21}]).to_excel(beijing_second, index=False)
    pd.DataFrame([{"GROUPCODE(货)": "G1", "货品编号": "001", "可用库存": 11, "采购在途": 5, "近90天销量(库存公式)": 10, "近30天销量": 10}]).to_excel(xingwang_second, index=False)
    second = (
        preview_template(template),
        preview_sales(sales_second),
        preview_beijing(beijing_second, codes=("CB",)),
        preview_xingwang(xingwang_second),
    )

    result = service.commit_batch(*second, snapshot_date=date(2026, 8, 11), confirmed=True)

    assert result.status == "complete"
    assert len(service.get_snapshot(date(2026, 8, 10))) == 1
    assert len(service.get_snapshot(date(2026, 8, 11))) == 1


def test_all_committed_files_can_be_reused_for_a_new_snapshot_date(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )
    service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)

    reused = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )
    result = service.commit_batch(*reused, snapshot_date=date(2026, 8, 11), confirmed=True)

    assert result.status == "complete"
    assert len(service.get_snapshot(date(2026, 8, 11))) == 1
    assert service.get_snapshot(date(2026, 8, 11)).iloc[0]["stock_total"] == 35
    assert sum(issue.code == "reused_file" for issue in result.report.infos) == 4


def test_same_date_requires_explicit_overwrite_and_replaces_snapshot(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    first = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )
    service.commit_batch(*first, snapshot_date=date(2026, 8, 10), confirmed=True)

    sales_new = tmp_path / "sales-new.xlsx"
    beijing_new = tmp_path / "beijing-new.xlsx"
    xingwang_new = tmp_path / "xingwang-new.xlsx"
    pd.DataFrame([{"时间": "2026-08-10", "GROUP CODE": "G1", "货品编号": "001", "数量": 11}]).to_excel(sales_new, index=False)
    pd.DataFrame([{"库房": "CB", "产品组": "G1", "产品": "001", "可用数": 30}]).to_excel(beijing_new, index=False)
    pd.DataFrame([{"GROUPCODE(货)": "G1", "货品编号": "001", "可用库存": 12, "采购在途": 5, "近90天销量(库存公式)": 10, "近30天销量": 10}]).to_excel(xingwang_new, index=False)
    replacement = (
        preview_template(template),
        preview_sales(sales_new),
        preview_beijing(beijing_new, codes=("CB",)),
        preview_xingwang(xingwang_new),
    )

    with pytest.raises(OverwriteRequired):
        service.commit_batch(*replacement, snapshot_date=date(2026, 8, 10), confirmed=True)

    result = service.commit_batch(*replacement, snapshot_date=date(2026, 8, 10), confirmed=True, overwrite=True)

    assert result.status == "complete"
    row = service.get_snapshot(date(2026, 8, 10)).iloc[0]
    assert row["stock_total"] == 47
    assert row["sales"] == 11


def test_delete_snapshots_is_permanent_for_results_but_keeps_shared_data_and_audit(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )
    service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)
    service.commit_batch(*previews, snapshot_date=date(2026, 8, 11), confirmed=True)
    logs_before = len(service.database.import_logs())

    with pytest.raises(ConfirmationRequired):
        service.delete_snapshots([date(2026, 8, 10)], confirmed=False)

    deleted = service.delete_snapshots([date(2026, 8, 10)], confirmed=True)

    assert deleted.deleted_dates == (date(2026, 8, 10),)
    assert service.get_snapshot(date(2026, 8, 10)).empty
    assert len(service.get_snapshot(date(2026, 8, 11))) == 1
    assert service.database.existing_sales_dates() == {date(2026, 8, 10)}
    assert len(service.database.import_logs()) == logs_before
    assert len(service.database.deletion_logs()) == 1


def test_batch_delete_missing_date_rolls_back_all_deletions(tmp_path) -> None:
    template, sales, beijing, xingwang = _write_inputs(tmp_path)
    service = InventoryTrackerService(tmp_path / "app.db", data_dir=tmp_path / "data")
    previews = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )
    service.commit_batch(*previews, snapshot_date=date(2026, 8, 10), confirmed=True)

    with pytest.raises(SnapshotNotFound):
        service.delete_snapshots([date(2026, 8, 10), date(2026, 8, 12)], confirmed=True)

    assert len(service.get_snapshot(date(2026, 8, 10))) == 1
    assert service.database.deletion_logs() == []
