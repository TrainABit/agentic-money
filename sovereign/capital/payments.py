from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx

from sovereign.memory.store import iso, usdc_amount, usdc_minor


PAYMENT_STATE_KEY = "payment_watch_v2"
CHAINS = ("eth", "sol")
ETH_CONFIRMATIONS = 2


class _ConfirmedBalance(float):
    """Float-compatible balance carrying the finalized block/slot it came from."""

    amount_minor: int
    height: int

    def __new__(cls, amount_minor: int, height: int):
        value = super().__new__(cls, usdc_amount(amount_minor))
        value.amount_minor = amount_minor
        value.height = height
        return value


@dataclass(frozen=True)
class _BalanceObservation:
    amount_minor: int
    height: int | None = None


def balance_of_calldata(holder: str) -> str:
    addr = holder.lower().replace("0x", "")
    if not addr or len(addr) > 40 or any(c not in "0123456789abcdef" for c in addr):
        raise ValueError("invalid EVM holder address")
    return "0x70a08231" + addr.rjust(64, "0")


def decode_uint256(hex_value: str) -> int:
    if not isinstance(hex_value, str) or not hex_value.startswith("0x"):
        raise ValueError("invalid JSON-RPC hex quantity")
    raw = hex_value[2:] or "0"
    if len(raw) > 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise ValueError("invalid JSON-RPC hex quantity")
    return int(raw, 16)


def _rpc_result(response: httpx.Response, expected_id: int) -> Any:
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("invalid JSON-RPC response")
    if body.get("jsonrpc") != "2.0" or body.get("id") != expected_id:
        raise ValueError("mismatched JSON-RPC response")
    if body.get("error") is not None:
        error = body["error"]
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "unknown error"
        raise RuntimeError(f"JSON-RPC error: {str(error)[:160]}")
    if "result" not in body or body["result"] is None:
        raise ValueError("JSON-RPC response missing result")
    return body["result"]


def usdc_balance(
    rpc_url: str,
    token: str,
    holder: str,
    timeout: float = 15.0,
    confirmations: int = ETH_CONFIRMATIONS,
) -> float:
    """Read USDC at an explicit confirmed Ethereum block."""
    block_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_blockNumber",
        "params": [],
    }
    with httpx.Client(timeout=timeout) as client:
        latest = decode_uint256(_rpc_result(client.post(rpc_url, json=block_payload), 1))
        confirmed_block = max(0, latest - max(0, int(confirmations)))
        call_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_call",
            "params": [
                {"to": token, "data": balance_of_calldata(holder)},
                hex(confirmed_block),
            ],
        }
        raw_balance = decode_uint256(_rpc_result(client.post(rpc_url, json=call_payload), 2))
    return _ConfirmedBalance(raw_balance, confirmed_block)


