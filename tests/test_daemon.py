from sovereign.engine.daemon import FileLock


def test_file_lock_exclusive(tmp_path):
    p = tmp_path / "engine.lock"
    a = FileLock(p)
    a.acquire()
    b = FileLock(p)
    try:
        b.acquire()
        raise AssertionError("second lock should fail")
    except RuntimeError:
        pass
    finally:
        a.release()
    c = FileLock(p)
    c.acquire()
    c.release()
