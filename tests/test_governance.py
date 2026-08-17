from sovereign.governance.council import Council
from sovereign.governance.reputation import Reputation
from sovereign.memory.store import Store


def test_spend_quorum(tmp_path):
    c = Council(Store(tmp_path / "g.db"))
    votes = c.auto_votes_for_spend(usd=10, operating_cash=100, frozen=False, autonomy=50)
    ok, reason = c.quorum("a1", "spend", votes)
    assert ok
    votes2 = c.auto_votes_for_spend(usd=80, operating_cash=100, frozen=False, autonomy=10)
    # 80 > 25% of cash → risk no; 80 > autonomy → treasurer no
    ok2, _ = c.quorum("a2", "spend", votes2)
    assert not ok2


def test_reputation_scales_autonomy():
    r = Reputation({"crafter": 100, "trader": 10})
    assert r.autonomy_usd("crafter", 200) == 200
    assert r.should_freeze("trader")
    r.boost("trader", 50, "recover")
    assert not r.should_freeze("trader")
