import os

import pytest

from sovereign.capital.wallet import Wallet, generate_bundle


def test_generate_distinct_chains():
    a = generate_bundle()
    b = generate_bundle()
    assert a.eth_address.startswith("0x")
    assert a.eth_address != b.eth_address
    assert a.sol_address != b.sol_address
    assert len(a.mnemonic.split()) == 12


def test_persist_and_reload(tmp_path):
    w = Wallet(tmp_path / "s.enc", tmp_path / "master.key")
    first = w.load_or_create()
    w2 = Wallet(tmp_path / "s.enc", tmp_path / "master.key")
    second = w2.load_or_create()
    assert first.eth_address == second.eth_address
    assert first.sol_address == second.sol_address


def test_reveal_guard(tmp_path):
    w = Wallet(tmp_path / "s.enc", tmp_path / "master.key")
    w.load_or_create()
    os.environ.pop("SOVEREIGN_CONFIRM_REVEAL", None)
    with pytest.raises(PermissionError):
        w.reveal_mnemonic()
    os.environ["SOVEREIGN_CONFIRM_REVEAL"] = "1"
    assert len(w.reveal_mnemonic().split()) == 12
    os.environ.pop("SOVEREIGN_CONFIRM_REVEAL", None)
