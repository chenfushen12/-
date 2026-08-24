from __future__ import annotations

import math
from enum import Enum
from typing import Iterable

import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
import numpy as np
import pandas as pd


class ChartMode(str, Enum):
    QUANTITY = "quantity"
    MOH = "moh"


QUANTITY_METRICS: tuple[str, ...] = (
    "sales",
    "stock_total",
    "in_transit",
    "beijing_available",
    "xingwang_available",
)
MOH_METRICS: tuple[str, ...] = ("moh30", "moh90")
METRICS_BY_MODE = {ChartMode.QUANTITY: QUANTITY_METRICS, ChartMode.MOH: MOH_METRICS}
METRIC_LABELS = {
    "sales": "销量",
    "stock_total": "库存总量",
    "in_transit": "在途库存",
    "beijing_available": "北京可用库存",
    "xingwang_available": "星望可用库存",
    "moh30": "30天MOH",
    "moh90": "90天MOH",
}
METRIC_COLORS = {
    "sales": "#2563eb",
    "stock_total": "#0f766e",
    "in_transit": "#9333ea",
    "beijing_available": "#d97706",
    "xingwang_available": "#dc2626",
    "moh30": "#2563eb",
    "moh90": "#9333ea",
}


class ChartSelection:
    """Stateful selection rules for the two mutually exclusive chart modes."""

    def __init__(self) -> None:
        self.mode = ChartMode.QUANTITY
        self._selected: dict[ChartMode, list[str]] = {
            ChartMode.QUANTITY: ["sales"],
            ChartMode.MOH: list(MOH_METRICS),
        }

    @property
    def selected(self) -> tuple[str, ...]:
        return tuple(self._selected[self.mode])

    def set_mode(self, mode: ChartMode) -> None:
        self.mode = ChartMode(mode)

    def set_selected(self, metric: str, selected: bool) -> bool:
        allowed = METRICS_BY_MODE[self.mode]
        if metric not in allowed:
            raise ValueError(f"指标 {metric!r} 不属于 {self.mode.value} 模式")
        current = self._selected[self.mode]
        if selected and metric not in current:
            current.append(metric)
            current.sort(key=allowed.index)
            return True
        if not selected and metric in current:
            if len(current) == 1:
                return False
            current.remove(metric)
            return True
        return False

    def select_all(self) -> None:
        self._selected[self.mode] = list(METRICS_BY_MODE[self.mode])

    def reset_defaults(self) -> None:
        self._selected[self.mode] = ["sales"] if self.mode is ChartMode.QUANTITY else list(MOH_METRICS)


