"""`pump_bot_wallet`: create a keystore, import a key into one, or show its key.

    pump_bot_wallet create <path> [--force]
    pump_bot_wallet import <path> [--from-env VAR | --keypair-file FILE] [--force]
    pump_bot_wallet show <path>

Only the public key goes to stdout; notes such as which variable gave the
password go to stderr. A relative path is taken from the current folder.

Password: `create` and `import` take `KEYSTORE_PASSWORD`, else
`PUMPFUN_PASSWORD`, else a hidden prompt asked twice, and refuse the two
variables holding different values, since a new file has one password. `show`
tries both variables like the bot does, else prompts.

A private key is read from a hidden prompt, an environment variable or a
keypair file, never from the command line: an argument shaped like key material
is refused, and no error repeats what was typed. The file format lives in
`core.keystore`.
"""

import argparse
import getpass
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

from solders.keypair import Keypair

from core.keystore import (
    FALLBACK_PASSWORD_ENV_VAR,
    PASSWORD_ENV_VAR,
    KeystoreError,
    keypair_from_secret,
    keystore_pubkey,
    load_keystore,
    password_from_env,
    unlock_from_env,
    write_keystore,
)

_PROMPT = "the prompt"
_COMMANDS = ("create", "import", "show")
# A Solana CLI keypair file is a few hundred bytes.
_MAX_KEYPAIR_FILE = 4096
_SHORT_PASSWORD = 8
# Typed values this short are not hidden in usage errors: they would match
# inside the message's own words, and no secret is that short.
_MIN_HIDDEN = 4
# What is stripped before an argument is judged: quoting and list punctuation.
_PUNCTUATION = re.compile(r"[\s\"'`\[\](){},;]")
# Digits with list punctuation only: a JSON byte array, or one piece of it.
_BYTE_LIST = re.compile(r"[\s\"'\[\],;\d]+")
# A base58 or hex key or seed is at least 32 characters; a mistyped one may
# hold any letter or digit.
_KEY_LIKE_LENGTH = 32


def _stop(message: str) -> NoReturn:
    """End the command with one line on stderr, exit status 1."""
    print(f"pump_bot_wallet: {message}", file=sys.stderr)
    raise SystemExit(1)


def _note(message: str) -> None:
    print(f"pump_bot_wallet: {message}", file=sys.stderr)


class _Parser(argparse.ArgumentParser):
    """An argument parser whose errors never repeat a value that was typed."""

    typed: tuple[str, ...] = ()

    def error(self, message: str) -> NoReturn:
        message = re.sub(
            r"(unrecognized arguments:).*", r"\1 (not shown)", message, flags=re.S
        )
        message = re.sub(
            r"(invalid choice:).*?(\(choose from)", r"\1 (not shown) \2", message
        )
        for arg in self.typed:
            if len(arg) >= _MIN_HIDDEN and arg not in _COMMANDS:
                if not re.fullmatch(r"--?[a-z-]+", arg):
                    message = message.replace(arg, "<not shown>")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="pump_bot_wallet",
        description="Create, import or show the bot's encrypted keystore.",
        epilog="Password: KEYSTORE_PASSWORD, else PUMPFUN_PASSWORD, else a hidden "
        "prompt. The private key is never taken on the command line.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="make a new keypair and store it")
    imp = commands.add_parser("import", help="store an existing private key")
    show = commands.add_parser("show", help="print the public key after unlocking")
    for command in (create, imp, show):
        command.add_argument(
            "path", type=Path, help="the keystore file, relative to this folder"
        )
    for command in (create, imp):
        command.add_argument(
            "--force", action="store_true", help="replace an existing file"
        )
    source = imp.add_mutually_exclusive_group()
    source.add_argument(
        "--from-env",
        metavar="VAR",
        help="read the key (base58 or JSON byte array) from this variable",
    )
    source.add_argument(
        "--keypair-file",
        metavar="FILE",
        type=Path,
        help="read the key from a Solana CLI keypair file (JSON byte array)",
    )
    return parser


