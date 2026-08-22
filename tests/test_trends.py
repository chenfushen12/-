from datetime import date

import pandas as pd
import pytest

from inventory_tracker.trends import filter_history_window


@pytest.fixture
def history() -> pd.DataFrame:
    return pd.DataFrame({"snapshot_date": pd.date_range("2026-08-01", periods=40, freq="D"), "sales": range(40)})


def test_recent_windows_use_latest_available_date_and_include_boundaries(history) -> None:
    recent7 = filter_history_window(history, "近7天")
    recent30 = filter_history_window(history, "近30天")

    assert len(recent7) == 7
    assert recent7.iloc[0]["snapshot_date"] == date(2026, 9, 3)
    assert recent7.iloc[-1]["snapshot_date"] == date(2026, 9, 9)
    assert len(recent30) == 30


def test_custom_window_is_inclusive_and_skips_missing_days(history) -> None:
    sparse = history.drop(index=[10, 11])
    filtered = filter_history_window(sparse, "自定义", start_date=date(2026, 8, 9), end_date=date(2026, 8, 13))

    assert filtered["snapshot_date"].tolist() == [date(2026, 8, 9), date(2026, 8, 10), date(2026, 8, 13)]


def test_custom_window_requires_valid_order(history) -> None:
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        filter_history_window(history, "自定义", start_date=date(2026, 8, 10), end_date=date(2026, 8, 1))
