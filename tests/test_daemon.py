from sovereign.config import EngineConfig
from sovereign.engine.daemon import FileLock, serve


def test_file_lock_exclusive(tmp_path):
    p = tmp_path / "engine.lock"
    a = FileLock(p)
    a.acquire()
    b = FileLock(p)
    try:
        b.acquire()
        raise AssertionError("second lock should fail")
    except RuntimeError:
        assert p.read_text()  # A failed contender must not truncate holder metadata.
    finally:
        a.release()
    c = FileLock(p)
    c.acquire()
    c.release()


def test_daemon_heals_and_continues_after_tick_crash(tmp_path, monkeypatch):
    from sovereign.engine import daemon as dmod

    n = {"calls": 0}
    real = dmod.step

    def boom(world):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("injected crash")
        return real(world)

    monkeypatch.setattr(dmod, "step", boom)
    serve(EngineConfig(mode="sim", data_dir=tmp_path), ticks=1, verbose=False)  # type: ignore[arg-type]
    assert n["calls"] >= 2
    assert (tmp_path / "artifacts" / "health.json").exists()
