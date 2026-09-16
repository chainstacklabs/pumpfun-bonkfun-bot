"""Verify the PumpPortal listener accepts lets_bonk tokens.

`listener_type: "pumpportal"` on `platform: "lets_bonk"` detected nothing at all,
while the same feed kept the pump.fun side busy. The tokens were arriving and
the frames were being parsed - they were failing a required-field check in
`LetsBonkPumpPortalProcessor.process_token_data` before a `TokenInfo` was ever
built, which showed up only as a repeating

    Missing required fields in PumpPortal LetsBonk token data

Captured from the live feed on 2026-09-16: over 90 seconds it carried 24 pump
creates and 7 bonk creates, and the two payloads are not the same shape. A bonk
create carries no `name`, no `symbol` and no `uri`, where a pump create has all
three. Requiring name and symbol therefore rejected every bonk token that ever
arrived - 100% of them, which matches the field report on the original run.

Only `mint` and `traderPublicKey` cannot be derived from something else, so only
those two are required now. `name` and `symbol` are used for logging and for the
`filters.match_string` filter, so a bonk token gets a mint-derived placeholder
symbol and an empty name - see the check below that pins that consequence down.

Offline machine checks against committed fixtures, no network and no funds
moved. `--live` adds a read-only 90-second listen against the real feed:

  1. A real bonk create payload produces a TokenInfo.
  2. Its pool_state matches a pool address derived independently from the mint.
  3. A real pump create payload is unaffected.
  4. A payload missing mint or traderPublicKey is still rejected.
  5. Each payload is routed to the processor for its own pool.
  6. A bonk token gets a non-empty symbol, so logs stay readable, and
     match_string cannot match it - a documented consequence, not an accident.

Usage:
    uv run learning-examples/verify_pumpportal_bonk_fields.py
    uv run learning-examples/verify_pumpportal_bonk_fields.py --live
"""

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import websockets  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from platforms.letsbonk.address_provider import LetsBonkAddressProvider  # noqa: E402
from platforms.letsbonk.pumpportal_processor import (  # noqa: E402
    LetsBonkPumpPortalProcessor,
)
from platforms.pumpfun.pumpportal_processor import (  # noqa: E402
    PumpFunPumpPortalProcessor,
)

FIXTURE = Path(__file__).parent / "raw_new_token_from_pumpportal.json"
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
LIVE_LISTEN_SECONDS = 90

# The fields a bonk create does not carry. Asserted explicitly so a change in
# the feed that starts sending them is noticed rather than silently absorbed.
ABSENT_FROM_BONK = ("name", "symbol", "uri")


def _load_fixtures() -> dict:
    """Load the committed PumpPortal payloads.

    Returns:
        Mapping of pool name to raw payload
    """
    with FIXTURE.open() as handle:
        return json.load(handle)


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    """Print one check result.

    Args:
        label: What was checked
        passed: Whether it held
        detail: Evidence behind the verdict

    Returns:
        The value of `passed`
    """
    print(f"{'PASS' if passed else 'FAIL'}  {label}\n      {detail}")
    return passed


def check_bonk_payload_is_accepted(fixtures: dict) -> bool:
    """A real bonk create produces a TokenInfo.

    Args:
        fixtures: Committed payloads

    Returns:
        Whether the check passed
    """
    payload = fixtures["bonk"]
    missing = [field for field in ABSENT_FROM_BONK if field not in payload]
    token_info = LetsBonkPumpPortalProcessor().process_token_data(payload)
    return _check(
        "a real bonk create payload produces a TokenInfo",
        token_info is not None and missing == list(ABSENT_FROM_BONK),
        f"payload carries no {missing}; "
        f"result: {'TokenInfo' if token_info else 'None'} "
        f"mint={token_info.mint if token_info else '-'}",
    )


def check_pool_state_is_derived(fixtures: dict) -> bool:
    """The derived pool address matches an independent derivation.

    Args:
        fixtures: Committed payloads

    Returns:
        Whether the check passed
    """
    payload = fixtures["bonk"]
    token_info = LetsBonkPumpPortalProcessor().process_token_data(payload)
    expected = LetsBonkAddressProvider().derive_pool_address(
        Pubkey.from_string(payload["mint"])
    )
    return _check(
        "the pool address comes from the mint, not from the payload",
        token_info is not None and token_info.pool_state == expected,
        f"pool_state={token_info.pool_state if token_info else '-'}, "
        f"derived from mint={expected}",
    )


def check_pump_payload_unaffected(fixtures: dict) -> bool:
    """The pump.fun side still works.

    Args:
        fixtures: Committed payloads

    Returns:
        Whether the check passed
    """
    payload = fixtures["pump"]
    token_info = PumpFunPumpPortalProcessor().process_token_data(payload)
    return _check(
        "a real pump create payload is unaffected",
        token_info is not None and token_info.symbol == payload["symbol"],
        f"symbol={token_info.symbol if token_info else '-'}, "
        f"bonding_curve={token_info.bonding_curve if token_info else '-'}",
    )


