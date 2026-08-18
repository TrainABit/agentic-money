# Trading — certified algos on Hyperliquid

Live execution is **Hyperliquid only**. The trader still sizes from the
certified book (`tsmom_vol`, `dual_ma`, `zscore_mr`); this document is the
venue, not a new strategy.

Sim never leaves the paper broker. A live data dir with
`trading.venue: hyperliquid` still does nothing until
`trading.hyperliquid_enabled: true`.

## Fail-closed defaults

| Flag | Default | Meaning |
| --- | --- | --- |
| `trading.venue` | `paper` | `hyperliquid` is the only live venue |
| `trading.coin` | `BTC` | Perp coin on Hyperliquid |
| `trading.hyperliquid_enabled` | `false` | No orders until the operator opts in |
| `trading.hyperliquid_testnet` | `true` | Testnet info + exchange URLs |
| `trading.hyperliquid_allow_mainnet` | `false` | Mainnet also requires this flag |
| `trading.min_order_usd` | `12` | Skip dust |
| `trading.hyperliquid_fake` | `false` | In-process fake (tests / drills) |

Mainnet without `hyperliquid_allow_mainnet` raises at broker build.
`SOVEREIGN_HL_FAKE=1` is the env equivalent of `hyperliquid_fake`.

The engine **never withdraws, never transfers, and never talks to a vault**.
Those methods exist only as stubs that raise.

## Config

```yaml
# data/config.yaml
mode: live
trading:
  venue: hyperliquid
  coin: BTC
  hyperliquid_enabled: true
  hyperliquid_testnet: true
  hyperliquid_allow_mainnet: false
  slippage: 0.01
  min_order_usd: 12
```

Install the signer extra only on a live host that will place orders:

```bash
pip install -e ".[hyperliquid]"
```

The ETH key comes from the existing wallet mnemonic (`secrets.enc`). Info
reads (mids, candles, clearinghouse state) use HTTP and do not need the SDK.
`sovereign trading` prints venue, coin, testnet, and the broker snapshot —
never keys.

Live fills mark `income.trading`. Paper and sim still mark
`income.trading_paper`.

When `venue: hyperliquid`, daily closes are fetched from Hyperliquid
`candleSnapshot` first, then the existing public-market fallback.

## Workers

Multi-process agent waves are off by default (`workers.enabled: false`) so
sim tests stay single-process.

```yaml
workers:
  enabled: true
  max_procs: 4
  in_process: [mechanic, courier]
```

```bash
sovereign serve --mode live --workers
sovereign worker --agent bookkeeper --once   # does not take engine.lock
```

Mechanic always stays in the supervisor. Hunter still runs before closer,
and closer before crafter. Spawned workers reopen the SQLite world, never
pickle `World`, and never call `start_tick` / `finish_tick`.
