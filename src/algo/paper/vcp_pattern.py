from __future__ import annotations

"""Volatility Contraction Pattern (VCP) helpers for daily OHLCV screens.

Detects Minervini-style setups:
  - Price stacked above EMA 200 / 50 / 20
  - Successive tighter price contractions (VCP)
  - Volume drying up near EMA 10 or 20
  - Pivot breakout level + VCP low for stop
"""

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class OhlcvBar:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class VcpSetup:
    pivot: float
    vcp_low: float
    risk_pct: float
    contractions: int
    ema10: float
    ema20: float
    ema50: float
    ema200: float
    vol_dry_ratio: float
    near_ema: str  # "10" | "20"
    score: float


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Return EMA series aligned to ``values`` (same length)."""
    if not values or period < 1:
        return []
    k = 2.0 / (period + 1)
    out: list[float] = []
    ema = float(values[0])
    for v in values:
        ema = float(v) * k + ema * (1.0 - k)
        out.append(ema)
    return out


def _swing_highs(highs: Sequence[float], *, left: int = 3, right: int = 3) -> list[int]:
    n = len(highs)
    out: list[int] = []
    for i in range(left, n - right):
        h = highs[i]
        if all(h >= highs[i - j] for j in range(1, left + 1)) and all(
            h > highs[i + j] for j in range(1, right + 1)
        ):
            out.append(i)
    return out


def _pullback_depths(
    highs: Sequence[float],
    lows: Sequence[float],
    swing_hi: Sequence[int],
) -> list[tuple[float, float, float, int, int]]:
    """Between successive swing highs, measure pullback depth to intervening low.

    Returns list of (depth_pct, swing_high, pullback_low, hi_idx, lo_idx).
    """
    out: list[tuple[float, float, float, int, int]] = []
    for a, b in zip(swing_hi, swing_hi[1:]):
        if b <= a + 1:
            continue
        segment_lows = lows[a : b + 1]
        if not segment_lows:
            continue
        lo = min(segment_lows)
        lo_idx = a + segment_lows.index(lo)
        hi = float(highs[a])
        if hi <= 0:
            continue
        depth = (hi - lo) / hi * 100.0
        if depth <= 0.2:
            continue
        out.append((depth, hi, lo, a, lo_idx))
    return out


def detect_vcp(
    bars: Sequence[OhlcvBar],
    *,
    min_contractions: int = 2,
    max_contractions: int = 4,
    lookback: int = 55,
    min_risk_pct: float = 4.0,
    max_risk_pct: float = 8.0,
    near_ema_pct: float = 3.5,
    max_vol_dry_ratio: float = 0.85,
    prefer_risk_lo: float = 5.0,
    prefer_risk_hi: float = 7.0,
) -> VcpSetup | None:
    """Return a VCP setup on the latest bar, or None if filters fail."""
    if len(bars) < 210:
        return None

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [max(0.0, float(b.volume)) for b in bars]

    e10 = ema_series(closes, 10)
    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    e200 = ema_series(closes, 200)

    i = len(bars) - 1
    px = closes[i]
    if not (px > e200[i] and px > e50[i] and px > e20[i]):
        return None
    if e50[i] < e200[i] * 0.98:
        return None

    start = max(0, i - lookback)
    wh = highs[start : i + 1]
    wl = lows[start : i + 1]
    swings = _swing_highs(wh, left=3, right=2)
    if len(swings) < min_contractions + 1:
        # Fallback: slightly looser pivots
        swings = _swing_highs(wh, left=2, right=2)
    if len(swings) < min_contractions + 1:
        return None

    pulls = _pullback_depths(wh, wl, swings)
    if len(pulls) < min_contractions:
        return None

    # Longest trailing run of tightening pullbacks (each ≤ prior × 1.08)
    best: list[tuple[float, float, float, int, int]] = []
    run: list[tuple[float, float, float, int, int]] = [pulls[0]]
    for p in pulls[1:]:
        if p[0] <= run[-1][0] * 1.08:
            run.append(p)
        else:
            if len(run) >= len(best):
                best = list(run)
            run = [p]
    if len(run) >= len(best):
        best = list(run)

    # Prefer a trailing run that ends at the most recent pullback
    trailing = [pulls[0]]
    for p in pulls[1:]:
        if p[0] <= trailing[-1][0] * 1.08:
            trailing.append(p)
        else:
            trailing = [p]
    recent = trailing[-max_contractions:]
    if len(recent) < min_contractions:
        # Fall back to best run in window if trailing is too short
        recent = best[-max_contractions:]
    if len(recent) < min_contractions:
        return None

    depths = [p[0] for p in recent]
    if not all(depths[k] <= depths[k - 1] * 1.08 for k in range(1, len(depths))):
        return None
    # Final contraction should be meaningfully tight
    if depths[-1] > max_risk_pct + 1.0:
        return None

    # Pivot = highest of last few swing highs in the base; VCP low = lowest pullback
    base = recent[-min(3, len(recent)) :]
    pivot = max(p[1] for p in base)
    # Also consider the most recent swing high after last pullback
    last_swing_local = swings[-1]
    pivot = max(pivot, float(wh[last_swing_local]))
    vcp_low = min(p[2] for p in base)
    if pivot <= vcp_low or pivot <= 0:
        return None

    # Price should still be under/near the pivot (forming, not already extended)
    if px > pivot * 1.02:
        return None

    risk_pct = (pivot - vcp_low) / pivot * 100.0
    if risk_pct < min_risk_pct or risk_pct > max_risk_pct:
        return None

    # Volume dry-up: last 10 sessions vs prior 20
    dry_n = 10
    prior_n = 20
    if i + 1 < dry_n + prior_n:
        return None
    dry_avg = sum(vols[i - dry_n + 1 : i + 1]) / dry_n
    prior_avg = sum(vols[i - dry_n - prior_n + 1 : i - dry_n + 1]) / prior_n
    if prior_avg <= 0:
        return None
    vol_dry_ratio = dry_avg / prior_avg
    if vol_dry_ratio > max_vol_dry_ratio:
        return None

    near_tol = near_ema_pct / 100.0
    dist10 = abs(px - e10[i]) / px if px else 999.0
    dist20 = abs(px - e20[i]) / px if px else 999.0
    if dist10 <= near_tol and dist10 <= dist20:
        near_ema = "10"
        near_dist = dist10
    elif dist20 <= near_tol:
        near_ema = "20"
        near_dist = dist20
    elif dist10 <= near_tol:
        near_ema = "10"
        near_dist = dist10
    else:
        return None

    prefer_mid = (prefer_risk_lo + prefer_risk_hi) / 2.0
    risk_score = max(0.0, 1.0 - abs(risk_pct - prefer_mid) / prefer_mid)
    tight_score = max(0.0, 1.0 - depths[-1] / max(depths[0], 1e-6))
    dry_score = max(0.0, 1.0 - vol_dry_ratio)
    near_score = max(0.0, 1.0 - near_dist / max(near_tol, 1e-6))
    score = 0.35 * risk_score + 0.25 * tight_score + 0.25 * dry_score + 0.15 * near_score

    # Map local indices back is not required for trade levels
    return VcpSetup(
        pivot=round(pivot, 4),
        vcp_low=round(vcp_low, 4),
        risk_pct=round(risk_pct, 3),
        contractions=len(recent),
        ema10=round(e10[i], 4),
        ema20=round(e20[i], 4),
        ema50=round(e50[i], 4),
        ema200=round(e200[i], 4),
        vol_dry_ratio=round(vol_dry_ratio, 3),
        near_ema=near_ema,
        score=round(score, 4),
    )


def breakout_volume_ok(
    bars: Sequence[OhlcvBar],
    *,
    lookback: int = 20,
    mult: float = 1.5,
) -> bool:
    """True when latest volume expands vs recent average."""
    if len(bars) < lookback + 1:
        return False
    last = float(bars[-1].volume)
    avg = sum(float(b.volume) for b in bars[-(lookback + 1) : -1]) / lookback
    if avg <= 0:
        return last > 0
    return last >= avg * mult
