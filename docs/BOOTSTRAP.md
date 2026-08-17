# Bootstrap

The engine does not wait for a bank.

1. `pip install -e ".[dev]"`
2. `sovereign init` — identity + ETH/SOL wallets (encrypted in `data/secrets.enc`)
3. `claude login` — Claude Pro/Max subscription (not an API key)
4. `sovereign doctor`
5. `sovereign run --mode sim --ticks 30` — prove the loop
6. `sovereign dashboard` — observe only
7. Fill `sovereign inbox` items when you can (`sovereign reply hr_0001 ok=1`)

Optional later (Courier will ask; other plays keep running):

- Exchange API (spot, withdraw disabled) for live trading
- Stripe or merchant for fiat
- Domain DNS for email
- VPS API if you want the Operator to buy a box
- Upwork/Fiverr tokens if you want those boards (public boards already work)

Keys never go in git. `SOVEREIGN_CONFIRM_REVEAL=1 sovereign wallet --reveal` is the only mnemonic dump.
