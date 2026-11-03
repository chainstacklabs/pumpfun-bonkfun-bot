"""Verify the bot still builds, parses and decides exactly what it did when locked.

A refactor that moves code around must not move a byte of what reaches the
chain or what the bot decides from it. This verifier runs the bot's own code
over fixed inputs and compares the result with JSON snapshots committed under
`tests/regression/snapshots/`:

  A. `instructions.json`: every instruction, account (address, signer,
     writable) and data byte of each buy and sell the bot can build, with the
     accounts the priority-fee estimate reads and the compute-unit limit:
     pump.fun curve buy and sell (SOL standard, SOL mayhem, USDC), PumpSwap
     buy (SOL, USDC, SOL mayhem) and sell, LaunchLab buy and sell (letsbonk SOL, StonkFun reward SOL and
     xStock), CPMM sell. Fee recipients picked at random and the throwaway
     wrapped-SOL seed are pinned to their first choice so the bytes repeat.
  B. `parsers.json`: parser output for every `raw_*.json` fixture here —
     LaunchLab launches over the block and geyser routes, the preset parsers
     on StonkFun's and the every-config parser on other launchpads';
     graduated curves and AMM pools, and the pools of real PumpSwap buys;
     StonkFun trade instructions and the fee
     and transfer-fee read for their pools; withheld fees in token accounts;
     LaunchLab launches as the shreds listener decodes them pre-execution.
     `creation_timestamp` is a monotonic clock reading and is left out.
  C. `exit_decisions.json`: `Position.should_exit` and `should_exit_on_time`
     at each point of a recorded pump.fun price series
     (`recorded_pumpfun_prices_from_gettransaction.json`, curve prices from
     the program's TradeEvents, on the series' own clock), for the
     take-profit / stop-loss / max-hold combinations the `tp_sl` strategy
     builds, plus the boundary prices. `time_based` and `manual` never build a
     Position.

Offline, no network and no funds moved.

Usage:
    uv run tests/regression/verify_behaviour_snapshots.py
    uv run tests/regression/verify_behaviour_snapshots.py --update   # rewrite

Only rewrite a snapshot for a change that means to alter that behaviour, and
say which entries changed and why in the change that does it.
"""

import asyncio
import base64
import copy
import dataclasses
import json
import random
import secrets
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import base58

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.instruction import Instruction  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from cleanup.manager import withheld_transfer_fee  # noqa: E402
from core.pubkeys import USDC_MINT, WSOL_MINT, SystemAddresses  # noqa: E402
from geyser.generated import geyser_pb2  # noqa: E402
from interfaces.core import (  # noqa: E402
    AddressProvider,
    InstructionBuilder,
    Market,
    Platform,
    TokenInfo,
)
from monitoring.universal_shreds_listener import UniversalShredsListener  # noqa: E402
from platforms.curve_market import CurveMarket  # noqa: E402
from platforms.launchlab import LAUNCHLAB_PROGRAM, LaunchLabEventParser  # noqa: E402
from platforms.launchlab.cpmm_market import CpmmMarket  # noqa: E402
from platforms.letsbonk import (  # noqa: E402
    PLATFORM_CONFIGS as LETSBONK_CONFIGS,
)
from platforms.letsbonk import (  # noqa: E402
    LetsBonkAddressProvider,
    LetsBonkEventParser,
    LetsBonkInstructionBuilder,
)
from platforms.pumpfun.address_provider import PumpFunAddressProvider  # noqa: E402
from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402
from platforms.pumpfun.instruction_builder import (  # noqa: E402
    PumpFunInstructionBuilder,
)
from platforms.pumpfun.pumpswap_market import PumpSwapMarket  # noqa: E402
from platforms.stonkfun import (  # noqa: E402
    StonkFunAddressProvider,
    StonkFunCurveManager,
    StonkFunEventParser,
    StonkFunInstructionBuilder,
)
from platforms.stonkfun.addresses import REWARD_PLATFORM_CONFIG  # noqa: E402
from trading import position as position_module  # noqa: E402
from trading.position import Position  # noqa: E402
from utils.idl_manager import get_idl_manager  # noqa: E402

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE / "snapshots"

