"""Verify a graduated LaunchLab coin is bought in its Raydium CPMM pool.

Once a LaunchLab curve migrates it takes no more trades, and the coin trades in
the CPMM pool it moved to. The bot could sell there but not buy: the buyer
routed a graduated coin to `CpmmMarket`, which refused, so nothing was sent.

Fixture: `raw_cpmm_buys_from_gettransaction.json` — four migrated LaunchLab
coins (SPL Token and Token-2022 coins paired with SOL, a Token-2022 coin paired
with USDC, and a reward coin with a 1% transfer fee paired with an xStock), with
the accounts a buy is built from and a real buy into each pool.

Offline machine checks, no network and no funds moved:

  A. Each buy names the same 13 accounts as the real buy into that pool, apart
     from the buyer's own quote account, with the coin's account under the
     coin's token program. An account is writable exactly when it is writable
     in every real buy: the transaction-level flags of a routed buy can only
     add writes another instruction needed.
  B. The data is `swap_base_input`, the real buys' layout: the discriminator,
     the quote to spend and the token floor.
  C. A SOL buy wraps exactly the spend in a throwaway account opened before the
     swap and closed after it; a USDC or xStock buy pays from the wallet's
     account under the quote's token program. Every buy creates the coin's
     account first, under the coin's token program.
  D. A buy is the sell turned round: the same accounts with the input and
     output sides swapped.
  E. The buyer buys a graduated coin in the pool, read through the real
     LaunchLab curve manager: the configured spend unpadded, a token floor net
     of the pool fee, the coin's transfer fee and slippage, the pool's
     priority-fee accounts and compute limit, and the spend read back at the
     pool's quote vault. A curve still trading is bought on the curve. A curve
     waiting for its pool, or one that migrated to AMM v4, sends nothing, and
     `extreme_fast_mode` skips a graduated coin.
  F. Every buy and sell fits one transaction, with all three compute-budget
     instructions and no lookup table.

Usage:
    uv run tests/regression/verify_cpmm_buy.py
"""

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import base58

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.compute_budget import (  # noqa: E402
    set_compute_unit_limit,
    set_compute_unit_price,
    set_loaded_accounts_data_size_limit,
)
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.system_program import decode_create_account_with_seed  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

