"""Read-only JSON-RPC readers for inbound USDC transfer evidence.

These parse actual transfer logs/transactions so settlement can match a
payment by (amount, sender, txid) instead of inferring it from balance
deltas. Every function takes explicit endpoint/address arguments and talks
httpx directly (the same discipline as sovereign.capital.payments), so tests
can patch either this module's public functions or its httpx usage.

The JSON-RPC response validation deliberately replicates payments._rpc_result
rather than importing it: payments imports this module, and the reader must
stay import-safe with no cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_HEX_DIGITS = set("0123456789abcdef")


@dataclass(frozen=True)
class IncomingTransfer:
    chain: str
    txid: str
    sender: str
    amount_minor: int
    height: int
    memo: str | None = None


def _rpc_result(response: httpx.Response, expected_id: int) -> Any:
    """Fail closed on transport, envelope, or in-band JSON-RPC errors."""
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


def _decode_quantity(hex_value: Any) -> int:
    if not isinstance(hex_value, str) or not hex_value.startswith("0x"):
        raise ValueError("invalid JSON-RPC hex quantity")
    raw = hex_value[2:].lower() or "0"
    if len(raw) > 64 or any(c not in _HEX_DIGITS for c in raw):
        raise ValueError("invalid JSON-RPC hex quantity")
    return int(raw, 16)


def _holder_topic(holder: str) -> str:
    """Left-pad an EVM address into the 32-byte indexed-topic form."""
    addr = holder.lower().replace("0x", "")
    if not addr or len(addr) > 40 or any(c not in _HEX_DIGITS for c in addr):
        raise ValueError("invalid EVM holder address")
    return "0x" + addr.rjust(64, "0")


def _address_from_topic(topic: Any) -> str:
    """Recover the 20-byte address from a 32-byte indexed log topic."""
    if not isinstance(topic, str) or not topic.startswith("0x") or len(topic) != 66:
        raise ValueError("invalid address topic")
    raw = topic[2:].lower()
    if any(c not in _HEX_DIGITS for c in raw) or raw[:24] != "0" * 24:
        raise ValueError("invalid address topic")
    return "0x" + raw[24:]


def _parse_eth_transfer_log(log: Any) -> IncomingTransfer | None:
    """Parse one ERC-20 Transfer log defensively; None for anything malformed."""
    try:
        if not isinstance(log, dict) or log.get("removed"):
            return None
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) != 3:
            return None
        if not isinstance(topics[0], str) or topics[0].lower() != ERC20_TRANSFER_TOPIC:
            return None
        txid = log.get("transactionHash")
        if not isinstance(txid, str) or not txid.startswith("0x") or len(txid) != 66:
            return None
        if any(c not in _HEX_DIGITS for c in txid[2:].lower()):
            return None
        sender = _address_from_topic(topics[1])
        # USDC data is the raw 6-decimal integer amount as a uint256 word.
        amount_minor = _decode_quantity(log.get("data"))
        height = _decode_quantity(log.get("blockNumber"))
        if amount_minor <= 0:
            return None
        return IncomingTransfer(
            chain="eth",
            txid=txid.lower(),
            sender=sender,
            amount_minor=amount_minor,
            height=height,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def eth_incoming_usdc(
    rpc_url: str,
    token: str,
    holder: str,
    *,
    from_block: int | None = None,
    lookback_blocks: int = 50_000,
    confirmations: int = 5,
    timeout: float = 20.0,
) -> list[IncomingTransfer]:
    """Inbound USDC transfers to ``holder`` from confirmed eth_getLogs.

    Filters server-side on topic[2] == holder so only incoming transfers
    match, and reads only up to ``latest - confirmations`` so reorged logs
    never become settlement evidence. Malformed log entries are skipped;
    transport/RPC failures raise.
    """
    holder_topic = _holder_topic(holder)
    with httpx.Client(timeout=timeout) as client:
        block_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_blockNumber",
            "params": [],
        }
        latest = _decode_quantity(_rpc_result(client.post(rpc_url, json=block_payload), 1))
        confirmed_head = max(0, latest - max(0, int(confirmations)))
        if from_block is None:
            start_block = max(0, confirmed_head - max(0, int(lookback_blocks)))
        else:
            start_block = max(0, int(from_block))
        if start_block > confirmed_head:
            return []
        logs_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_getLogs",
            "params": [
                {
                    "address": token,
                    "fromBlock": hex(start_block),
                    "toBlock": hex(confirmed_head),
                    "topics": [ERC20_TRANSFER_TOPIC, None, holder_topic],
                }
            ],
        }
        logs = _rpc_result(client.post(rpc_url, json=logs_payload), 2)
    if not isinstance(logs, list):
        raise ValueError("invalid eth_getLogs result")
    transfers: list[IncomingTransfer] = []
    for log in logs:
        transfer = _parse_eth_transfer_log(log)
        if transfer is not None:
            transfers.append(transfer)
    return transfers


def _sol_token_minor(ui_token_amount: Any) -> int:
    """Scale one Solana token amount to USDC's 6-decimal integer form."""
    if not isinstance(ui_token_amount, dict):
        raise ValueError("malformed Solana token amount")
    raw_text = ui_token_amount.get("amount")
    decimals = int(ui_token_amount.get("decimals"))
    if not isinstance(raw_text, str) or not raw_text.isdigit() or not 0 <= decimals <= 18:
        raise ValueError("malformed Solana token amount")
    raw = int(raw_text)
    if decimals <= 6:
        return raw * (10 ** (6 - decimals))
    divisor = 10 ** (decimals - 6)
    if raw % divisor:
        raise ValueError("token amount exceeds USDC precision")
    return raw // divisor