# Fixed inputs. Any valid pubkey would do; these never change.
TRADER = Pubkey.from_string("7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
PUMP_MINT = Pubkey.from_string("CU7nUQaJ4beyYjC3xAUrh5RiSjw14fhU6oWTwRBse8gj")
PUMP_CREATOR = Pubkey.from_string("5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX")
LAUNCH_MINT = Pubkey.from_string("9mHx4Z3d7xd8Dq5pE7QWvHq9JNj3yNnN3ZL9nQdHbonk")
LAUNCH_CREATOR = Pubkey.from_string("3y1jpFtUBYepN6HvSqoT16xPMXbWKCspE4E7xUVr7mQh")
LAUNCHLAB_GLOBAL_CONFIG = Pubkey.from_string(
    "6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"
)

PUMP_BUY = (2_000_000, 50_000_000_000)  # (max lamports in, exact tokens out)
PUMP_SELL = (50_000_000_000, 1_000_000)  # (tokens in, min quote out)
USDC_BUY = (5_000_000, 50_000_000_000)
USDC_SELL = (50_000_000_000, 1_000_000)
LAUNCH_BUY = (10_000_000, 20_000_000_000)
LAUNCH_SELL = (20_000_000_000, 5_000_000)


# --- determinism -------------------------------------------------------------


def _pin_random_choices() -> None:
    """Make every random pick the bot makes take its first option.

    Fee and buyback recipients are spread at random and the wrapped-SOL
    settlement account sits at a random seed; the program accepts any of
    them, so a snapshot pins the pick instead of the choice.
    """
    secrets.choice = lambda seq: seq[0]
    secrets.token_hex = lambda nbytes=None: "00" * (nbytes or 32)
    random.choice = lambda seq: seq[0]


# --- serialization -----------------------------------------------------------


def _plain(value: object) -> object:  # noqa: PLR0911
    """Turn bot values into JSON: pubkeys and enums as strings, bytes as hex."""
    if isinstance(value, Pubkey):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes | bytearray):
        return value.hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, float | int | str | bool) or value is None:
        return value
    return str(value)


def _instruction(ix: Instruction) -> dict:
    return {
        "program": str(ix.program_id),
        "accounts": [
            [str(m.pubkey), int(m.is_signer), int(m.is_writable)] for m in ix.accounts
        ],
        "data": bytes(ix.data).hex(),
    }


def _token_info(token: TokenInfo | None) -> dict | None:
    if token is None:
        return None
    fields = _plain(token)
    fields.pop("creation_timestamp")
    return fields


# --- A: instructions ---------------------------------------------------------


def _pump_token(
    quote_mint: Pubkey, quote_program: Pubkey, *, mayhem: bool
) -> TokenInfo:
    provider = PumpFunAddressProvider()
    curve = provider.derive_pool_address(PUMP_MINT)
    return TokenInfo(
        name="snapshot",
        symbol="SNAP",
        uri="",
        mint=PUMP_MINT,
        platform=Platform.PUMP_FUN,
        bonding_curve=curve,
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            PUMP_MINT, curve, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        user=PUMP_CREATOR,
        creator=PUMP_CREATOR,
        creator_vault=provider.derive_creator_vault(PUMP_CREATOR),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        is_mayhem_mode=mayhem,
        quote_mint=quote_mint,
        quote_token_program_id=quote_program,
    )


def _launch_token(  # noqa: PLR0913
    platform: Platform,
    platform_config: Pubkey,
    quote_mint: Pubkey,
    quote_program: Pubkey,
    *,
    mint: Pubkey = LAUNCH_MINT,
    creator: Pubkey = LAUNCH_CREATOR,
    global_config: Pubkey = LAUNCHLAB_GLOBAL_CONFIG,
    token_program: Pubkey = SystemAddresses.TOKEN_2022_PROGRAM,
    transfer_fee_bps: int = 0,
) -> TokenInfo:
    return TokenInfo(
        name="snapshot",
        symbol="SNAP",
        uri="",
        mint=mint,
        platform=platform,
        global_config=global_config,
        platform_config=platform_config,
        user=creator,
        creator=creator,
        token_program_id=token_program,
        quote_mint=quote_mint,
        quote_token_program_id=quote_program,
        transfer_fee_bps=transfer_fee_bps,
    )


