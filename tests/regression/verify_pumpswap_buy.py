"""Verify a graduated pump.fun coin is bought in its canonical PumpSwap pool.

Once a pump.fun curve completes it takes no more trades, and the coin trades in
the PumpSwap pool it migrated to. The bot could sell there but not buy: the
buyer read the graduated curve, found no price and gave up, and in
`extreme_fast_mode` it sent a curve buy that reverts.

Fixture: `raw_pumpswap_buys_from_gettransaction.json` — three canonical pools
(SOL, USDC, and a SOL mayhem pool) with the accounts a buy is built from, and a
real buy into each.

Offline machine checks, no network and no funds moved:

  A. Each buy names the same accounts as the real buy into that pool, apart from
     the buyer's own token accounts and the fee and buyback recipients picked at
     random, which come from PumpSwap's GlobalConfig; a mayhem pool pays the
     reserved recipient. The IDL's 23 carry its writable and signer flags.
  B. The data is `buy_exact_quote_in`: the IDL discriminator, the quote to spend,
     the token floor and `track_volume` set — the real one's layout, which may
     leave the trailing `track_volume` out.
  C. A cashback pool adds only the accumulator's quote account to a buy,
     writable, and that plus the accumulator to a sell, after the fee program;
     `pool-v2` follows on a canonical pool only, and the buyback pair stays last.
  D. A SOL buy wraps exactly the amount it spends in the wallet's WSOL account
     and closes it after the swap; a USDC buy pays from the wallet's USDC
     account. Both create the base-token account first.
  E. The buyer buys a graduated coin in the pool: the configured spend unpadded,
     a token floor net of the pool fee and slippage, the pool's priority-fee
     accounts and compute limit, and the spend read back at the pool's quote
     account; a token program the listener guessed gives way to the pool's. A
     curve that has not graduated is still bought on the curve. A pool
     not readable yet sends nothing, and `extreme_fast_mode` skips a graduated
     coin rather than sending a curve buy.
  F. Every buy and sell fits one transaction, cashback pools included, with all
     three compute-budget instructions and no lookup table. The throwaway
     wrapped-SOL account the sells use left a SOL buy a few bytes under the
     limit and a cashback SOL buy over it.
  G. The pool's coin creator and cashback flag are read again for every trade:
     after fee sharing moves the coin creator, the next buy and sell on the
     same market name the new creator's vault, and a pool turned cashback adds
     its accounts. GlobalConfig is read with the pool in the same call: after
     its fee and buyback recipients and its LP fee change, the next trade names
     recipients from the new sets and prices with the new fee, and a build of a
     known pool makes that one read and no other.

Usage:
    uv run tests/regression/verify_pumpswap_buy.py
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

from _fakes import _deltas  # noqa: E402
from solders.compute_budget import (  # noqa: E402
    set_compute_unit_limit,
    set_compute_unit_price,
    set_loaded_accounts_data_size_limit,
)
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.system_program import decode_transfer  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

from core.pubkeys import (  # noqa: E402
    USDC_MINT,
    WSOL_MINT,
    SystemAddresses,
)
from core.trade_deltas import TradeDeltas  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from platforms.pumpfun.pumpswap_market import (  # noqa: E402
    _CONFIG_BUYBACK_RECIPIENTS_OFFSET,
    _CONFIG_LP_FEE_OFFSET,
    _CONFIG_PROTOCOL_RECIPIENTS_OFFSET,
    _CONFIG_RESERVED_RECIPIENT_OFFSET,
    _POOL_CASHBACK_OFFSET,
    _POOL_COIN_CREATOR_OFFSET,
    GLOBAL_CONFIG,
    PUMP_AMM_PROGRAM,
    PUMP_FEE_PROGRAM,
    PumpSwapMarket,
)
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES = {
    c["label"]: c
    for c in json.loads(
        (HERE / "raw_pumpswap_buys_from_gettransaction.json").read_text()
    )
}
IDL = json.loads((PROJECT_ROOT / "idl" / "pump_swap_idl.json").read_text())
IDL_BUY = next(i for i in IDL["instructions"] if i["name"] == "buy_exact_quote_in")
# The most a serialized transaction may take.
PACKET_DATA_SIZE = 1232
# Only PumpSwap's GlobalConfig is this large among a case's accounts.
GLOBAL_CONFIG_MIN_SIZE = 900
SPEND = 10_000_000  # the buyer's 0.01 SOL, in lamports
# What `_build` asks for: raw quote in, raw token floor.
AMOUNT_IN, MIN_OUT = 1_000_000, 2_000_000
USER = Pubkey.from_string("7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")


class _FixtureClient:
    """Serves the fixture's accounts by address; anything else does not exist."""

    def __init__(self, accounts: dict) -> None:
        self.accounts = {
            Pubkey.from_string(k): SimpleNamespace(
                data=base64.b64decode(v["data"]), owner=Pubkey.from_string(v["owner"])
            )
            for k, v in accounts.items()
        }
        self.calls: list[str] = []

    async def get_account_info(
        self,
        address: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> SimpleNamespace:
        self.calls.append("get_account_info")
        if address not in self.accounts:
            raise ValueError(f"Account {address} not found")  # noqa: TRY003
        return self.accounts[address]

    async def get_multiple_accounts(
        self,
        addresses: list,
        commitment: str | None = None,  # noqa: ARG002
    ) -> list:
        self.calls.append("get_multiple_accounts")
        return [self.accounts.get(a) for a in addresses]


def _token(case: dict) -> TokenInfo:
    return TokenInfo(
        name="",
        symbol="GRAD",
        uri="",
        mint=Pubkey.from_string(case["mint"]),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.from_string(case["curve"]),
        quote_mint=Pubkey.from_string(case["quote_mint"]),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
    )


def _config(case: dict) -> bytes:
    return next(
        base64.b64decode(v["data"])
        for v in case["accounts"].values()
        if len(base64.b64decode(v["data"])) > GLOBAL_CONFIG_MIN_SIZE
    )


def _recipients(config: bytes, offset: int, count: int) -> set[str]:
    return {
        str(Pubkey.from_bytes(config[offset + 32 * i : offset + 32 * (i + 1)]))
        for i in range(count)
    }


def _chain_buy(case: dict) -> tuple[list[str], bytes]:
    """Accounts and data of the real buy into the case's pool."""
    tx = case["buy_transaction"]
    loaded = tx["meta"].get("loadedAddresses") or {}
    keys = (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )
    for ix in list(tx["transaction"]["message"]["instructions"]) + [
        ix
        for g in tx["meta"].get("innerInstructions") or []
        for ix in g["instructions"]
    ]:
        accounts = [keys[i] for i in ix["accounts"]]
        if (
            keys[ix["programIdIndex"]] == str(PUMP_AMM_PROGRAM)
            and accounts[0] == case["pool"]
        ):
            return accounts, base58.b58decode(ix["data"])
    message = f"{case['label']}: fixture has no buy into the pool"
    raise AssertionError(message)


def _swap(instructions: list) -> object:
    return next(ix for ix in instructions if ix.program_id == PUMP_AMM_PROGRAM)


def _build(
    case: dict, user: Pubkey, *, side: str = "buy", accounts: dict | None = None
) -> list:
    market = PumpSwapMarket(_FixtureClient(accounts or case["accounts"]))
    build = getattr(market, f"build_{side}_instruction")
    return asyncio.run(build(_token(case), user, AMOUNT_IN, MIN_OUT))


def check_a_accounts_match_chain() -> bool:
    """A: same accounts as the real buys, bar the buyer's own and random picks."""
    ok = True
    flags = [
        (a.get("writable", False), a.get("signer", False)) for a in IDL_BUY["accounts"]
    ]
    for label, case in CASES.items():
        chain, _ = _chain_buy(case)
        buyer = Pubkey.from_string(chain[1])
        swap = _swap(_build(case, buyer))
        ours = [str(m.pubkey) for m in swap.accounts]
        config = _config(case)
        mayhem = "mayhem" in label
        fee_pool = (
            _recipients(config, _CONFIG_RESERVED_RECIPIENT_OFFSET, 1)
            if mayhem
            else _recipients(config, _CONFIG_PROTOCOL_RECIPIENTS_OFFSET, 8)
        )
        buyback = _recipients(config, _CONFIG_BUYBACK_RECIPIENTS_OFFSET, 8)
        problems = []
        if ours[9] not in fee_pool or chain[9] not in fee_pool:
            problems.append("fee recipient not from GlobalConfig")
        if mayhem and ours[9] != chain[9]:
            problems.append("mayhem pool not paying the reserved recipient")
        if ours[-2] not in buyback or chain[-2] not in buyback:
            problems.append("buyback recipient not from GlobalConfig")
        # The recipients' quote accounts, left out of the comparison below
        # because the recipients are random picks, are each one's own ATA, under
        # the quote program the real buy used.
        quote = Pubkey.from_string(case["quote_mint"])

        def ata(owner: str, program: Pubkey, quote: Pubkey = quote) -> str:
            return str(
                get_associated_token_address(Pubkey.from_string(owner), quote, program)
            )

        programs = [
            p
            for p in (SystemAddresses.TOKEN_PROGRAM, SystemAddresses.TOKEN_2022_PROGRAM)
            if ata(chain[9], p) == chain[10]
        ]
        if not programs or (
            ours[10] != ata(ours[9], programs[0])
            or ours[-1] != ata(ours[-2], programs[0])
        ):
            problems.append("a recipient's quote account is not its ATA")
        variable = {5, 6, 9, 10, len(chain) - 2, len(chain) - 1}
        differs = [
            i
            for i in range(max(len(ours), len(chain)))
            if i not in variable
            and (i >= len(ours) or i >= len(chain) or ours[i] != chain[i])
        ]
        if differs or len(ours) != len(chain):
            problems.append(
                f"{len(ours)} vs {len(chain)} accounts, differ at {differs}"
            )
        got = [(m.is_writable, m.is_signer) for m in swap.accounts[: len(flags)]]
        if got != flags:
            problems.append("IDL account flags differ")
        if problems:
            print(f"    {label}: {'; '.join(problems)}")
            ok = False
    return ok


def check_b_data_is_buy_exact_quote_in() -> bool:
    """B: discriminator, amounts, track_volume; the real one's layout."""
    case = CASES["sol mayhem"]
    chain_accounts, chain_data = _chain_buy(case)
    swap = _swap(_build(case, Pubkey.from_string(chain_accounts[1])))
    data = bytes(swap.data)
    expected = (
        bytes(IDL_BUY["discriminator"])
        + AMOUNT_IN.to_bytes(8, "little")
        + MIN_OUT.to_bytes(8, "little")
        + b"\x01"
    )
    ok = (
        data == expected
        and chain_data[:8] == bytes(IDL_BUY["discriminator"])
        and len(chain_data) in (len(data) - 1, len(data))
    )
    if not ok:
        print(
            f"    ours {data.hex()} vs expected {expected.hex()}; chain {chain_data.hex()}"
        )
    return ok


def _with_pool_bytes(case: dict, changes: dict[int, bytes]) -> dict:
    accounts = json.loads(json.dumps(case["accounts"]))
    pool = bytearray(base64.b64decode(accounts[case["pool"]]["data"]))
    for offset, value in changes.items():
        pool[offset : offset + len(value)] = value
    accounts[case["pool"]]["data"] = base64.b64encode(bytes(pool)).decode()
    return accounts


def check_c_cashback_and_pool_v2() -> bool:
    """C: cashback accounts per side, pool-v2 on canonical pools only, buyback last."""
    case = CASES["sol"]
    quote = WSOL_MINT
    accumulator = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(USER)], PUMP_AMM_PROGRAM
    )[0]
    accumulator_ata = get_associated_token_address(
        accumulator, quote, SystemAddresses.TOKEN_PROGRAM
    )
    pool_v2 = Pubkey.find_program_address(
        [b"pool-v2", bytes(Pubkey.from_string(case["mint"]))], PUMP_AMM_PROGRAM
    )[0]
    cashback = _with_pool_bytes(case, {_POOL_CASHBACK_OFFSET: b"\x01"})
    not_canonical = _with_pool_bytes(case, {_POOL_COIN_CREATOR_OFFSET: bytes(32)})

    def tail(side: str, accounts: dict | None) -> list[Pubkey]:
        metas = _swap(_build(case, USER, side=side, accounts=accounts)).accounts
        start = next(i for i, m in enumerate(metas) if m.pubkey == PUMP_FEE_PROGRAM)
        return [m.pubkey for m in metas[start + 1 : -2]]

    results = {
        "buy": tail("buy", None) == [pool_v2],
        "buy cashback": tail("buy", cashback) == [accumulator_ata, pool_v2],
        "sell cashback": tail("sell", cashback)
        == [accumulator_ata, accumulator, pool_v2],
        "buy not canonical": tail("buy", not_canonical) == [],
        "sell not canonical": tail("sell", not_canonical) == [],
    }
    lengths = {
        side: len(_swap(_build(case, USER, side=side)).accounts)
        for side in ("buy", "sell")
    }
    results["counts"] = lengths == {"buy": 26, "sell": 24}
    cashback_buy = _swap(_build(case, USER, accounts=cashback)).accounts
    results["buy cashback writable"] = any(
        m.pubkey == accumulator_ata and m.is_writable for m in cashback_buy
    )
    if not all(results.values()):
        print(f"    {results} {lengths}")
    return all(results.values())


