"""Deterministic technical indicators over a close/high/low series. Pure Python, no numpy."""

from __future__ import annotations

from typing import Any, Optional


def sma(values: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    ef, es = ema(values, fast), ema(values, slow)
    line: list[Optional[float]] = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal) if valid else []
    sig: list[Optional[float]] = [None] * len(values)
    offset = len(values) - len(valid)
    for i, v in enumerate(sig_valid):
        sig[offset + i] = v
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    mid = sma(values, n)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1: i + 1]
        m = mid[i]
        if m is None:
            continue
        var = sum((x - m) ** 2 for x in window) / n
        sd = var ** 0.5
        upper[i], lower[i] = m + k * sd, m - k * sd
    return upper, mid, lower


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < n:
        return out
    prev = sum(trs[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(trs)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(v, nd)


def indicators(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest values of standard indicators plus per-candle series for charting."""
    closes = [c["close"] for c in candles]
    highs = [c["high"] if c.get("high") is not None else c["close"] for c in candles]
    lows = [c["low"] if c.get("low") is not None else c["close"] for c in candles]
    if len(closes) < 2:
        return {"latest": {}, "series": [], "reason": "not enough candles"}
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    r14 = rsi(closes, 14)
    m_line, m_sig, m_hist = macd(closes)
    b_up, b_mid, b_lo = bollinger(closes)
    a14 = atr(highs, lows, closes, 14)
    series = []
    for i, c in enumerate(candles):
        series.append({"date": c["date"], "close": c["close"], "sma20": _r(s20[i]), "sma50": _r(s50[i]), "sma200": _r(s200[i]),
                       "ema12": _r(e12[i]), "ema26": _r(e26[i]), "rsi14": _r(r14[i], 2), "macd": _r(m_line[i]),
                       "macd_signal": _r(m_sig[i]), "macd_hist": _r(m_hist[i]), "bb_upper": _r(b_up[i]),
                       "bb_middle": _r(b_mid[i]), "bb_lower": _r(b_lo[i]), "atr14": _r(a14[i])})
    latest = {k: v for k, v in series[-1].items() if k not in ("date", "close")}
    latest["close"] = closes[-1]
    latest["pct_vs_sma50"] = _r((closes[-1] / s50[-1] - 1) * 100, 2) if s50[-1] else None
    latest["pct_vs_sma200"] = _r((closes[-1] / s200[-1] - 1) * 100, 2) if s200[-1] else None
    return {
        "definitions": {"sma": "simple moving average of close", "ema": "exponential moving average of close",
                        "rsi14": "Wilder RSI, 14 periods", "macd": "EMA12 - EMA26, signal EMA9 of MACD",
                        "bb": "SMA20 ± 2 std dev", "atr14": "Wilder average true range, 14 periods"},
        "latest": latest,
        "series": series,
    }
