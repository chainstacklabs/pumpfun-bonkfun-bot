"""Single point of IDL loading, so no platform class loads its own copy."""

from pathlib import Path
from typing import Any

from interfaces.core import Platform
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class IDLManager:
    """Centralized manager for IDL parsers across all platforms."""

    def __init__(self):
        """Initialize the IDL manager."""
        self._parsers: dict[Platform, IDLParser] = {}
        self._idl_paths: dict[Platform, str] = {}
        self._setup_platform_idl_paths()

    def _setup_platform_idl_paths(self) -> None:
        """Setup IDL file paths for each platform."""
        # Get the project root directory (3 levels up from this file)
        current_file = Path(__file__)
        project_root = current_file.parent.parent.parent

        # Define IDL paths for each platform
        self._idl_paths = {
            Platform.LETS_BONK: project_root / "idl" / "raydium_launchlab_idl.json",
            Platform.PUMP_FUN: project_root / "idl" / "pump_fun_idl.json",
        }

    def get_parser(self, platform: Platform, verbose: bool = False) -> IDLParser:
        """Get or create an IDL parser for the specified platform.

        Args:
            platform: Platform to get parser for
            verbose: Whether to enable verbose logging in the parser

        Returns:
            IDLParser instance for the platform

        Raises:
            ValueError: If platform is not supported or IDL file not found
        """
        # Return cached parser if available
        if platform in self._parsers:
            return self._parsers[platform]

        # Check if platform has IDL support
        if platform not in self._idl_paths:
            raise ValueError(
                f"Platform {platform.value} does not have IDL support configured"
            )

        idl_path = self._idl_paths[platform]

        # Verify IDL file exists
        if not idl_path.exists():
            raise FileNotFoundError(
                f"IDL file not found for {platform.value} at {idl_path}"
            )

        # Load and cache the parser
        logger.info(f"Loading IDL parser for {platform.value} from {idl_path}")
        parser = IDLParser(idl_path, verbose=verbose)
        self._parsers[platform] = parser

        instruction_count = len(parser.get_instruction_names())
        event_count = len(parser.get_event_names())
        logger.info(
            f"IDL parser loaded for {platform.value} with {instruction_count} instructions and {event_count} events"
        )

        return parser

    def has_idl_support(self, platform: Platform) -> bool:
        """Check if a platform has IDL support configured.

        Args:
            platform: Platform to check

        Returns:
            True if platform has IDL support
        """
        return platform in self._idl_paths

    def get_supported_platforms(self) -> list[Platform]:
        """Get list of platforms with IDL support.

        Returns:
            List of platforms that have IDL files configured
        """
        return list(self._idl_paths.keys())

    def clear_cache(self, platform: Platform | None = None) -> None:
        """Clear cached parsers.

        Args:
            platform: Specific platform to clear, or None to clear all
        """
        if platform is None:
            logger.info("Clearing all cached IDL parsers")
            self._parsers.clear()
        elif platform in self._parsers:
            logger.info(f"Clearing cached IDL parser for {platform.value}")
            del self._parsers[platform]

    def preload_parser(self, platform: Platform, verbose: bool = False) -> None:
        """Preload IDL parser for a platform.

        This can be useful for warming up the parser during initialization.

        Args:
            platform: Platform to preload parser for
            verbose: Whether to enable verbose logging in the parser
        """
        if platform not in self._parsers:
            logger.info(f"Preloading IDL parser for {platform.value}")
            self.get_parser(platform, verbose)
        else:
            logger.debug(f"IDL parser for {platform.value} already loaded")

    # --------------------------------------------------------------------------
    # Instruction-related convenience methods
    # --------------------------------------------------------------------------

    def get_instruction_discriminators(self, platform: Platform) -> dict[str, bytes]:
        """Get instruction discriminators for a platform.

        Args:
            platform: Platform to get discriminators for

        Returns:
            Dictionary mapping instruction names to discriminator bytes
        """
        parser = self.get_parser(platform)
        return parser.get_instruction_discriminators()

    def get_instruction_names(self, platform: Platform) -> list[str]:
        """Get available instruction names for a platform.

        Args:
            platform: Platform to get instruction names for
        """
        parser = self.get_parser(platform)
        return parser.get_instruction_names()

    # --------------------------------------------------------------------------
    # Event-related convenience methods
    # --------------------------------------------------------------------------

    def get_event_discriminators(self, platform: Platform) -> dict[str, bytes]:
        """Get event discriminators for a platform.

        Args:
            platform: Platform to get event discriminators for

        Returns:
            Dictionary mapping event names to discriminator bytes
        """
        parser = self.get_parser(platform)
        return parser.get_event_discriminators()

    def get_event_names(self, platform: Platform) -> list[str]:
        """Get available event names for a platform.

        Args:
            platform: Platform to get event names for
        """
        parser = self.get_parser(platform)
        return parser.get_event_names()

    def decode_event_from_logs(
        self, platform: Platform, logs: list[str], event_name: str | None = None
    ) -> dict | None:
        """Decode event data from transaction logs for a platform.

        Args:
            platform: Platform to use for decoding
            logs: List of log strings from transaction
            event_name: Optional specific event name to look for

        Returns:
            Decoded event data if found, None otherwise
        """
        parser = self.get_parser(platform)
        return parser.find_event_in_logs(logs, event_name)

    def decode_event_data(
        self, platform: Platform, event_data: bytes, event_name: str | None = None
    ) -> dict | None:
        """Decode raw event data for a platform.

        Args:
            platform: Platform to use for decoding
            event_data: Raw event data bytes
            event_name: Optional event name to decode as

        Returns:
            Decoded event data if successful, None otherwise
        """
        parser = self.get_parser(platform)
        return parser.decode_event_data(event_data, event_name)

    # --------------------------------------------------------------------------
    # Platform information methods
    # --------------------------------------------------------------------------

    def get_platform_capabilities(self, platform: Platform) -> dict[str, Any]:
        """Get comprehensive capability information for a platform.

        Args:
            platform: Platform to get capabilities for

        Returns:
            Dictionary with platform capabilities
        """
        if not self.has_idl_support(platform):
            return {
                "platform": platform.value,
                "has_idl_support": False,
                "instructions": [],
                "events": [],
                "instruction_count": 0,
                "event_count": 0,
            }

        try:
            parser = self.get_parser(platform)
            instruction_names = parser.get_instruction_names()
            event_names = parser.get_event_names()

            return {
                "platform": platform.value,
                "has_idl_support": True,
                "instructions": instruction_names,
                "events": event_names,
                "instruction_count": len(instruction_names),
                "event_count": len(event_names),
            }
        except Exception as e:
            logger.exception(f"Failed to get capabilities for {platform.value}")
            return {
                "platform": platform.value,
                "has_idl_support": False,
                "error": str(e),
                "instructions": [],
                "events": [],
                "instruction_count": 0,
                "event_count": 0,
            }


