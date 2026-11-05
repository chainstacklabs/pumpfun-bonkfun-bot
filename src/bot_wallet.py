"""`pump_bot_wallet`: create a keystore, import a key into one, or show its key.

    pump_bot_wallet create <path> [--force]
    pump_bot_wallet import <path> [--from-env VAR | --keypair-file FILE] [--force]
    pump_bot_wallet show <path>

Only the public key goes to stdout. The password is `KEYSTORE_PASSWORD`, else
`PUMPFUN_PASSWORD`, else a hidden prompt (asked twice before writing). A
private key is read from a hidden prompt, an environment variable or a keypair
file, never from the command line: an argument that holds one is refused
without being echoed. The file format lives in `core.keystore`.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import NoReturn

from solders.keypair import Keypair

from core.keystore import (
    KeystoreError,
    keypair_from_secret,
    load_keystore,
    password_from_env,
    write_keystore,
)

_PROMPT = "the prompt"


def _stop(message: str) -> NoReturn:
    """End the command with one line on stderr, exit status 1."""
    print(f"pump_bot_wallet: {message}", file=sys.stderr)
    raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
        command.add_argument("path", type=Path, help="the keystore file")
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


def _holds_private_key(arg: str) -> bool:
    try:
        keypair_from_secret(arg)
    except ValueError:
        return False
    return True


def _new_password(path: Path) -> str:
    password, _ = password_from_env(path)
    if password:
        return password
    password = getpass.getpass("Keystore password: ")
    if not password:
        _stop("the password is empty")
    if getpass.getpass("Repeat the password: ") != password:
        _stop("the passwords do not match")
    return password


def _private_key(args: argparse.Namespace) -> str:
    if args.from_env:
        value = os.environ.get(args.from_env)
        if not value:
            _stop(f"environment variable {args.from_env} is not set")
        return value
    if args.keypair_file:
        try:
            return args.keypair_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            _stop(f"keypair file could not be read: {e.strerror or e}")
    return getpass.getpass("Private key (base58 or JSON byte array): ")


def _write(args: argparse.Namespace) -> None:
    if args.path.exists() and not args.force:
        _stop(f"{args.path} already exists: pass --force to replace it")
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
    password, source = password_from_env(args.path)
    if not password:
        password, source = getpass.getpass("Keystore password: "), _PROMPT
    print(load_keystore(args.path, password, source).pubkey())


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; 0 on success, 1 when refused, 2 on bad usage."""
    argv = sys.argv[1:] if argv is None else argv
    # `--from-env=<key>` too, not only `<key>` on its own.
    if any(_holds_private_key(part) for arg in argv for part in arg.split("=", 1)):
        print(
            "pump_bot_wallet: refused: an argument holds a private key. Give the "
            "key at the prompt, with --from-env VAR or with --keypair-file FILE.",
            file=sys.stderr,
        )
        return 2
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
