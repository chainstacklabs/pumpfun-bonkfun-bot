"""The bot's encrypted keystore: one Solana keypair in a password-protected file.

A bot config names the file with `keystore:` instead of giving `private_key`,
and `pump_bot_wallet` creates one, imports a key into one or shows its public
key. A bot unlocks the file with `KEYSTORE_PASSWORD` and, when that is unset
or wrong, with `PUMPFUN_PASSWORD`: bots run in one process share one
environment, so each variable may hold another bot's password. Writing a new
file takes one password, so there two different values are refused.

The file is JSON, version 1:

    {
      "version": 1,
      "pubkey": "<base58 public key>",
      "encrypted_key": "<base64 of a 12-byte nonce, then the ciphertext>",
      "kdf": {"algorithm": "scrypt", "salt": "<base64, 32 bytes>",
              "n": 262144, "r": 8, "p": 1}
    }

The 64 keypair bytes are sealed with AES-256-GCM under a 32-byte key derived
from the UTF-8 password with scrypt. The reader takes the scrypt settings from
the file; the writer always uses the ones above. pumpfun-cli reads and writes
the same file, so pumpfun-cli and pumpclaw can trade from the bot's wallet and
the bot from theirs. Their reader ignores the file's scrypt settings and uses
these, so changing them would lock those tools out.
"""

import base64
import json
import os
import tempfile
from pathlib import Path

import base58
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

PASSWORD_ENV_VAR = "KEYSTORE_PASSWORD"  # noqa: S105 - a variable name
# Read when PASSWORD_ENV_VAR is unset; pumpfun-cli reads this one.
FALLBACK_PASSWORD_ENV_VAR = "PUMPFUN_PASSWORD"  # noqa: S105 - a variable name

_KEYSTORE_VERSION = 1
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_SALT_LENGTH = 32
# What every keystore is written with: 256 MiB of memory, one pass.
_SCRYPT_N = 2**18
_SCRYPT_R = 8
_SCRYPT_P = 1
# scrypt needs 128 * n * r bytes of memory, and p times that much work. A file
# asking for more than 1 GiB, or for more than that much work, is refused
# rather than allowed to exhaust RAM or hold up the start.
_MAX_SCRYPT_MEMORY = 1 << 30
_KEYPAIR_LENGTH = 64
_NEEDS_PASSWORD = (
    f"needs its password: set {PASSWORD_ENV_VAR} (or {FALLBACK_PASSWORD_ENV_VAR}) "
    "in the bot's env_file or the environment"
)
_NOT_A_PRIVATE_KEY = (
    "not a Solana private key: expected base58 of 64 bytes or a JSON array of "
    "64 numbers"
)


class KeystoreError(ValueError):
    """The keystore could not be read or written; the message says why.

    The message names the file and never holds the password or the key.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"keystore {path} {reason}")


class WrongPasswordError(KeystoreError):
    """The password did not decrypt the keystore."""


def resolve_keystore_path(value: str, config_dir: Path) -> Path:
    """Turn the `keystore` setting into a file path.

    `~` expands to the home folder. A relative path is taken from the bot
    config's folder when the file is there, otherwise from the working folder,
    the same lookup `env_file` uses.

    Args:
        value: The `keystore` value from the bot config
        config_dir: Folder holding the bot config file
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    beside_config = config_dir / path
    return beside_config if beside_config.exists() else path


def password_from_env(path: Path) -> tuple[str | None, str]:
    """Read the password for a new keystore from the environment.

    An empty variable counts as unset. Unlocking uses `unlock_from_env`, which
    tries both variables instead of refusing two values.

    Args:
        path: The keystore, named in the error

    Returns:
        The password, None when neither variable is set, and the name of the
        variable to blame for a wrong one

    Raises:
        KeystoreError: Both variables are set and differ.
    """
    primary = os.environ.get(PASSWORD_ENV_VAR)
    fallback = os.environ.get(FALLBACK_PASSWORD_ENV_VAR)
    if primary and fallback and primary != fallback:
        raise KeystoreError(
            path,
            f"has two passwords: {PASSWORD_ENV_VAR} and "
            f"{FALLBACK_PASSWORD_ENV_VAR} are both set and differ; unset one",
        )
    if primary:
        return primary, PASSWORD_ENV_VAR
    if fallback:
        return fallback, FALLBACK_PASSWORD_ENV_VAR
    return None, PASSWORD_ENV_VAR


