"""Verify a revert's error code is named from the IDL of the program that raised it.

Anchor error codes from 6000 up mean something different in every program, but
every `Custom(N)` the client reported was looked up in pump.fun's IDL. A
PumpSwap sell reverting with 6004, `ExceededSlippage` in PumpSwap's IDL, was
logged as "pump.fun IDL: 6004 MintDoesNotMatchBondingCurve"; LaunchLab and
other programs' reverts were mislabeled the same way.

Fixture: `recorded_reverts_from_gettransaction.json`, reverted mainnet
transactions as `getTransaction` returns them: a PumpSwap sell (6004), a
pump.fun curve buy (6002), a LaunchLab swap called directly (6004), a Jupiter
route that Jupiter itself reverted (6001), and a pump.fun instruction whose
System program call failed (Custom 1). No recorded revert was raised by pump
fees, which pump.fun's buy and PumpSwap's swaps call: that case is the PumpSwap
sell with its pump fees call failing instead, built from its logs.

Offline machine checks, no network and no funds moved:

  A. From the transaction: `verify_transaction_status`, the client's own line,
     `confirm_transaction_detailed` and `get_trade_deltas` name each recorded
     revert from the IDL of the program that raised it, labelled with that
     program, and nothing for a program with no IDL here; the transaction wins
     over the program the caller names.
  B. The program that raised it is the one the logs show failing first, also
     when the instruction at the error's index called it; no logs, or logs
     truncated before the failure, name no program.
  C. A revert seen in a signature status (the confirmation wait's, and
     `get_signature_outcome`'s) reads its transaction once to name the code
     from the program that raised it, whatever venue the caller names: pump
     fees inside a PumpSwap sell is named from pump fees' IDL. The read asks
     for at most 3 s and a node that never serves it holds the revert up no
     longer than that.
  D. When that transaction cannot be read, the code is named from the venue the
     caller sent its trade to, hedged ("PumpSwap IDL, if PumpSwap raised it:
     ..."), CPMM included, and not at all for an unknown program or none.
  E. Each venue hands its program: the pump.fun and LaunchLab curves, PumpSwap
     and CPMM; a pending buy's venue resolves to its program, and to none when
     the record names no venue of the platform.
  F. Naming never stops a revert being reported: an IDL that fails to load, or
     a transaction read that raises, leaves a REVERTED status with its error.

The buyer, the seller and the pending-buy resolver passing their venue's
program on are checked through the real trade paths in
`verify_logs_name_coin_and_venue.py` (E).

Usage:
    uv run tests/regression/verify_reverts_named_by_their_program.py
"""

import asyncio
import copy
import inspect
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402
from solders.rpc.responses import GetSignatureStatusesResp  # noqa: E402

from core import client as client_module  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from interfaces.core import ConfirmationStatus, Platform  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from platforms.launchlab.address_provider import LAUNCHLAB_PROGRAM  # noqa: E402
from platforms.launchlab.cpmm_market import CPMM_PROGRAM  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.pumpswap_market import PUMP_AMM_PROGRAM  # noqa: E402
from trading.position_manager import PositionManager  # noqa: E402

HERE = Path(__file__).resolve().parent
RECORDED = {
    entry["signature"][:8]: entry["transaction"]
    for entry in json.loads(
        (HERE / "recorded_reverts_from_gettransaction.json").read_text()
    )
}
PUMPSWAP_SELL = "5z5Nf6zw"  # PumpSwap sell, Custom 6004
PUMPFUN_BUY = "3BP2RDMc"  # pump.fun buy_v2, Custom 6002
LAUNCHLAB_SWAP = "21u8gR5N"  # LaunchLab swap at top level, Custom 6004
JUPITER_ROUTE = "42EgYd7e"  # Jupiter reverted its own route, Custom 6001
SYSTEM_IN_PUMPFUN = "5GS2GesF"  # pump.fun's System program call failed, Custom 1

SYSTEM_PROGRAM = "11111111111111111111111111111111"
PUMP_FEES = str(PumpFunAddresses.FEE_PROGRAM)
UNKNOWN_PROGRAM = Pubkey.new_unique()