def check_d_settlement() -> bool:
    """D: SOL wraps exactly amount_in in the WSOL ATA and closes it; USDC pays from its ATA."""
    sol = _build(CASES["sol"], USER)
    usdc = _build(CASES["usdc"], USER)
    programs = [str(ix.program_id) for ix in sol]
    wsol_ata = get_associated_token_address(
        USER, WSOL_MINT, SystemAddresses.TOKEN_PROGRAM
    )
    funded = decode_transfer(sol[1])
    ok_sol = (
        programs
        == [
            str(SystemAddresses.ASSOCIATED_TOKEN_PROGRAM),
            str(SystemAddresses.SYSTEM_PROGRAM),
            str(SystemAddresses.TOKEN_PROGRAM),
            str(SystemAddresses.ASSOCIATED_TOKEN_PROGRAM),
            str(PUMP_AMM_PROGRAM),
            str(SystemAddresses.TOKEN_PROGRAM),
        ]
        and sol[0].accounts[1].pubkey == wsol_ata
        and funded["to_pubkey"] == wsol_ata
        and funded["lamports"] == AMOUNT_IN
        and sol[2].accounts[0].pubkey == wsol_ata
        and sol[3].accounts[3].pubkey == Pubkey.from_string(CASES["sol"]["mint"])
        and _swap(sol).accounts[6].pubkey == wsol_ata
        and sol[5].accounts[0].pubkey == wsol_ata
    )
    usdc_programs = [str(ix.program_id) for ix in usdc]
    usdc_swap = _swap(usdc)
    ok_usdc = (
        usdc_programs
        == [
            str(SystemAddresses.ASSOCIATED_TOKEN_PROGRAM),
            str(SystemAddresses.ASSOCIATED_TOKEN_PROGRAM),
            str(PUMP_AMM_PROGRAM),
        ]
        and usdc_swap.accounts[6].pubkey
        == get_associated_token_address(USER, USDC_MINT, SystemAddresses.TOKEN_PROGRAM)
        and usdc[1].accounts[3].pubkey == Pubkey.from_string(CASES["usdc"]["mint"])
    )
    if not (ok_sol and ok_usdc):
        print(f"    sol {programs} funded {funded}; usdc {usdc_programs}")
    return ok_sol and ok_usdc