async def _curve_trades(  # noqa: PLR0913, PLR0917
    name: str,
    builder: InstructionBuilder,
    provider: AddressProvider,
    token: TokenInfo,
    buy: tuple[int, int],
    sell: tuple[int, int],
) -> dict:
    # Through the curve adapter the trader uses; building needs no curve state.
    market = CurveMarket(provider, builder, None)
    out = {}
    for side, (amount_in, amount_out) in (("buy", buy), ("sell", sell)):
        build = getattr(market, f"build_{side}_instruction")
        required = getattr(market, f"get_required_accounts_for_{side}")
        limit = getattr(market, f"get_{side}_compute_unit_limit")
        instructions = await build(token, TRADER, amount_in, amount_out)
        out[f"{name} {side}"] = {
            "inputs": {"amount_in": amount_in, "minimum_amount_out": amount_out},
            "instructions": [_instruction(ix) for ix in instructions],
            "priority_fee_accounts": _plain(required(token, TRADER)),
            "compute_unit_limit": limit(None),
        }
    return out


async def _market_sell(
    name: str,
    market: Market,
    token: TokenInfo,
    amount_in: int,
    amount_out: int,
) -> dict:
    instructions = await market.build_sell_instruction(
        token, TRADER, amount_in, amount_out
    )
    return {
        f"{name} sell": {
            "inputs": {"amount_in": amount_in, "minimum_amount_out": amount_out},
            "instructions": [_instruction(ix) for ix in instructions],
            "priority_fee_accounts": _plain(
                market.get_required_accounts_for_sell(token, TRADER)
            ),
            "compute_unit_limit": market.get_sell_compute_unit_limit(None),
        }
    }


async def _market_buy(
    name: str,
    market: Market,
    token: TokenInfo,
    amount_in: int,
    amount_out: int,
) -> dict:
    instructions = await market.build_buy_instruction(
        token, TRADER, amount_in, amount_out
    )
    return {
        f"{name} buy": {
            "inputs": {"amount_in": amount_in, "minimum_amount_out": amount_out},
            "instructions": [_instruction(ix) for ix in instructions],
            "priority_fee_accounts": _plain(
                market.get_required_accounts_for_buy(token, TRADER)
            ),
            "compute_unit_limit": market.get_buy_compute_unit_limit(None),
        }
    }


class _FixtureClient:
    """Serves base64 accounts by address; anything else does not exist."""

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


def _fixture(name: str) -> dict | list:
    return json.loads((HERE / name).read_text())