def metric_statuses(history: pd.DataFrame, metrics: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Classify each point as normal, missing, infinity, or negative."""
    result: dict[str, tuple[str, ...]] = {}
    for metric in metrics:
        statuses: list[str] = []
        for value in history.get(metric, pd.Series(index=history.index, dtype=float)):
            if value is None or pd.isna(value):
                statuses.append("missing")
            elif isinstance(value, (int, float, np.number)) and math.isinf(float(value)):
                statuses.append("infinity")
            elif isinstance(value, (int, float, np.number)) and float(value) < 0:
                statuses.append("negative")
            else:
                statuses.append("ok")
        result[metric] = tuple(statuses)
    return result


def _format_metric_value(value: object, mode: ChartMode) -> str:
    if value is None or pd.isna(value):
        return "数据不完整"
    numeric = float(value)
    if math.isinf(numeric):
        return "∞"
    if numeric < 0:
        return f"异常负值：{numeric:g}"
    return f"{numeric:,.1f}" if mode is ChartMode.MOH else f"{numeric:,.0f}"


def format_hover_text(history: pd.DataFrame, metrics: Iterable[str], index: int) -> str:
    """Return the date and every selected metric for one hover position."""
    if index < 0 or index >= len(history):
        raise IndexError(index)
    selected_metrics = tuple(metrics)
    snapshot_date = pd.to_datetime(history.iloc[index]["snapshot_date"]).strftime("%Y/%m/%d")
    mode = ChartMode.MOH if any(metric in MOH_METRICS for metric in selected_metrics) else ChartMode.QUANTITY
    lines = [snapshot_date]
    for metric in selected_metrics:
        value = history.iloc[index].get(metric)
        lines.append(f"{METRIC_LABELS[metric]}：{_format_metric_value(value, mode)}")
    return "\n".join(lines)


def _finite_values(history: pd.DataFrame, metric: str) -> np.ndarray:
    values = pd.to_numeric(history.get(metric, pd.Series(index=history.index, dtype=float)), errors="coerce")
    return values.where(np.isfinite(values) & (values >= 0), np.nan).to_numpy(dtype=float)


def _add_special_markers(axis, dates, history: pd.DataFrame, metric: str, color: str) -> None:
    values = history.get(metric, pd.Series(index=history.index, dtype=float))
    for index, value in enumerate(values):
        if value is None or pd.isna(value):
            continue
        numeric = float(value)
        if math.isinf(numeric):
            axis.annotate("∞", (dates.iloc[index], 0), xytext=(0, 8), textcoords="offset points", color=color, ha="center")
        elif numeric < 0:
            axis.scatter([dates.iloc[index]], [0], marker="x", color=color, zorder=5)
            axis.annotate(f"异常负值：{numeric:g}", (dates.iloc[index], 0), xytext=(0, -16), textcoords="offset points", color=color, ha="center", fontsize=8)


def build_chart_figure(
    history: pd.DataFrame,
    selected: Iterable[str],
    mode: ChartMode,
    *,
    title: str,
    moh30_threshold: float | None = None,
    moh90_threshold: float | None = None,
    empty_message: str | None = None,
) -> Figure:
    """Build the single-axis trend figure used by the Tk adapter."""
    selected_metrics = tuple(selected)
    figure = Figure(figsize=(12, 5.4), dpi=90)
    axis = figure.add_subplot(1, 1, 1)
    axis.set_title(title)
    axis.set_ylabel("数量" if mode is ChartMode.QUANTITY else "MOH（月）")
    axis.grid(alpha=0.25)
    axis.set_ylim(bottom=0)
    if empty_message:
        axis.text(0.5, 0.5, empty_message, transform=axis.transAxes, ha="center", va="center", color="#666666")

    dates = pd.to_datetime(history.get("snapshot_date", pd.Series(dtype="datetime64[ns]")))
    statuses = metric_statuses(history, selected_metrics)
    for metric in selected_metrics:
        color = METRIC_COLORS[metric]
        values = _finite_values(history, metric)
        label = METRIC_LABELS[metric]
        if mode is ChartMode.QUANTITY and metric == "sales":
            axis.bar(dates, values, width=0.8, alpha=0.42, color=color, label=label, zorder=1)
        else:
            linestyle = "--" if mode is ChartMode.MOH and metric == "moh90" else "-"
            axis.plot(dates, values, marker="o", color=color, linestyle=linestyle, label=label, zorder=3)
        _add_special_markers(axis, dates, history, metric, color)
        statuses_for_metric = statuses.get(metric, ())
        if statuses_for_metric and not any(status in {"ok", "infinity", "negative"} for status in statuses_for_metric):
            axis.text(0.5, 0.5, f"{METRIC_LABELS[metric]}：暂无数据", transform=axis.transAxes, ha="center", va="center", color=color)

    if mode is ChartMode.MOH:
        thresholds = (("moh30", moh30_threshold), ("moh90", moh90_threshold))
        visible = [(metric, value) for metric, value in thresholds if metric in selected_metrics and value is not None]
        grouped: dict[float, list[str]] = {}
        for metric, value in visible:
            grouped.setdefault(float(value), []).append(metric)
        for value, metrics in grouped.items():
            names = "/".join(METRIC_LABELS[metric].replace("MOH", "") + "阈值" for metric in metrics)
            color = METRIC_COLORS[metrics[0]]
            axis.axhline(value, color=color, linestyle=":", alpha=0.72, label=names)
            axis.annotate(names, xy=(1, value), xycoords=("axes fraction", "data"), xytext=(-4, 4), textcoords="offset points", ha="right", color=color, fontsize=8)

    locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
    axis.xaxis.set_major_locator(locator)
    axis.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    if mode is ChartMode.QUANTITY:
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    else:
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.1f}"))
    if selected_metrics:
        handles, labels = axis.get_legend_handles_labels()
        selected_labels = [METRIC_LABELS[metric] for metric in selected_metrics]
        visible_handles = [handle for handle, label in zip(handles, labels) if label in selected_labels]
        visible_labels = [label for label in labels if label in selected_labels]
        axis.legend(visible_handles, visible_labels, loc="lower left", bbox_to_anchor=(0, 1.01), ncol=min(5, len(selected_metrics)), frameon=False, borderaxespad=0)
    figure.tight_layout()
    return figure
