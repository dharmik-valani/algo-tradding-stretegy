from __future__ import annotations

from typing import Any


def summarize_closed_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Win rate from closed round-trips.

    win_rate = wins / (wins + losses) × 100
    Breakeven (pnl == 0) is tracked but excluded from the win-rate denominator
    so a flat scratch does not look like a loss.
    """
    pnls = [float(t.get("pnl", 0) or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flats = [p for p in pnls if p == 0]
    decided = len(wins) + len(losses)
    win_rate = (len(wins) / decided * 100.0) if decided else None
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(flats),
        "win_rate": round(win_rate, 2) if win_rate is not None else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "net_pnl": round(sum(pnls), 2) if pnls else 0.0,
        "profit_factor": (
            round(abs(sum(wins) / sum(losses)), 2) if wins and losses and sum(losses) != 0 else None
        ),
    }


def strategy_analytics_row(
    *,
    instance_id: str,
    strategy_id: str,
    name: str,
    enabled: bool,
    instrument: str,
    asset_kind: str,
    realized_pnl: float,
    unrealized_pnl: float,
    closed_trades: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = summarize_closed_trades(closed_trades)
    return {
        "instance_id": instance_id,
        "strategy_id": strategy_id,
        "name": name,
        "enabled": enabled,
        "instrument": instrument,
        "asset_kind": asset_kind,
        "option_type": (params or {}).get("option_type"),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        **stats,
    }