def check_f_fits_one_transaction() -> bool:
    """F: every PumpSwap buy and sell fits one transaction, cashback pools included.

    Built the way the client sends it: v0, no lookup tables, all three
    compute-budget instructions.
    """
    payer = Keypair()
    budget = [
        set_loaded_accounts_data_size_limit(12_500_000),
        set_compute_unit_limit(200_000),
        set_compute_unit_price(10_000),
    ]
    sizes = {}
    for label, case in CASES.items():
        cashback = _with_pool_bytes(case, {_POOL_CASHBACK_OFFSET: b"\x01"})
        for pool, accounts in (("", None), (" cashback", cashback)):
            for side in ("buy", "sell"):
                instructions = budget + _build(
                    case, payer.pubkey(), side=side, accounts=accounts
                )
                message = MessageV0.try_compile(
                    payer.pubkey(), instructions, [], Hash.default()
                )
                sizes[f"{label}{pool} {side}"] = len(
                    bytes(VersionedTransaction(message, [payer]))
                )
    too_large = {k: v for k, v in sizes.items() if v > PACKET_DATA_SIZE}
    if too_large:
        print(f"    over {PACKET_DATA_SIZE} bytes: {too_large}")
    return not too_large


class _StubClient:
    def __init__(self) -> None:
        self.sent: list = []
        self.destination: Pubkey | None = None

    async def build_and_send_transaction(
        self, instructions: list, *_a: object, **kwargs: object
    ) -> str:
        self.sent.append((instructions, kwargs["compute_unit_limit"]))
        return "STUB_SIGNATURE"

    async def confirm_transaction_detailed(
        self, *_a: object, **_k: object
    ) -> ConfirmationStatus:
        return ConfirmationStatus.SUCCESS

    async def get_trade_deltas(
        self,
        _sig: str,
        _wallet: Pubkey,
        _mint: Pubkey,
        _quote_mint: Pubkey,
        destination: Pubkey,
    ) -> TradeDeltas:
        self.destination = destination
        return _deltas(1_000_000_000, None)


