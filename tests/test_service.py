from datetime import date

import pandas as pd
import pytest

from inventory_tracker.importers import preview_beijing, preview_sales, preview_template, preview_xingwang
from inventory_tracker.service import InventoryTrackerService, OverlapError, ConfirmationRequired


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