# (fixture, what the revert must be named, a name it must not carry)
FROM_TRANSACTION = (
    (PUMPSWAP_SELL, "PumpSwap IDL: 6004 ExceededSlippage", "MintDoesNotMatch"),
    (PUMPFUN_BUY, "pump.fun IDL: 6002 TooMuchSolRequired", None),
    (LAUNCHLAB_SWAP, "Raydium LaunchLab IDL: 6004 ExceededSlippage", "MintDoesNot"),
    (JUPITER_ROUTE, None, None),
    (SYSTEM_IN_PUMPFUN, None, None),
)
# (program the caller sent its trade to, the name 6004 must get when the
# transaction cannot be read)
FROM_VENUE = (
    (PUMP_AMM_PROGRAM, "PumpSwap IDL, if PumpSwap raised it: 6004 ExceededSlippage"),
    (
        PumpFunAddresses.PROGRAM,
        "pump.fun IDL, if pump.fun raised it: 6004 MintDoesNotMatchBondingCurve",
    ),
    (
        LAUNCHLAB_PROGRAM,
        "Raydium LaunchLab IDL, if Raydium LaunchLab raised it: 6004 ExceededSlippage",
    ),
    (
        PumpFunAddresses.FEE_PROGRAM,
        "pump fees IDL, if pump fees raised it: 6004 OffsetNotContinuous",
    ),
    (
        CPMM_PROGRAM,
        "Raydium CPMM IDL, if Raydium CPMM raised it: 6004 IncorrectLpMint",
    ),
    (UNKNOWN_PROGRAM, None),
    (None, None),
)
# The most a status-shown revert's naming read may ask for, and may hold it up.
READ_CAP_SECONDS = 3.0
HELD_CAP_SECONDS = 3.5
# pump fees' 6031, UnsupportedQuoteMint; in PumpSwap's IDL 6031 is EndTimeInThePast.
FEES_CODE = 6031
SIGNATURE = "5z5Nf6zwkCUpfjafWmcn6ji92xzXeNWPakmpXNW7ed8SgWfjJcnuTt5VdCVMVuvocc8QhTevc5YvRcvVd8yip9FP"


async def _nothing(*_a: object, **_k: object) -> None:
    return None


class _Client(SolanaClient):
    """Serves one recorded transaction, or only a confirmed status with its error.

    Args:
        transaction: The `getTransaction` result; None: the node cannot serve it
        status_err: The `err` of the confirmed signature status; None: no status
    """

    def __init__(
        self, transaction: dict | None = None, status_err: object = None
    ) -> None:
        self._rate_limiter = SimpleNamespace(acquire=_nothing)
        self.transaction, self.status_err = transaction, status_err

    def _status(self) -> list:
        if self.status_err is None:
            return [None]
        return [
            {
                "slot": 9,
                "confirmations": None,
                "err": self.status_err,
                "status": {"Err": self.status_err},
                "confirmationStatus": "confirmed",
            }
        ]

    async def get_client(self) -> SimpleNamespace:
        async def confirm(*_a: object, **_k: object) -> object:
            if self.status_err is None:
                return None
            return GetSignatureStatusesResp.from_json(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"context": {"slot": 9}, "value": self._status()},
                    }
                )
            )

        return SimpleNamespace(confirm_transaction=confirm)

    async def post_rpc(self, *_a: object, **_k: object) -> dict:
        return {"result": {"context": {"slot": 9}, "value": self._status()}}

    async def _get_transaction_result(self, *_a: object, **_k: object) -> dict | None:
        return self.transaction


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _logged(run) -> tuple[object, list[str]]:  # noqa: ANN001
    records = _Records()
    root = logging.getLogger()
    root.addHandler(records)
    try:
        return run(), records.lines
    finally:
        root.removeHandler(records)


