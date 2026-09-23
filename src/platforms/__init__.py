"""Factory and registry for platform-specific implementations of the interfaces."""

from dataclasses import dataclass
from typing import Any

from core.client import SolanaClient
from interfaces.core import (
    AddressProvider,
    CurveManager,
    EventParser,
    InstructionBuilder,
    Platform,
)
from utils.idl_manager import get_idl_manager, has_idl_support
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PlatformImplementations:
    """Container for all platform-specific implementations."""

    address_provider: AddressProvider
    instruction_builder: InstructionBuilder
    curve_manager: CurveManager
    event_parser: EventParser


class PlatformRegistry:
    """Registry for platform implementations."""

    def __init__(self):
        self._implementations: dict[Platform, dict[str, type]] = {}
        self._instances: dict[tuple[Platform, str], PlatformImplementations] = {}

    def register_platform(
        self,
        platform: Platform,
        address_provider_class: type[AddressProvider],
        instruction_builder_class: type[InstructionBuilder],
        curve_manager_class: type[CurveManager],
        event_parser_class: type[EventParser],
    ) -> None:
        """Register platform implementations.

        Args:
            platform: Platform enum value
        """
        self._implementations[platform] = {
            "address_provider": address_provider_class,
            "instruction_builder": instruction_builder_class,
            "curve_manager": curve_manager_class,
            "event_parser": event_parser_class,
        }

    def create_platform_implementations(
        self, platform: Platform, client: SolanaClient, **kwargs: Any
    ) -> PlatformImplementations:
        """Create platform implementation instances with IDL support.

        Args:
            platform: Platform to create implementations for
            client: Solana RPC client
            **kwargs: Additional arguments for implementation constructors

        Returns:
            PlatformImplementations containing all interface implementations

        Raises:
            ValueError: If platform is not registered
        """
        if platform not in self._implementations:
            raise ValueError(f"Platform {platform} is not registered")

        # Use client address as cache key to allow multiple clients
        cache_key = (platform, str(client.rpc_endpoint))

        # Check if we already have instances for this platform + client combo
        if cache_key in self._instances:
            return self._instances[cache_key]

        impl_classes = self._implementations[platform]

        # Check if platform has IDL support and prepare IDL parser
        idl_parser = None
        if has_idl_support(platform):
            try:
                idl_manager = get_idl_manager()
                idl_parser = idl_manager.get_parser(
                    platform, verbose=kwargs.get("verbose_idl", False)
                )
                logger.info(
                    f"IDL parser loaded for {platform.value} platform implementations"
                )
            except Exception as e:
                logger.warning(f"Failed to load IDL parser for {platform.value}: {e}")

        # Create instances - pass IDL parser to classes that need it
        address_provider = impl_classes["address_provider"]()

        # For platforms with IDL support, pass the parser to relevant classes
        if idl_parser and platform in [Platform.LETS_BONK, Platform.PUMP_FUN]:
            instruction_builder = impl_classes["instruction_builder"](
                idl_parser=idl_parser
            )
            curve_manager = impl_classes["curve_manager"](client, idl_parser=idl_parser)
            event_parser = impl_classes["event_parser"](idl_parser=idl_parser)
        else:
            # Fallback for platforms without IDL support
            instruction_builder = impl_classes["instruction_builder"]()
            curve_manager = impl_classes["curve_manager"](client)
            event_parser = impl_classes["event_parser"]()

        implementations = PlatformImplementations(
            address_provider=address_provider,
            instruction_builder=instruction_builder,
            curve_manager=curve_manager,
            event_parser=event_parser,
        )

        self._instances[cache_key] = implementations

        return implementations

    def get_platform_implementations(
        self, platform: Platform, client_endpoint: str
    ) -> PlatformImplementations | None:
        """Get cached platform implementations.

        Args:
            platform: Platform to get implementations for
            client_endpoint: Client endpoint for cache lookup

        Returns:
            PlatformImplementations if available, None otherwise
        """
        cache_key = (platform, client_endpoint)
        return self._instances.get(cache_key)

    def get_supported_platforms(self) -> list[Platform]:
        """Get list of supported platforms.

        Returns:
            List of registered platforms
        """
        return list(self._implementations.keys())

    def is_platform_supported(self, platform: Platform) -> bool:
        """Check if a platform is supported.

        Args:
            platform: Platform to check

        Returns:
            True if platform is registered, False otherwise
        """
        return platform in self._implementations

    def clear_implementation_cache(self, platform: Platform | None = None) -> None:
        """Clear cached platform implementations.

        Args:
            platform: Specific platform to clear, or None to clear all
        """
        if platform is None:
            logger.info("Clearing all cached platform implementations")
            self._instances.clear()
        else:
            keys_to_remove = [
                key for key in self._instances.keys() if key[0] == platform
            ]
            for key in keys_to_remove:
                del self._instances[key]
            logger.info(f"Cleared cached implementations for {platform.value}")


