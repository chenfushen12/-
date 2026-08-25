from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from numbers import Number
from pathlib import Path
from typing import Any

import pandas as pd

from .models import ImportPreview, IssueLevel, QualityReport


_DEGREE_PATTERN = re.compile(r"-(\d{4})(?:\dP|(?=[^\d]|$))")


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
    "old_groupcode": ("老编号", "旧编号", "旧 Group code", "OLD GROUPCODE"),
    "new_groupcode": ("新编号", "新 Group code", "NEW GROUPCODE"),
    "mapping_name": ("名字", "名称"),
}


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_values(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    if source.suffix.lower() == ".csv":
        frame = pd.read_csv(path, dtype=object, encoding="utf-8-sig")
    else:
        frame = pd.read_excel(path, sheet_name=0, dtype=object)
    frame = frame.dropna(how="all").copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _canonicalize(
    frame: pd.DataFrame,
    required: Iterable[str],
    report: QualityReport,
    *,
    preferred_sources: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    if frame.empty:
        for field in required:
            report.add(IssueLevel.BLOCKING, "missing_column", f"缺少必填列: {field}", field=field)
        return pd.DataFrame()

    incoming = {str(column).strip(): column for column in frame.columns}
    mapped: dict[str, str] = {}
    used_source: set[str] = set()
    for canonical, aliases in ALIASES.items():
        preferred = (preferred_sources or {}).get(canonical)
        if preferred is not None:
            matches = [incoming[preferred]] if preferred in incoming else []
        else:
            matches = [incoming[alias] for alias in aliases if alias in incoming]
        if len(matches) > 1:
            report.add(IssueLevel.BLOCKING, "ambiguous_column", f"无法确定列 {canonical} 的来源: {matches}", field=canonical)
        elif matches:
            mapped[canonical] = matches[0]
            used_source.add(matches[0])
    for field in required:
        if field not in mapped:
            report.add(IssueLevel.BLOCKING, "missing_column", f"缺少必填列: {field}", field=field)

    if any(field not in mapped for field in required):
        return pd.DataFrame()

    result = pd.DataFrame(index=frame.index)
    source_columns: dict[str, tuple[int, str]] = {}
    for canonical, source in mapped.items():
        result[canonical] = frame[source]
        source_columns[canonical] = (list(frame.columns).index(source) + 1, str(source))
    result.attrs["source_columns"] = source_columns
    return result


def _is_numeric_key(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (Number, Decimal)):
        return False
    try:
        return not bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _clean_key(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    if _is_numeric_key(value):
        try:
            numeric = Decimal(str(value))
            if numeric.is_finite():
                return format(numeric.normalize(), "f")
        except (InvalidOperation, ValueError):
            pass
    cleaned = str(value).strip()
    return cleaned or None


def _clean_keys(
    frame: pd.DataFrame,
    report: QualityReport,
    *,
    code: str,
    source_name: str,
    missing_level: IssueLevel = IssueLevel.WARNING,
) -> pd.DataFrame:
    result = frame.copy()
    source_columns = frame.attrs.get("source_columns", {})
    for column in ("groupcode", "product_id"):
        numeric_values = result[column].map(_is_numeric_key)
        numeric_rows = list(result.index[numeric_values])
        if numeric_rows:
            column_number, source_header = source_columns.get(column, ("?", column))
            excel_rows = [int(index) + 2 for index in numeric_rows]
            shown_rows = "、".join(str(row) for row in excel_rows[:5])
            if len(excel_rows) > 5:
                shown_rows += " 等"
            report.add(
                IssueLevel.WARNING,
                "numeric_key_converted",
                f"{source_name} 的第 {column_number} 列「{source_header}」发现 {len(numeric_rows)} 个数字商品主键"
                f"（Excel 行 {shown_rows}），已自动转换为文本；若原编号包含前导零，Excel 可能已经将其丢失，请核对",
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


def preview_code_mapping(path: str | Path) -> ImportPreview:
    """Preview a Group code migration table without touching business data."""
    report = QualityReport()
    raw = _read_values(path)
    frame = _canonicalize(raw, ("old_groupcode", "new_groupcode"), report)
    if frame.empty:
        if not report.blocking:
            report.add(IssueLevel.BLOCKING, "empty_code_mapping", "新旧编码替换表没有有效映射")
        return ImportPreview("code_mapping", str(path), file_hash(path), frame, report, metadata={"raw_rows": len(raw)})

    for column in ("old_groupcode", "new_groupcode"):
        frame[column] = frame[column].map(_clean_key)
    missing = frame["old_groupcode"].isna() | frame["new_groupcode"].isna()
    for index in frame.index[missing]:
        missing_fields = []
        if pd.isna(frame.loc[index, "old_groupcode"]):
            missing_fields.append("老编号")
        if pd.isna(frame.loc[index, "new_groupcode"]):
            missing_fields.append("新编号")
        report.add(
            IssueLevel.BLOCKING,
            "mapping_missing_key",
            f"第 {int(index) + 2} 行缺少替换主键（{', '.join(missing_fields)}）",
            row=int(index) + 2,
        )
    frame = frame.loc[~missing].copy()
    if "mapping_name" not in frame:
        frame["mapping_name"] = None
    frame = frame.rename(columns={"mapping_name": "name"})[["old_groupcode", "new_groupcode", "name"]]
    duplicate_old = frame.duplicated("old_groupcode", keep=False)
    if duplicate_old.any():
        conflicts = frame.loc[duplicate_old].groupby("old_groupcode")["new_groupcode"].nunique()
        if (conflicts > 1).any():
            report.add(IssueLevel.BLOCKING, "conflicting_code_mapping", "同一老 Group code 对应多个新 Group code")
        else:
            report.add(IssueLevel.WARNING, "duplicate_code_mapping", "发现重复的新旧编码映射，已自动去重")
        frame = frame.drop_duplicates("old_groupcode", keep="first")
    mapping = dict(zip(frame["old_groupcode"], frame["new_groupcode"], strict=False))
    for start in mapping:
        seen: set[str] = set()
        current = start
        while current in mapping:
            if current in seen:
                report.add(IssueLevel.BLOCKING, "cyclic_code_mapping", f"发现 Group code 替换循环: {start}")
                break
            seen.add(current)
            current = mapping[current]
        if any(issue.code == "cyclic_code_mapping" for issue in report.blocking):
            break
    self_mapped = frame["old_groupcode"] == frame["new_groupcode"]
    if self_mapped.any():
        report.add(IssueLevel.WARNING, "self_code_mapping", "发现老、新 Group code 相同的映射，已保留为无变化映射")
    return ImportPreview("code_mapping", str(path), file_hash(path), frame, report, metadata={"raw_rows": len(raw)})


def preview_template(path: str | Path) -> ImportPreview:
    report = QualityReport()
    raw = _read_values(path)
    frame = _canonicalize(raw, ("groupcode", "product_id"), report)
    if frame.empty and not report.blocking:
        report.add(IssueLevel.BLOCKING, "empty_template", "商品主模板没有有效数据")
    if not frame.empty:
        frame = _clean_keys(
            frame,
            report,
            code="template_missing_key",
            source_name=Path(path).name,
            missing_level=IssueLevel.BLOCKING,
        )
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
    frame = _clean_keys(frame, report, code="sales_missing_key", source_name=Path(path).name)
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
    preferred_sources: Mapping[str, str] | None = None,
) -> ImportPreview:
    report = QualityReport()
    raw = _read_values(path)
    frame = _canonicalize(raw, required, report, preferred_sources=preferred_sources)
    if frame.empty:
        return ImportPreview(kind, str(path), file_hash(path), frame, report)
    if filter_codes is not None:
        codes = frame["warehouse_code"].map(_clean_key)
        frame = frame.loc[codes.isin(filter_codes)].copy()
    frame = _clean_keys(frame, report, code="inventory_missing_key", source_name=Path(path).name)
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
        preferred_sources={"groupcode": "产品组", "product_id": "产品"},
    )


def preview_xingwang(path: str | Path) -> ImportPreview:
    return _preview_inventory(
        path,
        kind="xingwang",
        required=("groupcode", "product_id", "xingwang_available", "in_transit", "source_sales90", "source_sales30"),
        numeric_fields=("xingwang_available", "in_transit", "source_sales90", "source_sales30"),
        sum_fields=("xingwang_available", "in_transit", "source_sales90", "source_sales30"),
    )


def _extract_degree(product_id: object) -> str | None:
    value = _clean_key(product_id)
    if value is None:
        return None
    matches = _DEGREE_PATTERN.findall(value)
    return matches[-1] if matches else None


def _copy_report(report: QualityReport) -> QualityReport:
    copied = QualityReport()
    copied.extend(report)
    return copied


def _mapping_targets(
    template: pd.DataFrame,
    mapping: dict[str, str],
    report: QualityReport,
) -> dict[tuple[str, str], str]:
    targets: dict[tuple[str, str], str] = {}
    target_groups = set(mapping.values())
    for _, row in template.iterrows():
        groupcode = _clean_key(row.get("groupcode"))
        product_id = _clean_key(row.get("product_id"))
        degree = _extract_degree(product_id)
        if groupcode is None or product_id is None or degree is None or groupcode not in target_groups:
            continue
        key = (groupcode, degree)
        previous = targets.get(key)
        if previous is not None and previous != product_id:
            report.add(IssueLevel.BLOCKING, "duplicate_target_degree", f"新 Group code {groupcode} 的度数 {degree} 对应多个货品编码")
        else:
            targets[key] = product_id
    return targets


def _canonical_item_key(
    groupcode: object,
    product_id: object,
    mapping: dict[str, str],
    targets: dict[tuple[str, str], str],
    report: QualityReport | None = None,
    *,
    row_number: int | None = None,
) -> tuple[str | None, str | None, bool]:
    source_group = _clean_key(groupcode)
    source_product = _clean_key(product_id)
    if source_group is None or source_product is None:
        return source_group, source_product, False
    target_group = mapping.get(source_group)
    if target_group is None:
        return source_group, source_product, False
    degree = _extract_degree(source_product)
    target_product = targets.get((target_group, degree)) if degree is not None else None
    if target_product is None:
        if report is not None:
            detail = f"{source_group}/{source_product} 找不到新组 {target_group} 的对应度数"
            report.add(IssueLevel.WARNING, "missing_mapping_target", detail, row=row_number, field="product_id")
        return source_group, source_product, False
    return target_group, target_product, True


def _normalize_frame(
    frame: pd.DataFrame,
    *,
    kind: str,
    mapping: dict[str, str],
    targets: dict[tuple[str, str], str],
    report: QualityReport,
) -> pd.DataFrame:
    if frame.empty or not mapping:
        return frame.copy()
    result = frame.copy()
    source_group = result["groupcode"].map(_clean_key)
    source_product = result["product_id"].map(_clean_key)
    mapped_group = source_group.map(mapping)
    degree = source_product.astype("string").str.findall(_DEGREE_PATTERN).str[-1]
    separator = "\x1f"
    target_lookup = {f"{group}{separator}{item_degree}": product for (group, item_degree), product in targets.items()}
    target_product = (mapped_group.astype("string") + separator + degree.fillna("")).map(target_lookup)
    canonicalized = mapped_group.notna() & target_product.notna()
    unresolved = mapped_group.notna() & ~canonicalized
    if unresolved.any():
        unresolved_rows = pd.DataFrame(
            {
                "source_group": source_group[unresolved],
                "source_product": source_product[unresolved],
                "target_group": mapped_group[unresolved],
                "degree": degree[unresolved],
            }
        ).drop_duplicates()
        for index, row in unresolved_rows.iterrows():
            detail = f"{row['source_group']}/{row['source_product']} 找不到新组 {row['target_group']} 的对应度数"
            report.add(IssueLevel.WARNING, "missing_mapping_target", detail, field="product_id")
    result["groupcode"] = mapped_group.where(canonicalized, source_group)
    result["product_id"] = target_product.where(canonicalized, source_product)
    if kind == "template":
        result = result.loc[~unresolved].copy()

    if kind == "template":
        result["_priority"] = canonicalized.loc[result.index].astype(int)
        result = result.sort_values("_priority").drop(columns=["_priority"])
        duplicate_mask = result.duplicated(["groupcode", "product_id"], keep=False)
        if duplicate_mask.any():
            result = result.drop_duplicates(["groupcode", "product_id"], keep="first")
    elif kind == "sales":
        result = result.groupby(["business_date", "groupcode", "product_id"], as_index=False)["quantity"].sum()
    elif kind == "beijing":
        result = result.groupby(["groupcode", "product_id"], as_index=False)["beijing_available"].sum(min_count=1)
    elif kind == "xingwang":
        fields = ["xingwang_available", "in_transit", "source_sales90", "source_sales30"]
        result = result.groupby(["groupcode", "product_id"], as_index=False)[fields].sum(min_count=1)
    return result


def normalize_import_previews(
    template: ImportPreview,
    sales: ImportPreview,
    beijing: ImportPreview,
    xingwang: ImportPreview,
    code_mapping: ImportPreview | None = None,
) -> tuple[ImportPreview, ImportPreview, ImportPreview, ImportPreview]:
    """Build dashboard-facing keys while retaining each preview's source frame."""
    if code_mapping is None or code_mapping.frame.empty:
        return template, sales, beijing, xingwang
    mapping = dict(zip(code_mapping.frame["old_groupcode"], code_mapping.frame["new_groupcode"], strict=False))
    mapping_report = QualityReport()
    targets = _mapping_targets(template.frame, mapping, mapping_report)
    normalized: list[ImportPreview] = []
    for preview in (template, sales, beijing, xingwang):
        preview_report = _copy_report(preview.report)
        frame = _normalize_frame(
            preview.frame,
            kind=preview.kind,
            mapping=mapping,
            targets=targets,
            report=preview_report,
        )
        metadata = dict(preview.metadata)
        metadata.setdefault("raw_frame", preview.frame.copy())
        metadata["mapping_applied"] = True
        if preview.kind == "sales":
            negative_keys = []
            for value in preview.metadata.get("negative_keys", []):
                groupcode, product_id, changed = _canonical_item_key(value[1], value[2], mapping, targets)
                negative_keys.append([value[0], groupcode, product_id])
            metadata["negative_keys"] = negative_keys
        normalized.append(
            ImportPreview(
                preview.kind,
                preview.source_path,
                preview.file_hash,
                frame,
                preview_report,
                preview.imported_dates,
                metadata,
            )
        )
    if mapping_report.issues:
        normalized[0].report.extend(mapping_report)
    return tuple(normalized)  # type: ignore[return-value]
