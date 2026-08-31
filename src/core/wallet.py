"""
Wallet management for Solana transactions.
"""

from hashlib import sha256

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses
from utils.logger import register_secret


class Wallet:
    """Manages a Solana wallet for trading operations."""

    def __init__(self, private_key: str):
        """Initialize a wallet without retaining the encoded private key."""
        if not isinstance(private_key, str):
            raise TypeError("private_key must be a base58 string")
        if not private_key:
            raise ValueError("private_key cannot be empty")
        register_secret(private_key)
        self._keypair = self._load_keypair(private_key)
        self._public_key_fingerprint = self._fingerprint_pubkey(self._keypair.pubkey())

    @property
    def pubkey(self) -> Pubkey:
        """Get the public key of the wallet."""
        return self._keypair.pubkey()

    @property
    def public_key_fingerprint(self) -> str:
        """Return a non-reversible fingerprint suitable for logs."""
        return self._public_key_fingerprint

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"public_key_fingerprint='{self.public_key_fingerprint}')"
        )

    def __str__(self) -> str:
        """Return the public identity expected by wallet policy checks."""
        return str(self.pubkey)

    @property
    def keypair(self) -> Keypair:
        """Get the keypair for signing transactions."""
        return self._keypair

    def get_associated_token_address(
        self, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Get the associated token account address for a mint.

        Args:
            mint: Token mint address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_2022_PROGRAM

        Returns:
            Associated token account address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
        return get_associated_token_address(self.pubkey, mint, token_program_id)

    @staticmethod
    def _load_keypair(private_key: str) -> Keypair:
        """Decode a keypair and promptly clear the mutable decode buffer."""
        try:
            private_key_buffer = bytearray(base58.b58decode(private_key))
        except (TypeError, ValueError) as exc:
            raise ValueError("private_key must be valid base58") from exc

        try:
            if len(private_key_buffer) != 64:
                raise ValueError("private_key must decode to exactly 64 bytes")
            return Keypair.from_bytes(bytes(private_key_buffer))
        finally:
            private_key_buffer[:] = b"\x00" * len(private_key_buffer)

    @staticmethod
    def _fingerprint_pubkey(pubkey: Pubkey) -> str:
        digest = sha256(bytes(pubkey)).hexdigest()
        return f"sha256:{digest[:12]}"
