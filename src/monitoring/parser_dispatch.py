"""Dispatch normalized monitoring events to existing platform parsers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from interfaces.core import Platform, TokenInfo
from monitoring.event_normalization import (
    NormalizationError,
    NormalizedTransactionEvent,
    account_key_bytes,
    attach_event_context,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _accept_token(
    token_info: TokenInfo,
    *,
    platform: Platform,
    event: NormalizedTransactionEvent,
    instruction: Any = None,
) -> TokenInfo | None:
    if token_info.platform != platform:
        logger.error(
            "Parser for %s returned token for %s at signature %s",
            platform.value,
            getattr(token_info.platform, "value", token_info.platform),
            event.signature,
        )
        return None
    return attach_event_context(token_info, event, instruction)


def _same_creation(left: TokenInfo, right: TokenInfo) -> bool:
    """Require the log event and instruction to describe one creation."""
    for field in (
        "mint",
        "bonding_curve",
        "associated_bonding_curve",
        "pool_state",
        "base_vault",
        "quote_vault",
        "user",
        "token_program_id",
    ):
        if getattr(left, field, None) != getattr(right, field, None):
            return False
    for field in ("quote_mint", "quote_token_program_id"):
        left_value = getattr(left, field, None)
        right_value = getattr(right, field, None)
        if (
            left_value is not None
            and right_value is not None
            and left_value != right_value
        ):
            return False
    return True


def _downgrade_unverified_log_token(token_info: TokenInfo) -> TokenInfo:
    """Prevent an uncorrelated logs-only observation from taking zero-RPC state."""
    token_info.state_from_event = False
    token_info.metadata_verified = False
    token_info.quote_mint = None
    token_info.quote_token_program_id = None
    token_info.virtual_quote_reserves = None
    return token_info


def _downgrade_instruction_only_token(token_info: TokenInfo) -> TokenInfo:
    """Keep instruction-only creations from inheriting log-event verification."""
    token_info.state_from_event = False
    token_info.metadata_verified = False
    return token_info


def parse_normalized_event(
    event: NormalizedTransactionEvent,
    platform_parsers: dict[Platform, Any],
) -> list[TokenInfo]:
    """Return only creations with unambiguous instruction/event provenance."""
    if event.transaction_error is not None:
        logger.warning(
            "Rejected failed %s transaction %s: %r",
            event.source,
            event.signature,
            event.transaction_error,
        )
        return []

    tokens: list[TokenInfo] = []
    for platform, parser in platform_parsers.items():
        platform_event = replace(event, platform=platform)
        log_token: TokenInfo | None = None
        if platform_event.logs:
            try:
                log_token = parser.parse_token_creation_from_logs(
                    list(platform_event.logs), platform_event.signature
                )
            except Exception:
                logger.exception(
                    "Parser error for %s %s logs at signature %s",
                    platform.value,
                    event.source,
                    event.signature,
                )

        try:
            parser_program_id = str(parser.get_program_id())
        except Exception:
            logger.exception("Could not read program ID for %s parser", platform.value)
            continue

        matching = [
            instruction
            for instruction in platform_event.instructions
            if instruction.program_id == parser_program_id
        ]
        instruction_tokens: list[tuple[TokenInfo, Any]] = []
        if matching:
            try:
                keys = account_key_bytes(platform_event)
            except NormalizationError:
                logger.exception(
                    "Rejected invalid account metadata for %s transaction %s",
                    platform_event.source,
                    platform_event.signature,
                )
                continue

            seen_instruction_mints: set[str] = set()
            for instruction in matching:
                if instruction.data is None:
                    logger.error(
                        "Rejected parsed-only %s instruction at %s tx=%s ix=%s inner=%s",
                        platform.value,
                        platform_event.source,
                        platform_event.transaction_index,
                        instruction.instruction_index,
                        instruction.inner_index,
                    )
                    continue
                try:
                    parsed_token = parser.parse_token_creation_from_instruction(
                        instruction.data,
                        list(instruction.accounts),
                        keys,
                    )
                except Exception:
                    logger.exception(
                        "Parser error for %s %s instruction at signature %s "
                        "tx=%s ix=%s inner=%s",
                        platform.value,
                        platform_event.source,
                        platform_event.signature,
                        platform_event.transaction_index,
                        instruction.instruction_index,
                        instruction.inner_index,
                    )
                    continue
                if parsed_token is None:
                    continue
                mint = str(parsed_token.mint)
                if mint in seen_instruction_mints:
                    logger.error(
                        "Rejected ambiguous duplicate %s creation for mint %s at %s",
                        platform.value,
                        mint,
                        platform_event.signature,
                    )
                    instruction_tokens = []
                    break
                seen_instruction_mints.add(mint)
                instruction_tokens.append((parsed_token, instruction))

        if log_token is not None:
            if matching:
                candidates = [
                    item
                    for item in instruction_tokens
                    if _same_creation(item[0], log_token)
                ]
                if len(candidates) != 1:
                    logger.error(
                        "Rejected uncorrelated or ambiguous %s log creation at %s",
                        platform.value,
                        platform_event.signature,
                    )
                    continue
                correlated_instruction = candidates[0][1]
                accepted = _accept_token(
                    log_token,
                    platform=platform,
                    event=platform_event,
                    instruction=correlated_instruction,
                )
                if accepted is None:
                    continue
                tokens.append(accepted)
                for parsed_token, instruction in instruction_tokens:
                    if instruction is correlated_instruction:
                        continue
                    accepted = _accept_token(
                        _downgrade_instruction_only_token(parsed_token),
                        platform=platform,
                        event=platform_event,
                        instruction=instruction,
                    )
                    if accepted is not None:
                        tokens.append(accepted)
                continue
            if platform_event.instructions:
                logger.error(
                    "Rejected %s log creation without a matching program instruction "
                    "at %s",
                    platform.value,
                    platform_event.signature,
                )
                continue
            log_token = _downgrade_unverified_log_token(log_token)
            accepted = _accept_token(
                log_token,
                platform=platform,
                event=platform_event,
            )
            if accepted is not None:
                tokens.append(accepted)
            continue

        for parsed_token, instruction in instruction_tokens:
            accepted = _accept_token(
                parsed_token,
                platform=platform,
                event=platform_event,
                instruction=instruction,
            )
            if accepted is not None:
                tokens.append(accepted)

    return tokens
