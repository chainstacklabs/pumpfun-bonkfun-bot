"""
IDL Parser module for Solana programs.
Provides functionality to load and parse Anchor IDL files and decode instruction data.
"""

import json
import struct
from typing import Any

import base58

# Constants for Anchor data layout
DISCRIMINATOR_SIZE = 8
PUBLIC_KEY_SIZE = 32
STRING_LENGTH_PREFIX_SIZE = 4
ENUM_DISCRIMINATOR_SIZE = 1


class IDLParser:
    """Parser for automatically decoding instructions using IDL definitions."""

    # A single source of truth for primitive type information, mapping the type name
    # to its struct format character and size in bytes.
    _PRIMITIVE_TYPE_INFO = {
        # type_name: (format_char, size_in_bytes)
        "u8": ("<B", 1),
        "u16": ("<H", 2),
        "u32": ("<I", 4),
        "u64": ("<Q", 8),
        "i8": ("<b", 1),
        "i16": ("<h", 2),
        "i32": ("<i", 4),
        "i64": ("<q", 8),
        "bool": ("<?", 1),
        "pubkey": (None, PUBLIC_KEY_SIZE),
        "string": (
            None,
            STRING_LENGTH_PREFIX_SIZE,
        ),  # Min size is for the length prefix
    }

    def __init__(self, idl_path: str, verbose: bool = False):
        """
        Initialize the IDL parser.

        Args:
            idl_path: Path to the IDL JSON file
            verbose: Whether to print debug information during initialization
        """
        self.verbose = verbose
        with open(idl_path) as f:
            self.idl = json.load(f)
        self.instructions: dict[bytes, dict[str, Any]] = {}
        self.types: dict[str, dict[str, Any]] = {}
        self.instruction_min_sizes: dict[bytes, int] = {}
        self._build_instruction_map()
        self._build_type_map()
        self._calculate_instruction_sizes()

    # --------------------------------------------------------------------------
    # Public Methods (External API)
    # --------------------------------------------------------------------------

    def get_instruction_discriminators(self) -> dict[str, bytes]:
        """Get a mapping of instruction names to their discriminators."""
        return {instr["name"]: disc for disc, instr in self.instructions.items()}

    def get_instruction_names(self) -> list[str]:
        """Get a list of all available instruction names."""
        return [instr["name"] for instr in self.instructions.values()]

    def validate_instruction_data_length(
        self, ix_data: bytes, discriminator: bytes
    ) -> bool:
        """Validate that instruction data meets minimum length requirements."""
        if discriminator not in self.instruction_min_sizes:
            return True  # Allow if we don't know the expected size

        expected_min_size = self.instruction_min_sizes[discriminator]
        actual_size = len(ix_data)

        if actual_size < expected_min_size:
            instruction_name = self.instructions[discriminator]["name"]
            if self.verbose:
                print(
                    f"⚠️  Instruction data for '{instruction_name}' is shorter than the expected minimum "
                    f"({actual_size}/{expected_min_size} bytes)."
                )
            return False

        return True

    def decode_instruction(
        self, ix_data: bytes, keys: list[bytes], accounts: list[int]
    ) -> dict[str, Any] | None:
        """Decode instruction data using IDL definitions."""
        if len(ix_data) < DISCRIMINATOR_SIZE:
            return None

        discriminator = ix_data[:DISCRIMINATOR_SIZE]
        if discriminator not in self.instructions:
            return None

        if not self.validate_instruction_data_length(ix_data, discriminator):
            return None

        instruction = self.instructions[discriminator]
        data_args = ix_data[DISCRIMINATOR_SIZE:]

        # Decode instruction arguments
        args = {}
        decode_offset = 0
        for arg in instruction.get("args", []):
            try:
                value, decode_offset = self._decode_type(
                    data_args, decode_offset, arg["type"]
                )
                args[arg["name"]] = value
            except Exception as e:
                if self.verbose:
                    print(f"❌ Decode error in argument '{arg['name']}': {e}")
                return None

        # Helper to safely retrieve account public keys
        def get_account_key(index: int) -> str | None:
            if index < len(accounts):
                account_index = accounts[index]
                if account_index < len(keys):
                    return base58.b58encode(keys[account_index]).decode("utf-8")
            return None  # Return None for invalid indices

        # Build account info based on instruction definition
        account_info = {}
        instruction_accounts = instruction.get("accounts", [])
        for i, account_def in enumerate(instruction_accounts):
            account_info[account_def["name"]] = get_account_key(i)

        return {
            "instruction_name": instruction["name"],
            "args": args,
            "accounts": account_info,
        }

    def decode_account_data(
        self,
        account_data: bytes,
        account_type_name: str,
        skip_discriminator: bool = True,
    ) -> dict[str, Any] | None:
        """
        Decode account data using a specific account type from the IDL.

        Args:
            account_data: Raw account data bytes.
            account_type_name: Name of the account type in the IDL (e.g., "MyAccount").
            skip_discriminator: Whether to skip the first 8 bytes, which Anchor uses as a
                                type discriminator for account data. Set to False if your
                                data does not have this prefix.

        Returns:
            Decoded account data as a dictionary, or None if decoding fails.
        """
        try:
            if account_type_name not in self.types:
                if self.verbose:
                    print(f"Account type '{account_type_name}' not found in IDL")
                return None

            data = account_data
            if skip_discriminator:
                if len(account_data) < DISCRIMINATOR_SIZE:
                    if self.verbose:
                        print(
                            f"Account data too short to contain a discriminator: {len(account_data)} bytes"
                        )
                    return None
                data = account_data[DISCRIMINATOR_SIZE:]

            decoded_data, _ = self._decode_defined_type(data, 0, account_type_name)
            return decoded_data

        except Exception as e:
            if self.verbose:
                print(f"Error decoding account data for {account_type_name}: {e}")
            return None

    # --------------------------------------------------------------------------
    # Internal Helper Methods
    # --------------------------------------------------------------------------

    def _build_instruction_map(self):
        """Build a map of discriminators to instruction definitions."""
        for instruction in self.idl.get("instructions", []):
            # The discriminator from the JSON IDL is a list of u8 integers.
            discriminator = bytes(instruction["discriminator"])
            self.instructions[discriminator] = instruction

    def _build_type_map(self):
        """Build a map of type names to their definitions."""
        for type_def in self.idl.get("types", []):
            self.types[type_def["name"]] = type_def

    def _calculate_instruction_sizes(self):
        """Calculate minimum data sizes for each instruction."""
        for discriminator, instruction in self.instructions.items():
            try:
                min_size = DISCRIMINATOR_SIZE
                for arg in instruction.get("args", []):
                    min_size += self._calculate_type_min_size(arg["type"])
                self.instruction_min_sizes[discriminator] = min_size
                if self.verbose and instruction["name"] == "initialize":
                    print(f"📏 Initialize instruction min size: {min_size} bytes")
            except Exception as e:
                if self.verbose:
                    print(f"⚠️  Could not calculate size for {instruction['name']}: {e}")
                self.instruction_min_sizes[discriminator] = DISCRIMINATOR_SIZE

    def _calculate_type_min_size(self, type_def: str | dict) -> int:
        """Calculate minimum size in bytes for a type definition."""
        if isinstance(type_def, str):
            return self._get_primitive_size(type_def)

        if isinstance(type_def, dict):
            if "defined" in type_def:
                type_name = self._get_defined_type_name(type_def)
                return self._calculate_defined_type_min_size(type_name)
            if "array" in type_def:
                element_type, array_length = type_def["array"]
                element_size = self._calculate_type_min_size(element_type)
                return element_size * array_length

        raise ValueError(
            f"Invalid or unknown type definition for size calculation: {type_def}"
        )

    def _get_primitive_size(self, type_name: str) -> int:
        """Get size in bytes for primitive types from the central map."""
        info = self._PRIMITIVE_TYPE_INFO.get(type_name)
        return info[1] if info else 0

    def _get_defined_type_name(self, type_def: dict[str, Any]) -> str:
        """Extracts the type name from a 'defined' type, handling old and new IDL formats."""
        defined_value = type_def["defined"]
        # New format: {'defined': {'name': 'MyType'}}
        # Old format: {'defined': 'MyType'}
        return (
            defined_value["name"] if isinstance(defined_value, dict) else defined_value
        )

    def _calculate_defined_type_min_size(self, type_name: str) -> int:
        """Calculate minimum size for user-defined types (structs and enums)."""
        if type_name not in self.types:
            raise ValueError(f"Unknown defined type: {type_name}")

        type_def = self.types[type_name]["type"]

        if type_def["kind"] == "struct":
            return sum(
                self._calculate_type_min_size(field["type"])
                for field in type_def["fields"]
            )

        if type_def["kind"] == "enum":
            # The size of an enum is its discriminator plus the size of its LARGEST variant,
            # as the data layout must accommodate any possible variant.
            max_variant_size = 0
            for variant in type_def["variants"]:
                variant_size = 0
                for field in variant.get("fields", []):
                    # A field can be a type string/dict (tuple variant) or a dict with a 'type' key (struct variant)
                    field_type = field["type"] if isinstance(field, dict) else field
                    variant_size += self._calculate_type_min_size(field_type)
                max_variant_size = max(max_variant_size, variant_size)
            return ENUM_DISCRIMINATOR_SIZE + max_variant_size

        raise ValueError(
            f"Unsupported type kind for size calculation: {type_def['kind']}"
        )

    def _decode_type(
        self, data: bytes, offset: int, type_def: str | dict
    ) -> tuple[Any, int]:
        """Decode a value based on its type definition."""
        if isinstance(type_def, str):
            return self._decode_primitive(data, offset, type_def)

        if isinstance(type_def, dict):
            if "defined" in type_def:
                type_name = self._get_defined_type_name(type_def)
                return self._decode_defined_type(data, offset, type_name)
            if "array" in type_def:
                return self._decode_array(data, offset, type_def["array"])

        raise ValueError(f"Invalid or unknown type definition for decoding: {type_def}")

    def _decode_array(
        self, data: bytes, offset: int, array_def: list
    ) -> tuple[list[Any], int]:
        """Decode fixed-size array types."""
        element_type, array_length = array_def
        array_data = []
        for _ in range(array_length):
            value, offset = self._decode_type(data, offset, element_type)
            array_data.append(value)
        return array_data, offset

    def _decode_primitive(
        self, data: bytes, offset: int, type_name: str
    ) -> tuple[Any, int]:
        """Decode primitive types."""
        if type_name not in self._PRIMITIVE_TYPE_INFO:
            raise ValueError(f"Unknown primitive type: {type_name}")

        if type_name == "string":
            length = struct.unpack_from("<I", data, offset)[0]
            offset += STRING_LENGTH_PREFIX_SIZE
            value = data[offset : offset + length].decode("utf-8")
            return value, offset + length

        if type_name == "pubkey":
            end = offset + PUBLIC_KEY_SIZE
            value = base58.b58encode(data[offset:end]).decode("utf-8")
            return value, end

        # Handle all numeric and bool types from the map
        fmt, size = self._PRIMITIVE_TYPE_INFO[type_name]
        value = struct.unpack_from(fmt, data, offset)[0]
        return value, offset + size

    def _decode_defined_type(
        self, data: bytes, offset: int, type_name: str
    ) -> tuple[dict[str, Any], int]:
        """Decode user-defined types (structs and enums)."""
        if type_name not in self.types:
            raise ValueError(f"Unknown defined type: {type_name}")

        type_def = self.types[type_name]["type"]

        if type_def["kind"] == "struct":
            struct_data = {}
            for field in type_def["fields"]:
                value, offset = self._decode_type(data, offset, field["type"])
                struct_data[field["name"]] = value
            return struct_data, offset

        if type_def["kind"] == "enum":
            variant_index = struct.unpack_from("<B", data, offset)[0]
            offset += ENUM_DISCRIMINATOR_SIZE

            variants = type_def["variants"]
            if variant_index >= len(variants):
                raise ValueError(
                    f"Invalid enum variant index {variant_index} for type {type_name}"
                )

            variant = variants[variant_index]
            result = {"variant": variant["name"]}
            variant_fields = variant.get("fields", [])

            if variant_fields:
                # Check if it's a struct variant (fields are dicts) or tuple variant (fields are strings/dicts)
                if isinstance(variant_fields[0], dict):
                    struct_data = {}
                    for field in variant_fields:
                        value, offset = self._decode_type(data, offset, field["type"])
                        struct_data[field["name"]] = value
                    result["data"] = struct_data
                else:  # Tuple variant
                    tuple_data = []
                    for field_type in variant_fields:
                        value, offset = self._decode_type(data, offset, field_type)
                        tuple_data.append(value)
                    result["data"] = tuple_data

            return result, offset

        raise ValueError(f"Unsupported type kind for decoding: {type_def['kind']}")


def load_idl_parser(idl_path: str, verbose: bool = False) -> IDLParser:
    """
    Convenience function to load an IDL parser.

    Args:
        idl_path: Path to the IDL JSON file
        verbose: Whether to print debug information

    Returns:
        Initialized IDLParser instance
    """
    return IDLParser(idl_path, verbose)
