from datetime import date, timedelta

import pandas as pd

from inventory_tracker.calculations import calculate_tracking, reevaluate_alerts
from inventory_tracker.models import TrackerConfig


def _dates(end: date, count: int) -> set[date]:
    return {end - timedelta(days=offset) for offset in range(count)}


def test_calculates_growth_moh_and_combined_alerts() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame(
        [
            {"groupcode": "G1", "product_id": "P1", "category": "彩片", "product_name": "商品1"},
            {"groupcode": "G1", "product_id": "P2", "category": "彩片", "product_name": "商品2"},
        ]
    )
    sales = pd.DataFrame(
        [
            {"business_date": snapshot_date, "groupcode": "G1", "product_id": "P1", "quantity": 10},
            {"business_date": snapshot_date - timedelta(days=1), "groupcode": "G1", "product_id": "P1", "quantity": 5},
            {"business_date": snapshot_date - timedelta(days=2), "groupcode": "G1", "product_id": "P1", "quantity": 5},
        ]
    )
    beijing = pd.DataFrame(
        [
            {"groupcode": "G1", "product_id": "P1", "beijing_available": 10},
            {"groupcode": "G1", "product_id": "P2", "beijing_available": 0},
        ]
    )
    xingwang = pd.DataFrame(
        [
            {
                "groupcode": "G1",
                "product_id": "P1",
                "xingwang_available": 5,
                "in_transit": 15,
                "source_sales90": 20,
                "source_sales30": 20,
            },
            {
                "groupcode": "G1",
                "product_id": "P2",
                "xingwang_available": 0,
                "in_transit": 0,
                "source_sales90": 0,
                "source_sales30": 0,
            },
        ]
    )

    result = calculate_tracking(
        products,
        sales,
        beijing,
        xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates=_dates(snapshot_date, 90),
        inventory_complete=True,
        config=TrackerConfig(),
    )

    first = result.loc[result["product_id"] == "P1"].iloc[0]
    assert first["sales"] == 10
    assert first["growth"] == 1.0
    assert first["sales30"] == 20
    assert first["sales90"] == 20
    assert first["moh30"] == 1.5
    assert first["moh90"] == 4.5
    assert "增长型缺货风险" in first["alert_labels"]
    assert "常规低库存" in first["alert_labels"]

    second = result.loc[result["product_id"] == "P2"].iloc[0]
    assert second["sales"] == 0
    assert second["sales_status"] == "confirmed_no_sales"
    assert pd.isna(second["moh30"])
    assert pd.isna(second["moh90"])
    assert "无库存预警" in second["alert_labels"]
    assert "滞销品预警" in second["alert_labels"]


def test_window_completeness_is_checked_per_product_key() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame(
        [
            {"groupcode": "G1", "product_id": "SPARSE"},
            {"groupcode": "G1", "product_id": "COMPLETE"},
        ]
    )
    sales = pd.DataFrame(
        [
            {"business_date": snapshot_date, "groupcode": "G1", "product_id": "SPARSE", "quantity": 3},
            *[
                {
                    "business_date": snapshot_date - timedelta(days=offset),
                    "groupcode": "G1",
                    "product_id": "COMPLETE",
                    "quantity": 2,
                }
                for offset in range(90)
            ],
        ]
    )
    beijing = pd.DataFrame(
        [
            {"groupcode": "G1", "product_id": "SPARSE", "beijing_available": 1},
            {"groupcode": "G1", "product_id": "COMPLETE", "beijing_available": 1},
        ]
    )
    xingwang = pd.DataFrame(
        [
            {
                "groupcode": "G1",
                "product_id": "SPARSE",
                "xingwang_available": 1,
                "in_transit": 0,
                "source_sales90": 90,
                "source_sales30": 30,
            },
            {
                "groupcode": "G1",
                "product_id": "COMPLETE",
                "xingwang_available": 1,
                "in_transit": 0,
                "source_sales90": 90,
                "source_sales30": 30,
            },
        ]
    )

    result = calculate_tracking(
        products,
        sales,
        beijing,
        xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates=_dates(snapshot_date, 90),
        inventory_complete=True,
        config=TrackerConfig(),
    ).set_index("product_id")

    assert result.loc["SPARSE", "sales30"] == 30
    assert result.loc["SPARSE", "sales90"] == 90
    assert result.loc["SPARSE", "sales30_status"] == "historical_shortage"
    assert result.loc["SPARSE", "sales90_status"] == "historical_shortage"
    assert result.loc["COMPLETE", "sales30"] == 60
    assert result.loc["COMPLETE", "sales90"] == 180
    assert result.loc["COMPLETE", "sales30_status"] == "calculated"
    assert result.loc["COMPLETE", "sales90_status"] == "calculated"


def test_uses_independent_fallback_for_incomplete_window() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame([{"groupcode": "G1", "product_id": "P1"}])
    sales = pd.DataFrame(
        [{"business_date": snapshot_date, "groupcode": "G1", "product_id": "P1", "quantity": 8}]
    )
    beijing = pd.DataFrame([{"groupcode": "G1", "product_id": "P1", "beijing_available": 20}])
    xingwang = pd.DataFrame(
        [
            {
                "groupcode": "G1",
                "product_id": "P1",
                "xingwang_available": 10,
                "in_transit": 0,
                "source_sales90": 100,
                "source_sales30": 50,
            }
        ]
    )

    result = calculate_tracking(
        products,
        sales,
        beijing,
        xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates={snapshot_date},
        inventory_complete=True,
        config=TrackerConfig(),
    )

    row = result.iloc[0]
    assert row["sales30"] == 50
    assert row["sales90"] == 100
    assert row["sales30_status"] == "historical_shortage"
    assert row["sales90_status"] == "historical_shortage"
    assert "历史不足" in row["quality_labels"]