async def build_instructions() -> dict:
    """A: every buy and sell, from fixed inputs."""
    pump = PumpFunInstructionBuilder(get_idl_manager().get_parser(Platform.PUMP_FUN))
    pump_provider = PumpFunAddressProvider()
    launch_parser = get_idl_manager().get_parser(Platform.STONK_FUN)
    out: dict = {}

    for name, token, buy, sell in (
        (
            "pump.fun SOL",
            _pump_token(WSOL_MINT, SystemAddresses.TOKEN_PROGRAM, mayhem=False),
            PUMP_BUY,
            PUMP_SELL,
        ),
        (
            "pump.fun SOL mayhem",
            _pump_token(WSOL_MINT, SystemAddresses.TOKEN_PROGRAM, mayhem=True),
            PUMP_BUY,
            PUMP_SELL,
        ),
        (
            "pump.fun USDC",
            _pump_token(USDC_MINT, SystemAddresses.TOKEN_PROGRAM, mayhem=False),
            USDC_BUY,
            USDC_SELL,
        ),
    ):
        out |= await _curve_trades(name, pump, pump_provider, token, buy, sell)

    trades = {
        t["label"]: t for t in _fixture("raw_stonkfun_trades_from_gettransaction.json")
    }
    xstock = trades["reward buy xstock"]
    for name, builder, provider, token in (
        (
            "letsbonk SOL",
            LetsBonkInstructionBuilder(launch_parser),
            LetsBonkAddressProvider(),
            _launch_token(
                Platform.LETS_BONK,
                next(iter(LETSBONK_CONFIGS)),
                WSOL_MINT,
                SystemAddresses.TOKEN_PROGRAM,
                token_program=SystemAddresses.TOKEN_PROGRAM,
            ),
        ),
        (
            "StonkFun reward SOL",
            StonkFunInstructionBuilder(launch_parser),
            StonkFunAddressProvider(),
            _launch_token(
                Platform.STONK_FUN,
                REWARD_PLATFORM_CONFIG,
                WSOL_MINT,
                SystemAddresses.TOKEN_PROGRAM,
                transfer_fee_bps=100,
            ),
        ),
        (
            "StonkFun reward xStock",
            StonkFunInstructionBuilder(launch_parser),
            StonkFunAddressProvider(),
            _launch_token(
                Platform.STONK_FUN,
                Pubkey.from_string(xstock["platform_config"]),
                Pubkey.from_string(xstock["quote_mint"]),
                SystemAddresses.TOKEN_2022_PROGRAM,
                mint=Pubkey.from_string(xstock["base_mint"]),
                creator=Pubkey.from_string(xstock["creator"]),
                global_config=Pubkey.from_string(xstock["global_config"]),
                transfer_fee_bps=100,
            ),
        ),
    ):
        out |= await _curve_trades(
            name, builder, provider, token, LAUNCH_BUY, LAUNCH_SELL
        )

    graduated = _fixture("raw_graduated_markets_from_getaccountinfo.json")
    pump_fixture, launch_fixture = graduated["pumpfun"], graduated["launchlab"]
    out |= await _market_sell(
        "PumpSwap SOL",
        PumpSwapMarket(_FixtureClient(pump_fixture["accounts"])),
        _graduated_pump_token(pump_fixture),
        *PUMP_SELL,
    )
    # PumpSwap buys spend exactly amount_in, so the inputs are (quote in,
    # token floor).
    out |= await _market_buy(
        "PumpSwap SOL",
        PumpSwapMarket(_FixtureClient(pump_fixture["accounts"])),
        _graduated_pump_token(pump_fixture),
        *PUMP_BUY,
    )
    for case in _fixture("raw_pumpswap_buys_from_gettransaction.json"):
        if case["label"] == "sol":
            continue
        out |= await _market_buy(
            f"PumpSwap {case['label'].replace('sol', 'SOL').replace('usdc', 'USDC')}",
            PumpSwapMarket(_FixtureClient(case["accounts"])),
            _graduated_pump_token(case),
            *(USDC_BUY if case["label"] == "usdc" else PUMP_BUY),
        )
    out |= await _market_sell(
        "CPMM",
        CpmmMarket(_FixtureClient(launch_fixture["accounts"])),
        _graduated_launch_token(launch_fixture),
        *LAUNCH_SELL,
    )
    return out


def _graduated_pump_token(fixture: dict) -> TokenInfo:
    return TokenInfo(
        name="",
        symbol="PUMP",
        uri="",
        mint=Pubkey.from_string(fixture["mint"]),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.from_string(fixture["curve"]),
        quote_mint=Pubkey.from_string(fixture.get("quote_mint", str(WSOL_MINT))),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
    )


def _graduated_launch_token(fixture: dict) -> TokenInfo:
    return TokenInfo(
        name="",
        symbol="STONK",
        uri="",
        mint=Pubkey.from_string(fixture["mint"]),
        platform=Platform.STONK_FUN,
        pool_state=Pubkey.from_string(fixture["launchlab_pool"]),
        platform_config=Pubkey.from_string(fixture["platform_config"]),
        quote_mint=Pubkey.from_string(fixture["quote_mint"]),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
    )


# --- B: parsers --------------------------------------------------------------


def _keys(tx: dict) -> list[str]:
    loaded = tx["meta"].get("loadedAddresses") or {}
    return (
        tx["transaction"]["message"]["accountKeys"]
        + loaded.get("writable", [])
        + loaded.get("readonly", [])
    )


