"""A coin's name, symbol and uri, read from its on-chain metadata.

Token-2022 coins carry it in the mint's own TokenMetadata extension; SPL Token
coins in a Metaplex metadata account. Listeners read these off the launch;
a coin found any other way (a migration) has only its mint to go on.
"""

import struct
from typing import Final, NamedTuple

from solders.pubkey import Pubkey

METADATA_PROGRAM: Final[Pubkey] = Pubkey.from_string(
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
)

# Token-2022 mint: 82-byte base mint, padding to 165, one account-type byte,
# then type-length-value extensions.
_EXTENSIONS_OFFSET = 166
_TOKEN_METADATA_EXTENSION = 19
# TokenMetadata: update authority and mint, then name, symbol, uri.
_TOKEN_METADATA_STRINGS_OFFSET = 64
# Metaplex Metadata: key u8, update authority, mint, then name, symbol, uri,
# each padded with NULs to a fixed width.
_METAPLEX_STRINGS_OFFSET = 65


class TokenMetadata(NamedTuple):
    name: str
    symbol: str
    uri: str


def metadata_address(mint: Pubkey) -> Pubkey:
    """The Metaplex metadata account of a mint."""
    return Pubkey.find_program_address(
        [b"metadata", bytes(METADATA_PROGRAM), bytes(mint)], METADATA_PROGRAM
    )[0]


def _strings(data: bytes, offset: int) -> TokenMetadata:
    """Three borsh strings (u32 length, utf-8 bytes), trailing NULs dropped."""
    values = []
    for _ in range(3):
        (length,) = struct.unpack_from("<I", data, offset)
        offset += 4
        if offset + length > len(data):
            raise ValueError("metadata string runs past the account")  # noqa: TRY003
        raw = data[offset : offset + length]
        values.append(raw.decode("utf-8", errors="replace").rstrip("\x00"))
        offset += length
    return TokenMetadata(*values)


def _token_2022_metadata(mint_data: bytes) -> TokenMetadata | None:
    offset = _EXTENSIONS_OFFSET
    while offset + 4 <= len(mint_data):
        kind, length = struct.unpack_from("<HH", mint_data, offset)
        if kind == 0 and length == 0:
            break
        if kind == _TOKEN_METADATA_EXTENSION:
            body = mint_data[offset + 4 : offset + 4 + length]
            return _strings(body, _TOKEN_METADATA_STRINGS_OFFSET)
        offset += 4 + length
    return None


def decode_token_metadata(
    mint_data: bytes, metaplex_data: bytes | None
) -> TokenMetadata | None:
    """The coin's metadata: the mint's own extension first, else Metaplex's account.

    Args:
        mint_data: The mint account's data
        metaplex_data: The data at `metadata_address(mint)`, None if no account

    Returns:
        None when neither holds any, or what is there does not decode
    """
    try:
        found = _token_2022_metadata(mint_data)
        if found is None and metaplex_data:
            found = _strings(metaplex_data, _METAPLEX_STRINGS_OFFSET)
    except (struct.error, ValueError):
        return None
    return found
