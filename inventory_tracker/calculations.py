from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from .models import TrackerConfig

KEY_COLUMNS = ("groupcode", "product_id")
ALERT_ORDER = ("无库存预警", "增长型缺货风险", "常规低库存", "滞销品预警", "数据质量异常")


def _key(groupcode: object, product_id: object) -> tuple[str, str]:
    return str(groupcode).strip(), str(product_id).strip()


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _number(value: object) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: object) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    return pd.Timestamp(value).date()


def _sales_lookup(sales: pd.DataFrame) -> dict[tuple[date, str, str], float]:
    if sales is None or sales.empty:
        return {}
    frame = sales.copy()
    frame["business_date"] = frame["business_date"].map(_date)
    frame["groupcode"] = frame["groupcode"].map(lambda value: str(value).strip())
    frame["product_id"] = frame["product_id"].map(lambda value: str(value).strip())
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce")
    grouped = frame.groupby(["business_date", "groupcode", "product_id"], dropna=False)["quantity"].sum(min_count=1)
    return {index: float(value) for index, value in grouped.items() if not _is_missing(value)}


def _sales_dates_by_key(
    sales_lookup: dict[tuple[date, str, str], float],
) -> dict[tuple[str, str], set[date]]:
    dates_by_key: dict[tuple[str, str], set[date]] = {}
    for business_date, groupcode, product_id in sales_lookup:
        dates_by_key.setdefault((groupcode, product_id), set()).add(business_date)
    return dates_by_key


def _inventory_lookup(frame: pd.DataFrame, value_columns: Iterable[str]) -> dict[tuple[str, str], dict[str, float | None]]:
    if frame is None or frame.empty:
        return {}
    lookup: dict[tuple[str, str], dict[str, float | None]] = {}
    for _, row in frame.iterrows():
        item_key = _key(row["groupcode"], row["product_id"])
        values = lookup.setdefault(item_key, {column: None for column in value_columns})
        for column in value_columns:
            value = _number(row.get(column))
            if value is not None:
                values[column] = (values[column] or 0.0) + value
    return lookup


def _window_total(
    sales_lookup: dict[tuple[date, str, str], float],
    sales_dates_by_key: dict[tuple[str, str], set[date]],
    snapshot_date: date,
    days: int,
    item_key: tuple[str, str],
    fallback: float | None,
) -> tuple[float | None, str]:
    window = {snapshot_date - timedelta(days=offset) for offset in range(days)}
    if window.issubset(sales_dates_by_key.get(item_key, set())):
        return sum(sales_lookup.get((business_date, *item_key), 0.0) for business_date in window), "calculated"
    return fallback, "historical_shortage"


def _moh(stock_total: float | None, sales_total: float | None, *, window_months: float) -> float | None:
    if stock_total is None or sales_total is None or sales_total < 0:
        return None
    if sales_total == 0:
        return None
    return stock_total / (sales_total / window_months)


def _labels(
    *,
    stock_total: float | None,
    sales30: float | None,
    sales90: float | None,
    moh30: float | None,
    moh90: float | None,
    growth: float | None,
    inventory_complete: bool,
    config: TrackerConfig,
    has_quality_error: bool,
) -> list[str]:
    labels: set[str] = set()
    if inventory_complete and stock_total is not None:
        if stock_total <= 0:
            labels.add("无库存预警")
        if sales30 == 0 or sales90 == 0:
            labels.add("滞销品预警")

        low30 = moh30 is not None and moh30 <= config.moh30_threshold
        low90 = moh90 is not None and moh90 <= config.moh90_threshold
        low_stock = low30 or low90
        if low_stock:
            labels.add("常规低库存")
            if growth is not None and growth >= config.growth_threshold:
                labels.add("增长型缺货风险")
    if has_quality_error:
        labels.add("数据质量异常")
    return [label for label in ALERT_ORDER if label in labels]


