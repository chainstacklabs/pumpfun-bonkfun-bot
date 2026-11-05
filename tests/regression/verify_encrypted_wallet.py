"""Verify the bot's encrypted keystore: reading, writing and `pump_bot_wallet`.

A bot config can name a keystore with `keystore:` instead of giving a raw
`private_key`; the password comes from `KEYSTORE_PASSWORD`, else
`PUMPFUN_PASSWORD`. pumpfun-cli reads and writes the same file, so these checks
hold both directions to its format and keep a bad password from starting a bot:

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
  E. The decrypted key and the password, from either variable, are masked in
     later log lines.
  F. `KEYSTORE_PASSWORD` wins, `PUMPFUN_PASSWORD` is read when it is unset or
     empty, and both set to different values is refused in one line naming
     both variables and neither value. `start_bot` trades from an `env_file`
     naming only `PUMPFUN_PASSWORD`, and logs no password when it refuses.
  G. `write_keystore` writes a file that unlocks to its keypair, mode 600 in a
     new mode-700 folder, with the fixture's fields, field order, scrypt
     settings, lengths and layout, so pumpfun-cli reads it; nothing secret in
     clear, no overwrite unless asked, no empty password, no temporary file
     left behind.
  H. `pump_bot_wallet` (the pyproject console script, run as a subprocess):
     `create`, `import` from `--from-env`, `--keypair-file` and the prompt, and
     `show` print only the public key and write mode-600 files; the password
     is asked twice and a mismatch writes nothing; an existing file needs
     `--force`; differing password variables are refused; a private key given
     on the command line, in any position, is refused without being echoed;
     neither key nor password reaches stdout, stderr or the file.

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
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import base58  # noqa: E402
import yaml  # noqa: E402
from _fakes import _report  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

import bot_runner  # noqa: E402
from config_loader import load_bot_config, validate_config  # noqa: E402
from core.keystore import (  # noqa: E402
    FALLBACK_PASSWORD_ENV_VAR,
    PASSWORD_ENV_VAR,
    KeystoreError,
    keystore_private_key,
    load_keystore,
    password_from_env,
    write_keystore,
)
from core.wallet import Wallet  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "pumpfun_cli_keystore.json"
FIXTURE_PASSWORD = "regression-test-password"  # noqa: S105 - test vector
# The fixture was written by pumpfun-cli from this seed.
FIXTURE_KEYPAIR = Keypair.from_seed(bytes(range(32)))
FIXTURE_KEY = base58.b58encode(bytes(FIXTURE_KEYPAIR)).decode()
LOGS_CONFIG = PROJECT_ROOT / "bots" / "bot-sniper-2-logs.yaml"
# The console script pyproject.toml must register as pump_bot_wallet.
WALLET_SCRIPT = "bot_wallet:main"
WALLET_PASSWORD = "subprocess-pass"  # noqa: S105 - test vector
OWNER_ONLY = 0o600


def _logs_config() -> dict:
    """The committed logs config, endpoints filled in, no geyser block."""
    with LOGS_CONFIG.open() as f:
        config = yaml.safe_load(f)
    config.pop("geyser", None)
    config["rpc_endpoint"] = "http://127.0.0.1:8899"
    config["wss_endpoint"] = "ws://127.0.0.1:8900"
    return config


def _clear_passwords() -> None:
    os.environ.pop(PASSWORD_ENV_VAR, None)
    os.environ.pop(FALLBACK_PASSWORD_ENV_VAR, None)


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
                ["not a keystore file"],
            ),
            (
                "another KDF",
                write("kdf.enc", _other_kdf(_keystore(keypair, "secret-pass"))),
                "secret-pass",
                ["not a keystore file"],
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
                ["not a keystore file"],
            ),
            (
                "bad base64",
                write(
                    "b64.enc", _keystore(keypair, "secret-pass", encrypted_key="@@@")
                ),
                "secret-pass",
                ["not a keystore file"],
            ),
            (
                "non-ASCII base64",
                write(
                    "u64.enc", _keystore(keypair, "secret-pass", encrypted_key="ñ@@@")
                ),
                "secret-pass",
                ["not a keystore file"],
            ),
            (
                "a list, not an object",
                write("list.enc", [1, 2, 3]),
                "secret-pass",
                ["not a keystore file"],
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
    _clear_passwords()
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
        _clear_passwords()
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

    def reached(*_a: object, **_k: object) -> str:
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
    try:
        for variable in (PASSWORD_ENV_VAR, FALLBACK_PASSWORD_ENV_VAR):
            password = f"masked-{variable.lower()}"
            capture.records = []
            os.environ[variable] = password
            try:
                # check D ran start_bot, which registered the key it unlocked;
                # the password is read from the environment whenever redaction
                # reinstalls.
                bot_runner.install_secret_redaction()
                logger.info("key %s password %s", FIXTURE_KEY, password)
            finally:
                os.environ.pop(variable, None)
            text = capture.records[0].getMessage() if capture.records else ""
            if FIXTURE_KEY in text:
                problems.append("the decrypted key reached a log line")
            if password in text:
                problems.append(f"the password in {variable} reached a log line")
    finally:
        logger.removeHandler(capture)
    return _report(problems)


def _env_password(primary: str | None, fallback: str | None) -> object:
    """password_from_env's answer with these variables set, or its refusal."""
    _clear_passwords()
    for variable, value in (
        (PASSWORD_ENV_VAR, primary),
        (FALLBACK_PASSWORD_ENV_VAR, fallback),
    ):
        if value is not None:
            os.environ[variable] = value
    try:
        return password_from_env(Path("/w/wallet.enc"))
    except KeystoreError as e:
        return f"refused: {e}"
    finally:
        _clear_passwords()


