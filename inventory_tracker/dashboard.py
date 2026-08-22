from __future__ import annotations

ALERT_SEVERITY = {
    "无库存预警": 0,
    "增长型缺货风险": 1,
    "常规低库存": 2,
    "滞销品预警": 3,
    "数据质量异常": 4,
}


def highest_alert_label(labels: list[str] | tuple[str, ...] | None) -> str | None:
    candidates = [label for label in (labels or []) if label != "数据质量异常" and label in ALERT_SEVERITY]
    return min(candidates, key=ALERT_SEVERITY.get) if candidates else None


def toggle_alert_filter(current: str, clicked: str) -> str:
    return "全部" if current == clicked else clicked