def sol_usdc_balance(rpc_url: str, owner: str, mint: str, timeout: float = 15.0) -> float:
    """Read integer SPL balances using Solana's finalized commitment."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "finalized"},
        ],
    }
    with httpx.Client(timeout=timeout) as client:
        result = _rpc_result(client.post(rpc_url, json=payload), 1)
    if not isinstance(result, dict):
        raise ValueError("invalid Solana token-account result")
    context = result.get("context")
    if not isinstance(context, dict) or int(context.get("slot") or 0) <= 0:
        raise ValueError("Solana response missing finalized context")
    accounts = result.get("value")
    if not isinstance(accounts, list):
        raise ValueError("Solana response missing token accounts")

    total_minor = 0
    for acc in accounts:
        try:
            token = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw_text = token["amount"]
            decimals = int(token["decimals"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed Solana token account") from exc
        if not isinstance(raw_text, str) or not raw_text.isdigit() or not 0 <= decimals <= 18:
            raise ValueError("malformed Solana token amount")
        raw = int(raw_text)
        if decimals <= 6:
            total_minor += raw * (10 ** (6 - decimals))
        else:
            divisor = 10 ** (decimals - 6)
            if raw % divisor:
                raise ValueError("token amount exceeds USDC precision")
            total_minor += raw // divisor
    return _ConfirmedBalance(total_minor, int(context["slot"]))


def _chain_balance(
    kind: str,
    fetch: Callable[[], float],
) -> tuple[_BalanceObservation | None, str | None]:
    try:
        raw_balance = fetch()
        if isinstance(raw_balance, _ConfirmedBalance):
            balance_minor = int(raw_balance.amount_minor)
            height: int | None = int(raw_balance.height)
        else:
            balance_minor = usdc_minor(raw_balance)
            height = None
        if balance_minor < 0:
            raise ValueError("negative token balance")
        return _BalanceObservation(balance_minor, height), None
    except Exception as e:
        return None, f"{kind}: {str(e)[:160]}"


def _new_state() -> dict[str, Any]:
    return {
        "version": 2,
        "chains": {
            chain: {
                "initialized": False,
                "last_balance_minor": 0,
                "last_height": None,
                "last_observed_ts": None,
                "suspense": [],
            }
            for chain in CHAINS
        },
        "lot_sequence": 0,
        "manual_reserved_minor": 0,
        "manual_attributed_minor": 0,
        "auto_attributed_minor": 0,
        "historical_attributed_minor": 0,
        "legacy_aggregate_minor": None,
    }


def _state_from_v2(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("version") != 2:
        return None
    state = _new_state()
    state["lot_sequence"] = max(0, int(raw.get("lot_sequence") or 0))
    state["manual_reserved_minor"] = max(0, int(raw.get("manual_reserved_minor") or 0))
    state["manual_attributed_minor"] = max(0, int(raw.get("manual_attributed_minor") or 0))
    state["auto_attributed_minor"] = max(0, int(raw.get("auto_attributed_minor") or 0))
    state["historical_attributed_minor"] = max(
        0,
        int(raw.get("historical_attributed_minor") or 0),
    )
    legacy_aggregate = raw.get("legacy_aggregate_minor")
    state["legacy_aggregate_minor"] = (
        max(0, int(legacy_aggregate))
        if legacy_aggregate is not None
        else None
    )
    raw_chains = raw.get("chains")
    if not isinstance(raw_chains, dict):
        return state
    for chain in CHAINS:
        incoming = raw_chains.get(chain)
        if not isinstance(incoming, dict):
            continue
        current = state["chains"][chain]
        current["initialized"] = bool(incoming.get("initialized"))
        current["last_balance_minor"] = max(0, int(incoming.get("last_balance_minor") or 0))
        height = incoming.get("last_height")
        current["last_height"] = max(0, int(height)) if height is not None else None
        current["last_observed_ts"] = incoming.get("last_observed_ts")
        lots = incoming.get("suspense")
        if not isinstance(lots, list):
            continue
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            amount_minor = int(lot.get("amount_minor") or 0)
            if amount_minor <= 0:
                continue
            current["suspense"].append(
                {
                    "id": str(lot.get("id") or ""),
                    "amount_minor": amount_minor,
                    "observed_ts": lot.get("observed_ts"),
                    "notice": lot.get("notice"),
                    "tainted": bool(lot.get("tainted")),
                }
            )
    return state


def _legacy_minor(
    world: Any,
    key: str,
    missing: object,
    invalid_keys: list[str],
) -> tuple[bool, int | None]:
    raw = world.store.get_kv(key, missing)
    if raw is missing:
        return False, None
    try:
        if isinstance(raw, bool):
            raise ValueError("boolean is not a balance")
        amount_minor = usdc_minor(raw)
        if amount_minor < 0:
            raise ValueError("negative balance")
    except (ArithmeticError, TypeError, ValueError):
        invalid_keys.append(key)
        return True, None
    return True, amount_minor


def _migrate_legacy_state(world: Any) -> dict[str, Any]:
    """Atomically initialize v2 once, retaining only safe legacy facts."""
    with world.store.transaction():
        existing = _state_from_v2(world.store.get_kv(PAYMENT_STATE_KEY))
        if existing is not None:
            return existing

        state = _new_state()
        missing = object()
        invalid_keys: list[str] = []
        legacy: dict[str, int | None] = {}
        present: dict[str, bool] = {}
        for key in (
            "usdc_onchain_eth",
            "usdc_onchain_sol",
            "usdc_onchain",
            "usdc_attributed",
        ):
            present[key], legacy[key] = _legacy_minor(
                world,
                key,
                missing,
                invalid_keys,
            )

        for chain in CHAINS:
            amount_minor = legacy[f"usdc_onchain_{chain}"]
            if amount_minor is None:
                continue
            chain_state = state["chains"][chain]
            chain_state["initialized"] = True
            chain_state["last_balance_minor"] = amount_minor
            chain_state["last_observed_ts"] = iso()

        state["legacy_aggregate_minor"] = legacy["usdc_onchain"]
        historical = legacy["usdc_attributed"]
        if historical is not None:
            state["historical_attributed_minor"] = historical

        world.store.set_kv(PAYMENT_STATE_KEY, state)
        if any(present.values()):
            world.store.emit(
                "pay_watch_migrated",
                {
                    "eth_baseline_usd": (
                        usdc_amount(legacy["usdc_onchain_eth"])
                        if legacy["usdc_onchain_eth"] is not None
                        else None
                    ),
                    "sol_baseline_usd": (
                        usdc_amount(legacy["usdc_onchain_sol"])
                        if legacy["usdc_onchain_sol"] is not None
                        else None
                    ),
                    "legacy_aggregate_usd": (
                        usdc_amount(legacy["usdc_onchain"])
                        if legacy["usdc_onchain"] is not None
                        else None
                    ),
                    "historical_attributed_usd": usdc_amount(
                        int(state["historical_attributed_minor"])
                    ),
                    "invalid_keys": sorted(invalid_keys),
                },
                "treasurer",
            )
        return state


def _load_state(world: Any) -> dict[str, Any]:
    state = _state_from_v2(world.store.get_kv(PAYMENT_STATE_KEY))
    if state is not None:
        return state
    return _migrate_legacy_state(world)


def _suspense_minor(state: dict[str, Any]) -> int:
    return sum(
        int(lot["amount_minor"])
        for chain in CHAINS
        for lot in state["chains"][chain]["suspense"]
    )


def _persist_state(world: Any, state: dict[str, Any]) -> None:
    world.store.set_kv(PAYMENT_STATE_KEY, state)
    world.store.set_kv("usdc_suspense", usdc_amount(_suspense_minor(state)))
    world.store.set_kv(
        "usdc_manual_reserved",
        usdc_amount(int(state["manual_reserved_minor"])),
    )
    attributed = (
        int(state["historical_attributed_minor"])
        + int(state["manual_attributed_minor"])
        + int(state["auto_attributed_minor"])
    )
    world.store.set_kv("usdc_attributed", usdc_amount(attributed))


def _add_suspense(
    state: dict[str, Any],
    chain: str,
    amount_minor: int,
    observed_ts: str,
) -> None:
    if amount_minor <= 0:
        return
    state["lot_sequence"] = int(state["lot_sequence"]) + 1
    state["chains"][chain]["suspense"].append(
        {
            "id": f"{chain}-{state['lot_sequence']}",
            "amount_minor": amount_minor,
            "observed_ts": observed_ts,
            "notice": None,
            "tainted": False,
        }
    )


def _consume_chain_suspense(
    state: dict[str, Any],
    chain: str,
    amount_minor: int,
    *,
    taint_remainder: bool,
) -> int:
    remaining = amount_minor
    lots = state["chains"][chain]["suspense"]
    kept: list[dict[str, Any]] = []
    consumed = 0
    for index, lot in enumerate(lots):
        available = int(lot["amount_minor"])
        take = min(available, remaining)
        available -= take
        remaining -= take
        consumed += take
        if available:
            lot["amount_minor"] = available
            lot["notice"] = None
            if taint_remainder and take:
                lot["tainted"] = True
            kept.append(lot)
        if remaining == 0:
            kept.extend(lots[index + 1:])
            break
    state["chains"][chain]["suspense"] = kept
    if taint_remainder and consumed:
        for lot in kept:
            lot["tainted"] = True
            lot["notice"] = None
    return consumed


def _consume_any_suspense(state: dict[str, Any], amount_minor: int) -> int:
    remaining = amount_minor
    consumed = 0
    for chain in CHAINS:
        got = _consume_chain_suspense(
            state,
            chain,
            remaining,
            taint_remainder=True,
        )
        consumed += got
        remaining -= got
        if remaining == 0:
            break
    return consumed


def reconcile_manual_collection(
    world: Any,
    amount: float,
    invoice_id: str,
    source: str,
) -> None:
    """Consume observed suspense or reserve a future delta for manual settlement."""
    amount_minor = usdc_minor(amount)
    with world.store.transaction():
        state = _load_state(world)
        consumed = _consume_any_suspense(state, amount_minor)
        reserved = amount_minor - consumed
        state["manual_reserved_minor"] = int(state["manual_reserved_minor"]) + reserved
        state["manual_attributed_minor"] = int(state["manual_attributed_minor"]) + amount_minor
        _persist_state(world, state)
        world.store.emit(
            "pay_manual_attribution",
            {
                "invoice_id": invoice_id,
                "source": source,
                "usd": usdc_amount(amount_minor),
                "suspense_usd": usdc_amount(consumed),
                "reserved_usd": usdc_amount(reserved),
            },
            "treasurer",
        )


def _observe_balance(
    world: Any,
    state: dict[str, Any],
    chain: str,
    observation: _BalanceObservation,
    observed_ts: str,
) -> tuple[bool, str | None]:
    chain_state = state["chains"][chain]
    balance_minor = observation.amount_minor
    previous = int(chain_state["last_balance_minor"])
    previous_height = chain_state.get("last_height")
    if observation.height is not None and previous_height is not None:
        if observation.height < int(previous_height):
            return False, f"{chain}: stale balance height {observation.height} < {previous_height}"
        if observation.height == int(previous_height):
            if balance_minor != previous:
                return False, f"{chain}: balance changed at unchanged height {observation.height}"
            return False, None

    chain_state["last_balance_minor"] = balance_minor
    if observation.height is not None:
        chain_state["last_height"] = observation.height
    chain_state["last_observed_ts"] = observed_ts
    if not chain_state["initialized"]:
        chain_state["initialized"] = True
        world.store.emit(
            "pay_watch_baseline",
            {"chain": chain, "usd": usdc_amount(balance_minor)},
            "treasurer",
        )
        return True, None

    delta = balance_minor - previous
    if delta < 0:
        withdrawn = -delta
        removed = _consume_chain_suspense(
            state,
            chain,
            withdrawn,
            taint_remainder=True,
        )
        world.store.emit(
            "pay_withdrawal",
            {
                "chain": chain,
                "usd": usdc_amount(withdrawn),
                "suspense_removed_usd": usdc_amount(removed),
            },
            "treasurer",
        )
        return True, None
    if delta == 0:
        return True, None

    reserved = min(delta, int(state["manual_reserved_minor"]))
    if reserved:
        state["manual_reserved_minor"] = int(state["manual_reserved_minor"]) - reserved
        world.store.emit(
            "pay_manual_reconciled",
            {"chain": chain, "usd": usdc_amount(reserved)},
            "treasurer",
        )
    _add_suspense(state, chain, delta - reserved, observed_ts)
    return True, None


def _notice_unmatched(world: Any, lot: dict[str, Any], chain: str, candidates: list[dict[str, Any]]) -> None:
    if len(candidates) > 1:
        signature = "ambiguous:" + ",".join(sorted(str(inv["id"]) for inv in candidates))
        kind = "pay_ambiguous"
        payload = {
            "chain": chain,
            "usd": usdc_amount(int(lot["amount_minor"])),
            "invoice_ids": [inv["id"] for inv in candidates],
        }
    elif lot.get("tainted"):
        signature = "tainted"
        kind = "pay_unattributed"
        payload = {
            "chain": chain,
            "usd": usdc_amount(int(lot["amount_minor"])),
            "reason": "withdrawal_obscured_identity",
        }
    else:
        signature = "unmatched"
        kind = "pay_unattributed"
        payload = {
            "chain": chain,
            "usd": usdc_amount(int(lot["amount_minor"])),
            "reason": "no_exact_invoice",
        }
    if lot.get("notice") == signature:
        return
    lot["notice"] = signature
    world.store.emit(kind, payload, "treasurer")


def _match_suspense(
    world: Any,
    state: dict[str, Any],
    fresh_chains: set[str],
) -> list[dict[str, Any]]:
    from sovereign.capital.invoice import collect

    collected: list[dict[str, Any]] = []
    for chain in CHAINS:
        if chain not in fresh_chains:
            continue
        lots = state["chains"][chain]["suspense"]
        index = 0
        while index < len(lots):
            lot = lots[index]
            amount_minor = int(lot["amount_minor"])
            candidates = [
                inv
                for inv in world.store.invoices("open")
                if usdc_minor(inv["amount"]) == amount_minor
            ]
            if len(candidates) == 1 and not lot.get("tainted"):
                settled = collect(world, candidates[0]["id"], source=f"chain:{chain}")
                if settled.get("status") == "paid":
                    collected.append(settled)
                    state["auto_attributed_minor"] = (
                        int(state["auto_attributed_minor"]) + amount_minor
                    )
                    lots.pop(index)
                    continue
            _notice_unmatched(world, lot, chain, candidates)
            index += 1
    return collected


def watch_and_collect(world: Any) -> list[dict[str, Any]]:
    """Attribute only fresh, exact per-chain USDC balance deltas."""
    if world.config.mode != "live":
        return []
    pub = world.wallet.public()
    errors: list[str] = []
    eth, err = _chain_balance(
        "eth",
        lambda: usdc_balance(world.config.rpc_url, world.config.usdc_token, pub["eth_address"]),
    )
    if err:
        errors.append(err)
    sol, err = _chain_balance(
        "sol",
        lambda: sol_usdc_balance(world.config.sol_rpc_url, pub["sol_address"], world.config.sol_usdc_mint),
    )
    if err:
        errors.append(err)

    observations = {"eth": eth, "sol": sol}
    with world.store.transaction():
        if errors:
            world.store.emit(
                "pay_watch_error",
                {"error": " | ".join(errors)[:240]},
                "treasurer",
            )
        successful_chains = {chain for chain, observation in observations.items() if observation is not None}
        if not successful_chains:
            return []

        state = _load_state(world)
        observed_ts = iso()
        fresh_chains: set[str] = set()
        for chain in CHAINS:
            observation = observations[chain]
            if observation is None:
                continue
            accepted, freshness_error = _observe_balance(
                world,
                state,
                chain,
                observation,
                observed_ts,
            )
            if freshness_error:
                world.store.emit(
                    "pay_watch_error",
                    {"error": freshness_error[:240]},
                    "treasurer",
                )
            if not accepted:
                continue
            fresh_chains.add(chain)
            world.store.set_kv(
                f"usdc_onchain_{chain}",
                usdc_amount(observation.amount_minor),
            )

        total_balance = sum(
            int(state["chains"][chain]["last_balance_minor"])
            for chain in CHAINS
            if state["chains"][chain]["initialized"]
        )
        world.store.set_kv("usdc_onchain", usdc_amount(total_balance))
        if not fresh_chains:
            _persist_state(world, state)
            return []
        collected = _match_suspense(world, state, fresh_chains)
        _persist_state(world, state)
        return collected
