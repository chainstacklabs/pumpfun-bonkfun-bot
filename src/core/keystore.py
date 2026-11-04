"""Unlock the encrypted wallet file pumpfun-cli writes.

`pumpfun wallet create` and `pumpfun wallet import` store the keypair as JSON:
the 64 keypair bytes encrypted with AES-256-GCM (a 12-byte nonce in front of the
ciphertext, base64), under a key derived from the password with scrypt, whose
salt and cost settings are stored beside it. A bot config names that file with
`keystore:` instead of giving `private_key`, and the password comes from
`PUMPFUN_PASSWORD`, the variable the CLI reads too.
"""

import base64
import binascii
import json
import os
from pathlib import Path

import base58
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

PASSWORD_ENV_VAR = "PUMPFUN_PASSWORD"  # noqa: S105 - a variable name

# Where pumpfun-cli keeps its wallet unless told otherwise (on Linux and macOS).
CLI_DEFAULT_KEYSTORE = "~/.config/pumpfun-cli/wallet.enc"

_KEYSTORE_VERSION = 1
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
# scrypt needs 128 * n * r bytes of memory, and p times that much work. The
# CLI's settings take 256 MiB with p = 1; a file asking for more than 1 GiB, or
# for more than that much work, is refused rather than allowed to exhaust RAM or
# hold up the start.
_MAX_SCRYPT_MEMORY = 1 << 30


class KeystoreError(ValueError):
    """The keystore could not be unlocked; the message says why and what to fix."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"keystore {path} {reason}")


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


def load_keystore(path: Path, password: str | None) -> Keypair:
    """Decrypt a pumpfun-cli wallet file.

    Args:
        path: The wallet file
        password: The wallet password; None or empty when it is not set

    Returns:
        The wallet's keypair

    Raises:
        KeystoreError: The file is missing or not a pumpfun-cli wallet, the
            password is not set or wrong, or the decrypted key does not match
            the file's public key.
    """
    if not password:
        raise KeystoreError(
            path,
            f"needs its password: set {PASSWORD_ENV_VAR} in the bot's env_file or "
            "the environment",
        )
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise KeystoreError(path, "not found") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise KeystoreError(path, f"could not be read: {e}") from None

    salt, n, r, p, nonce, ciphertext = _parse(data, path)
    try:
        secret_bytes = password.encode("utf-8")
    except UnicodeEncodeError:
        raise KeystoreError(
            path, f"could not be decrypted: {PASSWORD_ENV_VAR} is not valid UTF-8"
        ) from None
    try:
        key = Scrypt(salt=salt, length=_KEY_LENGTH, n=n, r=r, p=p).derive(secret_bytes)
    except ValueError:
        # Settings scrypt itself rejects, such as an n that is not a power of 2.
        raise KeystoreError(path, "has invalid scrypt settings") from None
    try:
        secret = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise KeystoreError(
            path, f"could not be decrypted: wrong password in {PASSWORD_ENV_VAR}"
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


def keystore_private_key(path: Path, password: str | None) -> str:
    """Decrypt a pumpfun-cli wallet file into the base58 key `private_key` takes.

    Args:
        path: The wallet file
        password: The wallet password; None or empty when it is not set

    Raises:
        KeystoreError: As `load_keystore`.
    """
    return base58.b58encode(bytes(load_keystore(path, password))).decode()


def password_from_env() -> str | None:
    """Read the keystore password from the environment."""
    return os.environ.get(PASSWORD_ENV_VAR)


def _parse(data: object, path: Path) -> tuple[bytes, int, int, int, bytes, bytes]:
    """Pull the scrypt settings, nonce and ciphertext out of a wallet file."""
    not_a_wallet = KeystoreError(
        path, f"is not a pumpfun-cli wallet file (version {_KEYSTORE_VERSION}, scrypt)"
    )
    try:
        kdf = data["kdf"]
        if data["version"] != _KEYSTORE_VERSION or kdf["algorithm"] != "scrypt":
            raise not_a_wallet
        n, r, p = kdf["n"], kdf["r"], kdf["p"]
        if not all(type(v) is int and v > 0 for v in (n, r, p)):
            raise not_a_wallet
        salt = base64.b64decode(kdf["salt"], validate=True)
        raw = base64.b64decode(data["encrypted_key"], validate=True)
    except (KeyError, TypeError, binascii.Error):
        raise not_a_wallet from None
    if 128 * n * r > _MAX_SCRYPT_MEMORY:
        raise KeystoreError(path, "asks for more scrypt memory than a wallet needs")
    if 128 * n * r * p > _MAX_SCRYPT_MEMORY:
        raise KeystoreError(path, "asks for more scrypt work than a wallet needs")
    if len(raw) <= _NONCE_LENGTH:
        raise not_a_wallet
    return salt, n, r, p, raw[:_NONCE_LENGTH], raw[_NONCE_LENGTH:]