class _Curve:
    def __init__(self, *, graduated: bool) -> None:
        self.graduated = graduated

    async def get_pool_state(
        self,
        _pool: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> dict:
        return {
            "graduated": self.graduated,
            "price_per_token": None if self.graduated else 0.00000004,
            "quote_mint": WSOL_MINT,
            "creator": str(USER),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
        }

    async def get_pool_state_and_token_program(
        self,
        pool: Pubkey,
        _mint: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> tuple[dict, None]:
        return await self.get_pool_state(pool), None


class _CurveBuilder:
    def __init__(self) -> None:
        self.calls = 0

    async def build_buy_instruction(self, *_a: object) -> list:
        self.calls += 1
        return ["curve-buy"]

    def get_required_accounts_for_buy(self, *_a: object) -> list:
        return []

    def get_buy_compute_unit_limit(self, _override: int | None = None) -> int:
        return 180_000


def _buy(
    *,
    graduated: bool,
    market: PumpSwapMarket | None,
    extreme_fast: bool = False,
    token: TokenInfo | None = None,
) -> tuple[object, _StubClient, _CurveBuilder, list]:
    client = _StubClient()
    builder = _CurveBuilder()
    fee_accounts: list = []

    async def fee(accounts: list) -> None:
        fee_accounts.extend(accounts)

    platform_aware.get_platform_implementations = lambda _p, _c: (
        PlatformImplementations(
            address_provider=SimpleNamespace(
                derive_pool_address=lambda _mint: Pubkey.new_unique(),
                derive_creator_vault=lambda _c: Pubkey.new_unique(),
                program_id=None,
            ),
            instruction_builder=builder,
            curve_manager=_Curve(graduated=graduated),
            event_parser=None,
            graduated_market=market,
        )
    )
    buyer = PlatformAwareBuyer(
        client,
        SimpleNamespace(pubkey=USER, keypair=None),
        SimpleNamespace(calculate_priority_fee=fee),
        amount=0.01,
        slippage=0.2,
        max_retries=1,
        extreme_fast_mode=extreme_fast,
        extreme_fast_token_amount=1_000,
    )
    result = asyncio.run(buyer.execute(token or _token(CASES["sol"])))
    return result, client, builder, fee_accounts


def check_e_buyer_routes_to_pool() -> bool:
    """E: graduated -> pool buy sized off the pool; otherwise unchanged."""
    case = CASES["sol"]
    market = PumpSwapMarket(_FixtureClient(case["accounts"]))
    state = asyncio.run(market.get_market_state(_token(case)))
    pool = market._pools[Pubkey.from_string(case["mint"])]  # noqa: SLF001

    result, client, builder, fee_accounts = _buy(graduated=True, market=market)
    expected_floor = int(
        0.01 / state["price_per_token"] * (1 - state["fee_fraction"]) * (1 - 0.2) * 1e6
    )
    swap = _swap(client.sent[0][0]) if client.sent else None
    data = bytes(swap.data) if swap else b""
    routed = (
        result.success
        and builder.calls == 0
        and swap is not None
        and int.from_bytes(data[8:16], "little") == SPEND
        and abs(int.from_bytes(data[16:24], "little") - expected_floor) <= 1
        and client.sent[0][1] == market.get_buy_compute_unit_limit()
        and fee_accounts == market.get_required_accounts_for_buy(_token(case), USER)
        and client.destination == pool.quote_account
    )

    _, curve_client, curve_builder, _ = _buy(graduated=False, market=market)
    on_curve = curve_builder.calls == 1 and curve_client.sent[0][0] == ["curve-buy"]

    # A listener's guess of the token program gives way to the pool's, so the
    # positions file and cleanup name the account the buy used.
    guessed = _token(case)
    guessed.token_program_id = next(
        p
        for p in (SystemAddresses.TOKEN_PROGRAM, SystemAddresses.TOKEN_2022_PROGRAM)
        if p != pool.base_program
    )
    _buy(graduated=True, market=market, token=guessed)
    program = guessed.token_program_id == pool.base_program

    not_ready = PumpSwapMarket(
        _FixtureClient({k: v for k, v in case["accounts"].items() if k != case["pool"]})
    )
    held_result, held_client, held_builder, _ = _buy(graduated=True, market=not_ready)
    held = (
        not held_result.success
        and not held_client.sent
        and held_builder.calls == 0
        and "graduated" in (held_result.error_message or "")
    )

    fast_result, fast_client, fast_builder, _ = _buy(
        graduated=True, market=market, extreme_fast=True
    )
    fast = (
        not fast_result.success
        and not fast_client.sent
        and fast_builder.calls == 0
        and "graduated" in (fast_result.error_message or "")
    )
    if not (routed and on_curve and held and fast and program):
        print(
            f"    routed={routed} on_curve={on_curve} held={held} fast={fast} "
            f"program={program} "
            f"floor={int.from_bytes(data[16:24], 'little') if data else None} "
            f"expected {expected_floor} destination={client.destination}"
        )
    return routed and on_curve and held and fast and program


def check_g_pool_reread_every_trade() -> bool:
    """G: a moved coin creator and a new cashback flag reach the next trade."""
    case = CASES["sol"]
    client = _FixtureClient(case["accounts"])
    market = PumpSwapMarket(client)
    token = _token(case)
    pool = Pubkey.from_string(case["pool"])

    def vault(creator: Pubkey) -> Pubkey:
        return Pubkey.find_program_address(
            [b"creator_vault", bytes(creator)], PUMP_AMM_PROGRAM
        )[0]

    def trade(side: str) -> list[Pubkey]:
        build = getattr(market, f"build_{side}_instruction")
        swap = _swap(asyncio.run(build(token, USER, AMOUNT_IN, MIN_OUT)))
        return [m.pubkey for m in swap.accounts]

    data = client.accounts[pool].data
    old_creator = Pubkey.from_bytes(
        data[_POOL_COIN_CREATOR_OFFSET : _POOL_COIN_CREATOR_OFFSET + 32]
    )
    first = trade("buy")
    new_creator = Pubkey.new_unique()
    moved = bytearray(data)
    moved[_POOL_COIN_CREATOR_OFFSET : _POOL_COIN_CREATOR_OFFSET + 32] = bytes(
        new_creator
    )
    moved[_POOL_CASHBACK_OFFSET] = 1
    client.accounts[pool] = SimpleNamespace(
        data=bytes(moved), owner=client.accounts[pool].owner
    )
    accumulator = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(USER)], PUMP_AMM_PROGRAM
    )[0]
    accumulator_ata = get_associated_token_address(
        accumulator, WSOL_MINT, SystemAddresses.TOKEN_PROGRAM
    )
    results = {"first buy, old vault": vault(old_creator) in first}
    for side in ("buy", "sell"):
        accounts = trade(side)
        results[f"{side}, new vault"] = (
            vault(new_creator) in accounts and vault(old_creator) not in accounts
        )
        results[f"{side}, cashback"] = accumulator_ata in accounts
    _global_config_change(client, market, token, results)
    if not all(results.values()):
        print(f"    {results}")
    return all(results.values())


