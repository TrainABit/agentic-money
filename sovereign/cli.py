from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap, load_prices
from sovereign.markets.data import certify, fetch_closes


def _config(mode: str, data_dir: str) -> EngineConfig:
    return EngineConfig(mode=mode, data_dir=Path(data_dir))  # type: ignore[arg-type]


def cmd_init(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    print(json.dumps({"ok": True, "identity": world.identity, "wallet": world.wallet.public()}, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    ticks = args.ticks
    reports = []
    for i in range(ticks):
        r = step(world)
        reports.append(r)
        if args.verbose or (i + 1) % 5 == 0 or i == ticks - 1:
            print(
                f"tick={r['tick']} actions={r['actions']} "
                f"equity={r['equity']:.2f} revenue={r['revenue']:.2f} frozen={r['frozen']}",
                flush=True,
            )
        if args.until_revenue and r["revenue"] >= args.until_revenue:
            print(f"hit revenue target {args.until_revenue}", flush=True)
            break
        if args.mode == "live" and args.ticks >= 10**6:
            time.sleep(world.config.tick_seconds)
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    (Path(args.data_dir) / "last_run.json").write_text(json.dumps(reports[-1] if reports else {}, indent=2))
    print(json.dumps(world.status()["goals"], indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    print(json.dumps(world.status(), indent=2, default=str))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    load_prices(world)
    import numpy as np

    close = np.array(world.market_close, dtype=float)
    source = world.last_prices.get("source", "cached")
    if args.live_data:
        close, source = fetch_closes()
        world.market_close = [float(x) for x in close]
        world.last_prices["BTCUSDT"] = float(close[-1])
        world.last_prices["source"] = source
    reports = certify(close, world.config.risk)
    for r in reports:
        r["data_source"] = source
        r["n_bars"] = int(len(close))
    world.certified = reports
    world.persist_kv()
    out = world.config.paths().artifacts / "strategy_certification.json"
    out.write_text(json.dumps(reports, indent=2))
    print(json.dumps(reports, indent=2))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    print(json.dumps(world.human.all(), indent=2))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    fields = {}
    for pair in args.field:
        k, _, v = pair.partition("=")
        fields[k] = v
    item = world.human.reply(args.request_id, fields)
    print(json.dumps(item, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args.mode, args.data_dir)
    checks = []

    def add(ok: bool, name: str, detail: str) -> None:
        checks.append({"ok": ok, "name": name, "detail": detail})

    add(sys.version_info >= (3, 11), "python", sys.version.split()[0])
    claude = shutil.which(cfg.models.claude_bin)
    add(bool(claude), "claude_cli", claude or "not on PATH — run sim, or install Claude Code and `claude login`")
    world = bootstrap(cfg)
    add(True, "wallet_eth", world.wallet.public()["eth_address"])
    add(True, "wallet_sol", world.wallet.public()["sol_address"])
    add(world.router.claude.available(), "subscription_cognition", world.router.provider_name())
    add(True, "human_inbox_open", str(len(world.human.open())))
    print(json.dumps(checks, indent=2))
    return 0 if all(c["ok"] or c["name"] in {"claude_cli", "subscription_cognition"} for c in checks) else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    from sovereign.dashboard.app import serve

    serve(args.data_dir, args.mode, args.host, args.port)
    return 0


def cmd_wallet(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args.mode, args.data_dir))
    if args.reveal:
        print(world.wallet.reveal_mnemonic())
        return 0
    print(json.dumps(world.wallet.public(), indent=2))
    return 0


def _globals(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--data-dir", default="data")
    sp.add_argument("--mode", default="sim", choices=["sim", "live"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sovereign", description="Sovereign autonomous economic engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Create identity, wallets, ledger")
    _globals(s)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="Run heartbeat ticks")
    _globals(s)
    s.add_argument("--ticks", type=int, default=24)
    s.add_argument("--until-revenue", type=float, default=0.0)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="Print firm status JSON")
    _globals(s)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("backtest", help="Certify or reject strategies")
    _globals(s)
    s.add_argument("--live-data", action="store_true")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("inbox", help="Show human login requests")
    _globals(s)
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("reply", help="Inject a login/credential (not a work approval)")
    _globals(s)
    s.add_argument("request_id")
    s.add_argument("field", nargs="+", help="KEY=VALUE")
    s.set_defaults(func=cmd_reply)

    s = sub.add_parser("doctor", help="Check subscription CLI, wallet, inbox")
    _globals(s)
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("dashboard", help="Read-only observer UI")
    _globals(s)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=7474)
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("wallet", help="Print public addresses")
    _globals(s)
    s.add_argument("--reveal", action="store_true", help="Requires SOVEREIGN_CONFIRM_REVEAL=1")
    s.set_defaults(func=cmd_wallet)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
