"""Verify LaunchLab trades price, size and build the way the program settles them.

Every StonkFun and letsbonk.fun coin is a Raydium LaunchLab pool. The letsbonk
code this replaced priced off the pool's virtual reserves alone — fixed at
launch — so every pool read as its launch price forever; passed a slippage-
padded amount to `buy_exact_in`, which spends all of it; ignored the curve fee
and the Token-2022 transfer fee; and hardcoded wrapped SOL as the quote.

Fixture: `raw_stonkfun_trades_from_gettransaction.json`, real StonkFun trades —
a SOL-quoted standard sell, xStock-quoted reward trades (a buy of a 1%
transfer-fee coin, a sell of a 3% one), a STONK-quoted standard buy — each with the GlobalConfig,
PlatformConfig and base mint accounts it traded against. The program's own
TradeEvent in each transaction records the reserves before the trade.

Offline machine checks, no network and no funds moved:

  A. The curve manager reads the fee rate and transfer fee from the fixture
     accounts, and the curve math then reproduces each trade to the unit: a
     buy delivers exactly the buyer's token balance change, a sell pays exactly
     the event's amount out, and the pool receives exactly the sent amount less
     the transfer fee.
  B. The price comes from virtual plus real reserves, and differs from the
     launch price on every fixture.
  C. The builder's swap instruction names the same 18 accounts, in order, as
     the trade on chain — fee vaults keyed by the pool's own quote mint — and
     its writable flags match the IDL.
  D. The buyer hands an exact-in builder the configured amount unpadded, and
     floors tokens net of curve fee and transfer fee; a builder that is not
     exact-in still gets the padded ceiling.
  E. The seller's floor is net of transfer fee and curve fee.
  F. Single-token mode passes over a coin its filters refuse — an unconfigured
     quote asset, a transfer fee above `max_transfer_fee_bps` — and waits for
     one it can buy, instead of exiting on the first launch it sees.

Usage:
    uv run tests/regression/verify_launchlab_trades.py
"""

import asyncio
import base64
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import base58

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM  # noqa: E402
from platforms.launchlab.curve_math import (  # noqa: E402
    buy_amount_out,
    sell_amount_out,
    withheld_fee,
)
from platforms.launchlab.instruction_builder import _SWAP_ACCOUNTS  # noqa: E402
from platforms.stonkfun import (  # noqa: E402
    StonkFunAddressProvider,
    StonkFunCurveManager,
    StonkFunInstructionBuilder,
)
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = Path(__file__).with_name("raw_stonkfun_trades_from_gettransaction.json")
IDL = json.loads((PROJECT_ROOT / "idl" / "raydium_launchlab_idl.json").read_text())

# Anchor's emit_cpi wraps an event in a self-invocation with this tag.
EVENT_CPI_TAG = bytes.fromhex("e445a52e51cb9a1d")
TRADE_EVENT = bytes(
    next(e for e in IDL["events"] if e["name"] == "TradeEvent")["discriminator"]
)
BUY_EXACT_IN = bytes(
    next(i for i in IDL["instructions"] if i["name"] == "buy_exact_in")["discriminator"]
)

PARSER = get_idl_manager().get_parser(Platform.STONK_FUN)
PROVIDER = StonkFunAddressProvider()
TRADER = Pubkey.from_string("11111111111111111111111111111112")


class _FixtureClient:
    """Serves the fixture's config and mint accounts by address."""

    def __init__(self, accounts: dict[Pubkey, bytes]) -> None:
        self.accounts = accounts

    async def get_multiple_accounts(
        self,
        pubkeys: list[Pubkey],
        commitment: str | None = None,  # noqa: ARG002
    ) -> list[SimpleNamespace]:
        return [SimpleNamespace(data=self.accounts[k], owner=None) for k in pubkeys]


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _instructions(tx: dict):
    yield from tx["transaction"]["message"]["instructions"]
    for group in tx["meta"].get("innerInstructions") or []:
        yield from group["instructions"]


