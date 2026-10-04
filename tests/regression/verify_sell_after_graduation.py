"""Verify a held coin that graduates is priced and sold in the pool it migrated to.

A bonding curve that completes takes no more trades: pump.fun hands the coin to
PumpSwap, Raydium LaunchLab to a CPMM pool. The bot only knew the curve. A
graduated pump.fun curve has zero reserves, so its decoder raised and every
price read and pre-sell refresh failed; a migrated LaunchLab pool keeps its
final reserves, so it reported a frozen price. Either way the exit sold into a
curve that refuses it, and the position was stranded.

Fixture: `raw_graduated_markets_from_getaccountinfo.json` — for one graduated
pump.fun coin and one graduated StonkFun coin: the curve, the AMM pool, its
token accounts and config, and a real sell into that pool.

Offline machine checks, no network and no funds moved:

  A. Both curve managers report `graduated` for the fixture curves, and
     `calculate_price` raises CurveGraduatedError instead of returning a price.
  B. The AMM pool is derived from the mint alone and matches the fixture's.
  C. Each market's sell names the same accounts as the real sell on chain,
     apart from the seller's own accounts and the randomly picked fee and
     buyback recipients, which come from PumpSwap's GlobalConfig.
  D. Each market prices off its pool: PumpSwap from the token-account balances
     plus the pool's virtual quote reserves, CPMM from the vault balances less
     the fees accrued in them.
  E. The seller sells a graduated coin through the market, floored by the
     market's fee and the coin's transfer fee; with the pool not readable yet
     it sends nothing and reports SUBMIT_FAILED.
  F. The trader's price read switches to the market when the curve graduates.

Usage:
    uv run tests/regression/verify_sell_after_graduation.py
"""

import asyncio
import base64
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.pubkeys import WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import (  # noqa: E402
    CurveGraduatedError,
    Platform,
    TokenInfo,
    TradeFailureReason,
)
from platforms.launchlab.cpmm_market import CPMM_PROGRAM, CpmmMarket  # noqa: E402
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from platforms.pumpfun.pumpswap_market import (  # noqa: E402
    _CONFIG_BUYBACK_RECIPIENTS_OFFSET,
    _CONFIG_PROTOCOL_RECIPIENTS_OFFSET,
    PUMP_AMM_PROGRAM,
    PumpSwapMarket,
    canonical_pool_address,
)
from platforms.stonkfun import StonkFunCurveManager  # noqa: E402
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareSeller  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

FIXTURE = json.loads(
    Path(__file__)
    .with_name("raw_graduated_markets_from_getaccountinfo.json")
    .read_text()
)
PUMP, LAUNCHLAB = FIXTURE["pumpfun"], FIXTURE["launchlab"]


class _FixtureClient:
    """Serves the fixture's accounts by address; anything else does not exist."""

    def __init__(self, accounts: dict) -> None:
        self.accounts = {
            Pubkey.from_string(k): SimpleNamespace(
                data=base64.b64decode(v["data"]), owner=Pubkey.from_string(v["owner"])
            )
            for k, v in accounts.items()
        }

    async def get_account_info(self, address: Pubkey, commitment: str | None = None):  # noqa: ARG002
        if address not in self.accounts:
            raise ValueError(f"Account {address} not found")  # noqa: TRY003
        return self.accounts[address]

    async def get_multiple_accounts(
        self, addresses: list, commitment: str | None = None
    ):  # noqa: ARG002
        return [self.accounts.get(a) for a in addresses]


def _pump_token() -> TokenInfo:
    return TokenInfo(
        name="",
        symbol="PUMP",
        uri="",
        mint=Pubkey.from_string(PUMP["mint"]),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.from_string(PUMP["curve"]),
        quote_mint=WSOL_MINT,
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
    )


