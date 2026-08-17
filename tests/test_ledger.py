from sovereign.capital.ledger import Ledger
from sovereign.memory.store import Store


def test_double_entry_and_revenue(tmp_path):
    store = Store(tmp_path / "t.db")
    led = Ledger(store)
    led.post("assets.usdc", "equity.treasury", 100, "float")
    led.post("assets.usdc", "income.labor", 500, "job")
    led.post("expenses.infra", "assets.usdc", 6, "vps")
    snap = led.snapshot()
    assert snap["equity_usd"] == 594
    assert snap["labor_usd"] == 500
    assert snap["revenue_usd"] == 500
    assert snap["expenses_usd"] == 6
    # assets = 100+500-6 = 594
    assert abs(led.balance("assets.usdc") - 594) < 1e-6