def calculate_tracking(
    products: pd.DataFrame,
    sales: pd.DataFrame,
    beijing_inventory: pd.DataFrame,
    xingwang_inventory: pd.DataFrame,
    *,
    snapshot_date: date,
    imported_sales_dates: set[date],
    inventory_complete: bool,
    config: TrackerConfig,
    negative_sales_keys: set[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Calculate one inventory snapshot from normalized frames.

    This is the main application seam for metric and alert behavior. It does not
    read files or write a database, which keeps the business rules deterministic.
    """

    sales_lookup = _sales_lookup(sales)
    sales_dates_by_key = _sales_dates_by_key(sales_lookup)
    beijing_lookup = _inventory_lookup(beijing_inventory, ("beijing_available",))
    xingwang_lookup = _inventory_lookup(
        xingwang_inventory,
        ("xingwang_available", "in_transit", "source_sales90", "source_sales30"),
    )
    imported_dates = {_date(value) for value in imported_sales_dates}
    beijing_loaded = inventory_complete
    xingwang_loaded = inventory_complete
    rows: list[dict[str, object]] = []

    for _, product in products.iterrows():
        groupcode = str(product["groupcode"]).strip()
        product_id = str(product["product_id"]).strip()
        item_key = (groupcode, product_id)
        sales_key = (snapshot_date, *item_key)
        previous_key = (snapshot_date - timedelta(days=1), *item_key)
        has_current_date = snapshot_date in imported_dates
        has_previous_date = snapshot_date - timedelta(days=1) in imported_dates
        current_sales = sales_lookup.get(sales_key, 0.0) if has_current_date else None
        previous_sales = sales_lookup.get(previous_key, 0.0) if has_previous_date else None

        if current_sales is None:
            sales_status = "historical_missing"
        else:
            sales_status = "confirmed_no_sales" if current_sales == 0 else "available"

        growth = None
        growth_status = "history_missing"
        if current_sales is not None and previous_sales is not None:
            if current_sales > 0 and previous_sales > 0:
                growth = current_sales / previous_sales - 1
                growth_status = "calculated"
            elif previous_sales == 0:
                growth_status = "base_zero"
            else:
                growth_status = "not_positive"

        xingwang = xingwang_lookup.get(item_key, {})
        sales90, sales90_status = _window_total(
            sales_lookup,
            sales_dates_by_key,
            snapshot_date,
            90,
            item_key,
            xingwang.get("source_sales90"),
        )
        sales30, sales30_status = _window_total(
            sales_lookup,
            sales_dates_by_key,
            snapshot_date,
            30,
            item_key,
            xingwang.get("source_sales30"),
        )

        beijing = beijing_lookup.get(item_key, {})
        beijing_available = _number(beijing.get("beijing_available"))
        xingwang_available = _number(xingwang.get("xingwang_available"))
        in_transit = _number(xingwang.get("in_transit"))
        quality_labels: list[str] = []
        if beijing_loaded and item_key not in beijing_lookup:
            quality_labels.append("北京库存未匹配")
        if xingwang_loaded and item_key not in xingwang_lookup:
            quality_labels.append("星望库存未匹配")
        for value, label in (
            (beijing_available, "北京可用库存"),
            (xingwang_available, "星望可用库存"),
            (in_transit, "在途库存"),
            (sales30, "近30天销量"),
            (sales90, "近90天销量"),
        ):
            if value is not None and value < 0:
                quality_labels.append(f"{label}为负数")
        if sales30_status == "historical_shortage":
            quality_labels.append("近30天历史不足")
        if sales90_status == "historical_shortage":
            quality_labels.append("近90天历史不足")
        if "近30天历史不足" in quality_labels or "近90天历史不足" in quality_labels:
            quality_labels.append("历史不足")
        if item_key in (negative_sales_keys or set()):
            quality_labels.append("销售包含负销量")

        stock_total: float | None = None
        if inventory_complete:
            stock_total = sum(
                value if value is not None else 0.0
                for value in (beijing_available, xingwang_available, in_transit)
            )
        moh30 = _moh(stock_total, sales30, window_months=1)
        moh90 = _moh(stock_total, sales90, window_months=3)
        labels = _labels(
            stock_total=stock_total,
            sales30=sales30,
            sales90=sales90,
            moh30=moh30,
            moh90=moh90,
            growth=growth,
            inventory_complete=inventory_complete,
            config=config,
                has_quality_error=any(
                    label.endswith("未匹配") or "负数" in label or "负销量" in label for label in quality_labels
                ),
        )

        row = {column: product.get(column) for column in products.columns}
        row.update(
            {
                "snapshot_date": snapshot_date,
                "sales": current_sales,
                "sales_status": sales_status,
                "previous_sales": previous_sales,
                "growth": growth,
                "growth_status": growth_status,
                "beijing_available": beijing_available,
                "xingwang_available": xingwang_available,
                "in_transit": in_transit,
                "stock_total": stock_total,
                "sales30": sales30,
                "sales30_status": sales30_status,
                "sales90": sales90,
                "sales90_status": sales90_status,
                "moh30": moh30,
                "moh90": moh90,
                "quality_labels": quality_labels,
                "alert_labels": labels,
                "inventory_status": "complete" if inventory_complete else "待补全",
                "snapshot_status": "complete" if inventory_complete else "partial",
            }
        )
        rows.append(row)

    return pd.DataFrame(rows)


def reevaluate_alerts(frame: pd.DataFrame, config: TrackerConfig) -> pd.DataFrame:
    """Re-evaluate only alert labels using stored metrics and current thresholds."""
    result = frame.copy()
    if result.empty:
        return result
    labels: list[list[str]] = []
    for _, row in result.iterrows():
        quality = row.get("quality_labels", []) or []
        labels.append(
            _labels(
                stock_total=_number(row.get("stock_total")),
                sales30=_number(row.get("sales30")),
                sales90=_number(row.get("sales90")),
                moh30=_number(row.get("moh30")),
                moh90=_number(row.get("moh90")),
                growth=_number(row.get("growth")),
                inventory_complete=row.get("snapshot_status") == "complete",
                config=config,
                has_quality_error=any(
                    str(label).endswith("未匹配") or "负数" in str(label) for label in quality
                ),
            )
        )
    result["alert_labels"] = labels
    return result
