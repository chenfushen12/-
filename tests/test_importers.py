from datetime import date

import pandas as pd

from inventory_tracker.importers import (
    preview_beijing,
    preview_sales,
    preview_template,
    preview_xingwang,
)


def test_template_preview_deduplicates_identical_rows(tmp_path) -> None:
    path = tmp_path / "template.xlsx"
    rows = [
        {"货品分类": "彩片", "GROUPCODE": " G1 ", "GROUPNAME": "组1", "货品编号": "001", "货品名称": "商品", "货品备注": ""},
        {"货品分类": "彩片", "GROUPCODE": "G1", "GROUPNAME": "组1", "货品编号": "001", "货品名称": "商品", "货品备注": ""},
    ]
    pd.DataFrame(rows).to_excel(path, index=False)

    preview = preview_template(path)

    assert preview.report.can_commit
    assert len(preview.frame) == 1
    assert preview.frame.iloc[0]["groupcode"] == "G1"
    assert any(issue.code == "duplicate_template_rows" for issue in preview.report.warnings)


def test_template_preview_blocks_conflicting_duplicate(tmp_path) -> None:
    path = tmp_path / "template.xlsx"
    pd.DataFrame(
        [
            {"货品分类": "彩片", "GROUPCODE": "G1", "GROUPNAME": "组1", "货品编号": "001", "货品名称": "商品", "货品备注": ""},
            {"货品分类": "彩片", "GROUPCODE": "G1", "GROUPNAME": "组1", "货品编号": "001", "货品名称": "另一个商品", "货品备注": ""},
        ]
    ).to_excel(path, index=False)

    preview = preview_template(path)

    assert not preview.report.can_commit
    assert any(issue.code == "conflicting_template_duplicate" for issue in preview.report.blocking)


def test_sales_preview_normalizes_dates_aggregates_and_reports_missing_key(tmp_path) -> None:
    path = tmp_path / "sales.xlsx"
    pd.DataFrame(
        [
            {"时间": "2026-08-01 09:00:00", "GROUP CODE": "G1", "货品编号": "001", "数量": 2},
            {"时间": "2026/8/1", "GROUP CODE": "G1", "货品编号": "001", "数量": -1},
            {"时间": "2026/8/1", "GROUP CODE": None, "货品编号": "001", "数量": 4},
        ]
    ).to_excel(path, index=False)

    preview = preview_sales(path)

    assert preview.report.can_commit
    assert preview.imported_dates == (date(2026, 8, 1),)
    assert preview.frame.iloc[0]["quantity"] == 1
    assert any(issue.code == "sales_missing_key" for issue in preview.report.warnings)


def test_beijing_preview_filters_codes_and_sums_duplicates(tmp_path) -> None:
    path = tmp_path / "beijing.xlsx"
    pd.DataFrame(
        [
            {"库房": "CB", "产品组": "G1", "产品": "001", "可用数": 2},
            {"库房": "CB", "产品组": "G1", "产品": "001", "可用数": 3},
            {"库房": "OTHER", "产品组": "G1", "产品": "001", "可用数": 100},
        ]
    ).to_excel(path, index=False)

    preview = preview_beijing(path, codes=("CB",))

    assert preview.frame.iloc[0]["beijing_available"] == 5
    assert any(issue.code == "duplicate_inventory_rows" for issue in preview.report.warnings)


def test_xingwang_preview_sums_numeric_duplicate_columns(tmp_path) -> None:
    path = tmp_path / "xingwang.xlsx"
    pd.DataFrame(
        [
            {"GROUPCODE(货)": "G1", "货品编号": "001", "可用库存": 2, "采购在途": 3, "近90天销量(库存公式)": 4, "近30天销量": 5},
            {"GROUPCODE(货)": "G1", "货品编号": "001", "可用库存": 1, "采购在途": 1, "近90天销量(库存公式)": 2, "近30天销量": 3},
        ]
    ).to_excel(path, index=False)

    preview = preview_xingwang(path)

    row = preview.frame.iloc[0]
    assert row["xingwang_available"] == 3
    assert row["in_transit"] == 4
    assert row["source_sales90"] == 6
    assert row["source_sales30"] == 8
