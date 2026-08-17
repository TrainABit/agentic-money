from __future__ import annotations

import fcntl
import os
import signal
import time
from pathlib import Path
from typing import Any

from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.log import setup_logging


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(f"engine already running ({self.path})") from e
        self.fd.write(str(os.getpid()))
        self.fd.flush()

    def release(self) -> None:
        if self.fd:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                self.fd.close()
                self.fd = None


def serve(config: EngineConfig, ticks: int = 0, verbose: bool = False) -> None:
    """Forever loop unless ticks>0. Crash-safe: state is in sqlite after each tick."""
    paths = config.paths()
    paths.ensure()
    log = setup_logging(paths.logs)
    lock = FileLock(paths.lock)
    lock.acquire()
    stop = {"flag": False}

    def _stop(*_args: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    world = bootstrap(config)
    from sovereign.heal.repair import setup as heal_setup

    heal_setup(world, full=True)
    n = 0
    log.info("daemon start mode=%s pid=%s", config.mode, os.getpid())
    try:
        while not stop["flag"]:
            try:
                r = step(world)
            except Exception:
                log.exception("tick crashed; healing")
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
            if verbose:
                print(
                    f"tick={r['tick']} equity={r['equity']:.2f} "
                    f"trailing={r.get('trailing', 0):.2f}",
                    flush=True,
                )
            if ticks and n >= ticks:
                break
            time.sleep(max(0.05, config.tick_seconds if config.mode == "live" else 0.05))
    finally:
        lock.release()
        log.info("daemon stop after %s ticks", n)
