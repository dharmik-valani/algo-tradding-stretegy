# Paper trading

Dhan Sandbox is **not** paper trading. This package simulates fills with virtual cash.

- Replay historical candles tonight
- Live LTP poll during market hours (Data API) — still no real orders
- Multi-strategy desk via `algo paper ui`
- **Resilience:** desk configs in `data/paper_desk.json`, open positions in `data/paper_runtime.json` + journal. If the process dies, restart with:

```bash
algo paper ui --watch
```

`--watch` auto-restarts after crash/sleep. On boot it restores strategies, reloads open trades, and auto-resumes live paper when it was running before.

See [docs/paper-trading.md](../../../docs/paper-trading.md).
