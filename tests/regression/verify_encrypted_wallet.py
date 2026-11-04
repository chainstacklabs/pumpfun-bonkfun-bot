"""Verify the bot trades from pumpfun-cli's encrypted wallet file.

A bot config can name the CLI's keystore with `keystore:` instead of giving a
raw `private_key`; the password comes from `PUMPFUN_PASSWORD`. These checks
hold that path to the CLI's file format and keep a bad password from starting
a bot:

  A. A keystore written by pumpfun-cli's own `encrypt_keypair`
     (`pumpfun_cli_keystore.json`) unlocks to the keypair it was made from, and
     the bot's `Wallet` signs as that key.
  B. Every way unlocking can fail raises `KeystoreError` with a message naming
     the cause: no password, a wrong one, a missing or unreadable file, a file
     that is not a version-1 scrypt keystore, a key that does not match the
     file's public key, scrypt settings asking for more than 1 GiB of memory
     or of work, and settings scrypt itself rejects.
  C. Config: `keystore` stands in for `private_key`; neither is refused as a
     missing `private_key`, both together are refused, a non-path `keystore`
     is refused. `~` expands, a relative path is found beside the config, and
     `${VAR}` interpolation applies.
  D. `start_bot` unlocks the keystore with the password from the bot's
     `env_file` and hands the trader that key. A wrong or missing password
     logs one error naming the cause, with no traceback, and starts no trader.
     A `private_key` config never touches the keystore code.
  E. The decrypted key and the password are masked in later log lines.

Offline: the trader is a recorder and the node always answers; config loading
and `start_bot` are real.

Usage:
    uv run tests/regression/verify_encrypted_wallet.py
"""

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import yaml  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import bot_runner  # noqa: E402
from config_loader import load_bot_config, validate_config  # noqa: E402
from core.keystore import (  # noqa: E402
    PASSWORD_ENV_VAR,
    KeystoreError,
    keystore_private_key,
    load_keystore,
)
from core.wallet import Wallet  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "pumpfun_cli_keystore.json"
FIXTURE_PASSWORD = "regression-test-password"  # noqa: S105 - test vector
# The fixture was written by pumpfun-cli from this seed.
FIXTURE_KEYPAIR = Keypair.from_seed(bytes(range(32)))
FIXTURE_KEY = base58.b58encode(bytes(FIXTURE_KEYPAIR)).decode()
LOGS_CONFIG = PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml"


def _logs_config() -> dict:
    """The committed logs config, endpoints filled in, no geyser block."""
    with LOGS_CONFIG.open() as f:
        config = yaml.safe_load(f)
    config.pop("geyser", None)
    config["rpc_endpoint"] = "http://127.0.0.1:8899"
    config["wss_endpoint"] = "ws://127.0.0.1:8900"
    return config


def _report(problems: list[str]) -> bool:
    for problem in problems:
        print(f"    {problem}")
    return not problems


def _keystore(
    keypair: Keypair, password: str, n: int = 2**10, **overrides: object
) -> dict:
    """A keystore in the CLI's format, with a cheap scrypt cost for speed."""
    salt = os.urandom(32)
    key = Scrypt(salt=salt, length=32, n=n, r=8, p=1).derive(password.encode())
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, bytes(keypair), None)
    data = {
        "version": 1,
        "pubkey": str(keypair.pubkey()),
        "encrypted_key": base64.b64encode(nonce + sealed).decode(),
        "kdf": {
            "algorithm": "scrypt",
            "salt": base64.b64encode(salt).decode(),
            "n": n,
            "r": 8,
            "p": 1,
        },
    }
    data.update(overrides)
    return data


def _other_kdf(data: dict) -> dict:
    """The same keystore, its KDF named as something other than scrypt."""
    return {**data, "kdf": {**data["kdf"], "algorithm": "pbkdf2"}}


def _refusal(path: Path, password: str | None) -> str | None:
    """The KeystoreError message, or None when the keystore unlocked."""
    try:
        load_keystore(path, password)
    except KeystoreError as e:
        return str(e)
    return None