def _key_shaped(text: str) -> bool:
    """Whether an argument looks like key material: a key, seed or byte list."""
    if re.search(r"\d", text) and re.search(r"[\[\],]", text):
        if _BYTE_LIST.fullmatch(text):
            return True
    core = _PUNCTUATION.sub("", text)
    return len(core) >= _KEY_LIKE_LENGTH and core.isascii() and core.isalnum()


def _new_password(path: Path) -> str:
    password, variable = password_from_env(path)
    if password:
        _note(f"password from {variable}")
    else:
        password = getpass.getpass("Keystore password: ")
        if not password:
            _stop("the password is empty")
        if getpass.getpass("Repeat the password: ") != password:
            _stop("the passwords do not match")
    if len(password) < _SHORT_PASSWORD:
        _note(f"warning: the password is shorter than {_SHORT_PASSWORD} characters")
    return password


def _read_keypair_file(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as f:
            text = f.read(_MAX_KEYPAIR_FILE + 1)
    except (OSError, UnicodeDecodeError) as e:
        _stop(f"keypair file could not be read: {getattr(e, 'strerror', None) or e}")
    if len(text) > _MAX_KEYPAIR_FILE:
        _stop(f"keypair file is over {_MAX_KEYPAIR_FILE} bytes: not a keypair file")
    return text


def _private_key(args: argparse.Namespace) -> str:
    if args.from_env:
        value = os.environ.get(args.from_env)
        if not value:
            _stop(f"environment variable {args.from_env} is not set")
        return value
    if args.keypair_file:
        return _read_keypair_file(args.keypair_file)
    return getpass.getpass("Private key (base58 or JSON byte array): ")


def _write(args: argparse.Namespace) -> None:
    if args.path.exists():
        # The public key is stored in clear; no password needed to name it.
        holding = keystore_pubkey(args.path) or "a key that cannot be read"
        if not args.force:
            _stop(f"{args.path} already exists ({holding}): pass --force to replace it")
        _note(f"replacing {args.path} ({holding})")
    if args.command == "create":
        keypair = Keypair()
    else:
        source = "the key entered"
        if args.from_env:
            source = args.from_env
        elif args.keypair_file:
            source = "the keypair file"
        try:
            keypair = keypair_from_secret(_private_key(args))
        except ValueError as e:
            _stop(f"{source} is {e}")
    write_keystore(args.path, keypair, _new_password(args.path), overwrite=args.force)
    print(keypair.pubkey())


def _show(args: argparse.Namespace) -> None:
    if os.environ.get(PASSWORD_ENV_VAR) or os.environ.get(FALLBACK_PASSWORD_ENV_VAR):
        keypair, variable = unlock_from_env(args.path)
        _note(f"password from {variable}")
    else:
        password = getpass.getpass("Keystore password: ")
        keypair = load_keystore(args.path, password, _PROMPT)
    print(keypair.pubkey())


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; 0 on success, 1 when refused, 2 on bad usage."""
    argv = sys.argv[1:] if argv is None else argv
    # `--from-env=<key>` too, not only `<key>` on its own.
    if any(_key_shaped(part) for arg in argv for part in arg.split("=", 1)):
        print(
            "pump_bot_wallet: refused: an argument looks like a private key or "
            "seed. Give the key at the prompt, with --from-env VAR or with "
            "--keypair-file FILE.",
            file=sys.stderr,
        )
        return 2
    _Parser.typed = tuple(argv)
    args = _parser().parse_args(argv)
    try:
        if args.command == "show":
            _show(args)
        else:
            _write(args)
    except KeystoreError as e:
        print(f"pump_bot_wallet: {e}", file=sys.stderr)
        return 1
    except EOFError:
        print("pump_bot_wallet: no input to read from", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