def _launchlab_token() -> TokenInfo:
    return TokenInfo(
        name="",
        symbol="STONK",
        uri="",
        mint=Pubkey.from_string(LAUNCHLAB["mint"]),
        platform=Platform.STONK_FUN,
        pool_state=Pubkey.from_string(LAUNCHLAB["launchlab_pool"]),
        platform_config=Pubkey.from_string(LAUNCHLAB["platform_config"]),
        quote_mint=Pubkey.from_string(LAUNCHLAB["quote_mint"]),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
    )


def _chain_sell(fixture: dict, program: Pubkey) -> list[str]:
    """Accounts of the real sell into the fixture pool."""
    tx = fixture["sell_transaction"]
    loaded = tx["meta"].get("loadedAddresses") or {}
    keys = (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )
    instructions = list(tx["transaction"]["message"]["instructions"]) + [
        ix
        for group in tx["meta"].get("innerInstructions") or []
        for ix in group["instructions"]
    ]
    for ix in instructions:
        accounts = [keys[i] for i in ix["accounts"]]
        if keys[ix["programIdIndex"]] == str(program) and len(accounts) > 12:
            return accounts
    raise AssertionError("fixture has no sell into the pool")


def check_a_curves_report_graduation() -> bool:
    """A: graduated, and calculate_price refuses to price the curve."""
    parser = get_idl_manager().get_parser(Platform.PUMP_FUN)
    results = []
    for manager, pool in (
        (
            PumpFunCurveManager(_FixtureClient(PUMP["accounts"]), parser),
            PUMP["curve"],
        ),
        (
            StonkFunCurveManager(
                _FixtureClient(LAUNCHLAB["accounts"]),
                get_idl_manager().get_parser(Platform.STONK_FUN),
            ),
            LAUNCHLAB["launchlab_pool"],
        ),
    ):
        address = Pubkey.from_string(pool)
        state = asyncio.run(manager.get_pool_state(address))
        try:
            asyncio.run(manager.calculate_price(address))
            raised = False
        except CurveGraduatedError:
            raised = True
        results.append(state["graduated"] and raised)
    if not all(results):
        print(f"    pump.fun, LaunchLab: {results}")
    return all(results)


def check_b_pools_derive_from_the_mint() -> bool:
    """B: canonical PumpSwap pool and CPMM pool, no search."""
    pump_pool = canonical_pool_address(Pubkey.from_string(PUMP["mint"]), WSOL_MINT)
    market = CpmmMarket(_FixtureClient(LAUNCHLAB["accounts"]))
    cpmm_pool = asyncio.run(market._resolve(_launchlab_token())).address
    ok = str(pump_pool) == PUMP["pool"] and str(cpmm_pool) == LAUNCHLAB["cpmm_pool"]
    if not ok:
        print(f"    pumpswap {pump_pool}, cpmm {cpmm_pool}")
    return ok


def _config_recipients(offset: int) -> set[str]:
    config = base64.b64decode(
        next(
            v["data"]
            for k, v in PUMP["accounts"].items()
            if len(base64.b64decode(v["data"])) > 900
        )
    )
    return {
        str(Pubkey.from_bytes(config[offset + 32 * i : offset + 32 * (i + 1)]))
        for i in range(8)
    }


