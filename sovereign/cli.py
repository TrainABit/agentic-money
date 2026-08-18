from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

from sovereign.agents.spec import AGENT_SPECS, spec_for
from sovereign.comms.bus import STATUSES as COMMS_STATUSES
from sovereign.config import EngineConfig
from sovereign.debug import TraceCollector
from sovereign.engine.heartbeat import TICK_METRICS_KEY, step
from sovereign.engine.world import bootstrap, load_prices
from sovereign.markets.data import certify, fetch_closes
from sovereign.ops import healthcheck, maintain, readiness


def _config(args: argparse.Namespace) -> EngineConfig:
    data_dir = Path(args.data_dir)
    config_path = data_dir / "config.yaml"
    values: dict[str, object] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text())
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
        values.update(loaded or {})
    # Explicit CLI globals always win over persisted configuration.
    values.update({"mode": args.mode, "data_dir": data_dir})
    cfg = EngineConfig.model_validate(values)
    if getattr(args, "realistic", False):
        cfg.sim.realism = True
        cfg.sim.close_rate = 0.55
        cfg.sim.pay_delay_ticks = 1
        cfg.sim.auto_accept = False
        cfg.sim.autocollect = False
    return cfg


def cmd_init(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    print(json.dumps({"ok": True, "identity": world.identity, "wallet": world.wallet.public()}, indent=2))
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    from sovereign.heal.repair import setup as heal_setup

    heal_setup(world, full=True)
    world.persist_kv()
    report = readiness(world)
    print(
        json.dumps(
            {
                "readiness": report,
                "identity": world.identity,
                "wallet": world.wallet.public(),
            },
            indent=2,
            default=str,
        )
    )
    return 0 if report["ready"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    from sovereign.engine.daemon import FileLock

    cfg = _config(args)
    lock = FileLock(cfg.paths().lock)
    try:
        lock.acquire()
    except RuntimeError as e:
        print(str(e))
        return 1
    try:
        world = bootstrap(cfg)
        ticks = args.ticks
        reports = []
        for i in range(ticks):
            r = step(world)
            reports.append(r)
            if args.verbose or (i + 1) % 5 == 0 or i == ticks - 1:
                print(
                    f"tick={r['tick']} actions={r['actions']} "
                    f"equity={r['equity']:.2f} revenue={r['revenue']:.2f} "
                    f"trailing={r.get('trailing', 0):.2f} pipeline={r.get('pipeline')}",
                    flush=True,
                )
            target = args.until_revenue
            if target and r.get("trailing", r["revenue"]) >= target:
                print(f"hit revenue target {target}", flush=True)
                break
            if args.mode == "live":
                time.sleep(world.config.tick_seconds)
        Path(args.data_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.data_dir) / "last_run.json").write_text(json.dumps(reports[-1] if reports else {}, indent=2, default=str))
        print(json.dumps(world.status()["goals"], indent=2))
        return 0
    finally:
        lock.release()


def cmd_status(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    print(json.dumps(world.status(), indent=2, default=str))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
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
    world = bootstrap(_config(args))
    print(json.dumps(world.human.all(), indent=2))
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    fields = {}
    for pair in args.field:
        k, _, v = pair.partition("=")
        if v == "-":
            v = sys.stdin.readline().rstrip("\n")
        fields[k] = v
    world.human.reply(args.request_id, fields)
    from sovereign.channels.replies import consume

    consume(world)
    world.persist_kv()
    shown = next((i for i in world.human.all() if i["id"] == args.request_id), {"id": args.request_id, "status": "filled"})
    print(json.dumps(shown, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args)
    checks = []

    def add(ok: bool, name: str, detail: str) -> None:
        checks.append({"ok": ok, "name": name, "detail": detail})

    add(sys.version_info >= (3, 11), "python", sys.version.split()[0])
    claude = shutil.which(cfg.models.claude_bin)
    add(bool(claude), "claude_cli", claude or "not on PATH — run sim, or install Claude Code and `claude login`")
    world = bootstrap(cfg)
    from sovereign.heal.checks import diagnose
    from sovereign.heal.repair import setup as heal_setup

    add(True, "wallet_eth", world.wallet.public()["eth_address"])
    add(True, "wallet_sol", world.wallet.public()["sol_address"])
    add(world.router.claude.available(), "subscription_cognition", world.router.provider_name())
    add(True, "human_inbox_open", str(len(world.human.open())))
    add(world.tools is not None, "tools_bound", str(len(world.tools.names()) if world.tools else 0))
    findings = diagnose(world)
    if getattr(args, "fix", False):
        health = heal_setup(world, full=True)
        world.persist_kv()
    else:
        health = {
            "healthy": all(f.ok for f in findings),
            "findings": [f.as_dict() for f in findings],
            "repairs": [],
        }
    print(json.dumps({"checks": checks, "health": health}, indent=2, default=str))
    health_ok = bool(health.get("healthy"))
    checks_ok = all(c["ok"] or c["name"] in {"claude_cli", "subscription_cognition"} for c in checks)
    return 0 if checks_ok and (health_ok or not getattr(args, "fix", False)) else 1


def cmd_setup(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    from sovereign.heal.repair import setup as heal_setup

    report = heal_setup(world, full=True)
    world.persist_kv()
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("healthy") else 1


def cmd_tools(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    if world.tools is None:
        print(json.dumps({"ok": False, "error": "tools unbound"}, indent=2))
        return 1
    agent = getattr(args, "agent", None)
    if agent:
        print(json.dumps({"agent": agent, "tools": world.tools.available_to(agent)}, indent=2))
    else:
        print(json.dumps(world.tools.manifest(), indent=2))
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))

    def entry(name: str, *, with_prompt: bool) -> dict[str, object]:
        spec = spec_for(name)
        tools = (
            world.tools.available_to(name)
            if world.tools is not None
            else sorted(spec.tools)
        )
        item: dict[str, object] = {
            "name": spec.name,
            "mission": spec.mission,
            "tier": spec.tier,
            "tools": tools,
            "handles": list(spec.handles),
            "frozen": name in world.frozen,
            "inbox_queued": len(
                world.store.messages(recipient=name, status="queued", limit=None)
            ),
        }
        if with_prompt:
            item["system_prompt"] = spec.system_prompt
        return item

    if args.agent:
        if args.agent not in AGENT_SPECS:
            raise KeyError(
                f"unknown agent {args.agent!r}; roster: {', '.join(sorted(AGENT_SPECS))}"
            )
        print(json.dumps(entry(args.agent, with_prompt=True), indent=2))
    else:
        print(json.dumps([entry(n, with_prompt=False) for n in sorted(AGENT_SPECS)], indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from sovereign.dashboard.app import serve

    serve(args.data_dir, args.mode, args.host, args.port)
    return 0


def cmd_wallet(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    if args.reveal:
        print(world.wallet.reveal_mnemonic())
        return 0
    print(json.dumps(world.wallet.public(), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from sovereign.engine.daemon import serve

    cfg = _config(args)
    if getattr(args, "workers", False):
        cfg.workers.enabled = True
    serve(cfg, ticks=args.ticks, verbose=True, force=args.force)
    return 0


def cmd_trading(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args), heal=False)
    snap = dict(world.broker.snapshot())
    forbidden = ("key", "secret", "mnemonic", "private")
    for field in list(snap):
        lowered = str(field).lower()
        if any(token in lowered for token in forbidden):
            snap.pop(field, None)
    payload = {
        "venue": world.config.trading.venue,
        "coin": world.config.trading.coin,
        "hyperliquid_enabled": world.config.trading.hyperliquid_enabled,
        "testnet": world.config.trading.hyperliquid_testnet,
        "allow_mainnet": world.config.trading.hyperliquid_allow_mainnet,
        "broker": snap,
        "workers": {
            "enabled": world.config.workers.enabled,
            "max_procs": world.config.workers.max_procs,
            "in_process": list(world.config.workers.in_process),
        },
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from sovereign.engine.workers import PIPELINE_NAMES, run_standalone_agent

    if args.agent not in PIPELINE_NAMES:
        raise KeyError(
            f"unknown agent {args.agent!r}; roster: {', '.join(PIPELINE_NAMES)}"
        )
    result = run_standalone_agent(_config(args), args.agent)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_backup(args: argparse.Namespace) -> int:
    from sovereign.backup import create_backup, restore_drill, verify_backup

    chosen = [flag for flag in (args.out, args.verify, args.restore_drill) if flag]
    if len(chosen) != 1:
        raise ValueError(
            "--out, --verify, and --restore-drill are mutually exclusive; "
            "pass exactly one of --out DIR, --verify DIR, or --restore-drill DIR"
        )
    if args.verify:
        report = verify_backup(Path(args.verify))
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1
    if args.restore_drill:
        report = restore_drill(_config(args), Path(args.restore_drill))
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1
    manifest = create_backup(
        _config(args), Path(args.out), include_secrets=not args.no_secrets
    )
    print(json.dumps(manifest, indent=2, default=str))
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    try:
        world = bootstrap(_config(args), heal=False)
        report = healthcheck(world, max_staleness_seconds=args.stale_seconds)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "ready": False,
                    "stale": args.stale_seconds is not None,
                    "reasons": [str(exc) or type(exc).__name__],
                },
                indent=2,
            )
        )
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


def cmd_maintain(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    report = maintain(
        world,
        vacuum=False if args.no_vacuum else None,
        comms_days=args.comms_days,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("ok") else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    from sovereign.memory.store import CURRENT_SCHEMA_VERSION

    world = bootstrap(_config(args))
    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": world.store.schema_version(),
                "current": CURRENT_SCHEMA_VERSION,
                "history": world.store.schema_history(),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_rotate_key(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise ValueError("rotating the master key requires --confirm (stop the daemon first)")
    world = bootstrap(_config(args))
    if args.to_keyring:
        from sovereign.capital.wallet import KeyringMasterKeyStore

        report = world.wallet.migrate_key_store(
            KeyringMasterKeyStore(
                service=world.config.wallet.keyring_service,
                username=world.config.wallet.keyring_username,
            ),
            delete_old_file=args.delete_old_file,
        )
    else:
        report = world.wallet.rotate_master_key()
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


_COMMS_FIELDS = ("id", "ts", "kind", "sender", "recipient", "status", "attempts", "error")


def _comms_row(record: dict[str, object]) -> dict[str, object]:
    """Payload-free projection of one messages row for operator output."""
    return {field: record.get(field) for field in _COMMS_FIELDS}


def cmd_comms(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    bus = world.comms
    if args.requeue:
        message = bus.requeue(args.requeue, now=world.now)
        print(json.dumps(_comms_row(message.to_record()), indent=2, default=str))
        return 0
    if args.purge_days is not None:
        pruned = bus.prune(now=world.now, older_than_days=args.purge_days)
        print(json.dumps({"pruned": pruned}, indent=2))
        return 0
    if args.status is not None and args.status not in COMMS_STATUSES:
        raise ValueError(
            f"unknown status {args.status!r}; expected one of "
            f"{', '.join(sorted(COMMS_STATUSES))}"
        )
    if args.limit <= 0:
        raise ValueError("--limit must be a positive integer")
    rows = world.store.messages(status=args.status, limit=None)
    newest_first = list(reversed(rows))[: args.limit]
    print(json.dumps([_comms_row(row) for row in newest_first], indent=2, default=str))
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    from sovereign.labor.pipeline import accept_job

    world = bootstrap(_config(args))
    job = accept_job(world, args.job_id, source="cli")
    world.persist_kv()
    print(json.dumps({"ok": True, "job": job["id"], "status": job["status"]}, indent=2))
    return 0


def cmd_paid(args: argparse.Namespace) -> int:
    from sovereign.capital.invoice import collect

    cfg = _config(args)
    if cfg.mode == "live" and not args.confirm:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "live manual settlement requires --confirm after verifying payment",
                },
                indent=2,
            )
        )
        return 1
    world = bootstrap(cfg)
    inv = collect(world, args.ref, source="cli")
    world.persist_kv()
    print(json.dumps({"ok": True, "invoice": inv["id"], "amount": inv["amount"], "status": inv["status"]}, indent=2))
    return 0


def cmd_invoices(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    print(json.dumps(world.store.invoices(args.status), indent=2, default=str))
    return 0


def cmd_mail(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    print(json.dumps(world.store.mail(), indent=2, default=str)[:20000])
    return 0


def _harvest_trace_errors(trace_files: list[Path]) -> list[dict[str, object]]:
    """Agent failures (short error + traceback tail) from trace event lines."""
    errors: list[dict[str, object]] = []
    for path in trace_files:
        for line in path.read_text().splitlines()[1:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "agent" and event.get("error"):
                errors.append(
                    {
                        "agent": event.get("agent"),
                        "error": event.get("error"),
                        "traceback_tail": event.get("traceback_tail"),
                    }
                )
    return errors


def cmd_debug(args: argparse.Namespace) -> int:
    cfg = _config(args)

    if args.show:
        collector = TraceCollector(cfg.paths().logs / "trace", cfg.debug)
        latest = collector.latest(1)
        if not latest:
            print(json.dumps({"trace_file": None, "summary": None,
                              "note": "no trace files yet; run `sovereign debug --ticks N`"}, indent=2))
            return 0
        print(json.dumps(
            {"trace_file": str(latest[0]), "summary": collector.read_summary(latest[0])},
            indent=2,
            default=str,
        ))
        return 0

    # Force tracing on for this process regardless of persisted config.
    cfg.debug.enabled = True
    world = bootstrap(cfg)
    reports = [step(world) for _ in range(max(0, args.ticks))]

    ring = world.store.get_kv(TICK_METRICS_KEY)
    ring = ring if isinstance(ring, list) else []
    run_ticks = {report["tick"] for report in reports}
    agent_totals: dict[str, float] = {}
    for entry in ring:
        if not isinstance(entry, dict) or entry.get("tick") not in run_ticks:
            continue
        for agent, ms in (entry.get("agents_ms") or {}).items():
            agent_totals[agent] = agent_totals.get(agent, 0.0) + float(ms)
    slowest_agents = {
        agent: round(total, 1)
        for agent, total in sorted(agent_totals.items(), key=lambda kv: kv[1], reverse=True)
    }

    stats = world.tools.stats_snapshot() if world.tools is not None else {}
    slowest_tools = [
        {"tool": name, **entry}
        for name, entry in sorted(
            stats.items(), key=lambda kv: kv[1]["total_ms"], reverse=True
        )[:8]
    ]

    trace_files = sorted(world.debug_trace.latest(len(reports))) if reports else []
    durations = [float(report["duration_ms"]) for report in reports]
    print(json.dumps(
        {
            "ticks_run": len(reports),
            "avg_tick_ms": round(sum(durations) / len(durations), 2) if durations else 0,
            "slowest_tools": slowest_tools,
            "slowest_agents": slowest_agents,
            "comms": world.comms.counts() if world.comms is not None else {},
            "errors": _harvest_trace_errors(trace_files),
            "trace_files": [str(path) for path in trace_files],
        },
        indent=2,
        default=str,
    ))
    return 0


def _web_vault(world):
    """The bootstrap-wired vault, or a local one on trees that predate it."""
    vault = getattr(world, "web_vault", None)
    if vault is not None:
        return vault
    from sovereign.web.vault import WebVault

    return WebVault(world.wallet, world.config.paths().root / "web_sessions")


def cmd_web_login(args: argparse.Namespace) -> int:
    from sovereign.web.login import (
        capture_headful_login,
        import_session_file,
        normalize_storage_state,
        request_web_login,
    )

    world = bootstrap(_config(args))
    vault = _web_vault(world)
    host = vault._domain_key(args.domain)
    if args.import_file:
        report = import_session_file(vault, host, Path(args.import_file))
        print(json.dumps({"ok": True, **report}, indent=2))
        return 0
    if args.headful:
        url = args.url or f"https://{host}/"
        try:
            from sovereign.web.session import default_driver_factory

            class _HeadfulShim:
                """Driver config: visible browser, full page, patient timeout."""

                headless = False
                block_media = False
                nav_timeout_ms = 60000

            def _started_driver():
                driver = default_driver_factory(_HeadfulShim())
                try:
                    driver.start()
                except BaseException:
                    driver.stop()
                    raise
                return driver

            state = normalize_storage_state(
                capture_headful_login(url, driver_factory=_started_driver)
            )
            vault.save_session(host, state)
        except Exception as exc:  # missing [web] extra, display, or browser
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": str(exc) or type(exc).__name__,
                        "hint": "use --import with an exported storage_state json",
                    },
                    indent=2,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "domain": host,
                    "cookies": len(state["cookies"]),
                    "origins": len(state["origins"]),
                },
                indent=2,
            )
        )
        return 0
    request = request_web_login(world, host, args.url or "")
    print(json.dumps(request, indent=2))
    return 0


def cmd_web_sessions(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    print(json.dumps(_web_vault(world).list_domains(), indent=2))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    world = bootstrap(_config(args))
    mcp_cfg = world.config.mcp
    servers = [
        {
            "name": s.name,
            "transport": s.transport,
            "allow_agents": list(s.allow_agents),
            "allowed_tools": list(s.allowed_tools),
            "calls_per_tick": s.calls_per_tick,
        }
        for s in mcp_cfg.servers
        if not args.server or s.name == args.server
    ]
    if not args.probe:
        # Config view only: no connect, and never commands, URLs, or
        # credential references.
        print(json.dumps({"enabled": mcp_cfg.enabled, "servers": servers}, indent=2))
        return 0
    registry = world.mcp
    grouped: dict[str, list[dict[str, str]]] = {}
    try:
        for spec in registry.tools() if registry is not None else []:
            if args.server and spec.server != args.server:
                continue
            grouped.setdefault(spec.server, []).append(
                {"name": spec.name, "description": spec.description}
            )
        errors = registry.errors() if registry is not None else []
    finally:
        if registry is not None:
            registry.close()
    print(
        json.dumps(
            {
                "enabled": mcp_cfg.enabled,
                "servers": servers,
                "tools": grouped,
                "errors": errors,
            },
            indent=2,
        )
    )
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    from sovereign.alerts import AlertManager, detect

    cfg = _config(args)
    world = bootstrap(cfg)
    manager = AlertManager(cfg.alerts)
    if args.test:
        print(json.dumps(manager.send_test(world), indent=2, default=str))
        return 0
    # Config view stays boolean for recipient/url so secrets (a webhook URL
    # can embed a token) never reach stdout.
    print(
        json.dumps(
            {
                "alerts": [alert.as_dict() for alert in detect(world)],
                "config": {
                    "enabled": cfg.alerts.enabled,
                    "channel": cfg.alerts.channel,
                    "min_severity": cfg.alerts.min_severity,
                    "throttle_minutes": cfg.alerts.throttle_minutes,
                    "to_present": bool(cfg.alerts.to),
                    "webhook_present": bool(cfg.alerts.webhook_url),
                },
            },
            indent=2,
            default=str,
        )
    )
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

    s = sub.add_parser("bootstrap", help="One-shot init + full repair + readiness report")
    _globals(s)
    s.set_defaults(func=cmd_bootstrap)

    s = sub.add_parser("run", help="Run heartbeat ticks")
    _globals(s)
    s.add_argument("--ticks", type=int, default=24)
    s.add_argument("--until-revenue", type=float, default=0.0)
    s.add_argument("--realistic", action="store_true", help="Sim: delayed pay, no auto-accept")
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
    s.add_argument("field", nargs="+", help="KEY=VALUE (use KEY=- to read the value from stdin)")
    s.set_defaults(func=cmd_reply)

    s = sub.add_parser("doctor", help="Check subscription CLI, wallet, inbox, engine health")
    _globals(s)
    s.add_argument("--fix", action="store_true", help="Repair what the mechanic can repair")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="Idempotent engine repair (paths, playbooks, inbox, lock, tools)")
    _globals(s)
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("tools", help="List the tool bus (optionally for one agent)")
    _globals(s)
    s.add_argument("--agent", default=None)
    s.set_defaults(func=cmd_tools)

    s = sub.add_parser("agents", help="Roster: mission, tier, tools, handles, freeze, inbox")
    _globals(s)
    s.add_argument("--agent", default=None, help="One agent, including its full system prompt")
    s.set_defaults(func=cmd_agents)

    s = sub.add_parser("dashboard", help="Read-only observer UI")
    _globals(s)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=7474)
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("wallet", help="Print public addresses")
    _globals(s)
    s.add_argument("--reveal", action="store_true", help="Requires SOVEREIGN_CONFIRM_REVEAL=1")
    s.set_defaults(func=cmd_wallet)

    s = sub.add_parser("serve", help="Daemon loop with file lock")
    _globals(s)
    s.add_argument("--ticks", type=int, default=0, help="0 = forever")
    s.add_argument(
        "--force",
        action="store_true",
        help="Start even when required readiness checks fail (live gate override)",
    )
    s.add_argument(
        "--workers",
        action="store_true",
        help="Run eligible agents in spawned worker processes this serve",
    )
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser(
        "trading",
        help="Show Hyperliquid/paper venue status (never keys)",
    )
    _globals(s)
    s.set_defaults(func=cmd_trading)

    s = sub.add_parser(
        "worker",
        help="Run one agent against the data dir (does not take engine.lock)",
    )
    _globals(s)
    s.add_argument("--agent", required=True, help="Agent name (e.g. bookkeeper)")
    s.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass (the only supported mode)",
    )
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser(
        "backup",
        help="Snapshot db/docs into an empty dir (never master.key), or verify one",
    )
    _globals(s)
    s.add_argument("--out", default=None, metavar="DIR", help="Create a backup in this empty directory")
    s.add_argument(
        "--verify", default=None, metavar="DIR", help="Verify a backup directory against its manifest"
    )
    s.add_argument("--no-secrets", action="store_true", help="Leave secrets.enc out of the backup")
    s.add_argument(
        "--restore-drill",
        default=None,
        metavar="DIR",
        help="Create a backup under DIR/backup, verify it, and probe the snapshot (never restores onto the live dir)",
    )
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser(
        "healthcheck",
        help="Exec probe: readiness, plus optional last-tick staleness (Docker/K8s)",
    )
    _globals(s)
    s.add_argument(
        "--stale-seconds",
        type=float,
        default=None,
        metavar="N",
        help="Also fail when the newest tick timestamp is older than N seconds",
    )
    s.set_defaults(func=cmd_healthcheck)

    s = sub.add_parser(
        "maintain",
        help="Prune retained events/comms and compact the SQLite file",
    )
    _globals(s)
    s.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM (prune only)")
    s.add_argument(
        "--comms-days",
        type=float,
        default=None,
        metavar="D",
        help="Prune done/expired messages older than D days (default: retention.comms_days)",
    )
    s.set_defaults(func=cmd_maintain)

    s = sub.add_parser("migrate", help="Apply pending schema migrations and print the version")
    _globals(s)
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser(
        "rotate-key",
        help="Re-encrypt secrets.enc with a new master key (stop the daemon first)",
    )
    _globals(s)
    s.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge that the daemon is stopped and you have a backup",
    )
    s.add_argument(
        "--to-keyring",
        action="store_true",
        help="Move custody from the file backend into the OS keyring",
    )
    s.add_argument(
        "--delete-old-file",
        action="store_true",
        help="With --to-keyring, delete data/master.key after a successful migrate",
    )
    s.set_defaults(func=cmd_rotate_key)

    s = sub.add_parser(
        "comms", help="List, requeue, or prune agent messages (payloads stay hidden)"
    )
    _globals(s)
    s.add_argument("--status", default=None, help="Filter the list by status (queued/done/expired/dead)")
    s.add_argument("--limit", type=int, default=20, help="Max rows to list, newest first")
    s.add_argument(
        "--requeue", default=None, metavar="MSG_ID", help="Requeue one dead or expired message"
    )
    s.add_argument(
        "--purge-days",
        type=float,
        default=None,
        metavar="D",
        help="Delete done/expired rows older than D days",
    )
    s.set_defaults(func=cmd_comms)

    s = sub.add_parser("accept", help="Mark a job accepted (live inbound)")
    _globals(s)
    s.add_argument("job_id")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("paid", help="Mark an invoice or job paid")
    _globals(s)
    s.add_argument("ref", help="invoice id or job id")
    s.add_argument(
        "--confirm",
        action="store_true",
        help="Live mode: confirm that payment was independently verified",
    )
    s.set_defaults(func=cmd_paid)

    s = sub.add_parser("invoices", help="List invoices")
    _globals(s)
    s.add_argument("--status", default=None)
    s.set_defaults(func=cmd_invoices)

    s = sub.add_parser("mail", help="List mailbox")
    _globals(s)
    s.set_defaults(func=cmd_mail)

    s = sub.add_parser(
        "debug",
        help="Trace N ticks and report hot tools/agents, or show the latest trace",
    )
    _globals(s)
    s.add_argument("--ticks", type=int, default=3, help="Traced ticks to run")
    s.add_argument(
        "--show",
        action="store_true",
        help="Print the latest trace summary without running any ticks",
    )
    s.set_defaults(func=cmd_debug)

    s = sub.add_parser(
        "web-login",
        help="Vault an encrypted browser session for a domain (import, headful, or ask a human)",
    )
    _globals(s)
    s.add_argument("domain", help="Site hostname (a full URL is normalized to its host)")
    s.add_argument("--url", default=None, help="Login page URL (defaults to https://DOMAIN/)")
    s.add_argument(
        "--import",
        dest="import_file",
        default=None,
        metavar="FILE",
        help="Vault an exported Playwright storage_state JSON file",
    )
    s.add_argument(
        "--headful",
        action="store_true",
        help="Open a visible browser here, log in, and capture the session",
    )
    s.set_defaults(func=cmd_web_login)

    s = sub.add_parser(
        "web-sessions", help="List domains with a vaulted browser session (never secrets)"
    )
    _globals(s)
    s.set_defaults(func=cmd_web_sessions)

    s = sub.add_parser(
        "mcp",
        help="Show configured MCP servers (never secrets); --probe connects and lists tools",
    )
    _globals(s)
    s.add_argument("--server", default=None, help="Limit output to one configured server")
    s.add_argument(
        "--probe",
        action="store_true",
        help="Connect and print discovered tools grouped by server plus any errors",
    )
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser(
        "alerts",
        help="Show detected P0/P1 incidents and alert config (never secrets)",
    )
    _globals(s)
    s.add_argument(
        "--test",
        action="store_true",
        help="Send one synthetic P0 test alert through the configured channel",
    )
    s.set_defaults(func=cmd_alerts)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, PermissionError, RuntimeError, ValueError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc) or type(exc).__name__}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