def _with_program(method, **kwargs: object) -> dict:  # noqa: ANN001
    """`kwargs` plus `program`, when the method takes one.

    Before the fix no method took a program, so a check of the name it gives
    must still run there and fail on the name, not on the call.
    """
    if "program" in inspect.signature(method).parameters:
        return kwargs
    return {k: v for k, v in kwargs.items() if k != "program"}


def _name_problems(
    where: str, text: str, wanted: str | None, banned: str | None
) -> list:
    bad = []
    if wanted is None:
        if "IDL:" in text:
            bad.append(f"{where}: {text!r} names it from another program's IDL")
        return bad
    if wanted not in text:
        bad.append(f"{where}: {text!r} is not named {wanted!r}")
    if banned and banned in text:
        bad.append(f"{where}: {text!r} carries another program's name")
    return bad


def _fees_raised_in_sell() -> dict:
    """The PumpSwap sell, its pump fees call failing with `FEES_CODE` instead.

    Built from the recorded logs: pump-amm's `GetFeesWithQuoteMint` call into
    pump fees fails, and pump-amm fails with the same code after it.
    """
    tx = copy.deepcopy(RECORDED[PUMPSWAP_SELL])
    logs = tx["meta"]["logMessages"]
    call = logs.index("Program log: Instruction: GetFeesWithQuoteMint")
    hex_code = hex(FEES_CODE)
    tx["meta"]["logMessages"] = [
        *logs[: call + 1],
        "Program log: AnchorError occurred. Error Code: UnsupportedQuoteMint. "
        f"Error Number: {FEES_CODE}. Error Message: Unsupported quote mint.",
        f"Program {PUMP_FEES} consumed 3000 of 160180 compute units",
        f"Program {PUMP_FEES} failed: custom program error: {hex_code}",
        f"Program {PUMP_AMM_PROGRAM} consumed 12000 of 199405 compute units",
        f"Program {PUMP_AMM_PROGRAM} failed: custom program error: {hex_code}",
    ]
    tx["meta"]["err"] = {"InstructionError": [4, {"Custom": FEES_CODE}]}
    return tx


def _status_reverts(
    client: SolanaClient, program: object
) -> list[tuple[str, object, str]]:
    """(path, status, error text) of a status-shown revert, from both status paths."""
    out = []
    revert: list[str] = []
    method = client.get_signature_outcome
    status, _ = asyncio.run(
        method(
            SIGNATURE, **_with_program(method, on_revert=revert.append, program=program)
        )
    )
    out.append(("outcome", status, "".join(revert)))
    revert = []
    method = client.confirm_transaction_detailed
    status = asyncio.run(
        method(
            SIGNATURE, **_with_program(method, on_revert=revert.append, program=program)
        )
    )
    out.append(("confirm", status, "".join(revert)))
    return out


def _report(name: str, bad: list[str]) -> bool:
    for line in bad:
        print(f"    {name}: {line}")
    return not bad


# --- A: from the transaction ------------------------------------------------------


def check_a_named_from_the_transaction() -> bool:
    """A: a read transaction names its revert from the raising program's IDL."""
    bad = []
    for key, wanted, banned in FROM_TRANSACTION:
        tx = RECORDED[key]
        err = tx["meta"]["err"]

        revert: list[str] = []
        status = asyncio.run(
            _Client(tx).verify_transaction_status(SIGNATURE, on_revert=revert.append)
        )
        if status is not ConfirmationStatus.REVERTED or len(revert) != 1:
            bad.append(f"{key}: status {status}, errors {revert}")
            continue
        if str(err) not in revert[0]:
            bad.append(f"{key}: {revert[0]!r} drops the error itself")
        bad += _name_problems(key, revert[0], wanted, banned)

        # The client's own line, when the caller takes no error.
        _, lines = _logged(
            lambda tx=tx: asyncio.run(_Client(tx).verify_transaction_status(SIGNATURE))
        )
        own = [x for x in lines if "landed and reverted" in x]
        bad += _name_problems(
            f"{key} own line", " ".join(own) or "<none>", wanted, banned
        )

        # The transaction outranks the program the caller names.
        client = _Client(tx)
        method = client.confirm_transaction_detailed
        revert = []
        asyncio.run(
            method(
                SIGNATURE,
                **_with_program(
                    method, on_revert=revert.append, program=PumpFunAddresses.PROGRAM
                ),
            )
        )
        bad += _name_problems(
            f"{key} confirm, caller says pump.fun", "".join(revert), wanted, banned
        )

        # The read-back of a trade's amounts logs the same name.
        _, lines = _logged(
            lambda tx=tx: asyncio.run(
                _Client(tx).get_trade_deltas(
                    SIGNATURE, Pubkey.new_unique(), Pubkey.new_unique(), None
                )
            )
        )
        failed = [x for x in lines if "failed with error" in x]
        bad += _name_problems(
            f"{key} trade read-back", " ".join(failed) or "<none>", wanted, banned
        )
    return _report("A", bad)


