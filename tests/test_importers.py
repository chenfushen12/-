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


def test_template_with_no_valid_keys_is_blocking(tmp_path) -> None:
    path = tmp_path / "template.xlsx"
    pd.DataFrame([{"货品分类": "彩片", "GROUPCODE": None, "货品编号": None}]).to_excel(path, index=False)

    preview = preview_template(path)

    assert not preview.report.can_commit
    assert any(issue.code == "empty_template" for issue in preview.report.blocking)


def test_missing_required_column_returns_blocking_report_without_crashing(tmp_path) -> None:
    path = tmp_path / "template.xlsx"
    pd.DataFrame([{"货品编号": "001"}]).to_excel(path, index=False)

    preview = preview_template(path)

    assert not preview.report.can_commit
    assert preview.frame.empty
    assert any(issue.code == "missing_column" and issue.field == "groupcode" for issue in preview.report.blocking)


def test_numeric_product_key_is_converted_to_text_without_blocking(tmp_path) -> None:
    path = tmp_path / "template.xlsx"
    pd.DataFrame([{"货品分类": "彩片", "GROUPCODE": "G1", "货品编号": 123, "货品名称": "商品"}]).to_excel(path, index=False)

    preview = preview_template(path)

    assert preview.report.can_commit
    assert preview.frame.iloc[0]["product_id"] == "123"
    issue = next(issue for issue in preview.report.warnings if issue.code == "numeric_key_converted")
    assert "template.xlsx" in issue.message
    assert "第 3 列" in issue.message
    assert "货品编号" in issue.message


def test_numeric_key_conversion_normalizes_integer_shaped_numbers(tmp_path) -> None:
    path = tmp_path / "template.xlsx"
    pd.DataFrame(
        [
            {"GROUPCODE": "G1", "货品编号": 123},
            {"GROUPCODE": "G2", "货品编号": 123.0},
            {"GROUPCODE": "G3", "货品编号": 123.45},
            {"GROUPCODE": "G4", "货品编号": "00123"},
        ]
    ).to_excel(path, index=False)

    preview = preview_template(path)

    assert preview.report.can_commit
    assert preview.frame["product_id"].tolist() == ["123", "123", "123.45", "00123"]


def test_all_import_types_accept_numeric_product_keys_as_text(tmp_path) -> None:
    template = tmp_path / "template.xlsx"
    sales = tmp_path / "sales.xlsx"
    beijing = tmp_path / "beijing.xlsx"
    xingwang = tmp_path / "xingwang.xlsx"
    pd.DataFrame(
        [{"货品分类": "彩片", "GROUPCODE": 100, "货品编号": 123, "货品名称": "商品"}]
    ).to_excel(template, index=False)
    pd.DataFrame(
        [{"时间": "2026-08-10", "GROUP CODE": 100, "货品编号": 123, "数量": 1}]
    ).to_excel(sales, index=False)
    pd.DataFrame(
        [{"库房": "CB", "产品组": 100, "产品": 123, "可用数": 2}]
    ).to_excel(beijing, index=False)
    pd.DataFrame(
        [{"GROUPCODE(货)": 100, "货品编号": 123, "可用库存": 3, "采购在途": 4, "近90天销量(库存公式)": 5, "近30天销量": 6}]
    ).to_excel(xingwang, index=False)

    previews = (
        preview_template(template),
        preview_sales(sales),
        preview_beijing(beijing, codes=("CB",)),
        preview_xingwang(xingwang),
    )

    for preview in previews:
        assert preview.report.can_commit
        assert preview.frame.iloc[0]["groupcode"] == "100"
        assert preview.frame.iloc[0]["product_id"] == "123"
        conversion_issues = [issue for issue in preview.report.warnings if issue.code == "numeric_key_converted"]
        assert len(conversion_issues) == 2
        assert all("第" in issue.message and "列" in issue.message for issue in conversion_issues)


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
