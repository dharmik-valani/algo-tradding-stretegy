from __future__ import annotations

from datetime import date
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from algo.analysis.load import export_parquet, load_candles
from algo.config import get_settings
from algo.domain.timeframes import parse_timeframe
from algo.ingest.downloader import HistoricalDownloader
from algo.providers.base import ProviderError
from algo.providers.dhan.adapter import DhanHistoricalDataProvider
from algo.storage.db import get_engine

app = typer.Typer(help="Algo trading toolkit — historical data + paper desk.")
instruments_app = typer.Typer(help="Instrument master")
data_app = typer.Typer(help="Download, inspect, validate stored candles")
paper_app = typer.Typer(help="Paper trading (virtual fills — not Dhan Sandbox)")
app.add_typer(instruments_app, name="instruments")
app.add_typer(data_app, name="data")
app.add_typer(paper_app, name="paper")

console = Console()
IST = ZoneInfo("Asia/Kolkata")


def _downloader() -> HistoricalDownloader:
    settings = get_settings()
    get_engine(settings)
    provider = DhanHistoricalDataProvider(settings)
    return HistoricalDownloader(settings, provider)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


@app.command("doctor")
def doctor() -> None:
    """Check credentials, data-plan status, and local database."""
    settings = get_settings()
    get_engine(settings)
    token = settings.dhan_access_token
    console.print(f"Database: {settings.resolve_db_url()}")
    console.print(f"Client ID: {settings.dhan_client_id or '(missing)'}")
    console.print(f"Access token: {_mask(token)}")
    if not settings.dhan_client_id or not token:
        console.print("[yellow]Fill DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env[/yellow]")
        raise typer.Exit(code=1)
    provider = DhanHistoricalDataProvider(settings)
    try:
        profile = provider.get_profile()
    except ProviderError as exc:
        console.print(f"[red]Dhan profile failed:[/red] {exc} (code={exc.code})")
        raise typer.Exit(code=1) from exc
    console.print(f"Token validity: {profile.get('tokenValidity')}")
    console.print(f"Data plan: {profile.get('dataPlan')} until {profile.get('dataValidity')}")
    if str(profile.get("dataPlan", "")).lower() not in {"active", "true"}:
        console.print("[yellow]Data APIs may not be subscribed. Historical download will fail with DH-902 / 806.[/yellow]")


@instruments_app.command("sync")
def instruments_sync() -> None:
    """Download Dhan instrument master and store the configured index universe."""
    downloader = _downloader()
    count = downloader.sync_instruments()
    console.print(f"Synced {count} instruments from {downloader.provider.name}")