def _precedence_problems() -> list[str]:
    problems = []
    for label, primary, fallback, want in (
        ("only the new variable", "new-pass", None, ("new-pass", PASSWORD_ENV_VAR)),
        (
            "only the old variable",
            None,
            "old-pass",
            ("old-pass", FALLBACK_PASSWORD_ENV_VAR),
        ),
        ("both, equal", "same-pass", "same-pass", ("same-pass", PASSWORD_ENV_VAR)),
        ("new one empty", "", "old-pass", ("old-pass", FALLBACK_PASSWORD_ENV_VAR)),
        ("neither", None, None, (None, PASSWORD_ENV_VAR)),
    ):
        got = _env_password(primary, fallback)
        if got != want:
            problems.append(f"{label}: {got!r}, want {want!r}")
    got = str(_env_password("new-pass", "old-pass"))
    if not got.startswith("refused: "):
        problems.append(f"both set and differing: not refused ({got!r})")
    problems += [
        f"conflict message {got!r} lacks {want!r}"
        for want in (PASSWORD_ENV_VAR, FALLBACK_PASSWORD_ENV_VAR, "/w/wallet.enc")
        if want not in got
    ]
    if "new-pass" in got or "old-pass" in got or "\n" in got:
        problems.append("conflict message holds a password or several lines")
    return problems


def _start_password_problems(folder: Path, config: dict) -> list[str]:
    problems = []
    # Existing setups name only the old variable.
    built, records = _start(
        folder, config, f"{FALLBACK_PASSWORD_ENV_VAR}={FIXTURE_PASSWORD}\n"
    )
    if len(built) != 1 or built[0].private_key != FIXTURE_KEY:
        problems.append("old variable alone: the bot did not trade from the keystore")
    for label, env, needles, secrets in (
        (
            "variables differ",
            f"{PASSWORD_ENV_VAR}={FIXTURE_PASSWORD}\n"
            f"{FALLBACK_PASSWORD_ENV_VAR}=not-it-either\n",
            [PASSWORD_ENV_VAR, FALLBACK_PASSWORD_ENV_VAR, "differ"],
            [FIXTURE_PASSWORD, "not-it-either"],
        ),
        (
            "old variable wrong",
            f"{FALLBACK_PASSWORD_ENV_VAR}=not-it-either\n",
            ["wrong password", FALLBACK_PASSWORD_ENV_VAR],
            ["not-it-either"],
        ),
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
            f"{label}: error {message!r} lacks {n!r}"
            for n in needles
            if n not in message
        ]
        if errors[0].exc_info:
            problems.append(f"{label}: error logged with a traceback")
        logged = "\n".join(r.getMessage() for r in records)
        problems += [f"{label}: a password was logged" for x in secrets if x in logged]
    return problems