def _launchlab_instructions(tx: dict) -> Iterator[tuple[str, dict]]:
    """(where, decoded instruction) for every LaunchLab instruction in a tx."""
    keys = _keys(tx)
    parser = get_idl_manager().get_parser(Platform.STONK_FUN)
    top = tx["transaction"]["message"]["instructions"]
    located = [(f"top {i}", ix) for i, ix in enumerate(top)]
    for group in tx["meta"].get("innerInstructions") or []:
        located += [
            (f"inner {group['index']}.{i}", ix)
            for i, ix in enumerate(group["instructions"])
        ]
    for where, ix in located:
        if keys[ix["programIdIndex"]] != str(LAUNCHLAB_PROGRAM):
            continue
        decoded = parser.decode_instruction(
            base58.b58decode(ix["data"]), keys, ix["accounts"]
        )
        if decoded is not None:
            yield where, _plain(decoded)


def _geyser_update(tx: dict) -> geyser_pb2.SubscribeUpdate:
    """A geyser frame carrying the same transaction as the JSON fixture."""
    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    message = info.transaction.message
    json_message = tx["transaction"]["message"]
    message.account_keys.extend(
        base58.b58decode(k) for k in json_message["accountKeys"]
    )
    for ix in json_message["instructions"]:
        compiled = message.instructions.add()
        compiled.program_id_index = ix["programIdIndex"]
        compiled.accounts = bytes(ix["accounts"])
        compiled.data = base58.b58decode(ix["data"])
    loaded = tx["meta"].get("loadedAddresses") or {}
    info.meta.loaded_writable_addresses.extend(
        base58.b58decode(k) for k in loaded.get("writable", [])
    )
    info.meta.loaded_readonly_addresses.extend(
        base58.b58decode(k) for k in loaded.get("readonly", [])
    )
    for group in tx["meta"].get("innerInstructions") or []:
        inner = info.meta.inner_instructions.add()
        inner.index = group["index"]
        for ix in group["instructions"]:
            compiled = inner.instructions.add()
            compiled.program_id_index = ix["programIdIndex"]
            compiled.accounts = bytes(ix["accounts"])
            compiled.data = base58.b58decode(ix["data"])
    return update


def _parse_launches() -> list[dict]:
    parser = get_idl_manager().get_parser(Platform.STONK_FUN)
    stonkfun, letsbonk = StonkFunEventParser(parser), LetsBonkEventParser(parser)
    out = []
    for tx in _fixture("raw_stonkfun_launches_from_gettransaction.json"):
        block = {
            "transactions": [{"transaction": tx["transaction"], "meta": tx["meta"]}]
        }
        update = _geyser_update(tx)
        out.append(
            {
                "signature": tx["transaction"]["signatures"][0],
                "version": tx.get("version"),
                "stonkfun block": _token_info(
                    stonkfun.parse_token_creation_from_block(copy.deepcopy(block))
                ),
                "stonkfun geyser": _token_info(
                    stonkfun.parse_token_creation_from_geyser(update)
                ),
                "letsbonk block": _token_info(
                    letsbonk.parse_token_creation_from_block(copy.deepcopy(block))
                ),
                "letsbonk geyser": _token_info(
                    letsbonk.parse_token_creation_from_geyser(update)
                ),
                "launchlab instructions": dict(_launchlab_instructions(tx)),
            }
        )
    return out


def _parse_deshred_launches() -> list[dict]:
    """LaunchLab launches as the shreds listener reads them, per platform."""
    listeners = {
        platform: UniversalShredsListener(
            geyser_endpoint="geyser.offline.invalid:443",
            geyser_api_token="unused",  # noqa: S106
            geyser_auth_type="x-token",
            platforms=[platform],
        )
        for platform in (Platform.LAUNCHLAB, Platform.LETS_BONK, Platform.STONK_FUN)
    }
    out = []
    for launch in _fixture("raw_launchlab_creates_from_deshred.json")["launches"]:
        update = geyser_pb2.SubscribeUpdateDeshred()
        update.ParseFromString(base64.b64decode(launch["deshred_update_base64"]))
        entry = {"signature": launch["signature"]}
        for platform, listener in listeners.items():
            entry[f"{platform.value} shreds"] = _token_info(
                listener._process_update(update)  # noqa: SLF001
            )
        out.append(entry)
    return out


