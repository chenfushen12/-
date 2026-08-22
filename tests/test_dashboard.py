from inventory_tracker.dashboard import highest_alert_label, toggle_alert_filter


def test_highest_alert_ignores_quality_and_uses_severity_order() -> None:
    assert highest_alert_label(["滞销品预警", "数据质量异常", "无库存预警"]) == "无库存预警"
    assert highest_alert_label(["数据质量异常"]) is None


def test_clicking_same_alert_filter_toggles_it_off() -> None:
    assert toggle_alert_filter("全部", "常规低库存") == "常规低库存"
    assert toggle_alert_filter("常规低库存", "常规低库存") == "全部"
    assert toggle_alert_filter("增长型缺货风险", "常规低库存") == "常规低库存"
