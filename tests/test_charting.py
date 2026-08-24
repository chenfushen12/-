import math

import pandas as pd

from inventory_tracker.charting import (
    ChartMode,
    ChartSelection,
    build_chart_figure,
    format_hover_text,
    metric_statuses,
)


def test_chart_selection_defaults_and_keeps_each_mode_independent() -> None:
    selection = ChartSelection()

    assert selection.mode is ChartMode.QUANTITY
    assert selection.selected == ("sales",)
    assert selection.set_selected("sales", False) is False
    assert selection.selected == ("sales",)

    selection.set_mode(ChartMode.MOH)
    assert selection.selected == ("moh30", "moh90")
    assert selection.set_selected("moh90", False) is True
    assert selection.selected == ("moh30",)

    selection.set_mode(ChartMode.QUANTITY)
    assert selection.selected == ("sales",)

    selection.select_all()
    assert selection.selected == ("sales", "stock_total", "in_transit", "beijing_available", "xingwang_available")
    selection.reset_defaults()
    assert selection.selected == ("sales",)


def test_metric_statuses_distinguish_missing_infinity_and_negative_values() -> None:
    history = pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2026-08-01", "2026-08-02", "2026-08-03"]),
            "sales": [3, 0, -1],
            "moh30": [1.2, math.inf, None],
        }
    )

    statuses = metric_statuses(history, ("sales", "moh30"))

    assert statuses["sales"] == ("ok", "ok", "negative")
    assert statuses["moh30"] == ("ok", "infinity", "missing")


def test_build_chart_figure_is_one_axes_and_uses_quantity_bars_and_lines() -> None:
    history = pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2026-08-01", "2026-08-03"]),
            "sales": [3, 5],
            "stock_total": [10, 12],
            "in_transit": [2, 3],
            "beijing_available": [4, 5],
            "xingwang_available": [4, 4],
            "moh30": [2.0, 2.5],
            "moh90": [3.0, 3.5],
        }
    )

    figure = build_chart_figure(
        history,
        ChartSelection().selected,
        ChartMode.QUANTITY,
        title="库存与销量趋势 — G1 / P1",
        moh30_threshold=2.5,
        moh90_threshold=3.5,
    )

    assert len(figure.axes) == 1
    axis = figure.axes[0]
    assert axis.get_ylabel() == "数量"
    assert axis.get_title() == "库存与销量趋势 — G1 / P1"
    assert len(axis.patches) == 2
    assert {line.get_label() for line in axis.lines} == set()


def test_build_chart_figure_moh_includes_selected_thresholds_and_infinity_marker() -> None:
    history = pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2026-08-01", "2026-08-02"]),
            "moh30": [math.inf, 2.0],
            "moh90": [3.0, 4.0],
        }
    )

    selection = ChartSelection()
    selection.set_mode(ChartMode.MOH)
    selection.set_selected("moh90", False)
    figure = build_chart_figure(
        history,
        selection.selected,
        selection.mode,
        title="MOH趋势 — G1 / P1",
        moh30_threshold=2.5,
        moh90_threshold=3.5,
    )

    axis = figure.axes[0]
    assert axis.get_ylabel() == "MOH（月）"
    assert {line.get_label() for line in axis.lines} == {"30天MOH", "30天阈值"}
    assert any(text.get_text() == "∞" for text in axis.texts)


def test_build_chart_figure_merges_equal_moh_threshold_labels() -> None:
    history = pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2026-08-01"]),
            "moh30": [2.0],
            "moh90": [3.0],
        }
    )
    figure = build_chart_figure(
        history,
        ("moh30", "moh90"),
        ChartMode.MOH,
        title="MOH趋势 — G1 / P1",
        moh30_threshold=2.5,
        moh90_threshold=2.5,
    )

    threshold_lines = [line for line in figure.axes[0].lines if line.get_label() != "30天MOH" and line.get_label() != "90天MOH"]
    assert len(threshold_lines) == 1
    assert threshold_lines[0].get_label() == "30天/90天阈值"


def test_build_chart_figure_shows_empty_state_inside_plot() -> None:
    figure = build_chart_figure(
        pd.DataFrame(columns=["snapshot_date", "sales"]),
        ("sales",),
        ChartMode.QUANTITY,
        title="库存与销量趋势 — G1 / P1",
        empty_message="当前条件下暂无趋势数据",
    )

    assert any(text.get_text() == "当前条件下暂无趋势数据" for text in figure.axes[0].texts)


def test_format_hover_text_aggregates_selected_metrics_for_one_date() -> None:
    history = pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2026-08-01"]),
            "sales": [1234],
            "moh30": [math.inf],
            "moh90": [None],
        }
    )

    text = format_hover_text(history, ("sales", "moh30", "moh90"), 0)

    assert "2026/08/01" in text
    assert "销量：1,234" in text
    assert "30天MOH：∞" in text
    assert "90天MOH：数据不完整" in text