def _parse_other_launchpads() -> list[dict]:
    """Launches on launchpads other than letsbonk and StonkFun."""
    parser = get_idl_manager().get_parser(Platform.LAUNCHLAB)
    parsers = {
        "launchlab": LaunchLabEventParser(parser),
        "stonkfun": StonkFunEventParser(parser),
        "letsbonk": LetsBonkEventParser(parser),
    }
    out = []
    for tx in _fixture("raw_launchlab_launches_from_gettransaction.json"):
        block = {
            "transactions": [{"transaction": tx["transaction"], "meta": tx["meta"]}]
        }
        update = _geyser_update(tx)
        entry = {
            "signature": tx["transaction"]["signatures"][0],
            "version": tx.get("version"),
        }
        for name, event_parser in parsers.items():
            entry[f"{name} block"] = _token_info(
                event_parser.parse_token_creation_from_block(copy.deepcopy(block))
            )
            entry[f"{name} geyser"] = _token_info(
                event_parser.parse_token_creation_from_geyser(update)
            )
        entry["launchlab instructions"] = dict(_launchlab_instructions(tx))
        out.append(entry)
    return out


async def _parse_graduated() -> dict:
    graduated = _fixture("raw_graduated_markets_from_getaccountinfo.json")
    pump_fixture, launch_fixture = graduated["pumpfun"], graduated["launchlab"]
    pump_curve = PumpFunCurveManager(
        _FixtureClient(pump_fixture["accounts"]),
        get_idl_manager().get_parser(Platform.PUMP_FUN),
    )
    launch_curve = StonkFunCurveManager(
        _FixtureClient(launch_fixture["accounts"]),
        get_idl_manager().get_parser(Platform.STONK_FUN),
    )
    pumpswap = PumpSwapMarket(_FixtureClient(pump_fixture["accounts"]))
    cpmm = CpmmMarket(_FixtureClient(launch_fixture["accounts"]))
    pump_token = _graduated_pump_token(pump_fixture)
    launch_token = _graduated_launch_token(launch_fixture)
    pumpswap_state = await pumpswap.get_market_state(pump_token)
    cpmm_state = await cpmm.get_market_state(launch_token)
    return {
        "pump.fun curve": _plain(
            await pump_curve.get_pool_state(Pubkey.from_string(pump_fixture["curve"]))
        ),
        "LaunchLab curve": _plain(
            await launch_curve.get_pool_state(
                Pubkey.from_string(launch_fixture["launchlab_pool"])
            )
        ),
        "PumpSwap market": _plain(pumpswap_state),
        "PumpSwap pool": _plain(pumpswap._pools[pump_token.mint]),  # noqa: SLF001
        "CPMM market": _plain(cpmm_state),
        "CPMM pool": _plain(cpmm._pools[launch_token.mint]),  # noqa: SLF001
    }


async def _parse_pumpswap_buys() -> dict:
    """Each real buy's curve and pool, as the curve manager and market read them."""
    out = {}
    for case in _fixture("raw_pumpswap_buys_from_gettransaction.json"):
        client = _FixtureClient(case["accounts"])
        curve = PumpFunCurveManager(
            client, get_idl_manager().get_parser(Platform.PUMP_FUN)
        )
        market = PumpSwapMarket(client)
        token = _graduated_pump_token(case)
        out[case["label"]] = {
            "pump.fun curve": _plain(
                await curve.get_pool_state(Pubkey.from_string(case["curve"]))
            ),
            "PumpSwap market": _plain(await market.get_market_state(token)),
            "PumpSwap pool": _plain(market._pools[token.mint]),  # noqa: SLF001
        }
    return out


