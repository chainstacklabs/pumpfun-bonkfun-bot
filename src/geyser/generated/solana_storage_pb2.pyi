from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RewardType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    Unspecified: _ClassVar[RewardType]
    Fee: _ClassVar[RewardType]
    Rent: _ClassVar[RewardType]
    Staking: _ClassVar[RewardType]
    Voting: _ClassVar[RewardType]
    DeactivatedStake: _ClassVar[RewardType]
    VATDebit: _ClassVar[RewardType]
Unspecified: RewardType
Fee: RewardType
Rent: RewardType
Staking: RewardType
Voting: RewardType
DeactivatedStake: RewardType
VATDebit: RewardType

class ConfirmedBlock(_message.Message):
    __slots__ = ("previous_blockhash", "blockhash", "parent_slot", "transactions", "rewards", "block_time", "block_height", "num_partitions")
    PREVIOUS_BLOCKHASH_FIELD_NUMBER: _ClassVar[int]
    BLOCKHASH_FIELD_NUMBER: _ClassVar[int]
    PARENT_SLOT_FIELD_NUMBER: _ClassVar[int]
    TRANSACTIONS_FIELD_NUMBER: _ClassVar[int]
    REWARDS_FIELD_NUMBER: _ClassVar[int]
    BLOCK_TIME_FIELD_NUMBER: _ClassVar[int]
    BLOCK_HEIGHT_FIELD_NUMBER: _ClassVar[int]
    NUM_PARTITIONS_FIELD_NUMBER: _ClassVar[int]
    previous_blockhash: str
    blockhash: str
    parent_slot: int
    transactions: _containers.RepeatedCompositeFieldContainer[ConfirmedTransaction]
    rewards: _containers.RepeatedCompositeFieldContainer[Reward]
    block_time: UnixTimestamp
    block_height: BlockHeight
    num_partitions: NumPartitions
    def __init__(self, previous_blockhash: _Optional[str] = ..., blockhash: _Optional[str] = ..., parent_slot: _Optional[int] = ..., transactions: _Optional[_Iterable[_Union[ConfirmedTransaction, _Mapping]]] = ..., rewards: _Optional[_Iterable[_Union[Reward, _Mapping]]] = ..., block_time: _Optional[_Union[UnixTimestamp, _Mapping]] = ..., block_height: _Optional[_Union[BlockHeight, _Mapping]] = ..., num_partitions: _Optional[_Union[NumPartitions, _Mapping]] = ...) -> None: ...

class ConfirmedTransaction(_message.Message):
    __slots__ = ("transaction", "meta")
    TRANSACTION_FIELD_NUMBER: _ClassVar[int]
    META_FIELD_NUMBER: _ClassVar[int]
    transaction: Transaction
    meta: TransactionStatusMeta
    def __init__(self, transaction: _Optional[_Union[Transaction, _Mapping]] = ..., meta: _Optional[_Union[TransactionStatusMeta, _Mapping]] = ...) -> None: ...

class Transaction(_message.Message):
    __slots__ = ("signatures", "message")
    SIGNATURES_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    signatures: _containers.RepeatedScalarFieldContainer[bytes]
    message: Message
    def __init__(self, signatures: _Optional[_Iterable[bytes]] = ..., message: _Optional[_Union[Message, _Mapping]] = ...) -> None: ...

class Message(_message.Message):
    __slots__ = ("header", "account_keys", "recent_blockhash", "instructions", "versioned", "address_table_lookups", "config")
    HEADER_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_KEYS_FIELD_NUMBER: _ClassVar[int]
    RECENT_BLOCKHASH_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    VERSIONED_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_TABLE_LOOKUPS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    header: MessageHeader
    account_keys: _containers.RepeatedScalarFieldContainer[bytes]
    recent_blockhash: bytes
    instructions: _containers.RepeatedCompositeFieldContainer[CompiledInstruction]
    versioned: bool
    address_table_lookups: _containers.RepeatedCompositeFieldContainer[MessageAddressTableLookup]
    config: TransactionConfig
    def __init__(self, header: _Optional[_Union[MessageHeader, _Mapping]] = ..., account_keys: _Optional[_Iterable[bytes]] = ..., recent_blockhash: _Optional[bytes] = ..., instructions: _Optional[_Iterable[_Union[CompiledInstruction, _Mapping]]] = ..., versioned: _Optional[bool] = ..., address_table_lookups: _Optional[_Iterable[_Union[MessageAddressTableLookup, _Mapping]]] = ..., config: _Optional[_Union[TransactionConfig, _Mapping]] = ...) -> None: ...