def _sol_owner_deltas(tx: dict[str, Any], mint: str) -> dict[str, int]:
    """Net 6dp balance change per owner for ``mint`` across the transaction."""
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("Solana transaction missing meta")
    deltas: dict[str, int] = {}
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        entries = meta.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError("malformed Solana token balances")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("mint") != mint:
                continue
            owner_key = entry.get("owner")
            if not isinstance(owner_key, str) or not owner_key:
                continue
            amount_minor = _sol_token_minor(entry.get("uiTokenAmount"))
            deltas[owner_key] = deltas.get(owner_key, 0) + sign * amount_minor
    return deltas


def _sol_memo(tx: dict[str, Any]) -> str | None:
    """Best-effort spl-memo text from the transaction's parsed instructions."""
    try:
        instructions = tx["transaction"]["message"]["instructions"]
    except (KeyError, TypeError):
        return None
    if not isinstance(instructions, list):
        return None
    for instruction in instructions:
        if not isinstance(instruction, dict):
            continue
        if instruction.get("program") not in {"spl-memo", "memo"}:
            continue
        parsed = instruction.get("parsed")
        if isinstance(parsed, str) and parsed:
            return parsed
    return None


def _sol_fee_payer(tx: dict[str, Any]) -> str:
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        first = keys[0]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(first, dict):
        return str(first.get("pubkey") or "")
    if isinstance(first, str):
        return first
    return ""


def _parse_sol_transaction(
    tx: Any,
    owner: str,
    mint: str,
    signature: str,
) -> IncomingTransfer | None:
    """One finalized Solana tx -> inbound USDC credit for ``owner``, if any."""
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta")
    if isinstance(meta, dict) and meta.get("err") is not None:
        return None
    deltas = _sol_owner_deltas(tx, mint)
    credited = deltas.get(owner, 0)
    if credited <= 0:
        return None
    debtors = [
        (delta, key)
        for key, delta in deltas.items()
        if key != owner and delta < 0
    ]
    if debtors:
        sender = min(debtors)[1]  # the largest outflow funded the credit
    else:
        sender = _sol_fee_payer(tx)
    return IncomingTransfer(
        chain="sol",
        txid=signature,
        sender=sender,
        amount_minor=credited,
        height=max(0, int(tx.get("slot") or 0)),
        memo=_sol_memo(tx),
    )


def sol_incoming_usdc(
    rpc_url: str,
    owner: str,
    mint: str,
    *,
    limit: int = 200,
    timeout: float = 20.0,
) -> list[IncomingTransfer]:
    """Best-effort inbound USDC credits to ``owner`` from recent signatures.

    Walks getSignaturesForAddress (finalized) and diffs pre/post token
    balances per transaction for the given mint. Individual transactions
    that fail to load or parse are skipped; only the signature listing
    itself failing raises, which callers treat as "this chain has no logs".
    """
    max_txs = int(limit)
    if max_txs <= 0:
        return []
    signatures_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [owner, {"limit": max_txs, "commitment": "finalized"}],
    }
    transfers: list[IncomingTransfer] = []
    with httpx.Client(timeout=timeout) as client:
        entries = _rpc_result(client.post(rpc_url, json=signatures_payload), 1)
        if not isinstance(entries, list):
            raise ValueError("invalid Solana signature listing")
        for index, entry in enumerate(entries[:max_txs]):
            if not isinstance(entry, dict) or entry.get("err") is not None:
                continue
            signature = entry.get("signature")
            if not isinstance(signature, str) or not signature:
                continue
            tx_payload = {
                "jsonrpc": "2.0",
                "id": index + 2,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "maxSupportedTransactionVersion": 0,
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                    },
                ],
            }
            try:
                tx = _rpc_result(client.post(rpc_url, json=tx_payload), index + 2)
                transfer = _parse_sol_transaction(tx, owner, mint, signature)
            except Exception:
                continue
            if transfer is not None:
                transfers.append(transfer)
    return transfers