def unlock_from_env(path: Path) -> tuple[Keypair, str]:
    """Decrypt a keystore with `KEYSTORE_PASSWORD`, else `PUMPFUN_PASSWORD`.

    The second variable is tried when the first is unset, empty or wrong.

    Returns:
        The keypair and the variable whose password unlocked it

    Raises:
        KeystoreError: Neither variable is set, or as `load_keystore`.
        WrongPasswordError: No variable's password decrypts the file.
    """
    tried: list[str] = []
    for variable in (PASSWORD_ENV_VAR, FALLBACK_PASSWORD_ENV_VAR):
        password = os.environ.get(variable)
        if not password or any(os.environ.get(v) == password for v in tried):
            continue
        try:
            return load_keystore(path, password, variable), variable
        except WrongPasswordError:
            tried.append(variable)
    if not tried:
        raise KeystoreError(path, _NEEDS_PASSWORD)
    raise WrongPasswordError(
        path, f"could not be decrypted: wrong password (from {' and '.join(tried)})"
    )


def keystore_pubkey(path: Path) -> str | None:
    """The public key a keystore names, read without its password.

    Returns:
        None when the file cannot be read or names none
    """
    try:
        pubkey = json.loads(Path(path).read_text(encoding="utf-8")).get("pubkey")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return None
    return pubkey if isinstance(pubkey, str) else None


def load_keystore(
    path: Path, password: str | None, source: str = PASSWORD_ENV_VAR
) -> Keypair:
    """Decrypt a keystore.

    Args:
        path: The keystore file
        password: None or empty when it is not set
        source: Where the password came from (a variable name, "the
            prompt"), named when it is wrong

    Raises:
        KeystoreError: The file is missing or not a keystore, the password is
            not set or wrong, or the decrypted key does not match the file's
            public key.
    """
    if not password:
        raise KeystoreError(path, _NEEDS_PASSWORD)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise KeystoreError(path, "not found") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise KeystoreError(path, f"could not be read: {e}") from None

    salt, n, r, p, nonce, ciphertext = _parse(data, path)
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        raise KeystoreError(
            path, f"could not be decrypted: the password (from {source}) is not UTF-8"
        ) from None
    key = _derive(encoded, salt, (n, r, p), path)
    try:
        secret = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise WrongPasswordError(
            path, f"could not be decrypted: wrong password (from {source})"
        ) from None

    try:
        keypair = Keypair.from_bytes(secret)
    except ValueError:
        raise KeystoreError(path, "does not hold a Solana keypair") from None
    expected = data.get("pubkey")
    if expected is not None and str(keypair.pubkey()) != expected:
        raise KeystoreError(
            path, f"decrypts to {keypair.pubkey()}, not the {expected} it names"
        )
    return keypair


def keystore_private_key(
    path: Path, password: str | None, source: str = PASSWORD_ENV_VAR
) -> str:
    """Decrypt a keystore into the base58 key `private_key` takes.

    Raises:
        KeystoreError: As `load_keystore`.
    """
    return base58.b58encode(bytes(load_keystore(path, password, source))).decode()


def write_keystore(
    path: Path, keypair: Keypair, password: str, *, overwrite: bool = False
) -> None:
    """Encrypt a keypair into a new keystore file, readable only by its owner.

    The file appears whole or not at all: it is written beside its final name,
    mode 600, then linked to it (renamed over it with `overwrite`). A missing
    folder is created, the last one mode 700.

    Args:
        password: Must be non-empty
        overwrite: Replace an existing file instead of refusing

    Raises:
        KeystoreError: The file exists and `overwrite` is off, the password is
            empty, or the file could not be written.
    """
    path = Path(path)
    if not password:
        raise KeystoreError(path, "needs a non-empty password")
    if path.exists() and not overwrite:
        raise KeystoreError(path, "already exists")
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        raise KeystoreError(path, "needs a password that is valid UTF-8") from None
    salt = os.urandom(_SALT_LENGTH)
    key = _derive(encoded, salt, (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P), path)
    nonce = os.urandom(_NONCE_LENGTH)
    sealed = AESGCM(key).encrypt(nonce, bytes(keypair), None)
    # Field order, indent and the missing final newline match pumpfun-cli's.
    text = json.dumps(
        {
            "version": _KEYSTORE_VERSION,
            "pubkey": str(keypair.pubkey()),
            "encrypted_key": base64.b64encode(nonce + sealed).decode(),
            "kdf": {
                "algorithm": "scrypt",
                "salt": base64.b64encode(salt).decode(),
                "n": _SCRYPT_N,
                "r": _SCRYPT_R,
                "p": _SCRYPT_P,
            },
        },
        indent=2,
    )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp creates the file mode 600.
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if overwrite:
                Path(temp).replace(path)
            else:
                _link_new(Path(temp), path)
        finally:
            Path(temp).unlink(missing_ok=True)
    except FileExistsError:
        raise KeystoreError(path, "already exists") from None
    except OSError as e:
        raise KeystoreError(path, f"could not be written: {e}") from None