def check_f_password_variables() -> bool:
    problems = _precedence_problems()
    base = _logs_config()
    base["name"] = "verify-keystore"
    config = {k: v for k, v in base.items() if k != "private_key"}
    config["keystore"] = str(FIXTURE)
    with tempfile.TemporaryDirectory() as tmp:
        problems += _start_password_problems(Path(tmp), config)
    return _report(problems)


def _layout(text: str) -> str:
    """A keystore's text with its per-wallet values blanked."""
    return re.sub(r'"(pubkey|salt|encrypted_key)": "[^"]*"', r'"\1": ""', text)


def _shape_problems(path: Path) -> list[str]:
    """How a written keystore differs from the CLI-written fixture's shape."""
    problems = []
    text, fixture_text = path.read_text(), FIXTURE.read_text()
    data, fixture = json.loads(text), json.loads(fixture_text)
    if list(data) != list(fixture) or list(data["kdf"]) != list(fixture["kdf"]):
        problems.append(f"fields {list(data)} / {list(data['kdf'])} differ")
    for field in ("algorithm", "n", "r", "p"):
        if data["kdf"].get(field) != fixture["kdf"][field]:
            problems.append(f"kdf {field} {data['kdf'].get(field)!r}")
    if data.get("version") != fixture["version"]:
        problems.append(f"version {data.get('version')!r}")
    for field, got, want in (
        ("salt", data["kdf"]["salt"], fixture["kdf"]["salt"]),
        ("encrypted_key", data["encrypted_key"], fixture["encrypted_key"]),
    ):
        if len(base64.b64decode(got)) != len(base64.b64decode(want)):
            problems.append(f"{field} is not the fixture's length")
    if _layout(text) != _layout(fixture_text):
        problems.append("layout (field order, indent, final newline) differs")
    return problems


def _clear_text_problems(path: Path, keypair: Keypair, password: str) -> list[str]:
    raw = path.read_bytes()
    text = raw.decode()
    secrets = (base58.b58encode(bytes(keypair)).decode(), password)
    problems = [f"{path.name} holds a secret in clear" for x in secrets if x in text]
    if bytes(keypair) in raw or bytes(keypair)[:32] in raw:
        problems.append(f"{path.name} holds the key bytes in clear")
    return problems


def check_g_written_keystore() -> bool:
    problems = []
    keypair, other = Keypair(), Keypair()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "new-folder"
        path = folder / "wallet.enc"
        write_keystore(path, keypair, "write-pass")
        if bytes(load_keystore(path, "write-pass")) != bytes(keypair):
            problems.append("a written keystore did not unlock to its keypair")
        for name, item, want in (("file", path, OWNER_ONLY), ("folder", folder, 0o700)):
            if item.stat().st_mode & 0o777 != want:
                problems.append(f"{name} mode {oct(item.stat().st_mode & 0o777)}")
        problems += _shape_problems(path)
        problems += _clear_text_problems(path, keypair, "write-pass")
        before = path.read_bytes()
        try:
            write_keystore(path, other, "write-pass")
            problems.append("an existing keystore was overwritten")
        except KeystoreError as e:
            if "already exists" not in str(e):
                problems.append(f"overwrite refusal: {e}")
        if path.read_bytes() != before:
            problems.append("a refused overwrite changed the file")
        write_keystore(path, other, "write-pass", overwrite=True)
        if bytes(load_keystore(path, "write-pass")) != bytes(other):
            problems.append("overwrite=True did not replace the keystore")
        try:
            write_keystore(folder / "empty.enc", keypair, "")
            problems.append("an empty password was accepted")
        except KeystoreError:
            pass
        left = sorted(p.name for p in folder.iterdir())
        if left != ["wallet.enc"]:
            problems.append(f"folder holds {left}")
    return _report(problems)


