"""Verify the launch is one v1 transaction, and stays one.

Creating a coin and buying it in separate transactions leaves a window in which
anyone watching the create can buy first. They were separate because the two
together exceed the 1232 bytes a v0 transaction is held to. SIMD-0296 raised the
limit to 4096 for the v1 transaction format (SIMD-0385), which is enough for
both with room to spare.

Three things about v1 are easy to get wrong and silent when wrong:

v1 has **no address lookup tables**. The larger limit exists so the whole
account list goes inline, so there is nothing to compress and no table to keep
warm.

v1 carries the compute budget in the message header, not as `ComputeBudget`
instructions. A v1 transaction that still sends them wastes compute units on
instructions the runtime ignores for configuration.

The priority fee changes model, not just location: v0 states micro-lamports per
compute unit, v1 states **one absolute total in lamports**. Carrying a v0
figure across unchanged overpays by the compute unit limit, and the transaction
still lands, because the result is a valid fee. solders' own docstring calls the
field micro-lamports and is wrong; SIMD-0385 defines it as total lamports.

Offline machine checks, no network and no funds moved:

  1. The launch compiles to a single v1 message holding create, extend, the
     buyer's token account and the buy.
  2. It fits the 4096-byte v1 limit.
  3. The same instructions do not fit a v0 transaction, which is why this is a
     v1 transaction and not a preference.
  4. No ComputeBudget instruction is present.
  5. The budget is in the message config, and the priority fee is the total in
     lamports that the old per-compute-unit figure worked out to.
  6. The config states a loaded-accounts data size limit large enough for the
     launch. Left unset the limit is zero rather than the network default, and
     the transaction is rejected for exceeding it -- invisibly, because the
     script skips preflight, so the signature simply never lands.

Usage:
    uv run tests/regression/verify_atomic_launch_v1.py
"""

import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "solana"))
sys.path.insert(0, str(PROJECT_ROOT / "cookbook" / "pumpfun" / "trade"))

import pumpfun_create_and_buy_token_txv1 as launch  # noqa: E402
import pumpfun_instructions as pump  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.instruction import Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0, MessageV1, TransactionConfig  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402
from spl.token.instructions import (  # noqa: E402
    create_idempotent_associated_token_account,
)

V0_PACKET_LIMIT = 1232
V1_PACKET_LIMIT = 4096
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"

# What the priority fee used to be expressed as, before v1 replaced
# micro-lamports per compute unit with one absolute total.
LEGACY_MICROLAMPORTS_PER_CU = 37_037
MICROLAMPORTS_PER_LAMPORT = 1_000_000

# A floor, not the requirement: the launch is rejected below this, so a limit at
# or under it cannot be right.
TOO_SMALL_FOR_THE_LAUNCH = 128 * 1024
SOLANA_MAX_LOADED_DATA = 64 * 1024 * 1024


def build_instructions() -> tuple[list[Instruction], Pubkey, list[Keypair]]:
    """The four instructions a launch sends, plus its payer and signers."""
    payer_kp = Keypair()
    mint_kp = Keypair()
    payer = payer_kp.pubkey()
    mint = mint_kp.pubkey()
    curve = pump.find_bonding_curve(mint)
    return (
        [
            pump.build_create_v2_instruction(
                mint=mint,
                user=payer,
                creator=payer,
                name="Test Token V2",
                symbol="TEST2",
                uri="https://example.com/token-v2.json",
            ),
            pump.build_extend_account_instruction(curve, payer),
            create_idempotent_associated_token_account(
                payer, payer, mint, pump.TOKEN_2022_PROGRAM
            ),
            pump.build_buy_v2_instruction(
                base_mint=mint,
                creator=payer,
                user=payer,
                token_amount_raw=3_540_900_000,
                max_quote_cost_raw=130_000,
                quote_mint=pump.WSOL_MINT,
                base_token_program=pump.TOKEN_2022_PROGRAM,
            ),
        ],
        payer,
        [payer_kp, mint_kp],
    )


def compile_v1(
    instructions: list[Instruction], payer: Pubkey, signers: list[Keypair]
) -> bytes:
    """Serialize the launch as the script sends it."""
    message = MessageV1.try_compile(
        payer,
        instructions,
        Hash.default(),
        TransactionConfig(
            compute_unit_limit=launch.COMPUTE_UNIT_LIMIT,
            priority_fee=launch.PRIORITY_FEE_LAMPORTS,
            loaded_accounts_data_size_limit=launch.LOADED_ACCOUNTS_DATA_LIMIT,
        ),
    )
    return bytes(VersionedTransaction(message, signers))


