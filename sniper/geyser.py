"""
Yellowstone Geyser gRPC listener for Pump.fun token creation events.

Production-grade implementation with:
- Secure SSL/gRPC connection
- x-token and basic auth support
- IDL-based event parsing
- Automatic reconnection
"""

import asyncio
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

import grpc
from solders.pubkey import Pubkey

# Add parent directory to path for geyser proto imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from geyser.generated import geyser_pb2, geyser_pb2_grpc


# Pump.fun program addresses
PUMP_FUN_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


@dataclass
class TokenInfo:
    """Token information extracted from creation event."""
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    creator: Pubkey
    creator_vault: Pubkey
    name: str
    symbol: str
    uri: str
    is_token_2022: bool
    token_program_id: Pubkey
    is_mayhem_mode: bool = False


class PumpFunEventParser:
    """Parse Pump.fun token creation events using IDL discriminators."""

    def __init__(self, idl_path: Path | None = None):
        """Initialize parser with IDL data."""
        if idl_path is None:
            idl_path = Path(__file__).parent.parent / "idl" / "pump_fun_idl.json"

        with open(idl_path) as f:
            self.idl = json.load(f)

        # Extract instruction discriminators
        self._discriminators = {}
        for ix in self.idl.get("instructions", []):
            name = ix["name"]
            disc = bytes(ix["discriminator"])
            self._discriminators[name] = disc

        self._create_discriminator = self._discriminators.get("create")
        self._create_v2_discriminator = self._discriminators.get("createV2")

        if not self._create_discriminator:
            raise ValueError("Could not find 'create' instruction in IDL")

    def parse_instruction(
        self,
        ix_data: bytes,
        account_indices: list[int],
        account_keys: list[bytes],
    ) -> TokenInfo | None:
        """Parse a Pump.fun create instruction."""
        if len(ix_data) < 8:
            return None

        discriminator = ix_data[:8]

        # Check for create or createV2 instruction
        is_v2 = False
        if discriminator == self._create_discriminator:
            is_v2 = False
        elif self._create_v2_discriminator and discriminator == self._create_v2_discriminator:
            is_v2 = True
        else:
            return None

        try:
            return self._parse_create_instruction(
                ix_data[8:], account_indices, account_keys, is_v2
            )
        except Exception:
            return None

    def _parse_create_instruction(
        self,
        data: bytes,
        account_indices: list[int],
        account_keys: list[bytes],
        is_v2: bool,
    ) -> TokenInfo | None:
        """Parse create instruction data and accounts."""
        # Parse instruction data: name (string), symbol (string), uri (string)
        offset = 0

        # Read name (4-byte length prefix + string)
        if len(data) < offset + 4:
            return None
        name_len = struct.unpack("<I", data[offset:offset + 4])[0]
        offset += 4
        if len(data) < offset + name_len:
            return None
        name = data[offset:offset + name_len].decode("utf-8", errors="replace")
        offset += name_len

        # Read symbol
        if len(data) < offset + 4:
            return None
        symbol_len = struct.unpack("<I", data[offset:offset + 4])[0]
        offset += 4
        if len(data) < offset + symbol_len:
            return None
        symbol = data[offset:offset + symbol_len].decode("utf-8", errors="replace")
        offset += symbol_len

        # Read URI
        if len(data) < offset + 4:
            return None
        uri_len = struct.unpack("<I", data[offset:offset + 4])[0]
        offset += 4
        if len(data) < offset + uri_len:
            return None
        uri = data[offset:offset + uri_len].decode("utf-8", errors="replace")

        # Extract account addresses from instruction
        # Account order for create: mint, bonding_curve, associated_bonding_curve,
        # global, mpl_token_metadata, metadata, user, system_program, token_program,
        # associated_token_program, rent, event_authority, program

        def get_pubkey(idx: int) -> Pubkey | None:
            if idx >= len(account_indices):
                return None
            account_idx = account_indices[idx]
            if account_idx >= len(account_keys):
                return None
            return Pubkey.from_bytes(account_keys[account_idx])

        mint = get_pubkey(0)
        bonding_curve = get_pubkey(1)
        associated_bonding_curve = get_pubkey(2)
        creator = get_pubkey(6)  # user account

        if not all([mint, bonding_curve, associated_bonding_curve, creator]):
            return None

        # Determine token program
        token_program_pubkey = get_pubkey(8)
        is_token_2022 = token_program_pubkey == TOKEN_2022_PROGRAM if token_program_pubkey else is_v2
        token_program_id = TOKEN_2022_PROGRAM if is_token_2022 else TOKEN_PROGRAM

        # Derive creator vault
        creator_vault, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator)],
            PUMP_FUN_PROGRAM,
        )

        return TokenInfo(
            mint=mint,
            bonding_curve=bonding_curve,
            associated_bonding_curve=associated_bonding_curve,
            creator=creator,
            creator_vault=creator_vault,
            name=name,
            symbol=symbol,
            uri=uri,
            is_token_2022=is_token_2022,
            token_program_id=token_program_id,
        )