class TransactionConfig(_message.Message):
    __slots__ = ("priority_fee", "compute_unit_limit", "loaded_accounts_data_size_limit", "heap_size")
    PRIORITY_FEE_FIELD_NUMBER: _ClassVar[int]
    COMPUTE_UNIT_LIMIT_FIELD_NUMBER: _ClassVar[int]
    LOADED_ACCOUNTS_DATA_SIZE_LIMIT_FIELD_NUMBER: _ClassVar[int]
    HEAP_SIZE_FIELD_NUMBER: _ClassVar[int]
    priority_fee: int
    compute_unit_limit: int
    loaded_accounts_data_size_limit: int
    heap_size: int
    def __init__(self, priority_fee: _Optional[int] = ..., compute_unit_limit: _Optional[int] = ..., loaded_accounts_data_size_limit: _Optional[int] = ..., heap_size: _Optional[int] = ...) -> None: ...

class MessageHeader(_message.Message):
    __slots__ = ("num_required_signatures", "num_readonly_signed_accounts", "num_readonly_unsigned_accounts")
    NUM_REQUIRED_SIGNATURES_FIELD_NUMBER: _ClassVar[int]
    NUM_READONLY_SIGNED_ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    NUM_READONLY_UNSIGNED_ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    num_required_signatures: int
    num_readonly_signed_accounts: int
    num_readonly_unsigned_accounts: int
    def __init__(self, num_required_signatures: _Optional[int] = ..., num_readonly_signed_accounts: _Optional[int] = ..., num_readonly_unsigned_accounts: _Optional[int] = ...) -> None: ...

class MessageAddressTableLookup(_message.Message):
    __slots__ = ("account_key", "writable_indexes", "readonly_indexes")
    ACCOUNT_KEY_FIELD_NUMBER: _ClassVar[int]
    WRITABLE_INDEXES_FIELD_NUMBER: _ClassVar[int]
    READONLY_INDEXES_FIELD_NUMBER: _ClassVar[int]
    account_key: bytes
    writable_indexes: bytes
    readonly_indexes: bytes
    def __init__(self, account_key: _Optional[bytes] = ..., writable_indexes: _Optional[bytes] = ..., readonly_indexes: _Optional[bytes] = ...) -> None: ...

