"""Decode the pump.fun instructions in a getTransaction response.

Usage:
    uv run cookbook/pumpfun/decode/pumpfun_decode_transaction_gettransaction.py [tx.json]

Three things to get right when writing your own decoder:

1. Identify an instruction by its **8-byte Anchor discriminator**, not by how
   many accounts it carries. Several pump.fun instructions share an account
   count, so matching on the count labels a `create_v2` as `claim_cashback` and
   then prints every account under the wrong name.

2. Read `meta.innerInstructions` as well as `message.instructions`. Most trades
   reach the program as a CPI from an aggregator or router, so a decoder that
   only walks the top level sees almost nothing.

3. Accept whichever encoding the response was captured in. `getTransaction`
   answers in `jsonParsed`, `json` or `base64`, and the three look nothing alike:
   `json` gives account *indices* where `jsonParsed` gives addresses, and
   `base64` gives the raw envelope and nothing else. `normalize_result` folds all
   three into the jsonParsed shape. The fixtures beside this script cover all
   three.
"""

import argparse
import base64
import hashlib
import json
import struct
from collections.abc import Iterator

import base58
from solders.transaction import VersionedTransaction

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DEFAULT_TX = "cookbook/pumpfun/decode/raw_buy_tx_from_gettransaction.json"
IDL_PATH = "idl/pump_fun_idl.json"

# Anchor's event-CPI prefix. Every emitted event shows up as an inner instruction
# that calls the program itself with this discriminator, followed by the event's own
# 8-byte discriminator. Without recognising it, half the inner instructions in a
# trade look like unknown instructions.
ANCHOR_EVENT_CPI = bytes([0xE4, 0x45, 0xA5, 0x2E, 0x51, 0xCB, 0x9A, 0x1D])

DISCRIMINATOR_LEN = 8
# v2 trade args: discriminator + two u64, with no track_volume OptionBool.
TRADE_DATA_LEN = 24
EXPECTED_ARGC = 2


def anchor_discriminator(name: str) -> bytes:
    """Return the 8-byte Anchor discriminator for an instruction name.

    Args:
        name: snake_case instruction name as it appears in the IDL

    Returns:
        First 8 bytes of sha256("global:<name>")
    """
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def build_instruction_index(idl: dict) -> dict[bytes, dict]:
    """Map every IDL instruction to its discriminator.

    Args:
        idl: Parsed Anchor IDL

    Returns:
        discriminator -> IDL instruction definition
    """
    return {anchor_discriminator(ix["name"]): ix for ix in idl.get("instructions", [])}


def build_event_index(idl: dict) -> dict[bytes, str]:
    """Map every IDL event to its discriminator.

    Args:
        idl: Parsed Anchor IDL

    Returns:
        discriminator -> event name
    """
    return {
        hashlib.sha256(f"event:{event['name']}".encode()).digest()[:8]: event["name"]
        for event in idl.get("events", [])
    }


def decode_create_instruction(data: bytes) -> dict:
    """Decode a legacy `create` instruction (Metaplex tokens).

    Args:
        data: Raw instruction data including the discriminator

    Returns:
        Decoded args: name, symbol, uri, creator
    """
    offset = 8  # Skip the 8-byte discriminator
    results = []
    for _ in range(3):
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        results.append(data[offset : offset + length].decode("utf-8"))
        offset += length

    creator = (
        base58.b58encode(data[offset : offset + 32]).decode("utf-8")
        if offset + 32 <= len(data)
        else None
    )

    return {
        "name": results[0],
        "symbol": results[1],
        "uri": results[2],
        "creator": creator,
        "token_standard": "legacy",
    }


def decode_create_v2_instruction(data: bytes) -> dict:
    """Decode a `create_v2` instruction (Token2022 tokens).

    IDL args: name, symbol, uri, creator (pubkey), is_mayhem_mode (bool),
    is_cashback_enabled (OptionBool = 1 byte).

    Args:
        data: Raw instruction data including the discriminator

    Returns:
        Decoded args including the mayhem and cashback flags
    """
    offset = 8  # Skip the 8-byte discriminator
    results = []
    for _ in range(3):
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        results.append(data[offset : offset + length].decode("utf-8"))
        offset += length

    creator = (
        base58.b58encode(data[offset : offset + 32]).decode("utf-8")
        if offset + 32 <= len(data)
        else None
    )
    offset += 32

    decoded = {
        "name": results[0],
        "symbol": results[1],
        "uri": results[2],
        "creator": creator,
        "token_standard": "token2022",
    }

    # Both trailing args may be cut off the wire. A missing one is left out, so
    # the caller can tell "not sent" from a flag that was sent as false.
    if offset < len(data):
        decoded["is_mayhem_mode"] = bool(data[offset])
        offset += 1
    if offset < len(data):
        decoded["is_cashback_enabled"] = bool(data[offset])

    return decoded