def _global_config_change(
    client: _FixtureClient, market: PumpSwapMarket, token: TokenInfo, results: dict
) -> None:
    """New recipients and a new LP fee in GlobalConfig reach the next trade."""
    before = asyncio.run(market.get_market_state(token))["fee_fraction"]
    config = bytearray(client.accounts[GLOBAL_CONFIG].data)
    fees = [Pubkey.new_unique() for _ in range(8)]
    buybacks = [Pubkey.new_unique() for _ in range(8)]
    for offset, keys in (
        (_CONFIG_PROTOCOL_RECIPIENTS_OFFSET, fees),
        (_CONFIG_BUYBACK_RECIPIENTS_OFFSET, buybacks),
    ):
        config[offset : offset + 32 * len(keys)] = b"".join(bytes(k) for k in keys)
    lp_fee = struct.unpack_from("<Q", config, _CONFIG_LP_FEE_OFFSET)[0]
    struct.pack_into("<Q", config, _CONFIG_LP_FEE_OFFSET, lp_fee + 100)
    client.accounts[GLOBAL_CONFIG] = SimpleNamespace(
        data=bytes(config), owner=client.accounts[GLOBAL_CONFIG].owner
    )
    for side in ("buy", "sell"):
        build = getattr(market, f"build_{side}_instruction")
        client.calls = []
        swap = _swap(asyncio.run(build(token, USER, AMOUNT_IN, MIN_OUT)))
        accounts = [m.pubkey for m in swap.accounts]
        results[f"{side}, new recipients"] = (
            accounts[9] in fees and accounts[-2] in buybacks
        )
        # The pool and GlobalConfig in one round trip, nothing else.
        results[f"{side}, one read"] = client.calls == ["get_multiple_accounts"]
    after = asyncio.run(market.get_market_state(token))["fee_fraction"]
    results["new LP fee"] = abs(after - before - 100 / 10_000) < 1e-12  # noqa: PLR2004


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: buy accounts match real buys", check_a_accounts_match_chain),
        ("B: data is buy_exact_quote_in", check_b_data_is_buy_exact_quote_in),
        ("C: cashback, pool-v2 and buyback order", check_c_cashback_and_pool_v2),
        ("D: quote settles exactly", check_d_settlement),
        ("E: buyer buys a graduated coin in its pool", check_e_buyer_routes_to_pool),
        ("F: every trade fits one transaction", check_f_fits_one_transaction),
        ("G: the pool is read again for every trade", check_g_pool_reread_every_trade),
    ]
    passed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as e:  # noqa: BLE001
            print(f"    {type(e).__name__}: {e}")
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