def check_c_sell_accounts_match_chain() -> bool:
    """C: same accounts as the on-chain sells, bar the seller's own and random picks."""
    ok = True
    for name, market, token, program in (
        (
            "pumpswap",
            PumpSwapMarket(_FixtureClient(PUMP["accounts"])),
            _pump_token(),
            PUMP_AMM_PROGRAM,
        ),
        (
            "cpmm",
            CpmmMarket(_FixtureClient(LAUNCHLAB["accounts"])),
            _launchlab_token(),
            CPMM_PROGRAM,
        ),
    ):
        fixture = PUMP if name == "pumpswap" else LAUNCHLAB
        chain = _chain_sell(fixture, program)
        seller = Pubkey.from_string(chain[1] if name == "pumpswap" else chain[0])
        instructions = asyncio.run(
            market.build_sell_instruction(token, seller, 1, 1, None)
        )
        ours = [
            str(m.pubkey)
            for m in next(
                ix for ix in instructions if ix.program_id == program
            ).accounts
        ]
        # The seller's own token accounts: the bot uses its ATA and a throwaway
        # WSOL account, the on-chain seller whatever it held.
        variable = {5, 6} if name == "pumpswap" else {4, 5}
        if name == "pumpswap":
            protocol = _config_recipients(_CONFIG_PROTOCOL_RECIPIENTS_OFFSET)
            buyback = _config_recipients(_CONFIG_BUYBACK_RECIPIENTS_OFFSET)
            # Recipient picks are random: check membership, skip their ATAs.
            if ours[9] not in protocol or chain[9] not in protocol:
                print(f"    {name}: fee recipient not from GlobalConfig")
                ok = False
            if ours[-2] not in buyback or chain[-2] not in buyback:
                print(f"    {name}: buyback recipient not from GlobalConfig")
                ok = False
            variable |= {9, 10, len(chain) - 2, len(chain) - 1}
        differs = [
            i
            for i in range(max(len(ours), len(chain)))
            if i not in variable
            and (i >= len(ours) or i >= len(chain) or ours[i] != chain[i])
        ]
        if differs or len(ours) != len(chain):
            print(
                f"    {name}: {len(ours)} vs {len(chain)} accounts, differ at {differs}"
            )
            ok = False
    return ok


def _amount(fixture: dict, address: Pubkey) -> int:
    data = base64.b64decode(fixture["accounts"][str(address)]["data"])
    return struct.unpack_from("<Q", data, 64)[0]


def check_d_markets_price_off_their_pools() -> bool:
    """D: price = reserves ratio as the program counts them."""
    pump_market = PumpSwapMarket(_FixtureClient(PUMP["accounts"]))
    pump_state = asyncio.run(pump_market.get_market_state(_pump_token()))
    pump_pool = pump_market._pools[Pubkey.from_string(PUMP["mint"])]
    pool_data = base64.b64decode(PUMP["accounts"][PUMP["pool"]]["data"])
    virtual = int.from_bytes(pool_data[245:261], "little", signed=True)
    pump_expected = ((_amount(PUMP, pump_pool.quote_account) + virtual) / 1e9) / (
        _amount(PUMP, pump_pool.base_account) / 1e6
    )

    cpmm_market = CpmmMarket(_FixtureClient(LAUNCHLAB["accounts"]))
    cpmm_state = asyncio.run(cpmm_market.get_market_state(_launchlab_token()))
    pool = cpmm_market._pools[Pubkey.from_string(LAUNCHLAB["mint"])]
    data = base64.b64decode(LAUNCHLAB["accounts"][LAUNCHLAB["cpmm_pool"]]["data"])
    fees = struct.unpack_from("<4Q", data, 8 + 320 + 5 + 8)
    creator = struct.unpack_from("<2Q", data, 8 + 320 + 5 + 56 + 2 + 6)
    accrued = [fees[i] + fees[2 + i] + creator[i] for i in (0, 1)]
    base_i = 0 if pool.base_is_token_0 else 1
    cpmm_expected = (
        (_amount(LAUNCHLAB, pool.quote_vault) - accrued[1 - base_i])
        / 10**pool.quote_decimals
    ) / (
        (_amount(LAUNCHLAB, pool.base_vault) - accrued[base_i]) / 10**pool.base_decimals
    )

    ok = (
        abs(pump_state["price_per_token"] - pump_expected) <= pump_expected * 1e-12
        and abs(cpmm_state["price_per_token"] - cpmm_expected) <= cpmm_expected * 1e-12
    )
    if not ok:
        print(
            f"    pumpswap {pump_state} vs {pump_expected}; cpmm {cpmm_state} vs {cpmm_expected}"
        )
    return ok