@data_app.command("download")
def data_download(
    instrument: str = typer.Option(..., "--instrument", help="Symbol, e.g. NIFTY"),
    timeframe: str = typer.Option(..., "--timeframe", help="1m, 5m, 15m, 1h, 1D"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD (exclusive for daily Dhan calls)"),
    expiry: Optional[str] = typer.Option(None, "--expiry", help="YYYY-MM-DD for futures/options"),
    strike: Optional[float] = typer.Option(None, "--strike"),
    option_type: Optional[str] = typer.Option(None, "--option-type", help="CE or PE"),
) -> None:
    downloader = _downloader()
    tf = parse_timeframe(timeframe)
    result = downloader.download(
        symbol=instrument,
        timeframe=tf,
        start=_parse_date(start),
        end=_parse_date(end),
        expiry=_parse_date(expiry) if expiry else None,
        strike=strike,
        option_type=option_type,
    )
    q = result.quality
    console.print("Download complete")
    console.print(f"Instrument: {result.instrument_id}")
    console.print(f"Rows inserted this run: {result.rows_inserted}")
    console.print(f"Chunks: {result.chunks}")
    console.print(f"Stored candles: {q.actual}")
    console.print(f"Duplicates: {q.duplicates}")
    console.print(f"Invalid: {q.invalid}")
    console.print(f"Missing vs NSE calendar: {q.missing}")
    console.print(f"Data quality: {q.status}")


@data_app.command("status")
def data_status(
    instrument: str = typer.Option(..., "--instrument"),
    timeframe: str = typer.Option("5m", "--timeframe"),
) -> None:
    downloader = _downloader()
    tf = parse_timeframe(timeframe)
    info = downloader.status(instrument, tf)
    q = info["quality"]
    console.print(instrument)
    console.print(tf.value)
    console.print("")
    console.print(f"Provider: {q.provider}")
    console.print(f"First: {_fmt_ist(info['first'])}")
    console.print(f"Last:  {_fmt_ist(info['last'])}")
    console.print("")
    console.print(f"Candles: {info['count']}")
    console.print(f"Missing: {q.missing}")
    console.print(f"Duplicates: {q.duplicates}")
    console.print(f"Invalid: {q.invalid}")
    console.print("")
    console.print(f"Status: {q.status}")


@data_app.command("validate")
def data_validate(
    instrument: str = typer.Option(..., "--instrument"),
    timeframe: str = typer.Option("5m", "--timeframe"),
) -> None:
    data_status(instrument=instrument, timeframe=timeframe)


@data_app.command("inspect")
def data_inspect(
    instrument: str = typer.Option(..., "--instrument"),
    timeframe: str = typer.Option("5m", "--timeframe"),
    rows: int = typer.Option(8, "--rows"),
) -> None:
    df = load_candles(instrument, timeframe)
    if df.empty:
        console.print("No candles stored. Run download first.")
        raise typer.Exit(code=1)
    console.print(f"{instrument} {timeframe}  rows={len(df)}")
    console.print(f"First IST: {df.index.min()}")
    console.print(f"Last  IST: {df.index.max()}")
    table = Table(title="Sample")
    table.add_column("timestamp")
    for col in ["open", "high", "low", "close", "volume"]:
        table.add_column(col)
    sample = pd_concat_head_tail(df, rows)
    for ts, row in sample.iterrows():
        table.add_row(
            str(ts),
            f"{row.open:.2f}",
            f"{row.high:.2f}",
            f"{row.low:.2f}",
            f"{row.close:.2f}",
            str(int(row.volume)),
        )
    console.print(table)
    console.print("If the first regular bar is not ~09:15 IST, check timestamp convention before backtesting.")


@data_app.command("export")
def data_export(
    instrument: str = typer.Option(..., "--instrument"),
    timeframe: str = typer.Option("5m", "--timeframe"),
) -> None:
    path = export_parquet(instrument, timeframe)
    console.print(f"Wrote {path}")


@paper_app.command("list")
def paper_list() -> None:
    """List registered paper strategies."""
    from algo.paper.registry import list_strategies

    for item in list_strategies():
        console.print(f"[bold]{item['id']}[/bold] — {item['name']}")
        console.print(f"  {item['description']}")
        console.print(f"  defaults: {item['default_params']}")


@paper_app.command("run")
def paper_run(
    strategy: str = typer.Option("ema_cross", "--strategy"),
    mode: str = typer.Option("replay", "--mode", help="replay | live"),
    instrument: str = typer.Option("NIFTY", "--instrument"),
    timeframe: str = typer.Option("5m", "--timeframe"),
    quantity: int = typer.Option(1, "--quantity"),
    cash: float = typer.Option(100_000.0, "--cash"),
    max_bars: Optional[int] = typer.Option(2500, "--max-bars"),
    poll_seconds: float = typer.Option(15.0, "--poll-seconds"),
) -> None:
    """Run one strategy in paper mode (CLI). Prefer `algo paper ui` for multi-strategy."""
    from algo.paper.session import reset_session

    get_engine(get_settings())
    session = reset_session()
    session.add_strategy(
        strategy,
        symbol=instrument,
        timeframe=timeframe,
        quantity=quantity,
        starting_cash=cash,
    )
    if mode == "replay":
        snap = session.run_replay_sync(max_bars=max_bars)
    elif mode == "live":
        session.start_live(poll_seconds=poll_seconds)
        console.print("Live paper running. Ctrl+C to stop.")
        try:
            while session.running:
                import time

                time.sleep(2)
                s = session.snapshot().strategies[0]
                console.print(
                    f"{session.message} | cash={s.cash:.2f} pnl={s.realized_pnl + s.unrealized_pnl:.2f}"
                )
        except KeyboardInterrupt:
            session.stop()
        snap = session.snapshot()
    else:
        console.print("mode must be replay or live")
        raise typer.Exit(code=1)

    for s in snap.strategies:
        console.print(f"\n[bold]{s.name}[/bold] ({s.instrument})")
        console.print(f"Cash: ₹{s.cash:,.2f}")
        console.print(f"Position: {s.position.quantity if s.position else 0}")
        console.print(f"Realized PnL: ₹{s.realized_pnl:,.2f}")
        console.print(f"Unrealized PnL: ₹{s.unrealized_pnl:,.2f}")
        console.print(f"Fills: {len(s.fills)}")
        for line in s.logs[-8:]:
            console.print(f"  {line}")


@paper_app.command("ui")
def paper_ui(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Auto-restart if the desk process exits (crash, laptop sleep, terminal close).",
    ),
) -> None:
    """Open the Paper Desk UI (multi-strategy)."""
    import subprocess
    import sys
    import time

    import uvicorn

    from algo.config import ROOT

    settings = get_settings()
    get_engine(settings)
    import os

    # Render (and most PaaS) inject PORT and expect 0.0.0.0
    env_port = os.environ.get("PORT")
    bind_host = host or (os.environ.get("HOST") if os.environ.get("RENDER") else None) or settings.paper_ui_host
    if host is None and (os.environ.get("RENDER") or env_port):
        bind_host = "0.0.0.0"
    bind_port = port or (int(env_port) if env_port else settings.paper_ui_port)

    if watch:
        console.print(f"Paper Desk (watch mode) → http://{bind_host}:{bind_port}")
        console.print("Auto-restarts on crash. Ctrl+C stops the watcher.")
        console.print("Open trades resume from checkpoint + journal.")
        while True:
            cmd = [
                sys.executable,
                "-m",
                "algo.cli",
                "paper",
                "ui",
                "--host",
                bind_host,
                "--port",
                str(bind_port),
            ]
            env = {**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(ROOT / "src")}
            try:
                code = subprocess.call(cmd, cwd=str(ROOT), env=env)
            except KeyboardInterrupt:
                console.print("Watcher stopped.")
                raise typer.Exit(0) from None
            console.print(f"[yellow]Desk exited (code={code}). Restarting in 2s…[/yellow]")
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                console.print("Watcher stopped.")
                raise typer.Exit(0) from None
        return

    console.print(f"Paper Desk → http://{bind_host}:{bind_port}")
    console.print("Virtual fills only. Dhan Sandbox is not paper trading.")
    console.print("Tip: use `algo paper ui --watch` so it auto-restarts after crashes.")
    uvicorn.run("algo.paper.web:app", host=bind_host, port=bind_port, reload=False)


def pd_concat_head_tail(df, rows: int):
    if len(df) <= rows:
        return df
    half = max(rows // 2, 1)
    return pd.concat([df.head(half), df.tail(half)])


def _fmt_ist(value) -> str:
    if value is None:
        return "-"
    return str(value.astimezone(IST))


def _mask(token: str) -> str:
    if not token:
        return "(missing)"
    if len(token) < 8:
        return "****"
    return f"{token[:4]}…{token[-4:]}"


if __name__ == "__main__":
    app()
