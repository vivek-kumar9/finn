import numpy as np
import pandas as pd

from pipeline.prices import signals


def test_signals_use_only_past_days():
    days = pd.bdate_range("2026-09-01", periods=30)
    close = pd.Series(100.0, index=days)
    close.iloc[-1] = 110.0  # +10% on the last day
    volume = pd.Series(1000, index=days)
    volume.iloc[-1] = 3000
    df = pd.DataFrame({"Close": close, "Volume": volume})
    idx = pd.DataFrame({"Close": pd.Series(np.linspace(100, 101, 30), index=days)})

    last = signals(df, idx).iloc[-1]
    assert round(last["ret"], 6) == 10.0
    assert round(last["vol_mult"], 6) == 3.0  # vs the previous 20 days, not including today
    assert last["ret_vs_index"] < 10.0


def test_todays_bar_waits_for_the_close():
    from datetime import datetime, timezone

    from pipeline.ingest import unfinished_day

    assert unfinished_day(datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc)) == "2026-11-13"  # 10:30 IST
    assert unfinished_day(datetime(2026, 11, 13, 11, 0, tzinfo=timezone.utc)) is None  # 16:30 IST
    assert unfinished_day(datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)) == "2026-11-13"  # 01:30 IST next day
