from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from .models import ImportPreview, IssueLevel, QualityReport


ALIASES: Mapping[str, tuple[str, ...]] = {
    "category": ("货品分类", "分类"),
    "groupcode": ("GROUPCODE", "GROUP CODE", "GROUPCODE(货)", "产品组"),
    "groupname": ("GROUPNAME", "GROUP NAME", "GROUPNAME(货)"),
    "product_id": ("货品编号", "产品", "产品编号"),
    "product_name": ("货品名称", "产品名称"),
    "note": ("货品备注", "备注"),
    "business_date": ("时间", "日期", "业务日期"),
    "quantity": ("数量", "销量"),
    "warehouse_code": ("库房", "库房代码"),
    "beijing_available": ("可用数",),
    "xingwang_available": ("可用库存",),
    "in_transit": ("采购在途",),
    "source_sales90": ("近90天销量(库存公式)",),
    "source_sales30": ("近30天销量",),
}


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_values(path: str | Path) -> pd.DataFrame:
    frame = pd.read_excel(path, sheet_name=0, dtype=object)
    frame = frame.dropna(how="all").copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _canonicalize(
    frame: pd.DataFrame,
    required: Iterable[str],
    report: QualityReport,
) -> pd.DataFrame:
    if frame.empty:
        for field in required:
            report.add(IssueLevel.BLOCKING, "missing_column", f"缺少必填列: {field}", field=field)
        return pd.DataFrame()

    incoming = {str(column).strip(): column for column in frame.columns}
    mapped: dict[str, str] = {}
    used_source: set[str] = set()
    for canonical, aliases in ALIASES.items():
        matches = [incoming[alias] for alias in aliases if alias in incoming]
        if len(matches) > 1:
            report.add(IssueLevel.BLOCKING, "ambiguous_column", f"无法确定列 {canonical} 的来源: {matches}", field=canonical)
        elif matches:
            mapped[canonical] = matches[0]
            used_source.add(matches[0])
    for field in required:
        if field not in mapped:
            report.add(IssueLevel.BLOCKING, "missing_column", f"缺少必填列: {field}", field=field)

    result = pd.DataFrame(index=frame.index)
    for canonical, source in mapped.items():
        result[canonical] = frame[source]
    return result