# Global IDL manager instance
_idl_manager: IDLManager | None = None


def get_idl_manager() -> IDLManager:
    """Get the global IDL manager instance.

    Returns:
        Global IDLManager instance
    """
    global _idl_manager
    if _idl_manager is None:
        _idl_manager = IDLManager()
    return _idl_manager


def get_idl_parser(platform: Platform, verbose: bool = False) -> IDLParser:
    """Convenience function to get an IDL parser for a platform.

    Args:
        platform: Platform to get parser for
        verbose: Whether to enable verbose logging in the parser

    Returns:
        IDLParser instance for the platform
    """
    return get_idl_manager().get_parser(platform, verbose)


def has_idl_support(platform: Platform) -> bool:
    """Check if a platform has IDL support.

    Args:
        platform: Platform to check

    Returns:
        True if platform has IDL support
    """
    return get_idl_manager().has_idl_support(platform)


def preload_platform_idl(platform: Platform, verbose: bool = False) -> None:
    """Preload IDL parser for a platform.

    Args:
        platform: Platform to preload parser for
        verbose: Whether to enable verbose logging
    """
    get_idl_manager().preload_parser(platform, verbose)


# --------------------------------------------------------------------------
# Convenience functions for event handling
# --------------------------------------------------------------------------


def get_event_discriminators(platform: Platform) -> dict[str, bytes]:
    """Convenience function to get event discriminators for a platform.

    Args:
        platform: Platform to get event discriminators for

    Returns:
        Dictionary mapping event names to discriminator bytes
    """
    return get_idl_manager().get_event_discriminators(platform)


def get_event_names(platform: Platform) -> list[str]:
    """Convenience function to get event names for a platform.

    Args:
        platform: Platform to get event names for
    """
    return get_idl_manager().get_event_names(platform)


def decode_event_from_logs(
    platform: Platform, logs: list[str], event_name: str | None = None
) -> dict | None:
    """Convenience function to decode events from logs.

    Args:
        platform: Platform to use for decoding
        logs: List of log strings from transaction
        event_name: Optional specific event name to look for

    Returns:
        Decoded event data if found, None otherwise
    """
    return get_idl_manager().decode_event_from_logs(platform, logs, event_name)
