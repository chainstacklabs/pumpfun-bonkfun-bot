"""Factory for creating platform-aware token listeners."""

from interfaces.core import Platform
from monitoring.base_listener import BaseTokenListener
from utils.logger import get_logger

logger = get_logger(__name__)


class ListenerFactory:
    """Factory for creating appropriate token listeners based on configuration."""

    @staticmethod
    def create_listener(
        listener_type: str,
        wss_endpoint: str | None = None,
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        platforms: list[Platform] | None = None,
    ) -> BaseTokenListener:
        """Create a token listener based on the specified type.

        Args:
            listener_type: Type of listener ('logs', 'blocks', 'geyser',
                'shreds' or 'pumpportal')
            wss_endpoint: WebSocket endpoint URL (for logs/blocks listeners)
            geyser_endpoint: Geyser gRPC endpoint URL (geyser/shreds listeners)
            geyser_api_token: Geyser API token (geyser/shreds listeners)
            pumpportal_url: PumpPortal WebSocket URL (for pumpportal listener)
            platforms: List of platforms to monitor (if None, monitor all)

        Returns:
            Configured token listener

        Raises:
            ValueError: If listener type is invalid or required parameters are missing
        """
        listener_type = listener_type.lower()

        if listener_type == "geyser":
            if not geyser_endpoint or not geyser_api_token:
                raise ValueError(
                    "Geyser endpoint and API token are required for geyser listener"
                )

            from monitoring.universal_geyser_listener import UniversalGeyserListener

            listener = UniversalGeyserListener(
                geyser_endpoint=geyser_endpoint,
                geyser_api_token=geyser_api_token,
                geyser_auth_type=geyser_auth_type,
                platforms=platforms,
            )
            logger.info("Created Universal Geyser listener for token monitoring")
            return listener

        elif listener_type == "shreds":
            if not geyser_endpoint or not geyser_api_token:
                raise ValueError(
                    "Geyser endpoint and API token are required for shreds listener"
                )

            from monitoring.universal_shreds_listener import UniversalShredsListener

            listener = UniversalShredsListener(
                geyser_endpoint=geyser_endpoint,
                geyser_api_token=geyser_api_token,
                geyser_auth_type=geyser_auth_type,
                platforms=platforms,
            )
            logger.info("Created Universal Shreds listener for token monitoring")
            return listener

        elif listener_type == "logs":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint is required for logs listener")

            from monitoring.universal_logs_listener import UniversalLogsListener

            listener = UniversalLogsListener(
                wss_endpoint=wss_endpoint,
                platforms=platforms,
            )
            logger.info("Created Universal Logs listener for token monitoring")
            return listener

        elif listener_type == "blocks":
            if not wss_endpoint:
                raise ValueError("WebSocket endpoint is required for blocks listener")

            from monitoring.universal_block_listener import UniversalBlockListener

            listener = UniversalBlockListener(
                wss_endpoint=wss_endpoint,
                platforms=platforms,
            )
            logger.info("Created Universal Block listener for token monitoring")
            return listener

        elif listener_type == "pumpportal":
            from monitoring.universal_pumpportal_listener import (
                UniversalPumpPortalListener,
            )

            # Validate that requested platforms support PumpPortal
            supported_pumpportal_platforms = [Platform.PUMP_FUN, Platform.LETS_BONK]

            if platforms:
                unsupported = [
                    p for p in platforms if p not in supported_pumpportal_platforms
                ]
                if unsupported:
                    logger.warning(
                        f"Platforms {[p.value for p in unsupported]} do not support PumpPortal"
                    )

                # Filter to only supported platforms
                filtered_platforms = [
                    p for p in platforms if p in supported_pumpportal_platforms
                ]
                if not filtered_platforms:
                    raise ValueError(
                        "No supported platforms specified for PumpPortal listener"
                    )
                platforms = filtered_platforms

            listener = UniversalPumpPortalListener(
                pumpportal_url=pumpportal_url,
                platforms=platforms,
            )
            logger.info(
                f"Created Universal PumpPortal listener for platforms: {[p.value for p in (platforms or supported_pumpportal_platforms)]}"
            )
            return listener

        else:
            raise ValueError(
                f"Invalid listener type '{listener_type}'. "
                f"Must be one of: 'logs', 'blocks', 'geyser', 'shreds', "
                f"'pumpportal'"
            )

    @staticmethod
    def get_supported_listener_types() -> list[str]:
        """Get list of supported listener types.

        Returns:
            List of supported listener type strings
        """
        return ["logs", "blocks", "geyser", "shreds", "pumpportal"]

    @staticmethod
    def get_platform_compatible_listeners(platform: Platform) -> list[str]:
        """Get list of listener types compatible with a specific platform.

        Args:
            platform: Platform to check compatibility for
        """
        if platform == Platform.PUMP_FUN:
            return ["logs", "blocks", "geyser", "shreds", "pumpportal"]
        elif platform == Platform.LETS_BONK:
            return ["blocks", "geyser", "pumpportal"]  # Added pumpportal support
        else:
            return ["blocks", "geyser"]  # Default universal listeners

    @staticmethod
    def get_pumpportal_supported_platforms() -> list[Platform]:
        """Get list of platforms that support PumpPortal listener."""
        return [Platform.PUMP_FUN, Platform.LETS_BONK]