def _clean_key(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_keys(
    frame: pd.DataFrame,
    report: QualityReport,
    *,
    code: str,
    missing_level: IssueLevel = IssueLevel.WARNING,
) -> pd.DataFrame:
    result = frame.copy()
    for column in ("groupcode", "product_id"):
        numeric_values = result[column].map(lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and not pd.isna(value))
        for index in result.index[numeric_values]:
            report.add(
                IssueLevel.BLOCKING,
                "numeric_key_format",
                "商品主键以数字读取，可能已经丢失前导零；请将 Excel 列设置为文本后重试",
                row=int(index) + 2,
                field=column,
            )
        result[column] = result[column].map(_clean_key)
    missing = result["groupcode"].isna() | result["product_id"].isna()
    for index in result.index[missing]:
        missing_fields = []
        if pd.isna(result.loc[index, "groupcode"]):
            missing_fields.append("GROUPCODE")
        if pd.isna(result.loc[index, "product_id"]):
            missing_fields.append("货品编号")
        report.add(
            missing_level,
            code,
            f"第 {int(index) + 2} 行缺少商品主键（{', '.join(missing_fields)}），无法匹配追踪商品，已排除",
            row=int(index) + 2,
        )
    return result.loc[~missing].copy()


def _parse_date(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("Asia/Shanghai")
        return timestamp.date()
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _parse_numeric(series: pd.Series, report: QualityReport, *, required: bool, field: str) -> pd.Series:
    parsed = pd.to_numeric(series, errors="coerce")
    invalid = parsed.isna() & (series.notna() if not required else True)
    for index in parsed.index[invalid]:
        report.add(
            IssueLevel.BLOCKING if required else IssueLevel.WARNING,
            "invalid_number",
            f"{field} 无法转换为数字",
            row=int(index) + 2,
            field=field,
        )
    return parsed


def preview_template(path: str | Path) -> ImportPreview:
    report = QualityReport()
    raw = _read_values(path)
    frame = _canonicalize(raw, ("groupcode", "product_id"), report)
    if frame.empty and not report.blocking:
        report.add(IssueLevel.BLOCKING, "empty_template", "商品主模板没有有效数据")
    if not frame.empty:
        frame = _clean_keys(frame, report, code="template_missing_key", missing_level=IssueLevel.BLOCKING)
        if frame.empty:
            report.add(IssueLevel.BLOCKING, "empty_template", "商品主模板没有有效商品主键")
        for optional in ("category", "groupname", "product_name", "note"):
            if optional not in frame:
                frame[optional] = None
        columns = ["category", "groupcode", "groupname", "product_id", "product_name", "note"]
        frame = frame[columns]
        duplicate_mask = frame.duplicated(["groupcode", "product_id"], keep=False)
        if duplicate_mask.any():
            conflicts = (
                frame.loc[duplicate_mask]
                .groupby(["groupcode", "product_id"], dropna=False)
                .apply(lambda group: len(group.drop_duplicates()), include_groups=False)
            )
            if (conflicts > 1).any():
                report.add(IssueLevel.BLOCKING, "conflicting_template_duplicate", "同一商品键对应的商品字段不一致")
            else:
                report.add(IssueLevel.WARNING, "duplicate_template_rows", "发现完全重复的商品行，已自动去重")
            frame = frame.drop_duplicates(["groupcode", "product_id"], keep="first")
    return ImportPreview("template", str(path), file_hash(path), frame, report, metadata={"raw_rows": len(raw)})


def preview_sales(path: str | Path) -> ImportPreview:
    report = QualityReport()
    raw = _read_values(path)
    frame = _canonicalize(raw, ("business_date", "groupcode", "product_id", "quantity"), report)
    if frame.empty:
        return ImportPreview("sales", str(path), file_hash(path), frame, report)
    frame["business_date"] = frame["business_date"].map(_parse_date)
    invalid_dates = frame["business_date"].isna()
    for index in frame.index[invalid_dates]:
        report.add(IssueLevel.BLOCKING, "invalid_date", "销售日期无法解析", row=int(index) + 2, field="business_date")
    frame["quantity"] = _parse_numeric(frame["quantity"], report, required=True, field="quantity")
    valid_dates = frame.loc[~invalid_dates, "business_date"]
    imported_dates = tuple(sorted(set(valid_dates.tolist())))
    frame = frame.loc[~invalid_dates].copy()
    frame = _clean_keys(frame, report, code="sales_missing_key")
    frame = frame.dropna(subset=["quantity"])
    negative_keys = {
        (row["business_date"], str(row["groupcode"]).strip(), str(row["product_id"]).strip())
        for _, row in frame.loc[frame["quantity"] < 0].iterrows()
    }
    if not frame.empty:
        negative_count = int((frame["quantity"] < 0).sum())
        if negative_count:
            report.add(IssueLevel.WARNING, "negative_sales", f"发现 {negative_count} 行负销量/退货")
        frame = (
            frame.groupby(["business_date", "groupcode", "product_id"], as_index=False)["quantity"]
            .sum()
        )
    return ImportPreview(
        "sales",
        str(path),
        file_hash(path),
        frame,
        report,
        imported_dates,
        metadata={"negative_keys": [list(key) for key in sorted(negative_keys)]},
    )


def _preview_inventory(
    path: str | Path,
    *,
    kind: str,
    required: tuple[str, ...],
    numeric_fields: tuple[str, ...],
    sum_fields: tuple[str, ...],
    filter_codes: tuple[str, ...] | None = None,
) -> ImportPreview:
    report = QualityReport()
    raw = _read_values(path)
    frame = _canonicalize(raw, required, report)
    if frame.empty:
        return ImportPreview(kind, str(path), file_hash(path), frame, report)
    if filter_codes is not None:
        codes = frame["warehouse_code"].map(_clean_key)
        frame = frame.loc[codes.isin(filter_codes)].copy()
    frame = _clean_keys(frame, report, code="inventory_missing_key")
    for field in numeric_fields:
        frame[field] = _parse_numeric(frame[field], report, required=False, field=field)
    if not frame.empty:
        duplicate_mask = frame.duplicated(["groupcode", "product_id"], keep=False)
        if duplicate_mask.any():
            report.add(IssueLevel.WARNING, "duplicate_inventory_rows", "发现重复库存键，已按数值列求和")
        frame = frame.groupby(["groupcode", "product_id"], as_index=False)[list(sum_fields)].sum(min_count=1)
    return ImportPreview(kind, str(path), file_hash(path), frame, report)


def preview_beijing(path: str | Path, *, codes: tuple[str, ...]) -> ImportPreview:
    return _preview_inventory(
        path,
        kind="beijing",
        required=("warehouse_code", "groupcode", "product_id", "beijing_available"),
        numeric_fields=("beijing_available",),
        sum_fields=("beijing_available",),
        filter_codes=tuple(str(code).strip() for code in codes),
    )


def preview_xingwang(path: str | Path) -> ImportPreview:
    return _preview_inventory(
        path,
        kind="xingwang",
        required=("groupcode", "product_id", "xingwang_available", "in_transit", "source_sales90", "source_sales30"),
        numeric_fields=("xingwang_available", "in_transit", "source_sales90", "source_sales30"),
        sum_fields=("xingwang_available", "in_transit", "source_sales90", "source_sales30"),
    )