# --- B: which program raised it ---------------------------------------------------


def check_b_the_raising_program() -> bool:
    """B: the program the logs show failing first; no logs, no program."""
    failing = getattr(client_module, "_failing_program", None)
    if failing is None:
        return _report("B", ["the client never asks which program raised an error"])
    bad = []
    wanted = {
        PUMPSWAP_SELL: str(PUMP_AMM_PROGRAM),
        PUMPFUN_BUY: str(PumpFunAddresses.PROGRAM),
        LAUNCHLAB_SWAP: str(LAUNCHLAB_PROGRAM),
        JUPITER_ROUTE: "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        # Instruction 2 is pump.fun's; the System program it called failed.
        SYSTEM_IN_PUMPFUN: SYSTEM_PROGRAM,
    }
    for key, program in wanted.items():
        if (got := failing(RECORDED[key])) != program:
            bad.append(f"{key}: raised by {got}, not {program}")

    top = RECORDED[SYSTEM_IN_PUMPFUN]["transaction"]["message"]["instructions"][2]
    if top.get("programId") != str(PumpFunAddresses.PROGRAM):
        bad.append(f"fixture: {SYSTEM_IN_PUMPFUN} instruction 2 is not pump.fun's")

    # Logs cut off before the failure line cannot say which program failed: a
    # program the instruction called may have, so no program is named.
    truncated = copy.deepcopy(RECORDED[PUMPFUN_BUY])
    logs = truncated["meta"]["logMessages"]
    cut = next(i for i, x in enumerate(logs) if " failed: " in x)
    truncated["meta"]["logMessages"] = [*logs[: cut - 3], "Log truncated"]
    if (got := failing(truncated)) is not None:
        bad.append(f"truncated logs: raised by {got}, not unknown")

    # Without logs nothing says whether a program the instruction called
    # raised it.
    for logs in (None, []):
        bare = copy.deepcopy(RECORDED[PUMPSWAP_SELL])
        bare["meta"]["logMessages"] = logs
        if (got := failing(bare)) is not None:
            bad.append(f"logs {logs!r}: raised by {got}, not unknown")

    if (got := failing(_fees_raised_in_sell())) != PUMP_FEES:
        bad.append(f"pump fees failing inside the sell: raised by {got}")

    bad += _shape_problems(failing)
    return _report("B", bad)


def _shape_problems(failing) -> list[str]:  # noqa: ANN001
    """An unrecognized transaction names no program and raises nothing."""
    bad = []
    for broken in ({}, {"meta": {"err": "AccountInUse"}}, {"meta": {"err": None}}):
        try:
            if (got := failing(broken)) is not None:
                bad.append(f"{broken}: raised by {got}")
        except Exception as e:  # noqa: BLE001
            bad.append(f"{broken}: raised {type(e).__name__}")
    return bad


# --- C, D: from the signature status ---------------------------------------------