async def _parse_trades() -> list[dict]:
    """Each StonkFun trade: its instructions, and its pool's fee read."""
    parser = get_idl_manager().get_parser(Platform.STONK_FUN)
    out = []
    for entry in _fixture("raw_stonkfun_trades_from_gettransaction.json"):
        accounts = {
            Pubkey.from_string(entry[name]): SimpleNamespace(
                data=base64.b64decode(entry["accounts_base64"][name]), owner=None
            )
            for name in ("global_config", "platform_config", "base_mint")
        }
        client = SimpleNamespace(
            get_multiple_accounts=_serve(accounts),
        )
        manager = StonkFunCurveManager(client, parser)
        # Reserves are not part of this read; fixed ones keep the price defined.
        pool = {
            "virtual_base": 1_073_025_605_596_382,
            "virtual_quote": 30_000_852_951,
            "real_base": 0,
            "real_quote": 0,
            "base_decimals": 6,
            "quote_decimals": 9,
            "global_config": Pubkey.from_string(entry["global_config"]),
            "platform_config": Pubkey.from_string(entry["platform_config"]),
            "base_mint": Pubkey.from_string(entry["base_mint"]),
            "status": 0,
        }
        out.append(
            {
                "label": entry["label"],
                "signature": entry["signature"],
                "launchlab instructions": dict(
                    _launchlab_instructions(entry["transaction"])
                ),
                "pool with fees": _plain(
                    await manager._with_fees(pool, None)  # noqa: SLF001
                ),
            }
        )
    return out


def _serve(accounts: dict) -> Callable[..., Awaitable[list]]:
    async def get_multiple_accounts(
        keys: list[Pubkey],
        commitment: str | None = None,  # noqa: ARG001
    ) -> list:
        return [accounts.get(k) for k in keys]

    return get_multiple_accounts


def _parse_token_accounts() -> dict:
    return {
        label: withheld_transfer_fee(base64.b64decode(entry["data_base64"]))
        for label, entry in _fixture(
            "raw_stonkfun_token_accounts_from_getaccountinfo.json"
        ).items()
    }


async def parse_fixtures() -> dict:
    """B: parser output for every raw_*.json fixture."""
    out = {
        "raw_stonkfun_launches_from_gettransaction.json": _parse_launches(),
        "raw_launchlab_launches_from_gettransaction.json": _parse_other_launchpads(),
        "raw_graduated_markets_from_getaccountinfo.json": await _parse_graduated(),
        "raw_stonkfun_trades_from_gettransaction.json": await _parse_trades(),
        "raw_stonkfun_token_accounts_from_getaccountinfo.json": _parse_token_accounts(),
        "raw_launchlab_creates_from_deshred.json": _parse_deshred_launches(),
        "raw_pumpswap_buys_from_gettransaction.json": await _parse_pumpswap_buys(),
    }
    covered = set(out)
    present = {p.name for p in HERE.glob("raw_*.json")}
    if present != covered:
        raise AssertionError(  # noqa: TRY003
            f"raw fixtures without a parser snapshot: {sorted(present - covered)}"
        )
    return out


# --- C: exit decisions -------------------------------------------------------


class _Clock(datetime):
    """`datetime` whose utcnow() is whatever the series says it is."""

    now_value = datetime(2026, 1, 1)

    @classmethod
    def utcnow(cls) -> datetime:
        return cls.now_value


def _prices() -> list[tuple[int, float]]:
    """(seconds since the first trade, price in SOL per token)."""
    series = _fixture("recorded_pumpfun_prices_from_gettransaction.json")["points"]
    start = series[0]["timestamp"]
    return [
        (
            p["timestamp"] - start,
            (p["virtual_sol_reserves"] / 1e9) / (p["virtual_token_reserves"] / 1e6),
        )
        for p in series
    ]


# The tp_sl settings a bot can be given; None is "not set".
EXIT_SETTINGS = {
    "tp 0.03, sl 0.2, max_hold 60": (0.03, 0.2, 60),
    "tp 0.5, sl 0.2, max_hold 30": (0.5, 0.2, 30),
    "tp 0.5, sl 0.1, no max_hold": (0.5, 0.1, None),
    "tp only 0.02": (0.02, None, None),
    "sl only 0.25": (None, 0.25, None),
    "max_hold only 25": (None, None, 25),
    "nothing set": (None, None, None),
}


