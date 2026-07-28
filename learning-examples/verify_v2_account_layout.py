"""Cross-check the hardcoded buy_v2/sell_v2 account layouts against the IDL.

The v2 instructions take 27 and 26 mandatory accounts in a fixed order. Getting
one position or writability flag wrong produces an on-chain failure that is
awkward to debug, so this script diffs the layouts in
`platforms.pumpfun.instruction_builder` against `idl/pump_fun_idl.json` and also
recomputes every PDA/ATA the address provider derives, comparing each against
the seeds declared in the IDL.

Runs entirely offline — no RPC, no keys, no transactions.

Usage:
    uv run learning-examples/verify_v2_account_layout.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "learning-examples"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

from core.pubkeys import USDC_MINT, WSOL_MINT, SystemAddresses  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from platforms.pumpfun.address_provider import (  # noqa: E402
    PumpFunAddresses,
    PumpFunAddressProvider,
)
from platforms.pumpfun.instruction_builder import (  # noqa: E402
    _BUY_V2_ACCOUNTS,
    _SELL_V2_ACCOUNTS,
)

IDL_PATH = PROJECT_ROOT / "idl" / "pump_fun_idl.json"


def load_idl_accounts(name: str) -> list[dict]:
    """Get the IDL account list for an instruction.

    Args:
        name: Instruction name

    Returns:
        List of IDL account definitions
    """
    idl = json.loads(IDL_PATH.read_text())
    for instruction in idl["instructions"]:
        if instruction["name"] == name:
            return instruction["accounts"]
    raise KeyError(f"Instruction {name} not present in {IDL_PATH}")


def check_layout(name: str, layout: list[tuple[str, bool]]) -> list[str]:
    """Compare a hardcoded layout against the IDL's account list.

    Args:
        name: Instruction name
        layout: Ordered (account name, is_writable) pairs from our builder

    Returns:
        List of human-readable problems (empty if the layout matches)
    """
    idl_accounts = load_idl_accounts(name)
    problems = []

    if len(layout) != len(idl_accounts):
        problems.append(
            f"{name}: account count {len(layout)} != IDL {len(idl_accounts)}"
        )

    for index, (idl_account, ours) in enumerate(zip(idl_accounts, layout), start=1):
        our_name, our_writable = ours
        if idl_account["name"] != our_name:
            problems.append(
                f"{name}[{index}]: name {our_name!r} != IDL {idl_account['name']!r}"
            )
        idl_writable = bool(idl_account.get("writable"))
        if idl_writable != our_writable:
            problems.append(
                f"{name}[{index}] {our_name}: writable={our_writable} "
                f"!= IDL writable={idl_writable}"
            )
        if idl_account.get("signer") and our_name != "user":
            problems.append(
                f"{name}[{index}] {our_name}: IDL marks this a signer but only "
                f"`user` is expected to sign"
            )

    return problems


def build_token_info(quote_mint: Pubkey, *, mayhem: bool) -> TokenInfo:
    """Construct a TokenInfo for a synthetic coin.

    Args:
        quote_mint: Quote mint to pair the coin against
        mayhem: Whether the coin is in mayhem mode

    Returns:
        TokenInfo suitable for driving the address provider
    """
    provider = PumpFunAddressProvider()
    # Fixed, arbitrary mint/creator so results are reproducible.
    mint = Pubkey.from_string("CU7nUQaJ4beyYjC3xAUrh5RiSjw14fhU6oWTwRBse8gj")
    creator = Pubkey.from_string("5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX")
    bonding_curve = provider.derive_pool_address(mint)

    return TokenInfo(
        name="layout-check",
        symbol="CHK",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            mint, bonding_curve, SystemAddresses.TOKEN_2022_PROGRAM
        ),
        creator=creator,
        creator_vault=provider.derive_creator_vault(creator),
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        is_mayhem_mode=mayhem,
        quote_mint=quote_mint,
    )


def check_derivations(quote_mint: Pubkey, *, mayhem: bool) -> list[str]:
    """Independently recompute every derived v2 account and compare.

    Args:
        quote_mint: Quote mint to pair the coin against
        mayhem: Whether the coin is in mayhem mode

    Returns:
        List of mismatches (empty if all derivations agree)
    """
    provider = PumpFunAddressProvider()
    token_info = build_token_info(quote_mint, mayhem=mayhem)
    user = Pubkey.from_string("Ba99j1dYxidfQZvuNGMaXGxJsUeWXu6VNW8damkrdLVd")
    accounts = provider.get_buy_v2_instruction_accounts(token_info, user)

    pump = PumpFunAddresses.PROGRAM
    fee_program = PumpFunAddresses.FEE_PROGRAM
    quote_program = SystemAddresses.TOKEN_PROGRAM
    base_program = SystemAddresses.TOKEN_2022_PROGRAM
    mint = token_info.mint
    bonding_curve = token_info.bonding_curve
    creator_vault = token_info.creator_vault
    uva, _ = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], pump
    )

    expected = {
        "bonding_curve": Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint)], pump
        )[0],
        "creator_vault": Pubkey.find_program_address(
            [b"creator-vault", bytes(token_info.creator)], pump
        )[0],
        "sharing_config": Pubkey.find_program_address(
            [b"sharing-config", bytes(mint)], fee_program
        )[0],
        "global_volume_accumulator": Pubkey.find_program_address(
            [b"global_volume_accumulator"], pump
        )[0],
        "user_volume_accumulator": uva,
        "fee_config": Pubkey.find_program_address(
            [b"fee_config", bytes(pump)], fee_program
        )[0],
        "event_authority": Pubkey.find_program_address([b"__event_authority"], pump)[0],
        "associated_base_bonding_curve": get_associated_token_address(
            bonding_curve, mint, base_program
        ),
        "associated_quote_bonding_curve": get_associated_token_address(
            bonding_curve, quote_mint, quote_program
        ),
        "associated_base_user": get_associated_token_address(user, mint, base_program),
        "associated_quote_user": get_associated_token_address(
            user, quote_mint, quote_program
        ),
        "associated_creator_vault": get_associated_token_address(
            creator_vault, quote_mint, quote_program
        ),
        "associated_quote_fee_recipient": get_associated_token_address(
            accounts["fee_recipient"], quote_mint, quote_program
        ),
        "associated_quote_buyback_fee_recipient": get_associated_token_address(
            accounts["buyback_fee_recipient"], quote_mint, quote_program
        ),
        "associated_user_volume_accumulator": get_associated_token_address(
            uva, quote_mint, quote_program
        ),
        "quote_mint": quote_mint,
        "base_mint": mint,
        "user": user,
        "program": pump,
        "fee_program": fee_program,
        "system_program": SystemAddresses.SYSTEM_PROGRAM,
        "associated_token_program": SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
        "base_token_program": base_program,
        "quote_token_program": quote_program,
        "global": PumpFunAddresses.GLOBAL,
    }

    problems = [
        f"{key}: provider={accounts[key]} != expected={value}"
        for key, value in expected.items()
        if accounts[key] != value
    ]

    # The fee recipient must come from the right documented set.
    recipient_set = (
        PumpFunAddresses.RESERVED_FEE_RECIPIENTS
        if mayhem
        else PumpFunAddresses.NORMAL_FEE_RECIPIENTS
    )
    if accounts["fee_recipient"] not in recipient_set:
        problems.append(
            f"fee_recipient {accounts['fee_recipient']} not in "
            f"{'reserved' if mayhem else 'normal'} fee recipient set"
        )
    if accounts["buyback_fee_recipient"] not in PumpFunAddresses.BUYBACK_FEE_RECIPIENTS:
        problems.append(
            f"buyback_fee_recipient {accounts['buyback_fee_recipient']} not in "
            f"buyback fee recipient set"
        )

    # Every account must be distinct except where the program expects reuse.
    if len(set(accounts.values())) != len(accounts):
        duplicates = [
            key
            for key, value in accounts.items()
            if list(accounts.values()).count(value) > 1
        ]
        problems.append(f"duplicate account addresses for: {sorted(duplicates)}")

    return problems


def check_instruction_encoding() -> list[str]:
    """Build real v2 instructions and assert their shape and data encoding.

    Covers the USDC path, which cannot be exercised on-chain until a
    USDC-paired coin exists.

    Returns:
        List of problems (empty if the instructions are well-formed)
    """
    import asyncio
    import struct

    from interfaces.core import Platform as _Platform  # noqa: F401
    from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder
    from utils.idl_manager import get_idl_manager

    provider = PumpFunAddressProvider()
    builder = PumpFunInstructionBuilder(get_idl_manager().get_parser(Platform.PUMP_FUN))
    user = Pubkey.from_string("Ba99j1dYxidfQZvuNGMaXGxJsUeWXu6VNW8damkrdLVd")
    problems = []

    for quote_mint, label, expect_quote_ata in (
        (WSOL_MINT, "SOL-paired", False),
        (USDC_MINT, "USDC-paired", True),
    ):
        token_info = build_token_info(quote_mint, mayhem=False)

        buy = asyncio.run(
            builder.build_buy_v2_instruction(
                token_info, user, 1_500_000, 20_000_000, provider
            )
        )
        sell = asyncio.run(
            builder.build_sell_v2_instruction(
                token_info, user, 20_000_000, 900_000, provider
            )
        )

        # Instruction counts: base ATA always, quote ATA only for non-SOL.
        expected_buy_ix = 3 if expect_quote_ata else 2
        if len(buy) != expected_buy_ix:
            problems.append(
                f"{label}: buy produced {len(buy)} instructions, "
                f"expected {expected_buy_ix}"
            )
        expected_sell_ix = 2 if expect_quote_ata else 1
        if len(sell) != expected_sell_ix:
            problems.append(
                f"{label}: sell produced {len(sell)} instructions, "
                f"expected {expected_sell_ix}"
            )

        if len(buy[-1].accounts) != 27:
            problems.append(
                f"{label}: buy_v2 has {len(buy[-1].accounts)} accounts != 27"
            )
        if len(sell[-1].accounts) != 26:
            problems.append(
                f"{label}: sell_v2 has {len(sell[-1].accounts)} accounts != 26"
            )

        # buy_v2 data: 8-byte discriminator + amount (tokens) + max_sol_cost.
        # No trailing track_volume OptionBool, unlike the legacy buy.
        buy_data = bytes(buy[-1].data)
        if len(buy_data) != 24:
            problems.append(
                f"{label}: buy_v2 data is {len(buy_data)} bytes, expected 24 "
                f"(discriminator + 2 u64, no track_volume)"
            )
        else:
            amount, max_cost = struct.unpack("<QQ", buy_data[8:])
            if amount != 20_000_000 or max_cost != 1_500_000:
                problems.append(
                    f"{label}: buy_v2 args decoded as amount={amount}, "
                    f"max_sol_cost={max_cost}; expected 20000000 and 1500000"
                )

        sell_data = bytes(sell[-1].data)
        if len(sell_data) != 24:
            problems.append(
                f"{label}: sell_v2 data is {len(sell_data)} bytes, expected 24"
            )
        else:
            amount, min_out = struct.unpack("<QQ", sell_data[8:])
            if amount != 20_000_000 or min_out != 900_000:
                problems.append(
                    f"{label}: sell_v2 args decoded as amount={amount}, "
                    f"min_sol_output={min_out}; expected 20000000 and 900000"
                )

        # Exactly one signer, and it must be the user.
        signers = [meta.pubkey for meta in buy[-1].accounts if meta.is_signer]
        if signers != [user]:
            problems.append(f"{label}: buy_v2 signers {signers} != [{user}]")

    return problems


def check_quote_config() -> list[str]:
    """Assert quote-amount config resolution and alias handling.

    Returns:
        List of problems (empty if config resolution behaves correctly)
    """
    from core.pubkeys import resolve_quote_amounts, resolve_quote_mint
    from trading.universal_trader import _resolve_quote_config

    problems = []

    if resolve_quote_mint("usdc") != USDC_MINT:
        problems.append("alias 'usdc' did not resolve to the USDC mint")
    if resolve_quote_mint("sol") != WSOL_MINT:
        problems.append("alias 'sol' did not resolve to wrapped SOL")
    if resolve_quote_mint(str(USDC_MINT)) != USDC_MINT:
        problems.append("raw USDC mint string did not resolve")

    try:
        resolve_quote_amounts({"usdc": 0})
        problems.append("resolve_quote_amounts accepted a zero amount")
    except ValueError:
        pass

    try:
        resolve_quote_mint("not-a-mint")
        problems.append("resolve_quote_mint accepted an invalid mint")
    except ValueError:
        pass

    # SOL always present from buy_amount; USDC only when configured.
    amounts, allowed = _resolve_quote_config(0.0001, None, None)
    if amounts.get(WSOL_MINT) != 0.0001:
        problems.append("SOL amount did not fall back to buy_amount")
    if USDC_MINT in amounts:
        problems.append("USDC present in amounts without being configured")
    if allowed is not None:
        problems.append("allowed_quote_mints should be None when unset")

    amounts, allowed = _resolve_quote_config(0.0001, {"usdc": 5.0}, ["sol", "usdc"])
    if amounts.get(USDC_MINT) != 5.0:
        problems.append("configured USDC amount not resolved")
    if allowed != {WSOL_MINT, USDC_MINT}:
        problems.append(f"allowed_quote_mints resolved to {allowed}")

    return problems


def check_examples_toolkit() -> list[str]:
    """Check learning-examples/pump_v2.py agrees with the IDL and with src/.

    The examples carry their own standalone copy of the v2 layout so they stay
    readable without importing src/. That copy is exactly the kind of thing that
    silently drifts, so diff it against both sources of truth.

    Returns:
        List of problems (empty if the toolkit agrees)
    """
    import pump_v2

    from platforms.pumpfun.address_provider import PumpFunAddresses

    problems = []
    user = Pubkey.from_string("Ba99j1dYxidfQZvuNGMaXGxJsUeWXu6VNW8damkrdLVd")
    mint = Pubkey.from_string("CU7nUQaJ4beyYjC3xAUrh5RiSjw14fhU6oWTwRBse8gj")
    creator = Pubkey.from_string("5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX")

    # Fee recipient sets must match src/ exactly.
    for label, theirs, ours in (
        (
            "normal",
            pump_v2.NORMAL_FEE_RECIPIENTS,
            PumpFunAddresses.NORMAL_FEE_RECIPIENTS,
        ),
        (
            "reserved",
            pump_v2.RESERVED_FEE_RECIPIENTS,
            PumpFunAddresses.RESERVED_FEE_RECIPIENTS,
        ),
        (
            "buyback",
            pump_v2.BUYBACK_FEE_RECIPIENTS,
            PumpFunAddresses.BUYBACK_FEE_RECIPIENTS,
        ),
    ):
        if theirs != ours:
            problems.append(f"pump_v2 {label} fee recipients differ from src/")

    # Discriminators must match the IDL.
    idl = json.loads(IDL_PATH.read_text())
    by_name = {i["name"]: i for i in idl["instructions"]}
    for name, disc in (
        ("buy_v2", pump_v2.BUY_V2_DISCRIMINATOR),
        ("sell_v2", pump_v2.SELL_V2_DISCRIMINATOR),
    ):
        expected = bytes(by_name[name]["discriminator"])
        if disc != expected:
            problems.append(
                f"pump_v2 {name} discriminator {list(disc)} != IDL {list(expected)}"
            )

    # Account lists must match the IDL in order and writability, for both
    # quote assets and both mayhem states.
    for quote_mint, label in (
        (pump_v2.WSOL_MINT, "SOL"),
        (pump_v2.USDC_MINT, "USDC"),
    ):
        for mayhem in (False, True):
            for name, builder in (
                ("buy_v2", pump_v2.build_buy_v2_instruction),
                ("sell_v2", pump_v2.build_sell_v2_instruction),
            ):
                kwargs = {
                    "base_mint": mint,
                    "creator": creator,
                    "user": user,
                    "quote_mint": quote_mint,
                    "is_mayhem_mode": mayhem,
                }
                if name == "buy_v2":
                    instruction = builder(
                        token_amount_raw=1, max_quote_cost_raw=2, **kwargs
                    )
                else:
                    instruction = builder(
                        token_amount_raw=1, min_quote_output_raw=2, **kwargs
                    )

                idl_accounts = by_name[name]["accounts"]
                if len(instruction.accounts) != len(idl_accounts):
                    problems.append(
                        f"pump_v2 {name} ({label}, mayhem={mayhem}): "
                        f"{len(instruction.accounts)} accounts != IDL "
                        f"{len(idl_accounts)}"
                    )
                    continue
                for index, (meta, idl_account) in enumerate(
                    zip(instruction.accounts, idl_accounts), start=1
                ):
                    if meta.is_writable != bool(idl_account.get("writable")):
                        problems.append(
                            f"pump_v2 {name}[{index}] {idl_account['name']} "
                            f"({label}, mayhem={mayhem}): writable "
                            f"{meta.is_writable} != IDL "
                            f"{bool(idl_account.get('writable'))}"
                        )

                # Cross-check every address against the src/ provider, which the
                # checks above already validated.
                provider = PumpFunAddressProvider()
                token_info = build_token_info(quote_mint, mayhem=mayhem)
                resolved = (
                    provider.get_buy_v2_instruction_accounts(token_info, user)
                    if name == "buy_v2"
                    else provider.get_sell_v2_instruction_accounts(token_info, user)
                )
                layout = _BUY_V2_ACCOUNTS if name == "buy_v2" else _SELL_V2_ACCOUNTS
                for (account_name, _), meta in zip(layout, instruction.accounts):
                    # Fee recipients are picked at random from a set, so compare
                    # membership rather than identity.
                    if "fee_recipient" in account_name:
                        continue
                    if resolved[account_name] != meta.pubkey:
                        problems.append(
                            f"pump_v2 {name} {account_name} ({label}, "
                            f"mayhem={mayhem}): {meta.pubkey} != src/ "
                            f"{resolved[account_name]}"
                        )

    return problems


def main() -> int:
    """Run all layout and derivation checks.

    Returns:
        Process exit code (0 on success)
    """
    all_problems = []

    for name, layout in (("buy_v2", _BUY_V2_ACCOUNTS), ("sell_v2", _SELL_V2_ACCOUNTS)):
        problems = check_layout(name, layout)
        status = "OK" if not problems else f"{len(problems)} PROBLEM(S)"
        print(f"{name}: {len(layout)} accounts vs IDL -> {status}")
        all_problems.extend(problems)

    for quote_mint, label in ((WSOL_MINT, "SOL-paired"), (USDC_MINT, "USDC-paired")):
        for mayhem in (False, True):
            problems = check_derivations(quote_mint, mayhem=mayhem)
            tag = f"{label}, mayhem={mayhem}"
            status = "OK" if not problems else f"{len(problems)} PROBLEM(S)"
            print(f"derivations ({tag}) -> {status}")
            all_problems.extend(problems)

    for name, check in (
        ("instruction encoding", check_instruction_encoding),
        ("quote config resolution", check_quote_config),
        ("learning-examples pump_v2 toolkit", check_examples_toolkit),
    ):
        problems = check()
        status = "OK" if not problems else f"{len(problems)} PROBLEM(S)"
        print(f"{name} -> {status}")
        all_problems.extend(problems)

    if all_problems:
        print("\nProblems found:")
        for problem in all_problems:
            print(f"  - {problem}")
        return 1

    print("\nAll v2 account layouts and derivations match the IDL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