class PlatformFactory:
    """Factory for creating platform-specific implementations with IDL support."""

    def __init__(self):
        self.registry = PlatformRegistry()
        self._setup_default_platforms()

    def _setup_default_platforms(self) -> None:
        """Setup default platform registrations."""
        # Import and register pump.fun platform
        try:
            from platforms.pumpfun import (
                PumpFunAddressProvider,
                PumpFunCurveManager,
                PumpFunEventParser,
                PumpFunInstructionBuilder,
            )

            self.registry.register_platform(
                Platform.PUMP_FUN,
                PumpFunAddressProvider,
                PumpFunInstructionBuilder,
                PumpFunCurveManager,
                PumpFunEventParser,
            )

        except ImportError as e:
            print(f"Warning: Could not register pump.fun platform: {e}")

        # Import and register LetsBonk platform
        try:
            from platforms.letsbonk import (
                LetsBonkAddressProvider,
                LetsBonkCurveManager,
                LetsBonkEventParser,
                LetsBonkInstructionBuilder,
            )

            self.registry.register_platform(
                Platform.LETS_BONK,
                LetsBonkAddressProvider,
                LetsBonkInstructionBuilder,
                LetsBonkCurveManager,
                LetsBonkEventParser,
            )

        except ImportError as e:
            print(f"Warning: Could not register LetsBonk platform: {e}")

    def create_for_platform(
        self, platform: Platform, client: SolanaClient, **config: Any
    ) -> PlatformImplementations:
        """Create all implementations for a specific platform.

        Args:
            platform: Platform to create implementations for
            client: Solana RPC client
            **config: Platform-specific configuration (including verbose_idl)

        Returns:
            PlatformImplementations containing all interface implementations
        """
        return self.registry.create_platform_implementations(platform, client, **config)

    def get_address_provider(
        self, platform: Platform, client: SolanaClient
    ) -> AddressProvider:
        """Get address provider for platform.

        Args:
            platform: Platform to get provider for
            client: Solana RPC client
        """
        implementations = self.registry.create_platform_implementations(
            platform, client
        )
        return implementations.address_provider

    def get_instruction_builder(
        self, platform: Platform, client: SolanaClient
    ) -> InstructionBuilder:
        """Get instruction builder for platform.

        Args:
            platform: Platform to get builder for
            client: Solana RPC client
        """
        implementations = self.registry.create_platform_implementations(
            platform, client
        )
        return implementations.instruction_builder

    def get_curve_manager(
        self, platform: Platform, client: SolanaClient
    ) -> CurveManager:
        """Get curve manager for platform.

        Args:
            platform: Platform to get manager for
            client: Solana RPC client
        """
        implementations = self.registry.create_platform_implementations(
            platform, client
        )
        return implementations.curve_manager

    def get_event_parser(self, platform: Platform, client: SolanaClient) -> EventParser:
        """Get event parser for platform.

        Args:
            platform: Platform to get parser for
            client: Solana RPC client
        """
        implementations = self.registry.create_platform_implementations(
            platform, client
        )
        return implementations.event_parser

    def get_supported_platforms(self) -> list[Platform]:
        """Get list of supported platforms."""
        return self.registry.get_supported_platforms()

    def clear_caches(self, platform: Platform | None = None) -> None:
        """Clear all caches for better memory management.

        Args:
            platform: Specific platform to clear, or None to clear all
        """
        self.registry.clear_implementation_cache(platform)

        # Clear IDL parser cache
        idl_manager = get_idl_manager()
        idl_manager.clear_cache(platform)


# Global factory instance
platform_factory = PlatformFactory()


def get_platform_implementations(
    platform: Platform, client: SolanaClient
) -> PlatformImplementations:
    """Convenience function to get platform implementations.

    Args:
        platform: Platform to get implementations for
        client: Solana RPC client

    Returns:
        PlatformImplementations containing all interface implementations
    """
    return platform_factory.create_for_platform(platform, client)


def register_platform_implementations(
    platform: Platform,
    address_provider_class: type[AddressProvider],
    instruction_builder_class: type[InstructionBuilder],
    curve_manager_class: type[CurveManager],
    event_parser_class: type[EventParser],
) -> None:
    """Register platform implementations with the global factory.

    Args:
        platform: Platform enum value
    """
    platform_factory.registry.register_platform(
        platform,
        address_provider_class,
        instruction_builder_class,
        curve_manager_class,
        event_parser_class,
    )