def decode_trade_instruction(data: bytes) -> dict:
    """Decode the two u64 args shared by buy/sell and their v2 forms.

    v2 args carry no `track_volume` OptionBool: 24 bytes of data, being the
    discriminator plus two u64. The quote-side limit is denominated in the quote
    mint's raw units — lamports for SOL, 1e-6 for USDC.

    Args:
        data: Raw instruction data including the discriminator

    Returns:
        The token amount and the quote-side limit
    """
    if len(data) < TRADE_DATA_LEN:
        return {"error": f"expected >={TRADE_DATA_LEN} bytes of data, got {len(data)}"}
    amount, quote_limit = struct.unpack_from("<QQ", data, 8)
    return {"amount": amount, "quote_limit": quote_limit}


DECODERS = {
    "create": decode_create_instruction,
    "create_v2": decode_create_v2_instruction,
    "buy": decode_trade_instruction,
    "buy_v2": decode_trade_instruction,
    "sell": decode_trade_instruction,
    "sell_v2": decode_trade_instruction,
}


def _account_keys(result: dict) -> list[str]:
    """Return every account the transaction can index, in index order.

    An instruction's account indices run past `message.accountKeys` into the
    address lookup tables, and the writable ones come before the readonly ones.
    Ignoring them makes a v0 transaction index out of range. getTransaction
    reports them as `meta.loadedAddresses.{writable,readonly}`; geyser splits
    the same two lists into `meta.loadedWritableAddresses` and
    `meta.loadedReadonlyAddresses`, so both spellings are read here.

    Args:
        result: A getTransaction result, jsonParsed or json encoded

    Returns:
        Static keys followed by the writable then readonly loaded addresses
    """
    keys = result["transaction"]["message"]["accountKeys"]
    static = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    meta = result.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    return [
        *static,
        *(loaded.get("writable") or meta.get("loadedWritableAddresses") or []),
        *(loaded.get("readonly") or meta.get("loadedReadonlyAddresses") or []),
    ]


def _resolve_indices(instructions: list[dict], keys: list[str]) -> list[dict]:
    """Rewrite json-encoded instructions into the jsonParsed shape.

    Args:
        instructions: Instructions carrying `programIdIndex` and index accounts
        keys: Account addresses in index order, from `_account_keys`

    Returns:
        The same instructions, with `programId` and address accounts
    """
    return [
        {
            **ix,
            "programId": keys[ix["programIdIndex"]],
            "accounts": [keys[i] for i in ix.get("accounts", [])],
        }
        if "programIdIndex" in ix
        else ix
        for ix in instructions
    ]


def normalize_result(payload: dict) -> dict:
    """Fold any getTransaction response into the jsonParsed shape.

    Handles the JSON-RPC envelope or a bare result, and all three encodings:
    `jsonParsed` passes through, `json` has its account indices resolved, and
    `base64` is deserialized. solders can only read a base64 **v1** transaction
    from 0.29 on; earlier versions raise `ValueError: io error: unexpected end
    of file`.

    Args:
        payload: A parsed getTransaction response, with or without the envelope

    Returns:
        A result whose instructions all carry `programId` and address accounts
    """
    result = payload["result"] if "result" in payload else payload
    transaction = result["transaction"]

    # base64: ["<encoded>", "base64"], and the envelope is all there is.
    if isinstance(transaction, list):
        decoded = VersionedTransaction.from_bytes(base64.b64decode(transaction[0]))
        message = decoded.message
        result = {
            **result,
            "transaction": {
                "message": {
                    "accountKeys": [str(key) for key in message.account_keys],
                    "recentBlockhash": str(message.recent_blockhash),
                    "instructions": [
                        {
                            "programIdIndex": ix.program_id_index,
                            "accounts": list(ix.accounts),
                            "data": base58.b58encode(bytes(ix.data)).decode(),
                        }
                        for ix in message.instructions
                    ],
                },
                "signatures": [str(sig) for sig in decoded.signatures],
            },
        }

    keys = _account_keys(result)
    message = result["transaction"]["message"]
    meta = result.get("meta") or {}
    return {
        **result,
        "transaction": {
            **result["transaction"],
            "message": {
                **message,
                "instructions": _resolve_indices(message.get("instructions", []), keys),
            },
        },
        "meta": {
            **meta,
            "innerInstructions": [
                {**group, "instructions": _resolve_indices(group["instructions"], keys)}
                for group in meta.get("innerInstructions") or []
            ],
        },
    }