class TransactionStatusMeta(_message.Message):
    __slots__ = ("err", "fee", "pre_balances", "post_balances", "inner_instructions", "inner_instructions_none", "log_messages", "log_messages_none", "pre_token_balances", "post_token_balances", "rewards", "loaded_writable_addresses", "loaded_readonly_addresses", "return_data", "return_data_none", "compute_units_consumed", "cost_units")
    ERR_FIELD_NUMBER: _ClassVar[int]
    FEE_FIELD_NUMBER: _ClassVar[int]
    PRE_BALANCES_FIELD_NUMBER: _ClassVar[int]
    POST_BALANCES_FIELD_NUMBER: _ClassVar[int]
    INNER_INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    INNER_INSTRUCTIONS_NONE_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGES_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGES_NONE_FIELD_NUMBER: _ClassVar[int]
    PRE_TOKEN_BALANCES_FIELD_NUMBER: _ClassVar[int]
    POST_TOKEN_BALANCES_FIELD_NUMBER: _ClassVar[int]
    REWARDS_FIELD_NUMBER: _ClassVar[int]
    LOADED_WRITABLE_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    LOADED_READONLY_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    RETURN_DATA_FIELD_NUMBER: _ClassVar[int]
    RETURN_DATA_NONE_FIELD_NUMBER: _ClassVar[int]
    COMPUTE_UNITS_CONSUMED_FIELD_NUMBER: _ClassVar[int]
    COST_UNITS_FIELD_NUMBER: _ClassVar[int]
    err: TransactionError
    fee: int
    pre_balances: _containers.RepeatedScalarFieldContainer[int]
    post_balances: _containers.RepeatedScalarFieldContainer[int]
    inner_instructions: _containers.RepeatedCompositeFieldContainer[InnerInstructions]
    inner_instructions_none: bool
    log_messages: _containers.RepeatedScalarFieldContainer[str]
    log_messages_none: bool
    pre_token_balances: _containers.RepeatedCompositeFieldContainer[TokenBalance]
    post_token_balances: _containers.RepeatedCompositeFieldContainer[TokenBalance]
    rewards: _containers.RepeatedCompositeFieldContainer[Reward]
    loaded_writable_addresses: _containers.RepeatedScalarFieldContainer[bytes]
    loaded_readonly_addresses: _containers.RepeatedScalarFieldContainer[bytes]
    return_data: ReturnData
    return_data_none: bool
    compute_units_consumed: int
    cost_units: int
    def __init__(self, err: _Optional[_Union[TransactionError, _Mapping]] = ..., fee: _Optional[int] = ..., pre_balances: _Optional[_Iterable[int]] = ..., post_balances: _Optional[_Iterable[int]] = ..., inner_instructions: _Optional[_Iterable[_Union[InnerInstructions, _Mapping]]] = ..., inner_instructions_none: _Optional[bool] = ..., log_messages: _Optional[_Iterable[str]] = ..., log_messages_none: _Optional[bool] = ..., pre_token_balances: _Optional[_Iterable[_Union[TokenBalance, _Mapping]]] = ..., post_token_balances: _Optional[_Iterable[_Union[TokenBalance, _Mapping]]] = ..., rewards: _Optional[_Iterable[_Union[Reward, _Mapping]]] = ..., loaded_writable_addresses: _Optional[_Iterable[bytes]] = ..., loaded_readonly_addresses: _Optional[_Iterable[bytes]] = ..., return_data: _Optional[_Union[ReturnData, _Mapping]] = ..., return_data_none: _Optional[bool] = ..., compute_units_consumed: _Optional[int] = ..., cost_units: _Optional[int] = ...) -> None: ...

class TransactionError(_message.Message):
    __slots__ = ("err",)
    ERR_FIELD_NUMBER: _ClassVar[int]
    err: bytes
    def __init__(self, err: _Optional[bytes] = ...) -> None: ...

class InnerInstructions(_message.Message):
    __slots__ = ("index", "instructions")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    index: int
    instructions: _containers.RepeatedCompositeFieldContainer[InnerInstruction]
    def __init__(self, index: _Optional[int] = ..., instructions: _Optional[_Iterable[_Union[InnerInstruction, _Mapping]]] = ...) -> None: ...

class InnerInstruction(_message.Message):
    __slots__ = ("program_id_index", "accounts", "data", "stack_height")
    PROGRAM_ID_INDEX_FIELD_NUMBER: _ClassVar[int]
    ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    STACK_HEIGHT_FIELD_NUMBER: _ClassVar[int]
    program_id_index: int
    accounts: bytes
    data: bytes
    stack_height: int
    def __init__(self, program_id_index: _Optional[int] = ..., accounts: _Optional[bytes] = ..., data: _Optional[bytes] = ..., stack_height: _Optional[int] = ...) -> None: ...

class CompiledInstruction(_message.Message):
    __slots__ = ("program_id_index", "accounts", "data")
    PROGRAM_ID_INDEX_FIELD_NUMBER: _ClassVar[int]
    ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    program_id_index: int
    accounts: bytes
    data: bytes
    def __init__(self, program_id_index: _Optional[int] = ..., accounts: _Optional[bytes] = ..., data: _Optional[bytes] = ...) -> None: ...