def decide_exits() -> dict:
    """C: should_exit at every recorded price, per setting, and at the edges."""
    position_module.datetime = _Clock
    prices = _prices()
    entry_time = _Clock.now_value
    out: dict = {"prices": [[t, p] for t, p in prices], "settings": {}}
    for label, (tp, sl, hold) in EXIT_SETTINGS.items():
        _Clock.now_value = entry_time
        position = Position.create_from_buy_result(
            mint=PUMP_MINT,
            symbol="SNAP",
            entry_price=prices[0][1],
            quantity=1000.0,
            quantity_raw=1_000_000_000,
            take_profit_percentage=tp,
            stop_loss_percentage=sl,
            max_hold_time=hold,
        )
        trace = []
        for seconds, price in prices:
            _Clock.now_value = entry_time + timedelta(seconds=seconds)
            exit_now, reason = position.should_exit(price)
            trace.append([exit_now, _plain(reason), position.should_exit_on_time()])
        out["settings"][label] = {
            "take_profit_price": position.take_profit_price,
            "stop_loss_price": position.stop_loss_price,
            "decisions": trace,
        }

    _Clock.now_value = entry_time
    edge = Position.create_from_buy_result(
        mint=PUMP_MINT,
        symbol="SNAP",
        entry_price=1.0,
        quantity=1.0,
        take_profit_percentage=0.5,
        stop_loss_percentage=0.25,
        max_hold_time=10,
    )
    edges = {}
    for label, price, seconds in (
        ("exactly take profit", 1.5, 0),
        ("exactly stop loss", 0.75, 0),
        ("zero price", 0.0, 0),
        ("between", 1.0, 0),
        ("between, one second before max_hold", 1.0, 9),
        ("between, at max_hold", 1.0, 10),
        ("take profit after max_hold", 2.0, 11),
    ):
        _Clock.now_value = entry_time + timedelta(seconds=seconds)
        edges[label] = _plain(list(edge.should_exit(price)))
    edge.close_position(1.0, edge.should_exit(1.5)[1])
    edges["closed position at take profit"] = _plain(list(edge.should_exit(1.5)))
    edges["closed position, time only"] = edge.should_exit_on_time()
    out["edges"] = edges
    position_module.datetime = datetime
    return out


# --- driver ------------------------------------------------------------------


def _first_difference(old: object, new: object, path: str = "") -> str | None:  # noqa: PLR0911
    if type(old) is not type(new):
        return f"{path or '/'}: {old!r} -> {new!r}"
    if isinstance(old, dict):
        for key in sorted(set(old) | set(new)):
            if key not in old or key not in new:
                return f"{path}/{key}: {'added' if key in new else 'removed'}"
            found = _first_difference(old[key], new[key], f"{path}/{key}")
            if found:
                return found
        return None
    if isinstance(old, list):
        if len(old) != len(new):
            return f"{path}: length {len(old)} -> {len(new)}"
        for i, (a, b) in enumerate(zip(old, new, strict=True)):
            found = _first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if old == new else f"{path}: {old!r} -> {new!r}"


def main() -> int:
    """Build every snapshot and compare it with the committed one."""
    _pin_random_choices()
    current = {
        "instructions.json": asyncio.run(build_instructions()),
        "parsers.json": asyncio.run(parse_fixtures()),
        "exit_decisions.json": decide_exits(),
    }
    # Round-trip so floats and tuples compare the way they are stored.
    current = {k: json.loads(json.dumps(v)) for k, v in current.items()}

    if "--update" in sys.argv:
        SNAPSHOTS.mkdir(exist_ok=True)
        for name, value in current.items():
            (SNAPSHOTS / name).write_text(json.dumps(value, indent=1) + "\n")
            print(f"wrote {SNAPSHOTS / name}")
        return 0

    labels = {
        "instructions.json": "A: instruction bytes match the snapshot",
        "parsers.json": "B: parser output matches the snapshot",
        "exit_decisions.json": "C: exit decisions match the snapshot",
    }
    passed = 0
    for name, value in current.items():
        path = SNAPSHOTS / name
        if not path.exists():
            print(f"FAIL {labels[name]}\n    {path} missing; run with --update")
            continue
        difference = _first_difference(json.loads(path.read_text()), value)
        print(f"{'PASS' if difference is None else 'FAIL'} {labels[name]}")
        if difference:
            print(f"    first difference: {difference[:400]}")
        passed += difference is None
    print(f"\n{passed}/{len(current)} checks passed")
    return 0 if passed == len(current) else 1


if __name__ == "__main__":
    sys.exit(main())