def _link_new(temp: Path, path: Path) -> None:
    """Give `temp` the name `path` only if nothing has that name yet.

    Raises:
        FileExistsError: `path` exists.
    """
    try:
        # Fails if path exists, with no window between the check and the write.
        os.link(temp, path)
    except FileExistsError:
        raise
    except OSError:
        # A filesystem without hard links: check, then rename.
        if path.exists():
            raise FileExistsError(path) from None
        temp.replace(path)


def keypair_from_secret(text: str) -> Keypair:
    """Read a private key as base58 or as the Solana CLI's JSON byte array.

    Raises:
        ValueError: Neither form holds a 64-byte keypair. The message never
            quotes the input.
    """
    raw = _secret_bytes(text.strip())
    if raw is None or len(raw) != _KEYPAIR_LENGTH:
        raise ValueError(_NOT_A_PRIVATE_KEY)
    try:
        return Keypair.from_bytes(raw)
    except ValueError:
        # The second half is not the first half's public key.
        raise ValueError(_NOT_A_PRIVATE_KEY) from None


def _secret_bytes(text: str) -> bytes | None:
    """The bytes a private key's text spells, or None when it is malformed."""
    try:
        if text.startswith("["):
            values = json.loads(text)
            if not isinstance(values, list) or any(type(v) is not int for v in values):
                return None
            return bytes(values)  # ValueError outside 0..255
        return base58.b58decode(text)
    except (ValueError, RecursionError):
        # RecursionError: JSON nested deeper than the parser's stack.
        return None


def _derive(
    secret: bytes, salt: bytes, cost: tuple[int, int, int], path: Path
) -> bytes:
    """The AES key scrypt derives from the encoded password; cost is (n, r, p)."""
    n, r, p = cost
    try:
        return Scrypt(salt=salt, length=_KEY_LENGTH, n=n, r=r, p=p).derive(secret)
    except ValueError:
        # Settings scrypt itself rejects, such as an n that is not a power of 2.
        raise KeystoreError(path, "has invalid scrypt settings") from None


def _parse(data: object, path: Path) -> tuple[bytes, int, int, int, bytes, bytes]:
    """Pull the scrypt settings, nonce and ciphertext out of a keystore."""
    not_a_keystore = KeystoreError(
        path, f"is not a keystore file (version {_KEYSTORE_VERSION}, scrypt)"
    )
    try:
        kdf = data["kdf"]
        if data["version"] != _KEYSTORE_VERSION or kdf["algorithm"] != "scrypt":
            raise not_a_keystore
        n, r, p = kdf["n"], kdf["r"], kdf["p"]
        if not all(type(v) is int and v > 0 for v in (n, r, p)):
            raise not_a_keystore
        salt = base64.b64decode(kdf["salt"], validate=True)
        raw = base64.b64decode(data["encrypted_key"], validate=True)
    except (KeyError, TypeError, ValueError):
        # ValueError: base64 that is not valid (binascii.Error), or not ASCII.
        raise not_a_keystore from None
    if 128 * n * r > _MAX_SCRYPT_MEMORY:
        raise KeystoreError(path, "asks for more scrypt memory than a keystore needs")
    if 128 * n * r * p > _MAX_SCRYPT_MEMORY:
        raise KeystoreError(path, "asks for more scrypt work than a keystore needs")
    if len(raw) <= _NONCE_LENGTH:
        raise not_a_keystore
    return salt, n, r, p, raw[:_NONCE_LENGTH], raw[_NONCE_LENGTH:]
