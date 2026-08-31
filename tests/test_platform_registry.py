from __future__ import annotations

from interfaces.core import Platform
from platforms import PlatformRegistry


def test_registry_cache_key_includes_client_identity_and_options() -> None:
    registry = PlatformRegistry()
    first = object()
    second = object()

    class Client:
        rpc_endpoint = "https://rpc"

    client_one = Client()
    client_two = Client()
    options = (("verbose_idl", False),)
    registry._instances[
        (Platform.PUMP_FUN, client_one.rpc_endpoint, id(client_one), options)
    ] = first  # type: ignore[assignment]
    registry._instances[
        (Platform.PUMP_FUN, client_two.rpc_endpoint, id(client_two), options)
    ] = second  # type: ignore[assignment]

    assert (
        registry.get_platform_implementations(
            Platform.PUMP_FUN,
            "https://rpc",
            client=client_one,  # type: ignore[arg-type]
            verbose_idl=False,
        )
        is first
    )
    assert (
        registry.get_platform_implementations(
            Platform.PUMP_FUN,
            "https://rpc",
            client=client_two,  # type: ignore[arg-type]
            verbose_idl=False,
        )
        is second
    )
    assert (
        registry.get_platform_implementations(
            Platform.PUMP_FUN,
            "https://rpc",
            verbose_idl=False,
        )
        is None
    )