def iter_pump_instructions(result: dict) -> Iterator[tuple[str, dict]]:
    """Yield every pump.fun instruction in a getTransaction result.

    Args:
        result: The `result` object of a jsonParsed getTransaction response

    Yields:
        (location, instruction) where location is "top-level" or "inner (ix N)"
    """
    for ix in result["transaction"]["message"].get("instructions", []):
        if ix.get("programId") == PUMP_PROGRAM and ix.get("data"):
            yield "top-level", ix
    for group in result.get("meta", {}).get("innerInstructions") or []:
        for ix in group.get("instructions", []):
            if ix.get("programId") == PUMP_PROGRAM and ix.get("data"):
                yield f"inner (ix {group['index']})", ix


def describe(
    location: str, ix: dict, index: dict[bytes, dict], events: dict[bytes, str]
) -> None:
    """Print one pump.fun instruction, resolved against the IDL.

    Args:
        location: Where the instruction sits in the transaction
        ix: The jsonParsed instruction
        index: discriminator -> IDL instruction definition
        events: discriminator -> IDL event name
    """
    data = base58.b58decode(ix["data"])
    accounts = ix.get("accounts", [])

    if data[:8] == ANCHOR_EVENT_CPI:
        event = events.get(data[8:16], "unknown event")
        print(f"\n[{location}] anchor event emit -> {event}")
        return

    definition = index.get(data[:8])

    if definition is None:
        disc = (
            struct.unpack("<Q", data[:DISCRIMINATOR_LEN])[0]
            if len(data) >= DISCRIMINATOR_LEN
            else None
        )
        print(f"\n[{location}] unknown pump.fun instruction (discriminator {disc})")
        return

    name = definition["name"]
    print(f"\n[{location}] {name}")

    decoder = DECODERS.get(name)
    print(f"  args: {decoder(data) if decoder else '(no decoder in this example)'}")

    # The IDL under-reports the legacy instructions and omits create_v2's optional
    # remaining accounts, so name what it covers and list the rest positionally.
    idl_accounts = definition.get("accounts", [])
    print(f"  accounts: {len(accounts)} on chain, {len(idl_accounts)} named in the IDL")
    for i, account in enumerate(accounts):
        label = idl_accounts[i]["name"] if i < len(idl_accounts) else f"<extra {i}>"
        print(f"    {i:2d} {label}: {account}")


def main() -> None:
    """Parse the command line and decode the transaction."""
    parser = argparse.ArgumentParser(
        description="Decode the pump.fun instructions in a getTransaction response"
    )
    parser.add_argument(
        "transaction",
        nargs="?",
        default=DEFAULT_TX,
        help=f"Saved getTransaction response (default {DEFAULT_TX})",
    )
    args = parser.parse_args()
    tx_file_path = args.transaction

    with open(IDL_PATH) as f:
        idl = json.load(f)
    index = build_instruction_index(idl)
    events = build_event_index(idl)

    with open(tx_file_path) as f:
        result = normalize_result(json.load(f))

    found = 0
    for location, ix in iter_pump_instructions(result):
        describe(location, ix, index, events)
        found += 1

    if not found:
        print(
            "\nNo pump.fun instruction in this transaction. That is normal for a "
            "router transaction whose pump.fun call was not captured in meta."
        )

    message = result["transaction"]["message"]
    print("\nTransaction Information:")
    print(f"Blockhash: {message['recentBlockhash']}")
    print(f"Fee payer: {_account_keys(result)[0]}")
    print(f"Signature: {result['transaction']['signatures'][0]}")
    print(f"Slot: {result.get('slot')}")
    print(f"Transaction version: {result.get('version', 'legacy')}")


if __name__ == "__main__":
    main()