class _SlowNode(_Client):
    """A node that has the status but never serves the transaction."""

    def __init__(self, status_err: object) -> None:
        super().__init__(status_err=status_err)
        self.asked: list[float] = []

    async def post_rpc(self, body: dict, *_a: object, **_k: object) -> dict:
        if body.get("method") == "getTransaction":
            await asyncio.sleep(0.05)
            return {"jsonrpc": "2.0", "id": 1, "result": None}
        return await super().post_rpc(body)

    async def _get_transaction_result(self, *a: object, **k: object) -> dict | None:
        self.asked.append(k.get("budget_seconds", a[1] if len(a) > 1 else None))
        return await SolanaClient._get_transaction_result(self, *a, **k)  # noqa: SLF001


def check_c_status_named_from_its_transaction() -> bool:
    """C: a status-shown revert reads its transaction to name the raiser."""
    bad = []
    cases = (
        # (transaction, venues the caller may name, wanted, banned)
        (
            RECORDED[PUMPSWAP_SELL],
            (PUMP_AMM_PROGRAM, CPMM_PROGRAM, None),
            "(PumpSwap IDL: 6004 ExceededSlippage",
            "MintDoesNotMatch",
        ),
        (
            _fees_raised_in_sell(),
            (PUMP_AMM_PROGRAM, PumpFunAddresses.PROGRAM),
            f"(pump fees IDL: {FEES_CODE} UnsupportedQuoteMint",
            "PumpSwap IDL",
        ),
        (
            RECORDED[PUMPFUN_BUY],
            (PumpFunAddresses.PROGRAM,),
            "(pump.fun IDL: 6002 TooMuchSolRequired",
            "if pump.fun raised it",
        ),
    )
    for tx, venues, wanted, banned in cases:
        err = tx["meta"]["err"]
        for venue in venues:
            client = _Client(tx, status_err=err)
            for path, status, text in _status_reverts(client, venue):
                where = f"{path} {err}, sent to {venue}"
                if status is not ConfirmationStatus.REVERTED:
                    bad.append(f"{where}: status {status}")
                bad += _name_problems(where, text, wanted, banned)

    # A node that never serves the transaction holds the revert up only for
    # the bounded read; the error is then named, hedged, from the venue.
    err = RECORDED[PUMPSWAP_SELL]["meta"]["err"]
    for path in ("outcome", "confirm"):
        node = _SlowNode(err)
        method = (
            node.get_signature_outcome
            if path == "outcome"
            else node.confirm_transaction_detailed
        )
        revert: list[str] = []
        started = time.monotonic()
        result = asyncio.run(
            method(
                SIGNATURE,
                **_with_program(
                    method, on_revert=revert.append, program=PUMP_AMM_PROGRAM
                ),
            )
        )
        took = time.monotonic() - started
        status = result[0] if isinstance(result, tuple) else result
        if not node.asked:
            bad.append(f"slow node {path}: the transaction was never read to name it")
        elif any(b is None or b > READ_CAP_SECONDS for b in node.asked):
            bad.append(f"slow node {path}: read asked for {node.asked} s, not <= 3")
        if took > HELD_CAP_SECONDS:
            bad.append(f"slow node {path}: the revert was held up {took:.1f} s")
        if status is not ConfirmationStatus.REVERTED:
            bad.append(f"slow node {path}: status {status}")
        bad += _name_problems(
            f"slow node {path}",
            "".join(revert),
            "(PumpSwap IDL, if PumpSwap raised it: 6004 ExceededSlippage",
            None,
        )
    return _report("C", bad)


def check_d_unread_named_from_the_venue() -> bool:
    """D: an unreadable transaction leaves the venue's name, hedged."""
    bad = []
    err = RECORDED[PUMPSWAP_SELL]["meta"]["err"]
    for program, wanted in FROM_VENUE:
        for path, status, text in _status_reverts(_Client(status_err=err), program):
            where = f"{path}, sent to {program}"
            if status is not ConfirmationStatus.REVERTED:
                bad.append(f"{where}: status {status}")
            banned = "pump.fun IDL" if program != PumpFunAddresses.PROGRAM else None
            bad += _name_problems(where, text, wanted, banned)
    return _report("D", bad)


# --- E: each venue's program ------------------------------------------------------


# Building a platform's markets reads nothing; the endpoint only keys the cache.
CLIENT = SimpleNamespace(rpc_endpoint="http://127.0.0.1:1")