def check_a_cli_keystore_unlocks() -> bool:
    problems = []
    keypair = load_keystore(FIXTURE, FIXTURE_PASSWORD)
    if bytes(keypair) != bytes(FIXTURE_KEYPAIR):
        problems.append(f"fixture unlocked to {keypair.pubkey()}")
    key = keystore_private_key(FIXTURE, FIXTURE_PASSWORD)
    if key != FIXTURE_KEY:
        problems.append("keystore_private_key is not the base58 keypair")
    wallet = Wallet(key)
    if wallet.pubkey != FIXTURE_KEYPAIR.pubkey():
        problems.append(f"Wallet signs as {wallet.pubkey}")
    return _report(problems)


def check_b_refusals_name_the_cause() -> bool:
    problems = []
    keypair, other = Keypair(), Keypair()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)

        def write(name: str, data: object) -> Path:
            path = folder / name
            text = data if isinstance(data, str) else json.dumps(data)
            path.write_text(text)
            return path

        good = write("good.enc", _keystore(keypair, "secret-pass"))
        if _refusal(good, "secret-pass") is not None:
            problems.append(
                f"a valid keystore refused: {_refusal(good, 'secret-pass')}"
            )
        tampered = _keystore(keypair, "secret-pass")
        raw = bytearray(base64.b64decode(tampered["encrypted_key"]))
        raw[-1] ^= 1
        tampered["encrypted_key"] = base64.b64encode(bytes(raw)).decode()
        cases = [
            ("no password", good, None, [PASSWORD_ENV_VAR, "needs its password"]),
            ("empty password", good, "", [PASSWORD_ENV_VAR, "needs its password"]),
            ("wrong password", good, "not-it", ["wrong password", PASSWORD_ENV_VAR]),
            (
                "fixture, wrong password",
                FIXTURE,
                "not-it",
                ["wrong password", PASSWORD_ENV_VAR],
            ),
            (
                "tampered ciphertext",
                write("t.enc", tampered),
                "secret-pass",
                ["wrong password"],
            ),
            ("missing file", folder / "absent.enc", "secret-pass", ["not found"]),
            (
                "not JSON",
                write("x.enc", "not json"),
                "secret-pass",
                ["could not be read"],
            ),
            (
                "version 2",
                write("v2.enc", _keystore(keypair, "secret-pass", version=2)),
                "secret-pass",
                ["not a pumpfun-cli wallet"],
            ),
            (
                "another KDF",
                write("kdf.enc", _other_kdf(_keystore(keypair, "secret-pass"))),
                "secret-pass",
                ["not a pumpfun-cli wallet"],
            ),
            (
                "no ciphertext",
                write(
                    "nokey.enc",
                    {
                        k: v
                        for k, v in _keystore(keypair, "secret-pass").items()
                        if k != "encrypted_key"
                    },
                ),
                "secret-pass",
                ["not a pumpfun-cli wallet"],
            ),
            (
                "bad base64",
                write(
                    "b64.enc", _keystore(keypair, "secret-pass", encrypted_key="@@@")
                ),
                "secret-pass",
                ["not a pumpfun-cli wallet"],
            ),
            (
                "a list, not an object",
                write("list.enc", [1, 2, 3]),
                "secret-pass",
                ["not a pumpfun-cli wallet"],
            ),
            (
                "pubkey of another wallet",
                write(
                    "pk.enc",
                    _keystore(keypair, "secret-pass", pubkey=str(other.pubkey())),
                ),
                "secret-pass",
                ["decrypts to", str(other.pubkey())],
            ),
            (
                "scrypt over 1 GiB",
                write("big.enc", _keystore(keypair, "secret-pass", n=2**10)),
                "secret-pass",
                ["more scrypt memory"],
            ),
            (
                "scrypt work over 1 GiB",
                write("slow.enc", _keystore(keypair, "secret-pass")),
                "secret-pass",
                ["more scrypt work"],
            ),
            (
                "scrypt n not a power of 2",
                write("n3.enc", _keystore(keypair, "secret-pass")),
                "secret-pass",
                ["invalid scrypt settings"],
            ),
        ]
        # _keystore derives with the settings it writes, so each is set after.
        for name, setting, value in (
            ("big.enc", "n", 2**21),  # 128 * 2^21 * 8 = 2 GiB
            ("slow.enc", "p", 2**11),  # 1 MiB of memory, 2048 times over
            ("n3.enc", "n", 3),
        ):
            data = json.loads((folder / name).read_text())
            data["kdf"][setting] = value
            (folder / name).write_text(json.dumps(data))
        for label, path, password, needles in cases:
            try:
                message = _refusal(path, password)
            except Exception as e:  # noqa: BLE001 - any other type is the bug
                problems.append(f"{label}: raised {type(e).__name__}: {e}")
                continue
            if message is None:
                problems.append(f"{label}: unlocked")
                continue
            missing = [n for n in needles if n not in message]
            if missing:
                problems.append(f"{label}: message {message!r} lacks {missing}")
            if str(path) not in message:
                problems.append(f"{label}: message does not name the file")
    return _report(problems)