class _StubClient:
    def __init__(self) -> None:
        self.sent: list = []

    async def build_and_send_transaction(self, instructions, *_a, **_k) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction_detailed(self, *_a, **_k):
        return platform_aware.ConfirmationStatus.REVERTED


class _GraduatedCurve:
    async def get_pool_state(self, _pool, commitment=None) -> dict:  # noqa: ARG002
        return {"graduated": True, "quote_mint": WSOL_MINT, "transfer_fee_bps": 100}

    async def calculate_price(self, _pool) -> float:
        raise CurveGraduatedError("stub")


class _Market:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.minimum_out: int | None = None

    async def get_market_state(self, _token) -> dict:
        if not self.ready:
            raise ValueError("pool not created yet")  # noqa: TRY003
        return {"price_per_token": 0.00000005, "fee_fraction": 0.0125, "pool": "POOL"}

    async def build_sell_instruction(self, _token, _user, _amount, minimum_out, _ap):
        self.minimum_out = minimum_out
        return ["market-sell"]

    def get_required_accounts_for_sell(self, *_a) -> list:
        return []

    def get_sell_compute_unit_limit(self, _override=None) -> int:
        return 150_000


async def _no_fee(_accounts) -> None:
    return None


def _sell(market: _Market) -> tuple[object, _StubClient]:
    client = _StubClient()
    platform_aware.get_platform_implementations = lambda _p, _c: SimpleNamespace(
        address_provider=SimpleNamespace(),
        instruction_builder=SimpleNamespace(),
        curve_manager=_GraduatedCurve(),
        graduated_market=market,
    )
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=None),
        SimpleNamespace(calculate_priority_fee=_no_fee),
        slippage=0.2,
        max_retries=1,
    )
    token = TokenInfo(
        name="",
        symbol="T",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.STONK_FUN,
        pool_state=Pubkey.new_unique(),
        quote_mint=WSOL_MINT,
    )
    result = asyncio.run(seller.execute(token, 1_000_000.0, 0.00000005))
    return result, client


def check_e_seller_routes_to_market() -> bool:
    """E: graduated -> market sell with fee- and tax-net floor; not ready -> nothing sent."""
    ready = _Market(ready=True)
    _, client = _sell(ready)
    expected = int(1_000_000 * (1 - 0.01) * 0.00000005 * (1 - 0.0125) * (1 - 0.2) * 1e9)
    routed = (
        client.sent == [["market-sell"]]
        and abs((ready.minimum_out or 0) - expected) <= 1
    )

    result, client = _sell(_Market(ready=False))
    held = (
        not client.sent
        and not result.success
        and result.failure_reason is TradeFailureReason.SUBMIT_FAILED
    )
    if not (routed and held):
        print(
            f"    routed={routed} floor={ready.minimum_out} expected {expected} held={held}"
        )
    return routed and held


def check_f_trader_price_follows_graduation() -> bool:
    """F: CurveGraduatedError -> the market's price."""
    trader = SimpleNamespace(
        platform=Platform.STONK_FUN,
        platform_implementations=SimpleNamespace(
            curve_manager=_GraduatedCurve(), graduated_market=_Market(ready=True)
        ),
        _graduated_mints=set(),
        _get_pool_address=lambda _token: Pubkey.new_unique(),
    )
    token = TokenInfo(
        name="",
        symbol="T",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.STONK_FUN,
    )
    price = asyncio.run(UniversalTrader._read_price(trader, token))
    ok = price == 0.00000005
    if not ok:
        print(f"    price {price}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: curves report graduation", check_a_curves_report_graduation),
        ("B: AMM pools derive from the mint", check_b_pools_derive_from_the_mint),
        ("C: market sells match the chain", check_c_sell_accounts_match_chain),
        ("D: markets price off their pools", check_d_markets_price_off_their_pools),
        (
            "E: seller routes a graduated coin to the market",
            check_e_seller_routes_to_market,
        ),
        (
            "F: trader prices off the market after graduation",
            check_f_trader_price_follows_graduation,
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
