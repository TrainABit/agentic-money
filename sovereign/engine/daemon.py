from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path
from typing import Any

from sovereign.alerts import AlertManager
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.log import setup_logging
from sovereign.ops import readiness


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Opening with "w" truncates the live holder's PID before flock reports
        # contention. Keep the inode and metadata intact until this descriptor
        # actually owns the lock.
        self.fd = open(self.path, "a+")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.fd.close()
            self.fd = None
            raise RuntimeError(f"engine already running ({self.path})") from e
        self.fd.seek(0)
        self.fd.truncate()
        self.fd.write(str(os.getpid()))
        self.fd.flush()

    def release(self) -> None:
        if self.fd:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                self.fd.close()
                self.fd = None


def serve(
    config: EngineConfig, ticks: int = 0, verbose: bool = False, force: bool = False
) -> None:
    """Forever loop unless ticks>0. Crash-safe: state is in sqlite after each tick.

    After bootstrap and a full heal, a readiness gate runs: in live mode a
    failing required check refuses to serve (RuntimeError) unless ``force``
    overrides it; sim mode (or force) only warns and continues. The file lock
    is always released on the way out, including on a refused start.
    """
    paths = config.paths()
    paths.ensure()
    log = setup_logging(paths.logs)
    lock = FileLock(paths.lock)
    lock.acquire()
    stop = {"flag": False}

    def _stop(*_args: object) -> None:
        stop["flag"] = True

    n = 0
    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        world = bootstrap(config)
        from sovereign.heal.repair import setup as heal_setup

        heal_setup(world, full=True)
        report = readiness(world)
        failing = [
            check["name"]
            for check in report["checks"]
            if check["required"] and not check["ok"]
        ]
        if failing:
            names = ", ".join(failing)
            if config.mode == "live" and not force:
                raise RuntimeError(
                    f"live serve refused; failing required readiness checks: {names}"
                )
            log.warning("serving despite failing required readiness checks: %s", names)
        log.info("daemon start mode=%s pid=%s", config.mode, os.getpid())
        alert_manager = AlertManager(config.alerts)
        idle_state: bool | None = None  # None until the first successful tick
        while not stop["flag"]:
            try:
                r = step(world)
            except Exception:
                log.exception("tick crashed; healing")
                if getattr(world, "web", None) is not None:
                    try:
                        world.web.close()  # never leave a browser running after a crash
                    except Exception:
                        log.exception("web close failed")
                try:
                    heal_setup(world, full=True)
                except Exception:
                    log.exception("heal failed")
                time.sleep(1.0)
                continue
            n += 1
            log.info(
                "tick=%s equity=%.2f trailing=%.2f frozen=%s",
                r["tick"],
                r["equity"],
                r.get("trailing", 0),
                r["frozen"],
            )
            # Out-of-band incident push after every successful tick. dispatch
            # already swallows its own failures; the belt-and-braces except
            # guarantees alerting can never break the serve loop.
            try:
                alert_manager.dispatch(world)
            except Exception:
                log.exception("alert dispatch failed")
            if verbose:
                print(
                    f"tick={r['tick']} equity={r['equity']:.2f} "
                    f"trailing={r.get('trailing', 0):.2f}",
                    flush=True,
                )
            if ticks and n >= ticks:
                break
            if config.mode == "live":
                # Adaptive throttle: idle engines poll less often. The
                # transition is logged once per state change, not every tick.
                idle = bool(r.get("idle"))
                delay = (
                    max(config.tick_seconds, config.idle_tick_seconds)
                    if idle
                    else config.tick_seconds
                )
                if idle is not idle_state:
                    idle_state = idle
                    log.info(
                        "engine %s; sleeping %.1fs between ticks",
                        "idle" if idle else "active",
                        delay,
                    )
                time.sleep(max(0.05, delay))
            else:
                time.sleep(0.05)
    finally:
        lock.release()
        log.info("daemon stop after %s ticks", n)