def _wallet_command(
    folder: Path, args: list[str], env: dict[str, str], stdin: str = ""
) -> tuple:
    """Run pump_bot_wallet as its console script would, in `folder`; (code, stdout, stderr)."""
    module, function = WALLET_SCRIPT.split(":")
    code = (
        f"import sys; sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r}); "
        f"from {module} import {function}; sys.exit({function}())"
    )
    clean = {
        k: v
        for k, v in os.environ.items()
        if k not in (PASSWORD_ENV_VAR, FALLBACK_PASSWORD_ENV_VAR)
    }
    # A new session has no terminal, so a prompt reads stdin instead of blocking.
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, *args],
        env={**clean, **env},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=180,
        start_new_session=True,
        cwd=folder,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


def _wallet_problems(
    label: str, run: tuple, pubkey: str, secrets: list[str], path: Path
) -> list[str]:
    """A successful run printed only `pubkey`, wrote a mode-600 file, leaked nothing."""
    code, out, err = run
    problems = []
    if code != 0:
        return [f"{label}: exit {code}: {err.strip()[-200:]}"]
    if out != f"{pubkey}\n":
        problems.append(f"{label}: stdout is not only the public key: {out!r}")
    if not path.exists() or path.stat().st_mode & 0o777 != OWNER_ONLY:
        problems.append(f"{label}: no mode-600 file")
    shown = out + err + (path.read_text() if path.exists() else "")
    problems += [
        f"{label}: a secret was printed or stored in clear"
        for x in secrets
        if x in shown
    ]
    return problems


def _wallet_write_problems(folder: Path) -> list[str]:
    problems = []
    password = WALLET_PASSWORD
    env = {PASSWORD_ENV_VAR: password}
    created = folder / "created.enc"
    code, out, err = run = _wallet_command(folder, ["create", str(created)], env)
    pubkey = out.strip()
    problems += _wallet_problems("create", run, pubkey, [password], created)
    if code == 0 and str(load_keystore(created, password).pubkey()) != pubkey:
        problems.append("create: printed key is not the stored one")
    before = created.read_bytes() if created.exists() else b""
    code, out, err = _wallet_command(folder, ["create", str(created)], env)
    if code == 0 or "--force" not in err or created.read_bytes() != before:
        problems.append(f"create over a file without --force: exit {code}, {err!r}")
    code, out, _ = run = _wallet_command(
        folder, ["create", str(created), "--force"], env
    )
    problems += _wallet_problems(
        "create --force", run, out.strip(), [password], created
    )
    if created.read_bytes() == before:
        problems.append("create --force left the file as it was")

    code, out, err = _wallet_command(folder, ["show", str(created)], env)
    if code != 0 or out != f"{load_keystore(created, password).pubkey()}\n":
        problems.append(f"show: exit {code}, stdout {out!r}")
    code, out, err = _wallet_command(
        folder, ["show", str(created)], {FALLBACK_PASSWORD_ENV_VAR: password}
    )
    if code != 0:
        problems.append(f"show with the old variable: exit {code}")
    code, out, err = _wallet_command(
        folder, ["show", str(created)], {**env, FALLBACK_PASSWORD_ENV_VAR: "other-pass"}
    )
    if code == 0 or FALLBACK_PASSWORD_ENV_VAR not in err or PASSWORD_ENV_VAR not in err:
        problems.append(f"show with differing variables: exit {code}, {err!r}")
    if password in out + err or "other-pass" in out + err:
        problems.append("show with differing variables printed a password")

    prompted = folder / "prompted.enc"
    code, out, err = run = _wallet_command(
        folder, ["create", str(prompted)], {}, "typed-pass\ntyped-pass\n"
    )
    problems += _wallet_problems(
        "create, password prompt", run, out.strip(), ["typed-pass"], prompted
    )
    if code == 0 and str(load_keystore(prompted, "typed-pass").pubkey()) != out.strip():
        problems.append("create, password prompt: not the typed password")
    mismatch = folder / "mismatch.enc"
    code, _, err = _wallet_command(
        folder, ["create", str(mismatch)], {}, "one-pass\ntwo-pass\n"
    )
    if code == 0 or mismatch.exists() or "one-pass" in err or "two-pass" in err:
        problems.append(f"create, passwords differ: exit {code}, {err!r}")
    return problems