def _config_refusal(config: dict) -> str | None:
    """The validation error for a config, or None when it validates."""
    try:
        validate_config(config)
    except ValueError as e:
        return str(e)
    return None


def _validation_problems(base: dict, no_key: dict) -> list[str]:
    problems = []
    if (e := _config_refusal(base)) is not None:
        problems.append(f"private_key config refused: {e}")
    if (e := _config_refusal({**no_key, "keystore": "/w/wallet.enc"})) is not None:
        problems.append(f"keystore config refused: {e}")
    e = _config_refusal(no_key)
    if e != "Missing required config key: private_key":
        problems.append(f"neither key: {e!r}")
    e = _config_refusal({**base, "keystore": "/w/wallet.enc"})
    if not e or "private_key" not in e or "keystore" not in e:
        problems.append(f"both keys: {e!r}")
    for bad in ("", "  ", 5, None, ["/w"]):
        e = _config_refusal({**no_key, "keystore": bad})
        if not e or "keystore" not in e:
            problems.append(f"keystore {bad!r}: {e!r}")
    return problems


def _path_problems(no_key: dict) -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "wallet.enc").write_text("{}")
        (folder / "bot.env").write_text("VERIFY_KEYSTORE_PATH=/from/env/wallet.enc\n")
        want = {
            "wallet.enc": str(folder / "wallet.enc"),
            "~/.config/pumpfun-cli/wallet.enc": str(
                Path.home() / ".config/pumpfun-cli/wallet.enc"
            ),
            "/abs/wallet.enc": "/abs/wallet.enc",
            "${VERIFY_KEYSTORE_PATH}": "/from/env/wallet.enc",
        }
        for keystore, expected in want.items():
            config = {**no_key, "keystore": keystore, "env_file": "bot.env"}
            path = folder / "bot.yaml"
            path.write_text(yaml.safe_dump(config))
            try:
                got = load_bot_config(str(path))["keystore"]
            except ValueError as e:
                got = f"refused: {e}"
            finally:
                os.environ.pop("VERIFY_KEYSTORE_PATH", None)
            if got != expected:
                problems.append(f"keystore {keystore!r} resolved to {got!r}")
    return problems


def check_c_config() -> bool:
    base = _logs_config()
    base["private_key"] = FIXTURE_KEY
    no_key = {k: v for k, v in base.items() if k != "private_key"}
    return _report(_validation_problems(base, no_key) + _path_problems(no_key))


class _Trader:
    """Stands in for UniversalTrader: records the settings it was given."""

    built: list = []  # noqa: RUF012
    stopped_on_error = False

    def __init__(self, settings: object) -> None:
        _Trader.built.append(settings)

    async def start(self) -> None:
        return None


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _start(folder: Path, config: dict, env: str) -> tuple[list, list]:
    """Run the real start_bot on one config; return trader settings and records."""
    os.environ.pop(PASSWORD_ENV_VAR, None)
    (folder / "bot.env").write_text(env)
    path = folder / "bot.yaml"
    path.write_text(yaml.safe_dump({**config, "env_file": "bot.env"}))
    _Trader.built = []
    capture = _Capture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.INFO)
    try:
        asyncio.run(bot_runner.start_bot(str(path)))
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)
        os.environ.pop(PASSWORD_ENV_VAR, None)
    return _Trader.built, capture.records


def _errors(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.levelno >= logging.ERROR]