def _trade(entry: dict) -> dict:
    """The swap instruction, its TradeEvent, and the signer's balance changes."""
    tx = entry["transaction"]
    keys = _keys(tx)
    swap, event = None, None
    for ix in _instructions(tx):
        if keys[ix["programIdIndex"]] != str(LAUNCHLAB_PROGRAM):
            continue
        data = base58.b58decode(ix["data"])
        if data[:8] == EVENT_CPI_TAG and data[8:16] == TRADE_EVENT:
            body = data[16:]
            values = struct.unpack_from("<13Q", body, 32)
            event = dict(
                zip(
                    (
                        "total_base_sell",
                        "virtual_base",
                        "virtual_quote",
                        "real_base_before",
                        "real_quote_before",
                        "real_base_after",
                        "real_quote_after",
                        "amount_in",
                        "amount_out",
                        "protocol_fee",
                        "platform_fee",
                        "creator_fee",
                        "share_fee",
                    ),
                    values,
                    strict=True,
                )
            )
        elif swap is None and len(ix["accounts"]) == len(_SWAP_ACCOUNTS):
            swap = {"data": data, "accounts": [keys[i] for i in ix["accounts"]]}

    signer = tx["transaction"]["message"]["accountKeys"][0]

    def balance(section: str, mint: str) -> int:
        return sum(
            int(b["uiTokenAmount"]["amount"])
            for b in tx["meta"][section]
            if b.get("owner") == signer and b["mint"] == mint
        )

    base_delta = balance("postTokenBalances", entry["base_mint"]) - balance(
        "preTokenBalances", entry["base_mint"]
    )
    return {"swap": swap, "event": event, "base_delta": base_delta}


def _pool_before(entry: dict, event: dict, base_decimals: int = 6) -> dict:
    quote_decimals = (
        next(
            b["uiTokenAmount"]["decimals"]
            for b in entry["transaction"]["meta"]["postTokenBalances"]
            if b["mint"] == entry["quote_mint"]
        )
        if entry["quote_mint"] != str(WSOL_MINT)
        else 9
    )
    return {
        "virtual_base": event["virtual_base"],
        "virtual_quote": event["virtual_quote"],
        "real_base": event["real_base_before"],
        "real_quote": event["real_quote_before"],
        "base_decimals": base_decimals,
        "quote_decimals": quote_decimals,
        "global_config": Pubkey.from_string(entry["global_config"]),
        "platform_config": Pubkey.from_string(entry["platform_config"]),
        "base_mint": Pubkey.from_string(entry["base_mint"]),
        "status": 0,
    }


async def _priced_pool(entry: dict, event: dict) -> dict:
    """Run the fixture pool through the curve manager's fee and price step."""
    accounts = {
        Pubkey.from_string(entry[name]): base64.b64decode(
            entry["accounts_base64"][name]
        )
        for name in ("global_config", "platform_config", "base_mint")
    }
    manager = StonkFunCurveManager(_FixtureClient(accounts), PARSER)
    return await manager._with_fees(_pool_before(entry, event), None)


def check_a_math_reproduces_trades() -> bool:
    """A: fee rate and transfer fee read from accounts; amounts exact to the unit."""
    ok = True
    for entry in json.loads(FIXTURE.read_text()):
        trade = _trade(entry)
        event = trade["event"]
        pool = asyncio.run(_priced_pool(entry, event))
        tax = (pool["transfer_fee_bps"], pool["transfer_fee_max"])
        is_buy = trade["swap"]["data"][:8] == BUY_EXACT_IN
        if is_buy:
            predicted = buy_amount_out(pool, event["amount_in"], pool["fee_rate"], tax)
            good = (
                predicted
                == trade["base_delta"]
                == event["amount_out"] - withheld_fee(event["amount_out"], *tax)
            )
            detail = f"predicted {predicted}, delivered {trade['base_delta']}"
        else:
            sent = -trade["base_delta"]
            predicted = sell_amount_out(pool, sent, pool["fee_rate"], tax)
            good = predicted == event["amount_out"] and event[
                "amount_in"
            ] == sent - withheld_fee(sent, *tax)
            detail = f"predicted {predicted}, paid {event['amount_out']}"
        print(
            f"    {entry['label']}: fee {pool['fee_rate']} ppm, tax "
            f"{pool['transfer_fee_bps']} bps, {detail}"
        )
        ok &= good
    return ok


def check_b_price_uses_real_reserves() -> bool:
    """B: price from virtual + real reserves, not the launch price."""
    ok = True
    for entry in json.loads(FIXTURE.read_text()):
        event = _trade(entry)["event"]
        pool = asyncio.run(_priced_pool(entry, event))
        scale = 10 ** pool["base_decimals"] / 10 ** pool["quote_decimals"]
        expected = (
            (event["virtual_quote"] + event["real_quote_before"])
            / (event["virtual_base"] - event["real_base_before"])
            * scale
        )
        launch = event["virtual_quote"] / event["virtual_base"] * scale
        good = (
            abs(pool["price_per_token"] - expected) <= expected * 1e-12
            and abs(pool["price_per_token"] - launch) > launch * 1e-6
        )
        if not good:
            print(
                f"    {entry['label']}: price {pool['price_per_token']} vs {expected}"
            )
        ok &= good
    return ok