def test_uses_calculated_30_day_sales_and_fallback_90_day_sales_independently() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame([{"groupcode": "G1", "product_id": "P1"}])
    sales = pd.DataFrame(
        [
            {
                "business_date": snapshot_date - timedelta(days=offset),
                "groupcode": "G1",
                "product_id": "P1",
                "quantity": 2,
            }
            for offset in range(30)
        ]
    )
    beijing = pd.DataFrame([{"groupcode": "G1", "product_id": "P1", "beijing_available": 20}])
    xingwang = pd.DataFrame(
        [
            {
                "groupcode": "G1",
                "product_id": "P1",
                "xingwang_available": 10,
                "in_transit": 0,
                "source_sales90": 90,
                "source_sales30": 999,
            }
        ]
    )

    result = calculate_tracking(
        products,
        sales,
        beijing,
        xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates=_dates(snapshot_date, 30),
        inventory_complete=True,
        config=TrackerConfig(),
    )

    row = result.iloc[0]
    assert row["sales30"] == 60
    assert row["sales30_status"] == "calculated"
    assert row["sales90"] == 90
    assert row["sales90_status"] == "historical_shortage"
    assert row["moh30"] == 0.5
    assert row["moh90"] == 1.0


def test_partial_inventory_suppresses_stock_alerts() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame([{"groupcode": "G1", "product_id": "P1"}])
    sales = pd.DataFrame(
        [{"business_date": snapshot_date, "groupcode": "G1", "product_id": "P1", "quantity": 10}]
    )
    beijing = pd.DataFrame([{"groupcode": "G1", "product_id": "P1", "beijing_available": 20}])
    xingwang = pd.DataFrame(columns=["groupcode", "product_id", "xingwang_available", "in_transit", "source_sales90", "source_sales30"])

    result = calculate_tracking(
        products,
        sales,
        beijing,
        xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates={snapshot_date},
        inventory_complete=False,
        config=TrackerConfig(),
    )

    row = result.iloc[0]
    assert row["snapshot_status"] == "partial"
    assert row["moh30"] is None
    assert row["inventory_status"] == "待补全"
    assert "常规低库存" not in row["alert_labels"]


def test_unmatched_product_inventory_is_zero_but_still_calculated() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame(
        [
            {"groupcode": "G1", "product_id": "MISSING_BEIJING"},
            {"groupcode": "G1", "product_id": "MISSING_BOTH"},
        ]
    )
    sales = pd.DataFrame(
        [
            {
                "business_date": snapshot_date - timedelta(days=offset),
                "groupcode": "G1",
                "product_id": product_id,
                "quantity": 1,
            }
            for product_id in ("MISSING_BEIJING", "MISSING_BOTH")
            for offset in range(90)
        ]
    )
    beijing = pd.DataFrame(
        [{"groupcode": "G1", "product_id": "OTHER", "beijing_available": 99}]
    )
    xingwang = pd.DataFrame(
        [
            {
                "groupcode": "G1",
                "product_id": "MISSING_BEIJING",
                "xingwang_available": 5,
                "in_transit": None,
            },
            {
                "groupcode": "G1",
                "product_id": "OTHER",
                "xingwang_available": 10,
                "in_transit": 2,
            },
        ]
    )

    result = calculate_tracking(
        products,
        sales,
        beijing,
        xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates=_dates(snapshot_date, 90),
        inventory_complete=True,
        config=TrackerConfig(),
    ).set_index("product_id")

    missing_beijing = result.loc["MISSING_BEIJING"]
    assert missing_beijing["stock_total"] == 5
    assert missing_beijing["moh30"] == 5 / 30
    assert "北京库存未匹配" in missing_beijing["quality_labels"]
    assert "数据质量异常" in missing_beijing["alert_labels"]

    missing_both = result.loc["MISSING_BOTH"]
    assert missing_both["stock_total"] == 0
    assert missing_both["moh30"] == 0
    assert "北京库存未匹配" in missing_both["quality_labels"]
    assert "星望库存未匹配" in missing_both["quality_labels"]
    assert "无库存预警" in missing_both["alert_labels"]


def test_empty_complete_inventory_sources_keep_unmatched_quality_alerts() -> None:
    snapshot_date = date(2026, 8, 10)
    products = pd.DataFrame([{"groupcode": "G1", "product_id": "P1"}])
    empty_beijing = pd.DataFrame(columns=["groupcode", "product_id", "beijing_available"])
    empty_xingwang = pd.DataFrame(
        columns=["groupcode", "product_id", "xingwang_available", "in_transit"]
    )

    result = calculate_tracking(
        products,
        pd.DataFrame(),
        empty_beijing,
        empty_xingwang,
        snapshot_date=snapshot_date,
        imported_sales_dates=set(),
        inventory_complete=True,
        config=TrackerConfig(),
    )

    row = result.iloc[0]
    assert row["stock_total"] == 0
    assert "北京库存未匹配" in row["quality_labels"]
    assert "星望库存未匹配" in row["quality_labels"]
    assert "数据质量异常" in row["alert_labels"]


def test_reevaluate_alerts_uses_current_thresholds_without_changing_metrics() -> None:
    frame = pd.DataFrame(
        [
            {
                "snapshot_status": "complete",
                "stock_total": 10,
                "sales30": 4,
                "sales90": 4,
                "moh30": 2.5,
                "moh90": 2.5,
                "growth": 0.1,
                "quality_labels": [],
                "alert_labels": [],
            }
        ]
    )

    evaluated = reevaluate_alerts(frame, TrackerConfig(moh30_threshold=2.0, moh90_threshold=2.0))

    assert evaluated.iloc[0]["moh30"] == 2.5
    assert evaluated.iloc[0]["alert_labels"] == []
