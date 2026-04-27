# Upstream pump.fun IDLs

Source: https://github.com/pump-fun/pump-public-docs

Snapshot commit: `7de0b959fa2bdab379a2f75f5433d3de1e35d229` (2026-04-23)

Files:
- `pump.json` / `pump.ts` — pump.fun bonding curve program
- `pump_amm.json` / `pump_amm.ts` — pump-swap (PumpAMM) program
- `pump_fees.json` / `pump_fees.ts` — pump fees program

Kept here verbatim for reference and diffing against the IDLs the bot actually loads from `idl/`. Do not import these directly from runtime code.

To refresh:

```bash
git clone --depth 1 https://github.com/pump-fun/pump-public-docs.git /tmp/pump-public-docs
cp /tmp/pump-public-docs/idl/{pump,pump_amm,pump_fees}.{json,ts} idl/upstream/
# update commit hash above
```
