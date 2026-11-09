"""EOD prices from Yahoo and the raw move signals derived from them."""
import pandas as pd
import yfinance as yf

from pipeline.portfolio import INDEX, yahoo

LOOKBACK = 20  # trading days for average volume and volatility


def download(symbols: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    tickers = [yahoo(s) for s in symbols] + [INDEX]
    raw = yf.download(tickers, period=period, interval="1d", group_by="ticker",
                      auto_adjust=False, progress=False, threads=True)
    frames = {}
    for sym, tick in zip(symbols + [INDEX], tickers):
        if tick not in raw.columns.get_level_values(0):
            print(f"no price data for {sym}")
            continue
        df = raw[tick].dropna(subset=["Close"])
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        if not df.empty:
            frames[sym] = df
    return frames


def signals(df: pd.DataFrame, index: pd.DataFrame | None) -> pd.DataFrame:
    """One row per day: return, return vs index, volatility, z-score, volume multiple.

    Volatility and average volume use the previous LOOKBACK days only, so a
    big day doesn't dampen its own alert.
    """
    out = pd.DataFrame(index=df.index)
    out["ret"] = df["Close"].pct_change() * 100
    if index is not None:
        idx_ret = index["Close"].pct_change().reindex(df.index) * 100
        out["ret_vs_index"] = out["ret"] - idx_ret
    out["vol"] = out["ret"].rolling(LOOKBACK).std().shift(1)
    out["z"] = out["ret"] / out["vol"]
    out["vol_mult"] = df["Volume"] / df["Volume"].rolling(LOOKBACK).mean().shift(1)
    return out.dropna(subset=["ret"])


def rows(symbol: str, df: pd.DataFrame, sig: pd.DataFrame, since: pd.Timestamp):
    price_rows = [
        {
            "symbol": symbol, "date": d.date().isoformat(),
            "open": _num(r["Open"]), "high": _num(r["High"]), "low": _num(r["Low"]),
            "close": _num(r["Close"]), "volume": int(r["Volume"]) if pd.notna(r["Volume"]) else None,
        }
        for d, r in df[df.index >= since].iterrows()
    ]
    signal_rows = [
        {"symbol": symbol, "date": d.date().isoformat(), **{k: _num(r.get(k)) for k in
                                                          ("ret", "ret_vs_index", "vol", "z", "vol_mult")}}
        for d, r in sig[sig.index >= since].iterrows()
    ]
    return price_rows, signal_rows


def _num(x, places: int = 4):
    return None if x is None or pd.isna(x) else round(float(x), places)