class _KeystoreReached(Exception):  # noqa: N818 - a signal, not an error
    """Raised by the stand-in keystore code a private_key config must not reach."""


def _refused_start_problems(folder: Path, config: dict) -> list[str]:
    problems = []
    for label, env, needle in (
        ("wrong password", f"{PASSWORD_ENV_VAR}=not-it\n", "wrong password"),
        ("no password", "", PASSWORD_ENV_VAR),
    ):
        built, records = _start(folder, config, env)
        errors = _errors(records)
        if built:
            problems.append(f"{label}: a trader was built")
        if len(errors) != 1:
            problems.append(f"{label}: {len(errors)} error lines")
            continue
        message = errors[0].getMessage()
        problems += [
            f"{label}: error {message!r} lacks {want!r}"
            for want in ("not started", needle, str(FIXTURE))
            if want not in message
        ]
        if errors[0].exc_info:
            problems.append(f"{label}: error logged with a traceback")
    return problems


def _private_key_start_problems(folder: Path, base: dict) -> list[str]:
    raw = base58.b58encode(bytes(Keypair())).decode()
    original = bot_runner.keystore_private_key

    def reached(*_a: object) -> str:
        raise _KeystoreReached

    bot_runner.keystore_private_key = reached
    try:
        built, _ = _start(folder, {**base, "private_key": raw}, "")
    except _KeystoreReached:
        return ["private_key config: the keystore code was reached"]
    finally:
        bot_runner.keystore_private_key = original
    if len(built) != 1 or built[0].private_key != raw:
        return ["private_key config: the trader did not get the raw key"]
    return []


def check_d_start_bot() -> bool:
    problems = []
    base = _logs_config()
    base["name"] = "verify-keystore"
    keystore_config = {k: v for k, v in base.items() if k != "private_key"}
    keystore_config["keystore"] = str(FIXTURE)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        built, records = _start(
            folder, keystore_config, f"{PASSWORD_ENV_VAR}={FIXTURE_PASSWORD}\n"
        )
        if len(built) != 1:
            problems.append(f"right password: {len(built)} traders built")
        elif built[0].private_key != FIXTURE_KEY:
            problems.append("right password: the trader got another key")
        if _errors(records):
            problems.append(f"right password logged {_errors(records)[0].getMessage()}")
        problems += _refused_start_problems(folder, keystore_config)
        problems += _private_key_start_problems(folder, base)
    return _report(problems)


def check_e_secrets_masked() -> bool:
    problems = []
    capture = _Capture()
    logger = logging.getLogger("verify.encrypted_wallet")
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    os.environ[PASSWORD_ENV_VAR] = FIXTURE_PASSWORD
    try:
        # check D ran start_bot, which registered the key it unlocked; the
        # password is read from the environment whenever redaction reinstalls.
        bot_runner.install_secret_redaction()
        logger.info("key %s password %s", FIXTURE_KEY, FIXTURE_PASSWORD)
    finally:
        os.environ.pop(PASSWORD_ENV_VAR, None)
        logger.removeHandler(capture)
    text = capture.records[0].getMessage() if capture.records else ""
    if FIXTURE_KEY in text:
        problems.append("the decrypted key reached a log line")
    if FIXTURE_PASSWORD in text:
        problems.append("the keystore password reached a log line")
    return _report(problems)


async def _node_answers(_endpoint: str) -> None:
    return None


def main() -> int:
    """Run every check and report."""
    bot_runner.UniversalTrader = _Trader
    bot_runner.setup_logging = lambda _name: None
    bot_runner.rpc_endpoint_problem = _node_answers
    checks = [
        ("A: a pumpfun-cli keystore unlocks", check_a_cli_keystore_unlocks),
        ("B: every refusal names its cause", check_b_refusals_name_the_cause),
        ("C: keystore config validates and resolves", check_c_config),
        ("D: start_bot unlocks or refuses to start", check_d_start_bot),
        ("E: the key and password are masked in logs", check_e_secrets_masked),
    ]
    passed = 0
    for label, check in checks:
        try:
            ok = check()
        except Exception as e:  # noqa: BLE001 - a crash is a failed check
            print(f"    raised {type(e).__name__}: {e}")
            ok = False
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