def check_c_builder_matches_chain() -> bool:
    """C: the builder's 18 accounts equal the on-chain trade's, flags per the IDL."""
    ok = True
    builder = StonkFunInstructionBuilder(PARSER)
    for entry in json.loads(FIXTURE.read_text()):
        swap = _trade(entry)["swap"]
        chain = swap["accounts"]
        user = Pubkey.from_string(chain[0])
        token_info = TokenInfo(
            name="",
            symbol="",
            uri="",
            mint=Pubkey.from_string(entry["base_mint"]),
            platform=Platform.STONK_FUN,
            pool_state=Pubkey.from_string(entry["pool"]),
            global_config=Pubkey.from_string(entry["global_config"]),
            platform_config=Pubkey.from_string(entry["platform_config"]),
            creator=Pubkey.from_string(entry["creator"]),
            token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
            quote_mint=Pubkey.from_string(entry["quote_mint"]),
            quote_token_program_id=Pubkey.from_string(chain[12]),
        )
        is_buy = swap["data"][:8] == BUY_EXACT_IN
        build = (
            builder.build_buy_instruction if is_buy else builder.build_sell_instruction
        )
        instructions = asyncio.run(build(token_info, user, 1, 1, PROVIDER))
        ours = next(ix for ix in instructions if ix.program_id == LAUNCHLAB_PROGRAM)
        built = [str(meta.pubkey) for meta in ours.accounts]
        # user_quote_token is the trader's own choice of account: a throwaway
        # WSOL account for SOL, usually the ATA otherwise.
        mismatched = [
            name
            for i, (name, _) in enumerate(_SWAP_ACCOUNTS)
            if built[i] != chain[i] and name != "user_quote_token"
        ]
        if mismatched:
            print(f"    {entry['label']}: differs at {mismatched}")
        ok &= not mismatched

    idl_swap = next(i for i in IDL["instructions"] if i["name"] == "buy_exact_in")
    idl_writable = {a["name"]: bool(a.get("writable")) for a in idl_swap["accounts"]}
    flag_mismatch = [
        name
        for name, writable in _SWAP_ACCOUNTS
        if name in idl_writable and name != "payer" and writable != idl_writable[name]
    ]
    if flag_mismatch:
        print(f"    writable flags differ from the IDL at {flag_mismatch}")
    return ok and not flag_mismatch


class _StubClient:
    async def build_and_send_transaction(
        self, *_args: object, **_kwargs: object
    ) -> str:
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_args: object, **_kwargs: object) -> bool:
        return False

    async def confirm_transaction_detailed(self, *_args: object, **_kwargs: object):
        return platform_aware.ConfirmationStatus.REVERTED


def _stub_implementations(pool_state: dict, captured: dict, *, exact_in: bool):
    async def build(_token_info, _user, amount_in, minimum_out, _provider):
        captured["amount_in"], captured["minimum_out"] = amount_in, minimum_out
        return ["stub-instruction"]

    class Curve:
        async def get_pool_state(self, _pool, commitment=None):  # noqa: ARG002
            return pool_state

    return SimpleNamespace(
        address_provider=PROVIDER,
        instruction_builder=SimpleNamespace(
            spends_exact_amount_in=exact_in,
            build_buy_instruction=build,
            build_sell_instruction=build,
            get_required_accounts_for_buy=lambda *_a, **_k: [],
            get_required_accounts_for_sell=lambda *_a, **_k: [],
            get_buy_compute_unit_limit=lambda _o: 150_000,
            get_sell_compute_unit_limit=lambda _o: 150_000,
        ),
        curve_manager=Curve(),
    )


async def _no_fee(_accounts: list) -> None:
    return None


POOL_STATE = {
    "price_per_token": 0.00000003,
    "fee_fraction": 0.0125,
    "transfer_fee_bps": 100,
    "quote_mint": WSOL_MINT,
    "creator": str(TRADER),
}
TOKEN = TokenInfo(
    name="T",
    symbol="T",
    uri="",
    mint=TRADER,
    platform=Platform.STONK_FUN,
    pool_state=TRADER,
    quote_mint=WSOL_MINT,
    creator=TRADER,
)


