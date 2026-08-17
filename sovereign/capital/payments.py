from __future__ import annotations

from typing import Any

import httpx


def balance_of_calldata(holder: str) -> str:
    addr = holder.lower().replace("0x", "")
    return "0x70a08231" + addr.rjust(64, "0")


def decode_uint256(hex_value: str) -> int:
    raw = (hex_value or "0x0").replace("0x", "") or "0"
    return int(raw, 16)


def usdc_balance(rpc_url: str, token: str, holder: str, timeout: float = 15.0) -> float:
    """USDC has 6 decimals. Returns token units as USD-ish float."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": token, "data": balance_of_calldata(holder)},
            "latest",
        ],
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(rpc_url, json=payload)
        r.raise_for_status()
        result = r.json().get("result") or "0x0"
    return decode_uint256(result) / 1_000_000


def watch_and_collect(world: Any) -> list[dict[str, Any]]:
    """If on-chain USDC rose, FIFO-pay open invoices. Never logs keys."""
    if world.config.mode != "live":
        return []
    open_inv = world.store.invoices("open")
    if not open_inv:
        return []
    holder = world.wallet.public()["eth_address"]
    try:
        bal = usdc_balance(world.config.rpc_url, world.config.usdc_token, holder)
    except Exception as e:
        world.store.emit("pay_watch_error", {"error": str(e)[:200]}, "treasurer")
        return []
    prev = float(world.store.get_kv("usdc_onchain", 0.0) or 0.0)
    world.store.set_kv("usdc_onchain", bal)
    delta = bal - prev
    if delta < 1.0:
        return []
    collected = []
    remaining = delta
    from sovereign.capital.invoice import collect

    for inv in open_inv:
        amt = float(inv["amount"])
        if remaining + 0.5 < amt:
            break
        collected.append(collect(world, inv["id"], source="chain"))
        remaining -= amt
    return collected