def _manager(platform: Platform) -> PositionManager:
    manager = object.__new__(PositionManager)
    manager.platform_implementations = get_platform_implementations(platform, CLIENT)
    return manager


def check_e_each_venue_names_its_program() -> bool:
    """E: the curves and pools hand the program a revert is named by."""
    bad = []
    expected = {
        Platform.PUMP_FUN: (PumpFunAddresses.PROGRAM, PUMP_AMM_PROGRAM),
        Platform.LAUNCHLAB: (LAUNCHLAB_PROGRAM, CPMM_PROGRAM),
        Platform.STONK_FUN: (LAUNCHLAB_PROGRAM, CPMM_PROGRAM),
    }
    for platform, (curve, pool) in expected.items():
        implementations = get_platform_implementations(platform, CLIENT)
        venues = (
            (implementations.curve_market, curve),
            (implementations.graduated_market, pool),
        )
        for market, program in venues:
            got = getattr(market, "program_id", None)
            if got != program:
                bad.append(f"{platform.value} {market.name}: program {got}")

        resolve = getattr(_manager(platform), "_venue_program", None)
        if resolve is None:
            bad.append("a pending buy's venue is never resolved to its program")
            continue
        for market, program in venues:
            if (got := resolve(market.name)) != program:
                bad.append(f"{platform.value} pending on {market.name}: {got}")
        for name in (None, "somewhere else"):
            if (got := resolve(name)) is not None:
                bad.append(f"{platform.value} pending on {name!r}: {got}")

    return _report("E", bad)


# --- F: naming never stops a revert --------------------------------------------


def check_f_naming_never_raises() -> bool:
    """F: a failure while naming leaves the revert reported, unnamed."""
    bad = []
    err = RECORDED[PUMPSWAP_SELL]["meta"]["err"]

    def broken(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("idl/pump_swap_idl.json")

    original = client_module.describe_program_error
    client_module.describe_program_error = broken
    try:
        revert: list[str] = []
        status = asyncio.run(
            _Client(RECORDED[PUMPSWAP_SELL]).verify_transaction_status(
                SIGNATURE, on_revert=revert.append
            )
        )
        runs = [("from the transaction", status, "".join(revert))]
        for where, client, program in (
            ("from a status", _Client(RECORDED[PUMPSWAP_SELL], status_err=err), None),
            ("from a status, unread", _Client(status_err=err), PUMP_AMM_PROGRAM),
        ):
            runs += [
                (f"{where}, {path}", status, text)
                for path, status, text in _status_reverts(client, program)
            ]
        for where, status, text in runs:
            if status is not ConfirmationStatus.REVERTED:
                bad.append(f"IDL broken, {where}: status {status}")
            if str(err) not in text or "IDL" in text:
                bad.append(f"IDL broken, {where}: {text!r}")
    finally:
        client_module.describe_program_error = original

    class _ReadRaises(_Client):
        async def _get_transaction_result(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("connection reset")  # noqa: TRY003

    for path, status, text in _status_reverts(
        _ReadRaises(status_err=err), PUMP_AMM_PROGRAM
    ):
        if status is not ConfirmationStatus.REVERTED:
            bad.append(f"read raises, {path}: status {status}")
        bad += _name_problems(
            f"read raises, {path}",
            text,
            "(PumpSwap IDL, if PumpSwap raised it: 6004 ExceededSlippage",
            None,
        )
    return _report("F", bad)


CHECKS = (
    check_a_named_from_the_transaction,
    check_b_the_raising_program,
    check_c_status_named_from_its_transaction,
    check_d_unread_named_from_the_venue,
    check_e_each_venue_names_its_program,
    check_f_naming_never_raises,
)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    passed = 0
    for check in CHECKS:
        print(check.__doc__.splitlines()[0])
        try:
            ok = check()
        except Exception as e:  # noqa: BLE001
            print(f"    raised {type(e).__name__}: {e}")
            ok = False
        print("  PASS" if ok else "  FAIL")
        passed += ok
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