def _buy(*, exact_in: bool) -> dict:
    captured: dict = {}
    platform_aware.get_platform_implementations = lambda _p, _c: _stub_implementations(
        POOL_STATE, captured, exact_in=exact_in
    )
    buyer = PlatformAwareBuyer(
        _StubClient(),
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        amount=0.01,
        slippage=0.2,
        max_retries=1,
    )
    asyncio.run(buyer.execute(TOKEN))
    return captured


def check_d_buyer_sizing() -> bool:
    """D: exact-in gets the unpadded amount; the token floor is net of fee and tax."""
    exact = _buy(exact_in=True)
    padded = _buy(exact_in=False)
    expected_tokens = 0.01 / POOL_STATE["price_per_token"] * (1 - 0.0125) * (1 - 0.01)
    expected_floor = int(expected_tokens * (1 - 0.2) * 10**6)
    ok = (
        exact["amount_in"] == 10_000_000
        and padded["amount_in"] == int(0.01 * 10**9 * 1.2)
        and abs(exact["minimum_out"] - expected_floor) <= 1
    )
    if not ok:
        print(f"    exact={exact} padded={padded} expected floor {expected_floor}")
    return ok


def check_e_seller_floor() -> bool:
    """E: the sell floor is net of transfer fee and curve fee."""
    captured: dict = {}
    platform_aware.get_platform_implementations = lambda _p, _c: _stub_implementations(
        POOL_STATE, captured, exact_in=True
    )
    seller = PlatformAwareSeller(
        _StubClient(),
        SimpleNamespace(pubkey=TRADER, keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        slippage=0.2,
        max_retries=1,
    )
    tokens, price = 1_000_000.0, 0.00000003
    asyncio.run(seller.execute(TOKEN, tokens, price))
    expected = int(tokens * (1 - 0.01) * price * (1 - 0.0125) * (1 - 0.2) * 10**9)
    ok = abs(captured.get("minimum_out", -10) - expected) <= 1
    if not ok:
        print(f"    floor {captured.get('minimum_out')} expected {expected}")
    return ok


def check_f_single_token_mode_waits_past_refused_coins() -> bool:
    """F: the first launch is an xStock coin, the second taxed 3%, the third buyable."""
    spcxx = Pubkey.from_string("Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8")
    launches = [
        TokenInfo(
            name="",
            symbol="XSTOCK",
            uri="",
            mint=Pubkey.new_unique(),
            platform=Platform.STONK_FUN,
            quote_mint=spcxx,
        ),
        TokenInfo(
            name="",
            symbol="TAXED",
            uri="",
            mint=Pubkey.new_unique(),
            platform=Platform.STONK_FUN,
            quote_mint=WSOL_MINT,
            transfer_fee_bps=300,
        ),
        TokenInfo(
            name="",
            symbol="BUYABLE",
            uri="",
            mint=Pubkey.new_unique(),
            platform=Platform.STONK_FUN,
            quote_mint=WSOL_MINT,
            transfer_fee_bps=100,
        ),
    ]

    class Listener:
        async def listen_for_tokens(self, callback, *_args):
            # Launches arrive apart, so the waiter can wake on the first one
            # before the next overwrites it.
            for launch in launches:
                await callback(launch)
                await asyncio.sleep(0.05)
            await asyncio.sleep(3600)

    trader = SimpleNamespace(
        token_listener=Listener(),
        match_string=None,
        bro_address=None,
        token_wait_timeout=5,
        processed_tokens=set(),
        token_timestamps={},
        allowed_quote_mints=None,
        quote_amounts={WSOL_MINT: 0.001},
        max_transfer_fee_bps=100,
    )
    trader._skip_reason = lambda token: UniversalTrader._skip_reason(trader, token)
    found = asyncio.run(UniversalTrader._wait_for_token(trader))
    ok = found is not None and found.symbol == "BUYABLE"
    if not ok:
        print(f"    picked {found and found.symbol}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: curve math reproduces real trades", check_a_math_reproduces_trades),
        ("B: price from virtual + real reserves", check_b_price_uses_real_reserves),
        ("C: builder accounts match the chain", check_c_builder_matches_chain),
        ("D: buyer sizing for exact-in", check_d_buyer_sizing),
        ("E: seller floor net of fees", check_e_seller_floor),
        (
            "F: single-token mode waits past refused coins",
            check_f_single_token_mode_waits_past_refused_coins,
        ),
    ]
    passed = 0
    for label, check in checks:
        ok = check()
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