def check_single_message_holds_create_and_buy() -> bool:
    instructions, payer, _ = build_instructions()
    message = MessageV1.try_compile(payer, instructions, Hash.default())
    expected = 4
    if len(message.instructions) != expected:
        print(f"  message holds {len(message.instructions)} instructions")
        return False
    data = [bytes(ix.data)[:8] for ix in message.instructions]
    if pump.CREATE_V2_DISCRIMINATOR not in data:
        print("  no create_v2 in the message")
        return False
    if pump.BUY_V2_DISCRIMINATOR not in data:
        print("  no buy_v2 in the message — the launch is not atomic")
        return False
    return True


def check_fits_the_v1_limit() -> bool:
    instructions, payer, signers = build_instructions()
    size = len(compile_v1(instructions, payer, signers))
    if size > V1_PACKET_LIMIT:
        print(f"  {size} bytes exceeds the {V1_PACKET_LIMIT}-byte v1 limit")
        return False
    return True


def check_v0_could_not_hold_it() -> bool:
    instructions, payer, signers = build_instructions()
    v0 = bytes(
        VersionedTransaction(
            MessageV0.try_compile(payer, instructions, [], Hash.default()), signers
        )
    )
    if len(v0) <= V0_PACKET_LIMIT:
        print(
            f"  the launch is {len(v0)} bytes as v0, within {V0_PACKET_LIMIT} — "
            f"v1 is no longer required and this check needs rethinking"
        )
        return False
    return True


def check_no_compute_budget_instructions() -> bool:
    instructions, _, _ = build_instructions()
    offenders = [
        index
        for index, ix in enumerate(instructions)
        if str(ix.program_id) == COMPUTE_BUDGET_PROGRAM
    ]
    if offenders:
        print(f"  ComputeBudget instructions at {offenders}; v1 ignores them")
        return False
    return True


def check_priority_fee_is_a_lamport_total() -> bool:
    expected = round(
        LEGACY_MICROLAMPORTS_PER_CU
        * launch.COMPUTE_UNIT_LIMIT
        / MICROLAMPORTS_PER_LAMPORT
    )
    if launch.PRIORITY_FEE_LAMPORTS != expected:
        print(
            f"  PRIORITY_FEE_LAMPORTS is {launch.PRIORITY_FEE_LAMPORTS}, but the "
            f"per-compute-unit figure it replaces works out to {expected} lamports"
        )
        return False
    # Carrying the v0 number across unchanged is the failure this guards.
    if launch.PRIORITY_FEE_LAMPORTS == LEGACY_MICROLAMPORTS_PER_CU:
        print("  the micro-lamport figure was reused as a lamport total")
        return False
    instructions, payer, signers = build_instructions()
    raw = compile_v1(instructions, payer, signers)
    if struct.pack("<Q", launch.PRIORITY_FEE_LAMPORTS) not in raw:
        print("  the fee is not in the serialized message config")
        return False
    return True


def check_loaded_data_limit_is_declared() -> bool:
    limit = getattr(launch, "LOADED_ACCOUNTS_DATA_LIMIT", None)
    if not limit:
        print("  no loaded-accounts data size limit; v1 reads that as zero")
        return False
    if limit <= TOO_SMALL_FOR_THE_LAUNCH:
        print(f"  {limit} bytes is at or below the measured failing size")
        return False
    if limit > SOLANA_MAX_LOADED_DATA:
        print(f"  {limit} bytes is above the {SOLANA_MAX_LOADED_DATA} cap")
        return False
    instructions, payer, signers = build_instructions()
    if struct.pack("<I", limit) not in compile_v1(instructions, payer, signers):
        print("  the limit is not in the serialized message config")
        return False
    return True


def main() -> int:
    checks = [
        ("one message holds create and buy", check_single_message_holds_create_and_buy),
        ("fits the v1 limit", check_fits_the_v1_limit),
        ("v0 could not hold it", check_v0_could_not_hold_it),
        ("no ComputeBudget instructions", check_no_compute_budget_instructions),
        ("priority fee is a lamport total", check_priority_fee_is_a_lamport_total),
        ("loaded-accounts data limit declared", check_loaded_data_limit_is_declared),
    ]
    failed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failed += 1
            continue
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
