"""Verify cleanup harvests withheld transfer fees before closing a token account.

A Token-2022 coin with a transfer fee withholds the fee inside the account that
receives it — the bot's own account, on a buy — and Token-2022 refuses to close
an account still holding withheld fees (custom error 0x23). Cleanup used to send
a bare CloseAccount, which reverts for every LaunchLab reward coin, leaving its
rent locked and the wallet not back to SOL only.

Fixture: `raw_stonkfun_token_accounts_from_getaccountinfo.json`, two real
Token-2022 token accounts: one holding withheld fees from a StonkFun reward
coin, one from a standard coin with no transfer-fee extension.

Offline machine checks, no network and no funds moved:

  A. The withheld amount reads from the account's TransferFeeAmount
     extension; an account without the extension, and a classic SPL account,
     read as zero.
  B. Cleanup of the account holding withheld fees sends HarvestWithheldTokensToMint
     (mint and account, writable, no signer) before CloseAccount, in one
     transaction.
  C. Cleanup of an account with nothing withheld sends CloseAccount alone.

Usage:
    uv run tests/regression/verify_cleanup_harvests_withheld_fees.py
"""

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from cleanup import manager as cleanup_manager  # noqa: E402
from cleanup.manager import AccountCleanupManager, withheld_transfer_fee  # noqa: E402
from core.pubkeys import SystemAddresses  # noqa: E402

FIXTURE = Path(__file__).with_name(
    "raw_stonkfun_token_accounts_from_getaccountinfo.json"
)
HARVEST_WITHHELD_TO_MINT = bytes([26, 4])
CLOSE_ACCOUNT = bytes([9])


def _accounts() -> dict:
    return json.loads(FIXTURE.read_text())


class _CleanupClient:
    """Serves one token account and records what cleanup sends."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.sent: list = []

    async def get_account_info(self, _address: Pubkey) -> SimpleNamespace:
        return SimpleNamespace(data=self.data, owner=SystemAddresses.TOKEN_2022_PROGRAM)

    async def get_token_account_balance(self, _address: Pubkey) -> int:
        return 0

    async def build_and_send_transaction(self, instructions: list, *_a, **_k) -> str:
        self.sent.append(instructions)
        return "STUB_SIGNATURE"

    async def confirm_transaction(self, *_a, **_k) -> bool:
        return True


async def _no_sleep(_seconds: float) -> None:
    return None


def _cleanup(entry: dict) -> list:
    address = Pubkey.from_string(entry["address"])
    client = _CleanupClient(base64.b64decode(entry["data_base64"]))
    wallet = SimpleNamespace(
        pubkey=Pubkey.from_string(entry["owner"]),
        keypair=None,
        get_associated_token_address=lambda _mint, _program: address,
    )
    cleanup_manager.asyncio.sleep = _no_sleep
    manager = AccountCleanupManager(client, wallet, priority_fee_manager=None)
    asyncio.run(
        manager.cleanup_ata(
            Pubkey.from_string(entry["mint"]), SystemAddresses.TOKEN_2022_PROGRAM
        )
    )
    return client.sent


def check_a_withheld_amount_reads() -> bool:
    """A: withheld amount from the extension; zero without one."""
    accounts = _accounts()
    withheld = withheld_transfer_fee(
        base64.b64decode(accounts["with_withheld_fee"]["data_base64"])
    )
    none = withheld_transfer_fee(
        base64.b64decode(accounts["without_transfer_fee_extension"]["data_base64"])
    )
    classic = withheld_transfer_fee(bytes(165))
    ok = withheld > 0 and none == 0 and classic == 0
    if not ok:
        print(f"    withheld={withheld} without_extension={none} classic={classic}")
    return ok


def check_b_harvest_before_close() -> bool:
    """B: harvest (mint, account; writable, unsigned) then close, one transaction."""
    entry = _accounts()["with_withheld_fee"]
    sent = _cleanup(entry)
    if len(sent) != 1 or len(sent[0]) != 2:  # noqa: PLR2004
        print(f"    sent {sent}")
        return False
    harvest, close = sent[0]
    metas = [(str(m.pubkey), m.is_writable, m.is_signer) for m in harvest.accounts]
    ok = (
        harvest.program_id == SystemAddresses.TOKEN_2022_PROGRAM
        and bytes(harvest.data) == HARVEST_WITHHELD_TO_MINT
        and metas == [(entry["mint"], True, False), (entry["address"], True, False)]
        and bytes(close.data) == CLOSE_ACCOUNT
    )
    if not ok:
        print(f"    harvest={harvest} close={close}")
    return ok


def check_c_close_alone_without_withheld() -> bool:
    """C: nothing withheld -> CloseAccount alone."""
    sent = _cleanup(_accounts()["without_transfer_fee_extension"])
    ok = len(sent) == 1 and [bytes(ix.data) for ix in sent[0]] == [CLOSE_ACCOUNT]
    if not ok:
        print(f"    sent {sent}")
    return ok


def main() -> int:
    """Run every check and report."""
    checks = [
        ("A: withheld amount reads from the account", check_a_withheld_amount_reads),
        ("B: harvest precedes close", check_b_harvest_before_close),
        (
            "C: close alone when nothing is withheld",
            check_c_close_alone_without_withheld,
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
