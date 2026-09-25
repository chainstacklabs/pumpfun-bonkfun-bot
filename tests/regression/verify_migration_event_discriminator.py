"""Verify the migration decoder rejects payloads that are not its event.

`parse_migrate_instruction` skipped the first 8 bytes as a discriminator but never
checked them, so every `Program data:` line in a migration transaction was decoded
against the migration schema whatever event it actually was. The schema needs 301
bytes; any payload at least that long came back as a fully populated dict of
meaningless values rather than as a failure — a `baseMintDecimals` of 176 where the
field can only legitimately be 0-9. Shorter payloads happened to fail inside
`struct.unpack`, which is why the underrun was never the visible symptom.

Silently wrong output is worse than a crash here: the values look structurally
plausible, so anything reading the example inherits bad data with no signal.

The event is the wrapper program's `CreatePoolEvent`, not the
`CompletePumpAmmMigrationEvent` in `idl/pump_fun_idl.json`, which is why the layout
is hand-rolled and the discriminator has to be asserted rather than inherited from
an IDL parser.

Offline machine checks, no network and no funds moved. The fixture is a real
344-byte payload captured from the wrapper program on mainnet, slot 450319072:

  1. The real payload still decodes, and its decimals and pool bump are in range.
  2. The same payload with a foreign discriminator is rejected — same length, same
     body, so only the discriminator can be doing the work.
  3. A 344-byte payload of foreign bytes is rejected, so length is not the guard.
  4. A truncated payload is rejected rather than underrunning the buffer.
  5. Both copies of the decoder carry the check, so a third copy cannot skip it.

Usage:
    uv run tests/regression/verify_migration_event_discriminator.py
"""

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DECODER_FILES = (
    PROJECT_ROOT
    / "cookbook/pumpfun/graduation/pumpfun_listen_migrations_logsubscribe.py",
    PROJECT_ROOT / "tools/compare_migration_listeners.py",
)

# Captured from 64XX5RvH8tuUH8xcjCLWxpKbV76XEvvy4emA7jLqBpQhizKidXA2JpBskoPmXSF339h9GKKvEw7SYitYfnhscEV8
REAL_EVENT_BASE64 = (
    "sTEM0qB2p3TFRbZqAAAAAAAAgu92lXybnLAhVPxEgUNwWsHZjf0HhGpKPxZqwqzSRAo7PCcIdzHz"
    "bjOi+1BCWfvrYYBHT3/jky9BlSkjKrvRvwabiFf+q4GE+2h/Y0YYwDXaxDncGus7VZig8AAAAAAB"
    "BgkACAGpLLwAABD20ckTAAAAAAgBqSy8AAAQ9tHJEwAAAGQAAAAAAAAA7EJrWdADAACIQmtZ0AMA"
    "AP09v2FQlhz3OoZ2ArMAVANd/SfIvh9dnU1NIsN3S31arf81CJ5nJ3q/uNV/kSaz+qZ783cc3dOa"
    "HYPIrTZorNDHLxoLcdVoeJ9ZN0Q/1Vlm+brLQrSUbJWZpMPVORZ1JDq2eGxgCgysRkkiPlx1Y50a"
    "aCkd3znC9PiWNSNIFXnoG3Ov/WZxIeqJuAtGZYzOJz4hPPh+bkt2br4Eb8DEn7RwAAAAAAAAAAAA"
    "AAA="
)
REAL_EVENT = base64.b64decode(REAL_EVENT_BASE64)

EXPECTED = {
    "baseMint": "4zEFhAkgZhjXfZeMaNsvxufgTkFvrz2trYYKDqnnpump",
    "quoteMint": "So11111111111111111111111111111111111111112",
    "baseMintDecimals": 6,
    "quoteMintDecimals": 9,
    "poolBump": 253,
}

# The smallest payload the schema can read to the end of: 8 discriminator, three
# 8-byte ints, a u16, seven pubkeys, four u8s and six more 8-byte ints.
SCHEMA_MIN_LENGTH = 301

EXPECTED_DISCRIMINATOR = hashlib.sha256(b"event:CreatePoolEvent").digest()[:8]