class GeyserListener:
    """Yellowstone Geyser gRPC listener for Pump.fun."""

    def __init__(
        self,
        endpoint: str,
        api_token: str,
        auth_type: str = "x-token",
    ):
        """
        Initialize Geyser listener.

        Args:
            endpoint: Geyser gRPC endpoint (e.g., grpc.provider.io:443)
            api_token: API token for authentication
            auth_type: Authentication type ("x-token" or "basic")
        """
        self.endpoint = endpoint
        self.api_token = api_token
        self.auth_type = auth_type.lower()

        if self.auth_type not in {"x-token", "basic"}:
            raise ValueError(f"Invalid auth_type: {auth_type}. Use 'x-token' or 'basic'")

        self.parser = PumpFunEventParser()
        self._running = False
        self._channel = None
        self._stub = None

    async def _create_connection(self):
        """Establish secure gRPC connection with authentication."""
        if self.auth_type == "x-token":
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("x-token", self.api_token),), None
                )
            )
        else:
            auth = grpc.metadata_call_credentials(
                lambda _, callback: callback(
                    (("authorization", f"Basic {self.api_token}"),), None
                )
            )

        creds = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(),
            auth,
        )

        self._channel = grpc.aio.secure_channel(self.endpoint, creds)
        self._stub = geyser_pb2_grpc.GeyserStub(self._channel)

        return self._stub

    def _create_subscription_request(self) -> geyser_pb2.SubscribeRequest:
        """Create subscription request for Pump.fun transactions."""
        request = geyser_pb2.SubscribeRequest()

        # Subscribe to transactions involving Pump.fun program
        filter_name = "pumpfun_txs"
        request.transactions[filter_name].account_include.append(str(PUMP_FUN_PROGRAM))
        request.transactions[filter_name].failed = False

        # Use PROCESSED commitment for fastest detection
        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED

        return request

    async def listen(
        self,
        callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
        max_token_age: float = 5.0,
        stop_after_first: bool = True,
    ) -> TokenInfo | None:
        """
        Listen for new token creations.

        Args:
            callback: Async callback function called with TokenInfo
            match_string: Optional filter for token name/symbol
            creator_address: Optional filter for creator address
            max_token_age: Maximum age in seconds for tokens to process
            stop_after_first: Stop listening after first token (single-token mode)

        Returns:
            TokenInfo of first detected token if stop_after_first=True, else None
        """
        self._running = True
        first_token: TokenInfo | None = None

        while self._running:
            try:
                stub = await self._create_connection()
                request = self._create_subscription_request()

                print(f"[GEYSER] Connected to {self.endpoint}")
                print(f"[GEYSER] Monitoring Pump.fun program: {PUMP_FUN_PROGRAM}")
                if match_string:
                    print(f"[GEYSER] Filter: name/symbol contains '{match_string}'")
                if creator_address:
                    print(f"[GEYSER] Filter: creator = {creator_address}")

                async for update in stub.Subscribe(iter([request])):
                    if not self._running:
                        break

                    token_info = self._process_update(update)
                    if not token_info:
                        continue

                    # Apply filters
                    if match_string:
                        if (match_string.lower() not in token_info.name.lower() and
                            match_string.lower() not in token_info.symbol.lower()):
                            continue

                    if creator_address:
                        if str(token_info.creator) != creator_address:
                            continue

                    print(f"[GEYSER] New token: {token_info.name} ({token_info.symbol})")
                    print(f"[GEYSER] Mint: {token_info.mint}")
                    print(f"[GEYSER] Creator: {token_info.creator}")

                    # Execute callback
                    await callback(token_info)

                    if stop_after_first:
                        first_token = token_info
                        self._running = False
                        break

            except grpc.aio.AioRpcError as e:
                print(f"[GEYSER] gRPC error: {e.details()}")
                await asyncio.sleep(5)

            except Exception as e:
                print(f"[GEYSER] Error: {e}")
                await asyncio.sleep(5)

            finally:
                if self._channel:
                    await self._channel.close()
                    self._channel = None

        return first_token

    def _process_update(self, update) -> TokenInfo | None:
        """Process a Geyser update and extract token creation."""
        try:
            if not update.HasField("transaction"):
                return None

            tx = update.transaction.transaction.transaction
            msg = getattr(tx, "message", None)
            if msg is None:
                return None

            # Iterate through instructions looking for create instruction
            for ix in msg.instructions:
                program_idx = ix.program_id_index
                if program_idx >= len(msg.account_keys):
                    continue

                program_id = Pubkey.from_bytes(msg.account_keys[program_idx])
                if program_id != PUMP_FUN_PROGRAM:
                    continue

                # Parse the instruction
                token_info = self.parser.parse_instruction(
                    ix.data,
                    list(ix.accounts),
                    list(msg.account_keys),
                )

                if token_info:
                    return token_info

            return None

        except Exception:
            return None

    def stop(self):
        """Stop the listener."""
        self._running = False


async def main():
    """Test the Geyser listener."""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    endpoint = os.getenv("GEYSER_ENDPOINT")
    api_token = os.getenv("GEYSER_API_TOKEN")
    auth_type = os.getenv("GEYSER_AUTH_TYPE", "x-token")

    if not endpoint or not api_token:
        print("Error: GEYSER_ENDPOINT and GEYSER_API_TOKEN must be set")
        return

    listener = GeyserListener(endpoint, api_token, auth_type)

    async def on_token(token: TokenInfo):
        print(f"\nToken detected: {token.name}")
        print(f"Symbol: {token.symbol}")
        print(f"Mint: {token.mint}")
        print(f"Bonding Curve: {token.bonding_curve}")
        print(f"Creator: {token.creator}")

    print("Starting Geyser listener (press Ctrl+C to stop)...")
    await listener.listen(on_token, stop_after_first=False)


if __name__ == "__main__":
    asyncio.run(main())