def check_undeliverable_payloads_rejected(fixtures: dict) -> bool:
    """Payloads that cannot be traded are still rejected.

    Args:
        fixtures: Committed payloads

    Returns:
        Whether the check passed
    """
    processor = LetsBonkPumpPortalProcessor()
    no_mint = {k: v for k, v in fixtures["bonk"].items() if k != "mint"}
    no_creator = {k: v for k, v in fixtures["bonk"].items() if k != "traderPublicKey"}
    return _check(
        "a payload missing mint or traderPublicKey is still rejected",
        processor.process_token_data(no_mint) is None
        and processor.process_token_data(no_creator) is None,
        f"without mint: {processor.process_token_data(no_mint)}, "
        f"without traderPublicKey: {processor.process_token_data(no_creator)}",
    )


def check_routing(fixtures: dict) -> bool:
    """Each payload is claimed by the processor for its own pool.

    Args:
        fixtures: Committed payloads

    Returns:
        Whether the check passed
    """
    bonk, pump = LetsBonkPumpPortalProcessor(), PumpFunPumpPortalProcessor()
    return _check(
        "each payload is routed to the processor for its own pool",
        bonk.can_process(fixtures["bonk"])
        and not bonk.can_process(fixtures["pump"])
        and pump.can_process(fixtures["pump"])
        and not pump.can_process(fixtures["bonk"]),
        f"bonk processor claims bonk={bonk.can_process(fixtures['bonk'])} "
        f"pump={bonk.can_process(fixtures['pump'])}; pump processor claims "
        f"pump={pump.can_process(fixtures['pump'])} "
        f"bonk={pump.can_process(fixtures['bonk'])}",
    )


def check_placeholder_symbol(fixtures: dict) -> bool:
    """A bonk token is loggable, and match_string cannot match it.

    Args:
        fixtures: Committed payloads

    Returns:
        Whether the check passed
    """
    payload = fixtures["bonk"]
    token_info = LetsBonkPumpPortalProcessor().process_token_data(payload)
    mint = payload["mint"]
    expected_symbol = f"{mint[:4]}..{mint[-4:]}"
    return _check(
        "the symbol is a mint-derived placeholder, so match_string cannot match",
        token_info is not None
        and token_info.symbol == expected_symbol
        and token_info.name == "",
        f"symbol={token_info.symbol if token_info else '-'!r} "
        f"name={token_info.name if token_info else '-'!r} - "
        f"filters.match_string has nothing to match on for bonk tokens",
    )


async def check_live_feed() -> bool:
    """Listen to the real feed and confirm bonk creates now yield TokenInfo.

    Returns:
        Whether the check passed
    """
    bonk_seen = bonk_accepted = pump_seen = 0
    processor = LetsBonkPumpPortalProcessor()
    print(f"\nListening to {PUMPPORTAL_WS_URL} for {LIVE_LISTEN_SECONDS}s...")
    async with websockets.connect(PUMPPORTAL_WS_URL) as websocket:
        await websocket.send(json.dumps({"method": "subscribeNewToken"}))
        try:
            async with asyncio.timeout(LIVE_LISTEN_SECONDS):
                while True:
                    payload = json.loads(await websocket.recv())
                    if "mint" not in payload:
                        continue
                    if processor.can_process(payload):
                        bonk_seen += 1
                        if processor.process_token_data(payload) is not None:
                            bonk_accepted += 1
                    elif (payload.get("pool") or "").lower() == "pump":
                        pump_seen += 1
        except TimeoutError:
            pass

    return _check(
        "live feed: every bonk create is accepted",
        bonk_seen > 0 and bonk_accepted == bonk_seen,
        f"{bonk_accepted}/{bonk_seen} bonk create(s) accepted over "
        f"{LIVE_LISTEN_SECONDS}s ({pump_seen} pump create(s) alongside)",
    )


async def main() -> None:
    """Run every check and exit non-zero if any failed."""
    live = "--live" in sys.argv
    print("=" * 72)
    print("Verifying PumpPortal lets_bonk detection")
    print("=" * 72)

    fixtures = _load_fixtures()
    results = [
        check_bonk_payload_is_accepted(fixtures),
        check_pool_state_is_derived(fixtures),
        check_pump_payload_unaffected(fixtures),
        check_undeliverable_payloads_rejected(fixtures),
        check_routing(fixtures),
        check_placeholder_symbol(fixtures),
    ]
    if live:
        results.append(await check_live_feed())
    else:
        print("\n(skipping the live feed check; pass --live to include it)")

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