class TokenBalance(_message.Message):
    __slots__ = ("account_index", "mint", "ui_token_amount", "owner", "program_id")
    ACCOUNT_INDEX_FIELD_NUMBER: _ClassVar[int]
    MINT_FIELD_NUMBER: _ClassVar[int]
    UI_TOKEN_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    PROGRAM_ID_FIELD_NUMBER: _ClassVar[int]
    account_index: int
    mint: str
    ui_token_amount: UiTokenAmount
    owner: str
    program_id: str
    def __init__(self, account_index: _Optional[int] = ..., mint: _Optional[str] = ..., ui_token_amount: _Optional[_Union[UiTokenAmount, _Mapping]] = ..., owner: _Optional[str] = ..., program_id: _Optional[str] = ...) -> None: ...

class UiTokenAmount(_message.Message):
    __slots__ = ("ui_amount", "decimals", "amount", "ui_amount_string")
    UI_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    DECIMALS_FIELD_NUMBER: _ClassVar[int]
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    UI_AMOUNT_STRING_FIELD_NUMBER: _ClassVar[int]
    ui_amount: float
    decimals: int
    amount: str
    ui_amount_string: str
    def __init__(self, ui_amount: _Optional[float] = ..., decimals: _Optional[int] = ..., amount: _Optional[str] = ..., ui_amount_string: _Optional[str] = ...) -> None: ...

class ReturnData(_message.Message):
    __slots__ = ("program_id", "data")
    PROGRAM_ID_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    program_id: bytes
    data: bytes
    def __init__(self, program_id: _Optional[bytes] = ..., data: _Optional[bytes] = ...) -> None: ...

class Reward(_message.Message):
    __slots__ = ("pubkey", "lamports", "post_balance", "reward_type", "commission", "commission_bps")
    PUBKEY_FIELD_NUMBER: _ClassVar[int]
    LAMPORTS_FIELD_NUMBER: _ClassVar[int]
    POST_BALANCE_FIELD_NUMBER: _ClassVar[int]
    REWARD_TYPE_FIELD_NUMBER: _ClassVar[int]
    COMMISSION_FIELD_NUMBER: _ClassVar[int]
    COMMISSION_BPS_FIELD_NUMBER: _ClassVar[int]
    pubkey: str
    lamports: int
    post_balance: int
    reward_type: RewardType
    commission: str
    commission_bps: str
    def __init__(self, pubkey: _Optional[str] = ..., lamports: _Optional[int] = ..., post_balance: _Optional[int] = ..., reward_type: _Optional[_Union[RewardType, str]] = ..., commission: _Optional[str] = ..., commission_bps: _Optional[str] = ...) -> None: ...

class Rewards(_message.Message):
    __slots__ = ("rewards", "num_partitions")
    REWARDS_FIELD_NUMBER: _ClassVar[int]
    NUM_PARTITIONS_FIELD_NUMBER: _ClassVar[int]
    rewards: _containers.RepeatedCompositeFieldContainer[Reward]
    num_partitions: NumPartitions
    def __init__(self, rewards: _Optional[_Iterable[_Union[Reward, _Mapping]]] = ..., num_partitions: _Optional[_Union[NumPartitions, _Mapping]] = ...) -> None: ...

class UnixTimestamp(_message.Message):
    __slots__ = ("timestamp",)
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    timestamp: int
    def __init__(self, timestamp: _Optional[int] = ...) -> None: ...

class BlockHeight(_message.Message):
    __slots__ = ("block_height",)
    BLOCK_HEIGHT_FIELD_NUMBER: _ClassVar[int]
    block_height: int
    def __init__(self, block_height: _Optional[int] = ...) -> None: ...

class NumPartitions(_message.Message):
    __slots__ = ("num_partitions",)
    NUM_PARTITIONS_FIELD_NUMBER: _ClassVar[int]
    num_partitions: int
    def __init__(self, num_partitions: _Optional[int] = ...) -> None: ...