def _wallet_import_problems(folder: Path) -> list[str]:
    problems = []
    password = WALLET_PASSWORD
    keypair = Keypair()
    key = base58.b58encode(bytes(keypair)).decode()
    as_json = json.dumps(list(bytes(keypair)))
    keypair_file = folder / "id.json"
    keypair_file.write_text(as_json)
    secrets = [
        password,
        key,
        as_json,
        json.dumps(list(bytes(keypair)), separators=(",", ":")),
    ]
    pubkey = str(keypair.pubkey())
    for label, args, env, stdin in (
        (
            "import --from-env",
            ["--from-env", "VERIFY_WALLET_KEY"],
            {"VERIFY_WALLET_KEY": key},
            "",
        ),
        ("import --keypair-file", ["--keypair-file", str(keypair_file)], {}, ""),
        ("import, key prompt", [], {}, key + "\n"),
    ):
        path = folder / f"{label.split()[-1].strip('-')}.enc"
        run = _wallet_command(
            folder,
            ["import", str(path), *args],
            {PASSWORD_ENV_VAR: password, **env},
            stdin,
        )
        problems += _wallet_problems(label, run, pubkey, secrets, path)
        if run[0] == 0 and bytes(load_keystore(path, password)) != bytes(keypair):
            problems.append(f"{label}: the stored key is not the one imported")

    refused = folder / "refused.enc"
    compact = json.dumps(list(bytes(keypair)), separators=(",", ":"))
    for args in (
        ["import", str(refused), key],
        ["import", key],
        ["import", str(refused), "--from-env", key],
        ["import", str(refused), f"--from-env={key}"],
        ["import", str(refused), compact],
        ["create", key],
    ):
        code, out, err = _wallet_command(folder, args, {PASSWORD_ENV_VAR: password})
        shape = " ".join(
            "<key>" if key in a or compact in a else a.split("/")[-1] for a in args
        )
        if code == 0 or refused.exists():
            problems.append(f"key on the command line accepted: {shape}")
        if key in out + err or compact in out + err:
            problems.append(f"key on the command line echoed back: {shape}")
    named = [p for p in folder.iterdir() if key in p.name or compact in p.name]
    if named:
        problems.append("a file was written under a name holding the key")
    return problems


def check_h_wallet_command() -> bool:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        scripts = tomllib.load(f)["project"]["scripts"]
    problems = []
    if scripts.get("pump_bot_wallet") != WALLET_SCRIPT:
        problems.append(
            f"pyproject pump_bot_wallet = {scripts.get('pump_bot_wallet')!r}"
        )
    with tempfile.TemporaryDirectory() as tmp:
        problems += _wallet_write_problems(Path(tmp))
        problems += _wallet_import_problems(Path(tmp))
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
        (
            "F: KEYSTORE_PASSWORD first, PUMPFUN_PASSWORD after",
            check_f_password_variables,
        ),
        ("G: write_keystore writes the CLI's format", check_g_written_keystore),
        ("H: pump_bot_wallet prints only the public key", check_h_wallet_command),
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
