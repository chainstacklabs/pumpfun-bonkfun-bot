"""Verify cleanup closes the token accounts the bot leaves behind.

Account closes went out with no priority fee (the shipped configs turned it
off) and were never sent again: under load some were dropped, each after a long
wait and an error with a traceback, and their rent stayed. A position cleared
on start because its coin was sold by hand while the bot was down left its
empty account open. `post_session`, documented as closing every empty account,
closed only the accounts of the coins traded that session.

Offline machine checks, no network and no funds moved. The chain is a stub
that holds token accounts and plays out each close as told (landed, dropped,
landed but missed by the wait, reverted, refused by the node):

  A. Closes go out with the bot's priority fee unless `cleanup.with_priority_fee`
     is false: a config that leaves the key out, and every shipped config,
     sends them with it, and anything but true, false or nothing is refused.
     Each close carries a compute unit limit below the client's default.
  B. A close that is not confirmed by the time its blockhash expires is sent
     again, at most three attempts in total, then cleanup gives up with one
     warning line and no traceback. With `quiet`, the client's confirmation
     logs nothing when the wait runs out and one warning on any other error,
     and its send logs no traceback. A close the node refused is sent again;
     one whose send failed after it may have reached the node is logged once
     and waited on.
  C. A close that landed, or may still land, is never sent again: not when the
     confirmation wait missed it, when it was seen but not yet confirmed, when
     the lookup failed and the account is gone, when it reverted, when the wait
     ended early before the blockhash expired, or when it never settles (given
     up with one warning). With finalized reads lagging behind a close that
     landed, the read before a resend is at confirmed, pinned to the slot the
     expiry was seen at.
  D. A position cleared because the wallet no longer holds its coin (on start,
     a pending buy that landed and was sold, or one whose blockhash expired
     with none held) closes the coin's empty account under `after_sell`, with
     the priority fee, before shutdown ends; its token program is read from
     the mint when the record has none. Under `post_session` that waits for
     the sweep, `disabled` closes nothing, and another wallet's record or an
     unreadable balance closes nothing and stays in the file.
  E. `post_session` closes every empty token account the wallet owns, under
     SPL Token and Token-2022, also one no session traded and one that is not
     an associated account, also when nothing was traded; never a quote
     mint's (wSOL, USDC, a configured quote), never one of a coin still in the
     positions file (kept or pending), never one frozen or another key may
     close, and never burns an account it did not trade; swept accounts are
     not logged as traded ATAs. Under `manual` or `marry_mode` it burns
     nothing at all.
  F. A sold position's after-sell close holds no position slot, and
     `wait_all` and shutdown wait for it.

Usage:
    uv run tests/regression/verify_cleanup_closes_what_it_leaves.py
"""

import asyncio
import logging
import struct
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import yaml  # noqa: E402
from solana.rpc.core import TransactionExpiredBlockheightExceededError  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

import bot_runner  # noqa: E402
import config_loader  # noqa: E402
from cleanup.manager import AccountCleanupManager  # noqa: E402
from cleanup.modes import handle_cleanup_after_sell  # noqa: E402
from core.client import SolanaClient, TransactionNotSent  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from core.pubkeys import (  # noqa: E402
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    USDC_MINT,
    WSOL_MINT,
)
from core.wallet import Wallet  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform, TokenInfo  # noqa: E402
from trading import position_manager, universal_trader  # noqa: E402
from trading.base import PendingBuy, TradeResult  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402
from trading.position_store import PositionStore  # noqa: E402
from trading.settings import TraderSettings  # noqa: E402

KEY = base58.b58encode(bytes(Keypair())).decode()
WALLET = Wallet(KEY)
FEE = 123_456  # microlamports, the bot's fixed priority fee here
CLOSE_ACCOUNT = 9
BURN = 8
FROZEN = 2
CLOSE_ATTEMPTS = 3  # sends of one close before cleanup gives up

LIFETIME = 3  # blocks a close's blockhash stays valid on the stub chain
DEFAULT_CU_LIMIT = 85_000  # what the client sets when a fee comes without a limit

# No settle wait, short looks at a close in flight: the stub answers at once.
AccountCleanupManager.SETTLE_SECONDS = 0.0
AccountCleanupManager.RESEND_DELAY = 0.001
AccountCleanupManager.MAX_WAIT = 0.2


