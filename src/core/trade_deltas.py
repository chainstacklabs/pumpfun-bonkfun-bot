"""What a landed buy or sell did to the wallet, read from its transaction.

One reader for both sides: the wallet's own balance changes, so every venue,
quote asset and token program reads the same way. A Token-2022 transfer fee is
withheld outside the wallet, so the token change is what the wallet really got
or gave. Only a transaction the wallet paid for is read, as the bot's own are:
rent in its token accounts is then rent it put in or took back.
"""

from dataclasses import dataclass

from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT, normalize_quote_mint

_WSOL = str(WSOL_MINT)


@dataclass(frozen=True)
class TradeDeltas:
    """A trade's effect on the wallet, in raw units; positive means received."""

    #: The coin: positive for a buy, negative for a sell.
    tokens_raw: int
    #: The quote asset, protocol fees included; network fee and the rent of
    #: the wallet's own token accounts left out. For SOL it also holds any
    #: other lamports the trade moved, such as rent for an account the wallet
    #: does not own. None: no account of the wallet's holds the quote mint.
    quote_raw: int | None
    #: The transaction fee, priority fee included.
    network_fee_lamports: int
    #: Rent put into token accounts the wallet owns; it comes back when the
    #: account closes, so it is no trading loss.
    rent_deposited_lamports: int = 0
    #: Rent that came back from token accounts the wallet owns and closed.
    rent_refunded_lamports: int = 0
    #: For a non-SOL quote: other lamports the trade moved, such as rent for an
    #: account another program owns. 0 for a SOL quote, where `quote_raw` has it.
    other_lamports: int = 0
    #: The quote the venue took in: the buy's fill, protocol fees left out.
    #: SOL quote: needs `venue_account`. Other quotes: the largest gain of the
    #: quote mint in any account. None when not found.
    venue_quote_raw: int | None = None
    #: The slot the transaction landed in; None when the result does not say.
    slot: int | None = None


def _account_keys(result: dict) -> list[str]:
    """Every account the transaction loaded, in balance-index order."""
    message = result.get("transaction", {}).get("message", {})
    raw = message.get("accountKeys", [])
    keys = [k if isinstance(k, str) else k.get("pubkey", "") for k in raw]
    if raw and isinstance(raw[0], str):
        # Plain `json` encoding lists lookup-table accounts apart; jsonParsed
        # already has them in `accountKeys`.
        loaded = result.get("meta", {}).get("loadedAddresses") or {}
        keys += loaded.get("writable", []) + loaded.get("readonly", [])
    return keys


def _amount(balance: dict | None) -> int:
    if balance is None:
        return 0
    return int(balance.get("uiTokenAmount", {}).get("amount", 0))


def paid_by(result: dict, wallet: Pubkey) -> bool:
    """Whether `wallet` paid for the transaction: it is the first account."""
    keys = _account_keys(result)
    return bool(keys) and keys[0] == str(wallet)


def _lamports(balances: list, index: int) -> int:
    return balances[index] if index < len(balances) else 0


def _rent(balances: list, index: int, balance: dict | None, *, wsol: bool) -> int:
    """Rent a token account holds: its lamports beyond any wrapped SOL; 0 if absent."""
    return _lamports(balances, index) - (_amount(balance) if wsol else 0)


def _lamports_gained(meta: dict, keys: list[str], account: str) -> int | None:
    """What `account` gained in lamports; None if it is absent or gained nothing."""
    if account not in keys:
        return None
    at = keys.index(account)
    gained = _lamports(meta.get("postBalances", []), at) - _lamports(
        meta.get("preBalances", []), at
    )
    return gained if gained > 0 else None


def _walk_token_accounts(
    meta: dict, wallet: str, mint: str, quote: str
) -> tuple[int | None, int | None, int | None, int, int, int]:
    """The wallet's token-account changes in one transaction.

    Returns:
        The coin's change, the quote mint's change (None where the wallet holds
        no such account), the largest gain of the quote mint in any account,
        the wallet's wrapped SOL change, and rent put into and taken back from
        the wallet's token accounts
    """
    pre_lamports = meta.get("preBalances", [])
    post_lamports = meta.get("postBalances", [])
    sides: dict[int, list] = {}
    for side, balances in (
        (0, meta.get("preTokenBalances") or []),
        (1, meta.get("postTokenBalances") or []),
    ):
        for balance in balances:
            sides.setdefault(balance.get("accountIndex"), [None, None])[side] = balance

    tokens = quote_change = largest_quote_gain = None
    wsol_change = deposited = refunded = 0
    for index, (pre, post) in sides.items():
        info = post or pre
        change = _amount(post) - _amount(pre)
        if info.get("mint") == quote and change > 0:
            largest_quote_gain = max(largest_quote_gain or 0, change)
        if info.get("owner") != wallet:
            continue
        if info.get("mint") == mint:
            tokens = (tokens or 0) + change
        if info.get("mint") == quote:
            quote_change = (quote_change or 0) + change
        wsol = info.get("mint") == _WSOL
        wsol_change += change if wsol else 0
        rent_change = _rent(post_lamports, index, post, wsol=wsol) - _rent(
            pre_lamports, index, pre, wsol=wsol
        )
        deposited += max(rent_change, 0)
        refunded += max(-rent_change, 0)
    return tokens, quote_change, largest_quote_gain, wsol_change, deposited, refunded


def read_trade_deltas(
    result: dict,
    wallet: Pubkey,
    mint: Pubkey,
    quote_mint: Pubkey | None,
    venue_account: Pubkey | str | None = None,
) -> TradeDeltas | None:
    """The wallet's side of a landed trade.

    Args:
        result: A `getTransaction` result (jsonParsed or json encoding)
        quote_mint: Raw or normalized
        venue_account: Where a buy's SOL lands (curve, or the pool's quote
            vault), to read the fill price off

    Returns:
        The deltas, or None when the transaction reverted, the wallet did not
        pay for it, or it shows no token account of the wallet's holding the coin
    """
    meta = result.get("meta") or {}
    if meta.get("err"):
        return None
    keys = _account_keys(result)
    wallet, mint = str(wallet), str(mint)
    quote = str(normalize_quote_mint(quote_mint))
    sol_quote = quote == _WSOL
    pre_lamports = meta.get("preBalances", [])
    post_lamports = meta.get("postBalances", [])

    walk = _walk_token_accounts(meta, wallet, mint, quote)
    tokens, quote_change, largest_quote_gain = walk[:3]
    wsol_change, deposited, refunded = walk[3:]

    # The fee payer is the first account.
    if tokens is None or not keys or keys[0] != wallet:
        return None
    at, fee = 0, meta.get("fee", 0)
    # Every lamport the wallet gained or lost that is neither the fee nor
    # rent parked in its own token accounts, wrapped SOL counted as SOL.
    moved = (
        _lamports(post_lamports, at)
        - _lamports(pre_lamports, at)
        + fee
        + deposited
        - refunded
        + wsol_change
    )

    if not sol_quote:
        # A non-SOL quote lands in a token vault: the largest gain of the
        # quote mint is the venue's.
        venue_quote = largest_quote_gain
    elif venue_account is not None:
        venue_quote = _lamports_gained(meta, keys, str(venue_account))
    else:
        venue_quote = None

    return TradeDeltas(
        tokens_raw=tokens,
        quote_raw=moved if sol_quote else quote_change,
        network_fee_lamports=fee,
        rent_deposited_lamports=deposited,
        rent_refunded_lamports=refunded,
        other_lamports=0 if sol_quote else moved,
        venue_quote_raw=venue_quote,
        slot=result.get("slot"),
    )