from core.pubkeys import (  # noqa: E402
    TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
    WSOL_MINT,
    SystemAddresses,
    resolve_quote_token_program,
)
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms import PlatformImplementations  # noqa: E402
from platforms.launchlab.cpmm_market import CPMM_PROGRAM, CpmmMarket  # noqa: E402
from platforms.launchlab.curve_manager import LaunchLabCurveManager  # noqa: E402
from trading import platform_aware  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES = {
    c["label"]: c
    for c in json.loads((HERE / "raw_cpmm_buys_from_gettransaction.json").read_text())
}
SWAP_BASE_INPUT = hashlib.sha256(b"global:swap_base_input").digest()[:8]
# The most a serialized transaction may take.
PACKET_DATA_SIZE = 1232
# What `_build` asks for: raw quote in, raw token floor.
AMOUNT_IN, MIN_OUT = 1_000_000, 2_000_000
USER = Pubkey.from_string("7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
# LaunchLab PoolState: discriminator, epoch, auth_bump, then status and, two
# decimals bytes later, migrate_type.
STATUS_OFFSET, MIGRATE_TYPE_OFFSET = 17, 20
# LaunchLab PoolState.quote_vault: after the global and platform configs, both
# mints and the base vault.
CURVE_QUOTE_VAULT_OFFSET = 8 + 8 + 5 + 10 * 8 + 5 * 8 + 5 * 32
# The buyer's own quote account: a fresh wrapped-SOL account or their ATA.
BUYER_QUOTE_INDEX = 4
# Input and output sides of swap_base_input: the user's account, vault, token
# program and mint each come input first.
SIDE_PAIRS = ((4, 5), (6, 7), (8, 9), (10, 11))


class _FixtureClient:
    """Serves the fixture's accounts by address; anything else does not exist."""

    def __init__(self, accounts: dict) -> None:
        self.accounts = {
            Pubkey.from_string(k): SimpleNamespace(
                data=base64.b64decode(v["data"]), owner=Pubkey.from_string(v["owner"])
            )
            for k, v in accounts.items()
        }

    async def get_account_info(
        self,
        address: Pubkey,
        commitment: str | None = None,  # noqa: ARG002
    ) -> SimpleNamespace:
        if address not in self.accounts:
            raise ValueError(f"Account {address} not found")  # noqa: TRY003
        return self.accounts[address]

    async def get_multiple_accounts(
        self,
        addresses: list,
        commitment: str | None = None,  # noqa: ARG002
    ) -> list:
        return [self.accounts.get(a) for a in addresses]


def _resolve_quotes() -> None:
    """Seed each case's quote decimals and token program from its mint account."""
    for case in CASES.values():
        mint = Pubkey.from_string(case["quote_mint"])
        if mint == WSOL_MINT:
            data, owner = (
                bytes(44) + bytes([9]) + bytes(37),
                SystemAddresses.TOKEN_PROGRAM,
            )
        else:
            raw = case["accounts"][case["quote_mint"]]
            data, owner = (
                base64.b64decode(raw["data"]),
                Pubkey.from_string(raw["owner"]),
            )

        async def read(
            _address: Pubkey, data: bytes = data, owner: Pubkey = owner
        ) -> SimpleNamespace:
            return SimpleNamespace(data=data, owner=owner)

        asyncio.run(resolve_quote_token_program(mint, read))


def _token(case: dict) -> TokenInfo:
    curve = base64.b64decode(case["accounts"][case["launchlab_pool"]]["data"])
    return TokenInfo(
        name="",
        symbol="GRAD",
        uri="",
        mint=Pubkey.from_string(case["mint"]),
        platform=Platform(case["platform"]),
        pool_state=Pubkey.from_string(case["launchlab_pool"]),
        platform_config=Pubkey.from_string(case["platform_config"]),
        quote_mint=Pubkey.from_string(case["quote_mint"]),
        quote_vault=Pubkey.from_bytes(
            curve[CURVE_QUOTE_VAULT_OFFSET : CURVE_QUOTE_VAULT_OFFSET + 32]
        ),
    )


def _owner(case: dict, address: str) -> Pubkey:
    return Pubkey.from_string(case["accounts"][address]["owner"])


def _chain_buy(case: dict) -> tuple[list[str], list[bool], bytes]:
    """Accounts of the real buy into the pool, their transaction-level writable flags, data."""
    tx = case["buy_transaction"]
    message = tx["transaction"]["message"]
    loaded = tx["meta"].get("loadedAddresses") or {}
    static = message["accountKeys"]
    keys = static + loaded.get("writable", []) + loaded.get("readonly", [])
    header = message["header"]
    signers = header["numRequiredSignatures"]

    def writable(i: int) -> bool:
        if i >= len(static):
            return keys[i] in loaded.get("writable", [])
        if i < signers:
            return i < signers - header["numReadonlySignedAccounts"]
        return i < len(static) - header["numReadonlyUnsignedAccounts"]

    located = list(message["instructions"]) + [
        ix
        for g in tx["meta"].get("innerInstructions") or []
        for ix in g["instructions"]
    ]
    for ix in located:
        accounts = [keys[i] for i in ix["accounts"]]
        if (
            keys[ix["programIdIndex"]] == str(CPMM_PROGRAM)
            and accounts[3] == case["cpmm_pool"]
        ):
            flags = [writable(i) for i in ix["accounts"]]
            return accounts, flags, base58.b58decode(ix["data"])
    message = f"{case['label']}: fixture has no buy into the pool"
    raise AssertionError(message)


def _swap(instructions: list) -> object:
    return next(ix for ix in instructions if ix.program_id == CPMM_PROGRAM)


def _build(case: dict, user: Pubkey, *, side: str = "buy") -> list:
    market = CpmmMarket(_FixtureClient(case["accounts"]))
    build = getattr(market, f"build_{side}_instruction")
    return asyncio.run(build(_token(case), user, AMOUNT_IN, MIN_OUT))


def check_a_accounts_match_chain() -> bool:
    """A: same accounts as the real buys, bar the buyer's quote account."""
    ok = True
    every_buy = [_chain_buy(case)[1] for case in CASES.values()]
    writable = [all(flags) for flags in zip(*every_buy, strict=True)]
    for label, case in CASES.items():
        chain, _, _ = _chain_buy(case)
        buyer = Pubkey.from_string(chain[0])
        swap = _swap(_build(case, buyer))
        ours = [str(m.pubkey) for m in swap.accounts]
        flags = [m.is_writable for m in swap.accounts]
        problems = []
        differs = [
            i
            for i in range(len(chain))
            if i != BUYER_QUOTE_INDEX and ours[i] != chain[i]
        ]
        if len(ours) != len(chain) or differs:
            problems.append(
                f"{len(ours)} vs {len(chain)} accounts, differ at {differs}"
            )
        coin_account = get_associated_token_address(
            buyer, Pubkey.from_string(case["mint"]), _owner(case, case["mint"])
        )
        if ours[5] != str(coin_account):
            problems.append("coin account not the ATA under the coin's program")
        if not swap.accounts[0].is_signer or any(
            m.is_signer for m in swap.accounts[1:]
        ):
            problems.append("signer flags")
        if flags != writable:
            problems.append(f"writable {flags} vs the real buys' {writable}")
        if problems:
            print(f"    {label}: {'; '.join(problems)}")
            ok = False
    return ok


def check_b_data_is_swap_base_input() -> bool:
    """B: discriminator and two u64 amounts, as the real buys send."""
    ok = True
    expected = (
        SWAP_BASE_INPUT
        + AMOUNT_IN.to_bytes(8, "little")
        + MIN_OUT.to_bytes(8, "little")
    )
    for label, case in CASES.items():
        _, _, chain_data = _chain_buy(case)
        data = bytes(_swap(_build(case, USER)).data)
        if (
            data != expected
            or chain_data[:8] != SWAP_BASE_INPUT
            or len(chain_data) != len(data)
        ):
            print(f"    {label}: ours {data.hex()} chain {chain_data.hex()}")
            ok = False
    return ok


def _sol_settlement(built: list, swap: object) -> list[str]:
    """Problems with a SOL buy: wrap exactly the spend, pay from it, close it."""
    problems = []
    programs = [ix.program_id for ix in built]
    funded = decode_create_account_with_seed(built[0])
    account = funded["to_pubkey"]
    if programs != [
        SystemAddresses.SYSTEM_PROGRAM,
        SystemAddresses.TOKEN_PROGRAM,
        SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        CPMM_PROGRAM,
        SystemAddresses.TOKEN_PROGRAM,
    ]:
        problems.append(f"programs {programs}")
    if funded["lamports"] != AMOUNT_IN + TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE:
        problems.append(f"wrapped {funded['lamports']}")
    if swap.accounts[4].pubkey != account or built[-1].accounts[0].pubkey != account:
        problems.append("swap does not pay from the wrapped account, or it stays open")
    return problems


def _token_settlement(case: dict, built: list, swap: object) -> list[str]:
    """Problems with a USDC or xStock buy: pay from the ATA under the quote's program."""
    problems = []
    programs = [ix.program_id for ix in built]
    quote_program = _owner(case, case["quote_mint"])
    ata = get_associated_token_address(
        USER, Pubkey.from_string(case["quote_mint"]), quote_program
    )
    if programs != [
        SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        CPMM_PROGRAM,
    ]:
        problems.append(f"programs {programs}")
    if swap.accounts[4].pubkey != ata or swap.accounts[8].pubkey != quote_program:
        problems.append("swap does not pay from the quote ATA under its program")
    return problems


def check_c_settlement() -> bool:
    """C: SOL wraps exactly the spend in a throwaway account; others pay from the ATA."""
    ok = True
    for label, case in CASES.items():
        built = _build(case, USER)
        swap = _swap(built)
        mint = Pubkey.from_string(case["mint"])
        create_coin = next(
            ix
            for ix in built
            if ix.program_id == SystemAddresses.ASSOCIATED_TOKEN_PROGRAM
            and ix.accounts[3].pubkey == mint
        )
        problems = []
        if built.index(create_coin) != built.index(swap) - 1:
            problems.append("coin account not created right before the swap")
        if create_coin.accounts[5].pubkey != _owner(case, case["mint"]):
            problems.append("coin account created under the wrong program")
        if case["quote_mint"] == str(WSOL_MINT):
            problems += _sol_settlement(built, swap)
        else:
            problems += _token_settlement(case, built, swap)
        if problems:
            print(f"    {label}: {'; '.join(problems)}")
            ok = False
    return ok


def check_d_buy_mirrors_sell() -> bool:
    """D: the buy's accounts are the sell's with input and output swapped."""
    ok = True
    for label, case in CASES.items():
        if case["quote_mint"] == str(WSOL_MINT):
            continue  # the wrapped-SOL account sits at a fresh seed each time
        buy = [m.pubkey for m in _swap(_build(case, USER)).accounts]
        sell = [m.pubkey for m in _swap(_build(case, USER, side="sell")).accounts]
        turned = list(sell)
        for a, b in SIDE_PAIRS:
            turned[a], turned[b] = sell[b], sell[a]
        if buy != turned:
            print(f"    {label}: buy {buy} vs turned sell {turned}")
            ok = False
    return ok


def check_f_fits_one_transaction() -> bool:
    """F: every CPMM buy and sell fits one transaction.

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
        for side in ("buy", "sell"):
            instructions = budget + _build(case, payer.pubkey(), side=side)
            message = MessageV0.try_compile(
                payer.pubkey(), instructions, [], Hash.default()
            )
            sizes[f"{label} {side}"] = len(
                bytes(VersionedTransaction(message, [payer]))
            )
    too_large = {k: v for k, v in sizes.items() if v > PACKET_DATA_SIZE}
    if too_large:
        print(f"    over {PACKET_DATA_SIZE} bytes: {too_large}")
    return not too_large


class _StubClient:
    """Fixture reads, plus a send that records instead of sending."""

    def __init__(self, accounts: dict) -> None:
        self.fixture = _FixtureClient(accounts)
        self.sent: list = []
        self.destination: Pubkey | None = None

    async def get_account_info(self, *a: object, **k: object) -> object:
        return await self.fixture.get_account_info(*a, **k)

    async def get_multiple_accounts(self, *a: object, **k: object) -> list:
        return await self.fixture.get_multiple_accounts(*a, **k)

    async def build_and_send_transaction(
        self, instructions: list, *_a: object, **kwargs: object
    ) -> str:
        self.sent.append((instructions, kwargs["compute_unit_limit"]))
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a: object, **_k: object) -> bool:
        return True

    async def get_buy_transaction_details(
        self,
        _sig: str,
        _mint: Pubkey,
        destination: Pubkey,
        quote_mint: Pubkey,  # noqa: ARG002
    ) -> tuple[int, None]:
        self.destination = destination
        return 1_000_000_000, None

    async def get_token_account_balance(self, *_a: object, **_k: object) -> int:
        return 0


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


def _edit_pool(case: dict, changes: dict[int, int], *, drop_cpmm: bool) -> dict:
    """The case's accounts with LaunchLab pool bytes changed, the CPMM pool removed."""
    accounts = json.loads(json.dumps(case["accounts"]))
    pool = bytearray(base64.b64decode(accounts[case["launchlab_pool"]]["data"]))
    for offset, value in changes.items():
        pool[offset] = value
    accounts[case["launchlab_pool"]]["data"] = base64.b64encode(bytes(pool)).decode()
    if drop_cpmm:
        del accounts[case["cpmm_pool"]]
    return accounts


def _buy(
    case: dict, accounts: dict | None = None, *, extreme_fast: bool = False
) -> tuple[object, _StubClient, _CurveBuilder, list, CpmmMarket]:
    client = _StubClient(accounts or case["accounts"])
    builder = _CurveBuilder()
    market = CpmmMarket(client)
    fee_accounts: list = []

    async def fee(accounts: list) -> None:
        fee_accounts.extend(accounts)

    platform_aware.get_platform_implementations = lambda _p, _c: (
        PlatformImplementations(
            address_provider=SimpleNamespace(
                derive_pool_address=lambda _mint: Pubkey.new_unique()
            ),
            instruction_builder=builder,
            curve_manager=LaunchLabCurveManager(
                client, get_idl_manager().get_parser(Platform.STONK_FUN)
            ),
            event_parser=None,
            graduated_market=market,
        )
    )
    quote = Pubkey.from_string(case["quote_mint"])
    buyer = PlatformAwareBuyer(
        client,
        SimpleNamespace(pubkey=USER, keypair=None),
        SimpleNamespace(calculate_priority_fee=fee),
        amount=0.01,
        slippage=0.2,
        max_retries=1,
        extreme_fast_mode=extreme_fast,
        extreme_fast_token_amount=1_000,
        quote_amounts={} if quote == WSOL_MINT else {quote: 0.5},
    )
    result = asyncio.run(buyer.execute(_token(case)))
    return result, client, builder, fee_accounts, market


def _routed(case: dict, spend_whole: float, transfer_fee_bps: int) -> bool:
    """The buy the buyer sends for a graduated coin, against the pool's own numbers."""
    result, client, builder, fee_accounts, market = _buy(case)
    reference = CpmmMarket(_FixtureClient(case["accounts"]))
    state = asyncio.run(reference.get_market_state(_token(case)))
    pool = reference._pools[Pubkey.from_string(case["mint"])]  # noqa: SLF001
    spend = round(spend_whole * 10**pool.quote_decimals)
    expected_floor = int(
        spend_whole
        / state["price_per_token"]
        * (1 - state["fee_fraction"])
        * (1 - transfer_fee_bps / 10_000)
        * (1 - 0.2)
        * 1e6
    )
    swap = _swap(client.sent[0][0]) if client.sent else None
    data = bytes(swap.data) if swap else b""
    ok = (
        result.success
        and builder.calls == 0
        and swap is not None
        and int.from_bytes(data[8:16], "little") == spend
        and abs(int.from_bytes(data[16:24], "little") - expected_floor) <= 1
        and client.sent[0][1] == market.get_buy_compute_unit_limit()
        and fee_accounts
        == [pool.address, pool.base_vault, pool.quote_vault, CPMM_PROGRAM]
        and client.destination == pool.quote_vault
    )
    if not ok:
        print(
            f"    {case['label']}: success={result.success} {result.error_message} "
            f"builder={builder.calls} data={data.hex()} expected floor {expected_floor} "
            f"destination={client.destination}"
        )
    return ok


def check_e_buyer_routes_to_pool() -> bool:
    """E: graduated -> CPMM buy sized off the pool and the coin's transfer fee."""
    results = {
        "sol": _routed(CASES["sol spl"], 0.01, 0),
        "transfer fee": _routed(CASES["transfer fee"], 0.5, 100),
    }
    case = CASES["sol token-2022"]

    curve_result, curve_client, curve_builder, _, _ = _buy(
        case, _edit_pool(case, {STATUS_OFFSET: 0}, drop_cpmm=False)
    )
    results["on curve"] = (
        curve_result.success
        and curve_builder.calls == 1
        and curve_client.sent[0][0] == ["curve-buy"]
        and curve_client.destination == _token(case).quote_vault
    )

    for name, changes, says in (
        ("waiting for pool", {STATUS_OFFSET: 1}, "No CPMM pool"),
        ("amm v4", {MIGRATE_TYPE_OFFSET: 0}, "AMM v4"),
    ):
        held, held_client, held_builder, _, _ = _buy(
            case, _edit_pool(case, changes, drop_cpmm=True)
        )
        results[name] = (
            not held.success
            and not held_client.sent
            and held_builder.calls == 0
            and "graduated" in (held.error_message or "")
            and says in (held.error_message or "")
        )

    fast, fast_client, fast_builder, _, _ = _buy(case, extreme_fast=True)
    results["extreme_fast_mode"] = (
        not fast.success
        and not fast_client.sent
        and fast_builder.calls == 0
        and "graduated" in (fast.error_message or "")
    )
    if not all(results.values()):
        print(f"    {results}")
    return all(results.values())


def main() -> int:
    """Run every check and report."""
    _resolve_quotes()
    checks = [
        ("A: buy accounts match real buys", check_a_accounts_match_chain),
        ("B: data is swap_base_input", check_b_data_is_swap_base_input),
        ("C: quote settles exactly, coin account first", check_c_settlement),
        ("D: a buy is the sell turned round", check_d_buy_mirrors_sell),
        ("E: buyer buys a graduated coin in its pool", check_e_buyer_routes_to_pool),
        ("F: every trade fits one transaction", check_f_fits_one_transaction),
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
