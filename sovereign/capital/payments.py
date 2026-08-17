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


def sol_usdc_balance(rpc_url: str, owner: str, mint: str, timeout: float = 15.0) -> float:
    """SPL USDC (6 decimals). Sums parsed uiAmount across the owner's accounts."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(rpc_url, json=payload)
        r.raise_for_status()
        body = r.json()
    total = 0.0
    for acc in (body.get("result") or {}).get("value") or []:
        info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        tok = info.get("tokenAmount") or {}
        if tok.get("uiAmount") is not None:
            total += float(tok["uiAmount"])
            continue
        amt = float(tok.get("amount") or 0)
        decimals = int(tok.get("decimals") or 6)
        total += amt / (10 ** decimals)
    return total


def _chain_balance(world: Any, kind: str, fetch, kv_key: str) -> tuple[float | None, str | None]:
    try:
        bal = float(fetch())
        world.store.set_kv(kv_key, bal)
        return bal, None
    except Exception as e:
        return None, f"{kind}: {str(e)[:160]}"


def watch_and_collect(world: Any) -> list[dict[str, Any]]:
    """If on-chain USDC rose, match open invoices. Never logs keys.

    Tracks cumulative received vs attributed so a mismatch does not burn the delta.
    Watches ETH and Solana; a single-chain RPC failure keeps the last good reading.
    """
    if world.config.mode != "live":
        return []
    open_inv = world.store.invoices("open")
    if not open_inv:
        return []
    pub = world.wallet.public()
    errors: list[str] = []
    eth, err = _chain_balance(
        world,
        "eth",
        lambda: usdc_balance(world.config.rpc_url, world.config.usdc_token, pub["eth_address"]),
        "usdc_onchain_eth",
    )
    if err:
        errors.append(err)
    sol, err = _chain_balance(
        world,
        "sol",
        lambda: sol_usdc_balance(world.config.sol_rpc_url, pub["sol_address"], world.config.sol_usdc_mint),
        "usdc_onchain_sol",
    )
    if err:
        errors.append(err)
    if errors:
        world.store.emit("pay_watch_error", {"error": " | ".join(errors)[:240]}, "treasurer")
    if eth is None and sol is None:
        return []
    eth_bal = float(eth if eth is not None else (world.store.get_kv("usdc_onchain_eth") or 0.0))
    sol_bal = float(sol if sol is not None else (world.store.get_kv("usdc_onchain_sol") or 0.0))
    bal = eth_bal + sol_bal
    world.store.set_kv("usdc_onchain", bal)
    attributed = float(world.store.get_kv("usdc_attributed", 0.0) or 0.0)
    remaining = bal - attributed
    if remaining < 1.0:
        return []
    collected = []
    from sovereign.capital.invoice import collect

    for inv in open_inv:
        amt = float(inv["amount"])
        if remaining + 0.5 < amt:
            continue
        collected.append(collect(world, inv["id"], source="chain"))
        remaining -= amt
        attributed += amt
    world.store.set_kv("usdc_attributed", attributed)
    if remaining >= 1.0:
        world.store.emit("pay_unattributed", {"usd": round(remaining, 2)}, "treasurer")
    return collected