class _Logs(logging.Handler):
    """Every record logged while installed."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self) -> "_Logs":
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        logging.getLogger().removeHandler(self)

    def at(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]

    def tracebacks(self) -> int:
        return sum(r.exc_info is not None for r in self.records)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


def _account_data(
    mint: Pubkey,
    owner: Pubkey,
    amount: int,
    state: int = 1,
    close_authority: Pubkey | None = None,
) -> bytes:
    """An SPL token account (Token-2022's base account has the same layout)."""
    data = bytearray(165)
    data[0:32] = bytes(mint)
    data[32:64] = bytes(owner)
    struct.pack_into("<Q", data, 64, amount)
    data[108] = state
    if close_authority is not None:
        struct.pack_into("<I", data, 129, 1)
        data[133:165] = bytes(close_authority)
    return bytes(data)


class _Chain:
    """Token accounts by address, block height, and what each sent close does.

    `plan` holds how each send plays out, in order (default "land"). A close's
    blockhash is valid for `LIFETIME` blocks from the height it was sent at.
      land     lands; the confirmation wait sees it
      drop     never lands; the wait runs until the blockhash expired
      late     lands; the wait gives up anyway
      seen     lands; the wait runs out at expiry, and the first lookup still
               sees it unconfirmed
      lost     lands; the wait runs out at expiry and every lookup fails
      lag      lands; the wait runs out at expiry, no node knows the
               signature, and reads at the default commitment (finalized)
               still show the account
      early    the first wait ends early, before expiry (a rate limit), and no
               node knows it yet; it lands during the second wait
      early-unknown  the same, for a close whose blockhash height is unknown
      stuck    no wait, lookup or block height ever settles it
      revert   lands with an error
      refuse   the node refuses it: TransactionNotSent
      ambiguous  the send fails after it reached the node (a read timeout);
               it lands
    """

    rpc_endpoint = ""

    def __init__(self) -> None:
        self.accounts: dict[Pubkey, dict] = {}
        self.lagging: dict[Pubkey, dict] = {}  # closed, still seen at finalized
        self.broken: set[Pubkey] = set()  # reads fail
        self.mints: dict[Pubkey, Pubkey] = {}  # mint -> its token program
        self.plan: list[str] = []
        self.sent: list[dict] = []
        self.confirms: list[dict] = []
        self.reads: list[tuple[Pubkey, str | None, int | None]] = []
        self.landed: dict[str, ConfirmationStatus] = {}
        self.lookups: dict[str, int] = {}
        self.signed_last_valid: dict[str, int] = {}
        self.buy_details: tuple[int | None, int | None] = (None, None)
        self.height = 1_000

    def add(  # noqa: PLR0913
        self,
        mint: Pubkey,
        program: Pubkey,
        amount: int = 0,
        *,
        address: Pubkey | None = None,
        state: int = 1,
        close_authority: Pubkey | None = None,
        listed_amount: int | None = None,
    ) -> Pubkey:
        """Add a token account; `listed_amount` is what the listing shows, when
        tokens arrive between the listing and the close."""
        if address is None:
            address = WALLET.get_associated_token_address(mint, program)
        self.accounts[address] = {
            "mint": mint,
            "program": program,
            "amount": amount,
            "state": state,
            "close_authority": close_authority,
            "listed": amount if listed_amount is None else listed_amount,
        }
        self.mints.setdefault(mint, program)
        return address

    def _data(self, a: dict, *, listed: bool = False) -> bytes:
        amount = a["listed"] if listed else a["amount"]
        return _account_data(
            a["mint"], WALLET.pubkey, amount, a["state"], a["close_authority"]
        )

    def _view(self, address: Pubkey, commitment: str | None) -> dict | None:
        """The account as a read at `commitment` sees it (None: finalized)."""
        if commitment is None and address in self.lagging:
            return self.lagging[address]
        return self.accounts.get(address)

    def touched(self) -> set[Pubkey]:
        return {m.pubkey for tx in self.sent for ix in tx["ix"] for m in ix.accounts}

    def closes(self) -> list[Pubkey]:
        return [
            ix.accounts[0].pubkey
            for tx in self.sent
            for ix in tx["ix"]
            if bytes(ix.data)[:1] == bytes([CLOSE_ACCOUNT])
        ]

    def burns(self) -> list[Pubkey]:
        return [
            ix.accounts[0].pubkey
            for tx in self.sent
            for ix in tx["ix"]
            if bytes(ix.data)[:1] == bytes([BURN])
        ]

    def _tx(self, signature: str) -> dict:
        return next(tx for tx in self.sent if tx["sig"] == signature)

    def _land(self, tx: dict) -> None:
        self.landed[tx["sig"]] = ConfirmationStatus.SUCCESS
        for ix in tx["ix"]:
            address = ix.accounts[0].pubkey
            if bytes(ix.data)[:1] == bytes([BURN]):
                self.accounts[address]["amount"] = 0
            elif bytes(ix.data)[:1] == bytes([CLOSE_ACCOUNT]):
                closed = self.accounts.pop(address, None)
                if tx["play"] == "lag" and closed is not None:
                    self.lagging[address] = closed

    async def get_account_info(
        self,
        address: Pubkey,
        commitment: str | None = None,
        min_context_slot: int | None = None,
    ) -> SimpleNamespace:
        self.reads.append((address, commitment, min_context_slot))
        # Yield like a real round trip, so a close left running at shutdown
        # is caught unfinished.
        await asyncio.sleep(0)
        if address in self.broken:
            raise RuntimeError("node unavailable")  # noqa: TRY003
        account = self._view(address, commitment)
        if account is not None:
            return SimpleNamespace(data=self._data(account), owner=account["program"])
        if address in self.mints:
            return SimpleNamespace(data=bytes(82), owner=self.mints[address])
        raise ValueError(f"Account {address} not found")  # noqa: TRY003

    async def get_token_account_balance(
        self, address: Pubkey, commitment: str = "confirmed"
    ) -> int:
        del commitment
        # A node lagging behind for the account lags for its balance too.
        account = self.accounts.get(address) or self.lagging[address]
        return account["amount"]

    async def get_token_accounts(
        self, owner: Pubkey, program: Pubkey
    ) -> list[tuple[Pubkey, bytes]]:
        if owner != WALLET.pubkey:
            return []
        return [
            (address, self._data(a, listed=True))
            for address, a in self.accounts.items()
            if a["program"] == program
        ]

    async def get_block_height_and_slot(self) -> tuple[int, int]:
        return self.height, self.height + 5_000

    async def build_and_send_transaction(  # noqa: PLR0913, PLR0917 - the client's
        self,
        instructions: list,
        _keypair: object,
        skip_preflight: bool = True,  # noqa: FBT001, FBT002 - the client's signature
        max_retries: int = 3,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        on_signed: object = None,
        **_options: object,
    ) -> str:
        del skip_preflight, max_retries
        play = self.plan.pop(0) if self.plan else "land"
        signature = f"sig{len(self.sent)}"
        last_valid = self.height + LIFETIME
        if on_signed is not None:
            on_signed(signature, None if play == "early-unknown" else last_valid)
        self.signed_last_valid[signature] = last_valid
        tx = {
            "ix": instructions,
            "fee": priority_fee,
            "cu": compute_unit_limit,
            "play": play,
            "sig": signature,
            "waits": 0,
        }
        self.sent.append(tx)
        if play == "refuse":
            raise TransactionNotSent("node refused it")  # noqa: TRY003
        if play == "revert":
            self.landed[signature] = ConfirmationStatus.REVERTED
        elif play in ("land", "late", "seen", "lost", "lag", "ambiguous"):
            self._land(tx)
        if play == "ambiguous":
            raise TimeoutError("read timed out")  # noqa: TRY003
        return signature

    async def confirm_transaction(
        self,
        signature: str,
        commitment: str = "confirmed",
        *,
        last_valid_block_height: int | None = None,
        quiet: bool = False,
    ) -> bool:
        del commitment
        self.confirms.append(
            {"sig": signature, "last_valid": last_valid_block_height, "quiet": quiet}
        )
        tx = self._tx(signature)
        tx["waits"] += 1
        play = tx["play"]
        if play in ("land", "ambiguous"):
            return True
        if play in ("early", "early-unknown") and tx["waits"] > 1:
            self._land(tx)
            return True
        if play in ("drop", "lost", "lag", "late", "seen"):
            # A wait that runs to the end ends once the blockhash expired.
            self.height = max(self.height, self.signed_last_valid[signature] + 1)
        return False

    async def get_signature_outcome(
        self, signature: str, *, search_history: bool = True, quiet: bool = False
    ) -> tuple[ConfirmationStatus | None, int | None]:
        del search_history, quiet
        n = self.lookups[signature] = self.lookups.get(signature, 0) + 1
        play = self._tx(signature)["play"]
        if play == "lost":
            return ConfirmationStatus.UNCONFIRMED, None
        if play == "lag":
            return None, None
        if play == "seen" and n == 1:
            return ConfirmationStatus.UNCONFIRMED, 77
        status = self.landed.get(signature)
        return (status, 77) if status is not None else (None, None)

    async def get_buy_transaction_details(
        self, *_a: object, **_k: object
    ) -> tuple[int | None, int | None]:
        return self.buy_details

    async def get_health(self) -> str:
        return "ok"

    async def close(self) -> None:
        return None


def _fees() -> PriorityFeeManager:
    return PriorityFeeManager(
        client=None,
        enable_dynamic_fee=False,
        enable_fixed_fee=True,
        fixed_fee=FEE,
        extra_fee=0.0,
        hard_cap=10**9,
    )


def _after_sell(chain: _Chain, mint: Pubkey, *, with_fee: bool) -> None:
    asyncio.run(
        handle_cleanup_after_sell(
            chain,
            WALLET,
            mint,
            TOKEN_PROGRAM,
            _fees(),
            "after_sell",
            cleanup_with_prior_fee=with_fee,
            force_burn=False,
        )
    )


MINIMAL_CONFIG = {
    "name": "t3",
    "rpc_endpoint": "https://rpc.invalid",
    "wss_endpoint": "wss://rpc.invalid",
    "private_key": KEY,
    "trade": {"buy_amount": 0.001, "buy_slippage": 0.3, "sell_slippage": 0.3},
    "filters": {"listener_type": "logs", "max_token_age": 30},
}


def _with_fee_setting(cleanup: dict) -> bool:
    config = dict(MINIMAL_CONFIG, cleanup=cleanup)
    return bot_runner.build_trader_settings(
        config, Platform.PUMP_FUN
    ).cleanup_with_priority_fee


def _fee_setting_problems() -> list[str]:
    """The setting: on unless false, in the runner, the settings and the YAMLs."""
    problems = []
    for cleanup, expected in (
        ({}, True),
        ({"mode": "after_sell"}, True),
        ({"with_priority_fee": True}, True),
        ({"with_priority_fee": None}, True),
        ({"with_priority_fee": False}, False),
    ):
        got = _with_fee_setting(cleanup)
        if got is not expected:
            problems.append(f"cleanup {cleanup}: with priority fee {got}")
    if not TraderSettings.__dataclass_fields__["cleanup_with_priority_fee"].default:
        problems.append("TraderSettings defaults to closes without a priority fee")
    for path in sorted((PROJECT_ROOT / "bots").glob("*.yaml")):
        config = yaml.safe_load(path.read_text())
        if config.get("cleanup", {}).get("with_priority_fee") is False:
            problems.append(f"{path.name} sends closes without a priority fee")
    return problems


def _validation_problems() -> list[str]:
    """True, false or nothing pass; anything else is one clear config error."""
    problems = []
    for value in (True, False, None):
        try:
            config_loader.validate_config(
                dict(MINIMAL_CONFIG, cleanup={"with_priority_fee": value})
            )
        except ValueError as error:
            problems.append(f"with_priority_fee {value!r} refused: {error}")
    for value in (0, 1, "false", "true", "no"):
        try:
            config_loader.validate_config(
                dict(MINIMAL_CONFIG, cleanup={"with_priority_fee": value})
            )
        except ValueError as error:
            if "cleanup.with_priority_fee must be true or false" not in str(error):
                problems.append(f"with_priority_fee {value!r}: unclear error {error}")
        else:
            problems.append(f"with_priority_fee {value!r} accepted, not a bool")
    return problems


def check_a_fee_applied() -> list[str]:
    problems = _fee_setting_problems() + _validation_problems()
    for with_fee, expected in ((True, FEE), (False, None)):
        chain = _Chain()
        mint = Pubkey.new_unique()
        chain.add(mint, TOKEN_PROGRAM)
        _after_sell(chain, mint, with_fee=with_fee)
        fees = [tx["fee"] for tx in chain.sent]
        if fees != [expected]:
            problems.append(f"with_priority_fee {with_fee}: sent with fees {fees}")
        limits = [tx["cu"] for tx in chain.sent]
        if not limits or any(cu is None or cu >= DEFAULT_CU_LIMIT for cu in limits):
            problems.append(f"close compute unit limits {limits}, not a tight one")
    return problems


def _close_one(plan: list[str]) -> tuple[_Chain, _Logs, Pubkey]:
    chain = _Chain()
    chain.plan = list(plan)
    mint = Pubkey.new_unique()
    ata = chain.add(mint, TOKEN_PROGRAM)
    with _Logs() as logs:
        _after_sell(chain, mint, with_fee=True)
    return chain, logs, ata


def _client_quiet_problems() -> list[str]:  # noqa: C901 - one branch per case
    """Quiet: an expired wait logs nothing, any other error one warning line."""
    problems = []
    seen: dict = {}
    errors = [
        TransactionExpiredBlockheightExceededError("block height exceeded"),
        TimeoutError("read timed out"),
    ]

    class _Rpc:
        async def confirm_transaction(self, _sig: object, **kwargs: object) -> None:
            seen.update(kwargs)
            raise errors.pop(0)

        async def send_transaction(self, *_a: object, **_k: object) -> None:
            raise TimeoutError("read timed out")  # noqa: TRY003

    class _Limiter:
        async def acquire(self) -> None:
            return None

    async def get_client() -> _Rpc:
        return _Rpc()

    client = object.__new__(SolanaClient)
    client._rate_limiter = _Limiter()  # noqa: SLF001
    client.get_client = get_client
    client._blockhash_lock = asyncio.Lock()  # noqa: SLF001
    client._cached_blockhash = Hash.default()  # noqa: SLF001
    client._cached_last_valid_block_height = 5  # noqa: SLF001
    signature = str(Keypair().sign_message(b"close"))

    async def confirm() -> bool:
        return await client.confirm_transaction(
            signature, last_valid_block_height=4242, quiet=True
        )

    async def send() -> str:
        try:
            await client.build_and_send_transaction(
                [], Keypair(), max_retries=1, quiet=True
            )
        except TimeoutError:
            return "raised"
        return "returned"

    with _Logs() as expired:
        expired_confirmed = asyncio.run(confirm())
    if seen.get("last_valid_block_height") != 4242:  # noqa: PLR2004
        problems.append(f"client: confirmation waited with {seen}, not the blockhash")
    if expired_confirmed or expired.records:
        problems.append(
            f"client: an expired wait confirmed {expired_confirmed}, logged "
            f"{[r.getMessage() for r in expired.records]}"
        )
    with _Logs() as failed:
        failed_confirmed = asyncio.run(confirm())
    warnings = failed.at(logging.WARNING)
    if failed_confirmed or len(warnings) != 1 or len(failed.records) != 1:
        problems.append(
            f"client: a wait ending on an error confirmed {failed_confirmed}, "
            f"logged {[r.getMessage() for r in failed.records]}, not one warning"
        )
    with _Logs() as sent:
        outcome = asyncio.run(send())
    if outcome != "raised":
        problems.append(f"client: a failed quiet send {outcome}")
    tracebacks = expired.tracebacks() + failed.tracebacks() + sent.tracebacks()
    if tracebacks:
        problems.append(f"client: {tracebacks} traceback(s) logged when quiet")
    return problems


def check_b_dropped_close_retried() -> list[str]:  # noqa: C901 - one branch per case
    problems = []
    chain, logs, ata = _close_one(["drop", "drop", "drop", "drop"])
    attempts = CLOSE_ATTEMPTS
    if len(chain.sent) != attempts:
        problems.append(f"dropped close sent {len(chain.sent)} times, not {attempts}")
    if any(tx["fee"] != FEE for tx in chain.sent):
        problems.append("a re-sent close went out without the priority fee")
    waits = [(c["last_valid"], c["quiet"]) for c in chain.confirms]
    expected = [(chain.signed_last_valid[tx["sig"]], True) for tx in chain.sent]
    if waits != expected:
        problems.append(f"confirmation waits {waits}, expected {expected}")
    warnings = logs.at(logging.WARNING)
    if len(warnings) != 1 or "tools/cleanup_accounts.py" not in warnings[0]:
        problems.append(f"giving up logged warnings {warnings}")
    if logs.at(logging.ERROR) or logs.tracebacks():
        problems.append(
            f"errors {logs.at(logging.ERROR)}, {logs.tracebacks()} traceback(s)"
        )
    if ata not in chain.accounts:
        problems.append("the stub closed a dropped account")

    chain, logs, ata = _close_one(["drop", "land"])
    if len(chain.sent) != 2 or ata in chain.accounts or logs.at(logging.WARNING):  # noqa: PLR2004
        problems.append(
            f"drop then land: {len(chain.sent)} sends, open {ata in chain.accounts}, "
            f"warnings {logs.at(logging.WARNING)}"
        )
    chain, logs, ata = _close_one(["refuse", "land"])
    waited = [c["sig"] for c in chain.confirms]
    if len(chain.sent) != 2 or ata in chain.accounts or "sig0" in waited:  # noqa: PLR2004
        problems.append(
            f"refused then land: {len(chain.sent)} sends, open {ata in chain.accounts}, "
            f"waited on {waited}"
        )
    # The send failed after it may have reached the node: one warning naming
    # the error, then it is waited on like any other, not sent again.
    chain, logs, ata = _close_one(["ambiguous", "land"])
    warnings = logs.at(logging.WARNING)
    if (
        len(chain.sent) != 1
        or ata in chain.accounts
        or len(warnings) != 1
        or "read timed out" not in warnings[0]
    ):
        problems.append(
            f"send failed, landed: {len(chain.sent)} sends, open "
            f"{ata in chain.accounts}, warnings {warnings}"
        )
    try:
        problems += _client_quiet_problems()
    except Exception as error:  # noqa: BLE001 - what the check reports
        problems.append(f"client: {type(error).__name__}: {error}")
    return problems


def check_c_landed_close_not_resent() -> list[str]:  # noqa: C901 - one branch per case
    problems = []
    for plan, label in (
        (["late"], "the wait missed it"),
        (["seen"], "seen unconfirmed first"),
        (["lost"], "every lookup failed, account gone"),
    ):
        chain, logs, ata = _close_one(plan)
        if len(chain.sent) != 1:
            problems.append(f"{label}: sent {len(chain.sent)} times")
        if ata in chain.accounts:
            problems.append(f"{label}: account still open")
        if logs.at(logging.WARNING) or logs.at(logging.ERROR) or logs.tracebacks():
            problems.append(
                f"{label}: logged {logs.at(logging.WARNING) + logs.at(logging.ERROR)}"
            )
        if plan != ["lost"] and "Closed successfully" not in logs.text():
            problems.append(f"{label}: no 'Closed successfully' line")
    chain, logs, _ = _close_one(["revert", "land"])
    if len(chain.sent) != 1 or logs.tracebacks():
        problems.append(
            f"reverted: sent {len(chain.sent)} times, {logs.tracebacks()} traceback(s)"
        )

    # The wait ends early, before the blockhash expired, and no node knows the
    # close yet: it is still in flight and must be waited on, not sent again.
    for play in ("early", "early-unknown"):
        chain, logs, ata = _close_one([play, "land"])
        if len(chain.sent) != 1 or ata in chain.accounts:
            problems.append(
                f"wait ended early ({play}): sent {len(chain.sent)} times, open "
                f"{ata in chain.accounts}"
            )
    # Never settled: given up after MAX_WAIT with one warning, never re-sent.
    chain, logs, ata = _close_one(["stuck", "land"])
    warnings = logs.at(logging.WARNING)
    if len(chain.sent) != 1 or len(warnings) != 1 or logs.tracebacks():
        problems.append(
            f"never settled: sent {len(chain.sent)} times, warnings {warnings}"
        )
    # Landed, but no node knows the signature and finalized reads lag: the
    # read before a resend is at confirmed, pinned to the slot of the expiry.
    chain, logs, ata = _close_one(["lag", "land"])
    if len(chain.sent) != 1 or logs.at(logging.WARNING) or logs.at(logging.ERROR):
        problems.append(
            f"finalized lag: sent {len(chain.sent)} times, logged "
            f"{logs.at(logging.WARNING) + logs.at(logging.ERROR)}"
        )
    pinned = (ata, "confirmed", chain.height + 5_000)
    if pinned not in chain.reads:
        problems.append(
            f"finalized lag: no read {pinned} before a resend: {chain.reads}"
        )
    return problems


def _token(program: Pubkey | None = TOKEN_PROGRAM) -> TokenInfo:
    return TokenInfo(
        name="Gone",
        symbol="GONE",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.new_unique(),
        token_program_id=program,
        quote_mint=WSOL_MINT,
    )


def _settings(**overrides: object) -> TraderSettings:
    fields = {
        "rpc_endpoint": "",
        "wss_endpoint": "",
        "private_key": KEY,
        "buy_amount": 0.01,
        "buy_slippage": 0.1,
        "sell_slippage": 0.1,
        "exit_strategy": "tp_sl",
        "take_profit_percentage": 0.5,
        "stop_loss_percentage": 0.4,
        "max_hold_time": 60,
        "price_check_interval": 0.02,
        "cleanup_force_close_with_burn": True,
    }
    return TraderSettings(**(fields | overrides))


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _resume_cleared(  # noqa: PLR0913
    mode: str,
    *,
    program: Pubkey | None = TOKEN_PROGRAM,
    pending: bool = False,
    expired: bool = False,
    wallet: Pubkey | None = None,
    unreadable: bool = False,
) -> tuple[_Chain, Pubkey, bool, list[str]]:
    """Resume a position whose coin the wallet holds none of; then shut down.

    Args:
        pending: The record is a buy that landed (or, with `expired`, one no
            node knows whose blockhash has expired)
        wallet: The wallet the record names; None: this bot's
        unreadable: Every read of the coin's account fails

    Returns:
        The chain, the coin's account, whether the record is still in the
        file, and problems seen on the way
    """
    chain = _Chain()
    token = _token(program)
    # A record with no token program: the mint says SPL Token, not the default.
    ata = chain.add(token.mint, program or TOKEN_PROGRAM)
    if unreadable:
        chain.broken.add(ata)
    chain.buy_details = (1_000_000, 10_000)
    sent = TradeResult(
        success=True, price=1e-8, amount=1.0, amount_raw=1_000_000, tx_signature="buy"
    )
    if not expired:
        chain.landed["buy"] = ConfirmationStatus.SUCCESS
    chain.sent.append(
        {"ix": [], "fee": None, "cu": None, "sig": "buy", "waits": 0,
         "play": "drop" if expired else "land"}
    )  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        store = PositionStore(Path(tmp) / "p.json")
        store.add(
            token,
            sent,
            _now(),
            wallet or WALLET.pubkey,
            pending=(
                PendingBuy(max_amount_raw=1_000_000, last_valid_block_height=5)
                if pending
                else None
            ),
        )
        manager = PositionManager(
            _settings(cleanup_mode=mode),
            seller=SimpleNamespace(),
            solana_client=chain,
            wallet=WALLET,
            priority_fee_manager=_fees(),
            platform_implementations=SimpleNamespace(),
            store=store,
        )

        async def go() -> None:
            await manager.resume()
            await manager.wait_all()
            await manager.stop()

        with _Logs() as logs:
            asyncio.run(go())
        left = str(token.mint) in store.mints()
    chain.sent = [tx for tx in chain.sent if tx["sig"] != "buy"]
    problems = [f"{logs.tracebacks()} traceback(s)"] if logs.tracebacks() else []
    return chain, ata, left, problems


def check_d_cleared_position_closes() -> list[str]:
    problems = []
    for label, kwargs in (
        ("on start", {}),
        ("on start, no token program on record", {"program": None}),
        ("pending buy landed and sold", {"pending": True}),
        ("pending buy expired, none held", {"pending": True, "expired": True}),
    ):
        chain, ata, left, found = _resume_cleared("after_sell", **kwargs)
        problems += [f"{label}: {p}" for p in found]
        if left:
            problems.append(f"{label}: the cleared position is still in the file")
        if chain.closes() != [ata] or ata in chain.accounts:
            problems.append(f"{label}: closes {chain.closes()}, expected its account")
        if chain.burns():
            problems.append(f"{label}: burned {chain.burns()}")
        if [tx["fee"] for tx in chain.sent] != [FEE]:
            problems.append(f"{label}: fees {[tx['fee'] for tx in chain.sent]}")
    for label, mode, kwargs in (
        ("post_session", "post_session", {}),
        ("disabled", "disabled", {}),
        ("another wallet's record", "after_sell", {"wallet": Pubkey.new_unique()}),
        ("unreadable balance", "after_sell", {"unreadable": True}),
    ):
        chain, ata, left, found = _resume_cleared(mode, **kwargs)
        problems += [f"{label}: {p}" for p in found]
        if chain.sent:
            problems.append(f"{label}: closed {chain.closes()} on start")
        if left != (mode == "after_sell"):
            problems.append(f"{label}: record in the file {left}")
    return problems


async def _slot_free_while_closing() -> list[str]:
    """A sold position's after-sell close holds no slot; shutdown waits for it."""
    release = asyncio.Event()
    closed: list[Pubkey] = []

    async def slow_close(_c: object, _w: object, mint: Pubkey, *_a: object) -> None:
        await release.wait()
        closed.append(mint)

    manager = PositionManager(
        _settings(cleanup_mode="after_sell"),
        seller=SimpleNamespace(),
        solana_client=_Chain(),
        wallet=WALLET,
        priority_fee_manager=_fees(),
        platform_implementations=SimpleNamespace(),
    )
    token = _token()
    original = position_manager.handle_cleanup_after_sell
    position_manager.handle_cleanup_after_sell = slow_close
    problems = []
    try:
        # Stands in for the position task, ending in its after-sell cleanup.
        position = asyncio.create_task(manager._cleanup_after_exit(token))  # noqa: SLF001
        manager._open.add(position)  # noqa: SLF001
        position.add_done_callback(manager._open.discard)  # noqa: SLF001
        done, _ = await asyncio.wait({position}, timeout=0.5)
        if not done or manager.open_count:
            problems.append("the after-sell close holds the position's slot")
        waiter = asyncio.create_task(manager.wait_all())
        await asyncio.sleep(0.05)
        if waiter.done():
            problems.append("wait_all returned with a close still running")
        release.set()
        await asyncio.wait_for(waiter, 1.0)
        if closed != [token.mint]:
            problems.append(f"the close did not finish: {closed}")

        release.clear()
        closed.clear()
        await manager._cleanup_after_exit(token)  # noqa: SLF001
        stopper = asyncio.create_task(manager.stop())
        await asyncio.sleep(0.05)
        if stopper.done():
            problems.append("stop returned with a close still running")
        release.set()
        await asyncio.wait_for(stopper, 1.0)
        if closed != [token.mint]:
            problems.append(f"shutdown cut the close short: {closed}")
    finally:
        position_manager.handle_cleanup_after_sell = original
    return problems


def check_f_close_holds_no_slot() -> list[str]:
    return asyncio.run(_slot_free_while_closing())


def _build_trader(**overrides: object) -> universal_trader.UniversalTrader:
    originals = (
        universal_trader.SolanaClient,
        universal_trader.ListenerFactory.create_listener,
    )
    universal_trader.SolanaClient = lambda *_a, **_k: _Chain()
    universal_trader.ListenerFactory.create_listener = staticmethod(
        lambda **_k: SimpleNamespace()
    )
    try:
        return universal_trader.UniversalTrader(_settings(**overrides))
    finally:
        universal_trader.SolanaClient = originals[0]
        universal_trader.ListenerFactory.create_listener = staticmethod(originals[1])


def _sweep(
    tmp: Path, *, traded: bool = True, **overrides: object
) -> tuple[_Chain, dict[str, Pubkey], list[str]]:
    """A post-session cleanup over a wallet holding one of each kind of account."""
    quote = Pubkey.new_unique()
    trader = _build_trader(
        cleanup_mode="post_session",
        quote_amounts={str(quote): 1.0},
        positions_file=str(tmp / f"p{Pubkey.new_unique()}.json"),
        **overrides,
    )
    chain = _Chain()
    trader.solana_client = chain
    trader.priority_fee_manager = _fees()
    store = trader.positions.store
    accounts: dict[str, Pubkey] = {}
    for tag, program in (("spl", TOKEN_PROGRAM), ("t22", TOKEN_2022_PROGRAM)):
        accounts[f"foreign-{tag}"] = chain.add(Pubkey.new_unique(), program)
        accounts[f"foreign-not-ata-{tag}"] = chain.add(
            Pubkey.new_unique(), program, address=Pubkey.new_unique()
        )
        accounts[f"foreign-held-{tag}"] = chain.add(Pubkey.new_unique(), program, 500)
        accounts[f"frozen-{tag}"] = chain.add(
            Pubkey.new_unique(), program, state=FROZEN
        )
        accounts[f"filled-{tag}"] = chain.add(
            Pubkey.new_unique(), program, 300, listed_amount=0
        )
        accounts[f"other-closer-{tag}"] = chain.add(
            Pubkey.new_unique(), program, close_authority=Pubkey.new_unique()
        )
        for kind in ("kept", "pending"):
            token = _token(program)
            accounts[f"{kind}-{tag}"] = chain.add(token.mint, program)
            store.add(
                token,
                TradeResult(success=True, amount=1.0, amount_raw=1, price=1e-8),
                _now(),
                WALLET.pubkey,
                pending=PendingBuy() if kind == "pending" else None,
            )
    accounts["wsol"] = chain.add(WSOL_MINT, TOKEN_PROGRAM)
    accounts["usdc"] = chain.add(USDC_MINT, TOKEN_PROGRAM)
    accounts["configured-quote"] = chain.add(quote, TOKEN_2022_PROGRAM)
    if traded:
        for kind, amount in (("traded-sold", 0), ("traded-held", 700)):
            token = _token(TOKEN_2022_PROGRAM)
            accounts[kind] = chain.add(token.mint, TOKEN_2022_PROGRAM, amount)
            trader.traded_mints.add(token.mint)
            trader.traded_token_programs[str(token.mint)] = TOKEN_2022_PROGRAM
    with _Logs() as logs:
        asyncio.run(trader._cleanup_resources())  # noqa: SLF001
    problems = [f"{logs.tracebacks()} traceback(s)"] if logs.tracebacks() else []
    chain.log_text = logs.text()
    return chain, accounts, problems


def _sweep_problems(
    label: str, chain: _Chain, accounts: dict[str, Pubkey], closed: set[str]
) -> list[str]:
    problems = []
    got = {name for name, a in accounts.items() if a not in chain.accounts}
    if got != closed:
        problems.append(
            f"{label}: closed {sorted(got)}; expected {sorted(closed)} "
            f"(missing {sorted(closed - got)}, extra {sorted(got - closed)})"
        )
    left_alone = {a for name, a in accounts.items() if name not in closed}
    if chain.touched() & left_alone:
        names = sorted(
            n for n, a in accounts.items() if a in chain.touched() & left_alone
        )
        problems.append(f"{label}: sent instructions naming {names}")
    if any(tx["fee"] != FEE for tx in chain.sent):
        problems.append(f"{label}: a close went out without the priority fee")
    return problems


FOREIGN = {
    "foreign-spl",
    "foreign-t22",
    "foreign-not-ata-spl",
    "foreign-not-ata-t22",
}


def check_e_post_session_sweep() -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        chain, accounts, found = _sweep(Path(tmp))
        problems += found
        problems += _sweep_problems(
            "tp_sl with force burn",
            chain,
            accounts,
            FOREIGN | {"traded-sold", "traded-held"},
        )
        listed = "Closing 6 other empty token account(s) in the wallet"
        if listed not in chain.log_text:
            problems.append(f"the sweep did not log {listed!r}")
        for name in FOREIGN | {"filled-spl", "filled-t22"}:
            address = str(accounts[name])
            ata_lines = [
                line
                for line in chain.log_text.splitlines()
                if address in line and ("ATA" in line or "FORCE_CLOSE" in line)
            ]
            if ata_lines:
                problems.append(f"swept {name} logged as a traded ATA: {ata_lines}")
        if chain.burns() != [accounts["traded-held"]]:
            problems.append(f"burned {chain.burns()}, expected the traded coin only")

        chain, accounts, found = _sweep(Path(tmp), traded=False)
        problems += found
        problems += _sweep_problems("nothing traded", chain, accounts, FOREIGN)

        for label, overrides in (
            ("manual", {"exit_strategy": "manual"}),
            ("marry_mode", {"marry_mode": True}),
        ):
            chain, accounts, found = _sweep(Path(tmp), **overrides)
            problems += [f"{label}: {p}" for p in found]
            problems += _sweep_problems(
                label, chain, accounts, FOREIGN | {"traded-sold"}
            )
            if chain.burns():
                problems.append(f"{label}: burned {chain.burns()}")
    return problems


def main() -> int:
    """Run every check and report."""
    logging.getLogger().setLevel(logging.INFO)
    logging.lastResort = None
    checks = [
        ("A: closes carry the priority fee unless it is off", check_a_fee_applied),
        (
            "B: a dropped close is sent again, then given up quietly",
            check_b_dropped_close_retried,
        ),
        ("C: a close that landed is never sent again", check_c_landed_close_not_resent),
        (
            "D: a cleared position closes its empty account",
            check_d_cleared_position_closes,
        ),
        (
            "E: post_session sweeps every other empty account, never a kept one",
            check_e_post_session_sweep,
        ),
        (
            "F: an after-sell close holds no position slot, and is waited for",
            check_f_close_holds_no_slot,
        ),
    ]
    failed = 0
    for label, check in checks:
        try:
            problems = check()
        except Exception as error:  # noqa: BLE001 - report and continue
            problems = [f"{type(error).__name__}: {error}"]
        for problem in problems:
            print(f"    {problem}")
        print(f"{'FAIL' if problems else 'PASS'} {label}")
        failed += bool(problems)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
