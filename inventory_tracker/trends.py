from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def filter_history_window(
    history: pd.DataFrame,
    window: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """Filter existing snapshot points without interpolating missing dates."""
    if history.empty:
        return history.copy()
    frame = history.copy()
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"]).dt.date
    latest = max(frame["snapshot_date"])
    if window == "近7天":
        start_date, end_date = latest - timedelta(days=6), latest
    elif window == "近30天":
        start_date, end_date = latest - timedelta(days=29), latest
    elif window == "全部快照":
        return frame.sort_values("snapshot_date").reset_index(drop=True)
    elif window == "自定义":
        if start_date is None or end_date is None:
            raise ValueError("自定义范围需要开始日期和结束日期")
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
    else:
        raise ValueError(f"未知趋势窗口: {window}")
    return frame.loc[
        (frame["snapshot_date"] >= start_date) & (frame["snapshot_date"] <= end_date)
    ].sort_values("snapshot_date").reset_index(drop=True)