def _load(path: Path) -> ModuleType:
    """Import a standalone script by path, without adding it to sys.modules."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DECODERS = {path.name: _load(path).parse_migrate_instruction for path in DECODER_FILES}


def _check(label: str, passed: bool, detail: str) -> bool:  # noqa: FBT001
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return passed


def _rejects(payload: bytes, label: str) -> bool:
    """Every decoder must return None for this payload."""
    results = []
    for name, decode in DECODERS.items():
        parsed = decode(payload)
        results.append(
            _check(
                name,
                parsed is None,
                "rejected"
                if parsed is None
                else f"ACCEPTED — baseMintDecimals={parsed.get('baseMintDecimals')}, "
                f"poolBump={parsed.get('poolBump')}",
            )
        )
    print(f"     ({label})")
    return all(results)


def check_real_event_decodes() -> bool:
    print("\n1. A real CreatePoolEvent still decodes")
    results = []
    for name, decode in DECODERS.items():
        parsed = decode(REAL_EVENT)
        if parsed is None:
            results.append(_check(name, passed=False, detail="rejected a real event"))
            continue
        wrong = {k: parsed.get(k) for k, v in EXPECTED.items() if parsed.get(k) != v}
        results.append(
            _check(
                name,
                not wrong,
                f"baseMintDecimals={parsed['baseMintDecimals']}, "
                f"quoteMintDecimals={parsed['quoteMintDecimals']}, "
                f"poolBump={parsed['poolBump']}"
                if not wrong
                else f"mismatched fields: {wrong}",
            )
        )
    return all(results)


def check_discriminator_is_the_expected_one() -> bool:
    print("\n2. The payload's discriminator is sha256('event:CreatePoolEvent')[:8]")
    actual = REAL_EVENT[:8]
    return _check(
        "leading 8 bytes",
        actual == EXPECTED_DISCRIMINATOR,
        f"{actual.hex()} (expected {EXPECTED_DISCRIMINATOR.hex()})",
    )


def check_foreign_discriminator_rejected() -> bool:
    print("\n3. The same payload with a foreign discriminator is rejected")
    foreign = bytes([0xBD, 0xDB, 0x7F, 0xD3, 0x4E, 0xE6, 0x61, 0xEE]) + REAL_EVENT[8:]
    return _rejects(foreign, "same length and body, different event")


def check_long_foreign_payload_rejected() -> bool:
    print("\n4. A foreign payload long enough for the schema is rejected")
    # Deterministic filler; the point is the length, not the content.
    filler = bytes((i * 7 + 13) % 256 for i in range(len(REAL_EVENT) - 8))
    foreign = bytes([0xFF] * 8) + filler
    passed = _rejects(
        foreign, f"{len(foreign)} bytes, schema needs {SCHEMA_MIN_LENGTH}"
    )
    return passed and _check(
        "payload long enough to matter",
        len(foreign) >= SCHEMA_MIN_LENGTH,
        f"{len(foreign)} >= {SCHEMA_MIN_LENGTH}",
    )


def check_truncated_payload_rejected() -> bool:
    print("\n5. A truncated payload is rejected rather than underrunning")
    return _rejects(REAL_EVENT[:64], "64 bytes of a real event")


def check_every_decoder_is_guarded() -> bool:
    print("\n6. Every copy of the decoder carries the check")
    results = []
    for path in DECODER_FILES:
        source = path.read_text()
        guarded = (
            "CREATE_POOL_EVENT_DISCRIMINATOR" in source
            and "b1310cd2a076a774" in source
            and "data[:8] != CREATE_POOL_EVENT_DISCRIMINATOR" in source
        )
        results.append(
            _check(
                path.relative_to(PROJECT_ROOT).as_posix(),
                guarded,
                "checks the discriminator before decoding"
                if guarded
                else "no discriminator check found",
            )
        )
    return all(results)


def main() -> None:
    print("=" * 72)
    print("Verifying the migration event decoder asserts its discriminator")
    print("=" * 72)

    results = [
        check_real_event_decodes(),
        check_discriminator_is_the_expected_one(),
        check_foreign_discriminator_rejected(),
        check_long_foreign_payload_rejected(),
        check_truncated_payload_rejected(),
        check_every_decoder_is_guarded(),
    ]

    print("\n" + "=" * 72)
    if all(results):
        print(f"ALL {len(results)} CHECKS PASSED")
    else:
        print(f"{results.count(False)}/{len(results)} CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
