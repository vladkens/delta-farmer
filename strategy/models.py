# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Warning: May cause enlightenment
import sys
import tomllib
import warnings
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError, model_validator

from lib.models import AccountConfig, DurationSec, SizeRange, TgConfig, TimeRange
from lib.utils import round_to_tick_size

# MARK: Trading types

Side = Literal["bid", "ask"]


class OrderStatus(StrEnum):
    OPEN = "open"  # active on book (new / pending / partial)
    FILLED = "filled"  # fully filled
    CANCELED = "canceled"  # canceled, expired, or rejected


class Position(BaseModel):
    """Unified position model across all exchanges."""

    id: str
    symbol: str
    side: Side
    size: Decimal  # always positive, in base asset
    entry_price: Decimal
    unrealized_pnl: Decimal = Decimal(0)


class Order(BaseModel):
    """Unified order model across all exchanges."""

    id: str
    symbol: str
    side: Side
    size: Decimal  # total size
    filled: Decimal  # filled size
    price: Decimal | None  # None for market orders
    status: OrderStatus
    reduce_only: bool = False


class ProfileInfo(BaseModel):
    """Account profile summary for reporting (info command)."""

    addr: str  # pre-formatted display address
    balance: Decimal
    volume: Decimal
    pnl: Decimal  # net realized PnL as trading pnl - fees - funding
    points: Decimal
    ref_code: str | None = None
    rank: int | None = None
    mode: str | None = None


class OrderBookLevel(BaseModel):
    price: Decimal
    size: Decimal

    @classmethod
    def build(cls, level: Iterable[object]) -> "OrderBookLevel":
        price, size = level
        return cls(price=Decimal(str(price)), size=Decimal(str(size)))


class OrderBook(BaseModel):
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    @classmethod
    def build(
        cls, *, bids: Iterable[Iterable[object]], asks: Iterable[Iterable[object]]
    ) -> "OrderBook":
        return cls(
            bids=[OrderBookLevel.build(level) for level in bids],
            asks=[OrderBookLevel.build(level) for level in asks],
        )


# MARK: Protocol


@runtime_checkable
class TradingClient(Protocol):
    """Protocol for all trading clients."""

    exchange: str

    @property
    def name(self) -> str: ...

    # Lifecycle
    async def login(self, *, force: bool = False) -> str | None: ...

    # Account
    async def balance(self) -> Decimal: ...

    # Price & conversion
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]: ...  # (best_bid, best_ask)
    async def get_order_book(self, symbol: str) -> OrderBook: ...
    async def get_price(self, symbol: str) -> Decimal: ...

    async def get_lot_size(self, symbol: str) -> Decimal:
        """Minimum quantity increment (e.g. 0.0001 BTC)."""
        ...

    async def get_tick_size(self, symbol: str) -> Decimal:
        """Minimum price increment (e.g. $1 for BTC, $0.01 for smaller assets)."""
        ...

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        """Minimum notional trade value in USD. Hardcoded per exchange; TODO: derive from API."""
        ...

    # Positions
    async def positions(self) -> list[Position]: ...
    async def close_position(self, position: Position) -> bool: ...

    # Orders - always work with qty (base asset quantity)
    async def market_order(
        self, symbol: str, side: Side, qty: Decimal, reduce_only=False
    ) -> Order: ...

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order: ...

    async def cancel_order(self, order: Order) -> bool: ...
    async def get_order(self, order_id: str) -> Order | None: ...

    # Cleanup
    async def cancel_all_orders(self) -> int: ...
    async def close_all_positions(self) -> int: ...

    # Account checks
    async def registered(self) -> bool: ...

    # Market discovery
    async def get_symbols(self) -> list[str]:
        """All tradable symbols, sorted by liquidity/relevance (best candidates first)."""
        ...

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool: ...

    # Leverage
    async def get_leverage(self, symbol: str) -> int | None: ...
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...


# MARK: Utilities


def usd_to_qty(usd: Decimal, price: Decimal, lot_size: Decimal) -> Decimal:
    """Convert USD amount to quantity, rounded to lot size."""
    qty = usd / price
    return round_to_tick_size(qty, lot_size)


def opposite_side(side: Side) -> Side:
    """Return the opposite side."""
    return "ask" if side == "bid" else "bid"


def trading_client_trace(exc: BaseException) -> str | None:
    path: list[str] = []
    tb = exc.__traceback__

    while tb:
        frame = tb.tb_frame
        obj = frame.f_locals.get("self")
        if isinstance(obj, TradingClient):
            attr = frame.f_locals.get("_attr")  # decorators keep the original method here
            fn = getattr(attr, "__name__", frame.f_code.co_name)
            label = f"{type(obj).__name__}.{fn}({obj.name})"
            if not path or path[-1] != label:
                path.append(label)
        tb = tb.tb_next

    return " → ".join(path) if path else None


# MARK: Strategy config


class MarketHoursMode(StrEnum):
    AUTO = "auto"
    STRICT = "strict"
    OFF = "off"


class TradeConfig(BaseModel):
    """Parameters for one independent trading strategy."""

    symbols: list[str] = Field(..., min_length=1)
    symbols_per_trade: int = Field(1, gt=0, le=4)
    leverage: int = Field(10, gt=0, lt=50)
    trade_size_usd: SizeRange | None = None
    trade_size_pct: float | None = Field(None, ge=0.01, le=1.0)
    trade_duration: TimeRange
    trade_cooldown: TimeRange
    trade_heartbeat: DurationSec = DurationSec("15s")
    position_roi_limit: float = Field(0.8, gt=0, lt=1)
    combined_roi_limit: float = Field(0.1, gt=0, lt=1)
    max_failures: int = Field(0, ge=0)  # 0 = infinite retries
    market_hours: MarketHoursMode = MarketHoursMode.AUTO
    use_limit: bool = False
    limit_wait: DurationSec = DurationSec("90s")
    limit_wait_retries: int = Field(99, ge=0)
    limit_market_fallback: bool = True
    max_entry_spread_pct: Decimal | None = Field(Decimal("0.25"), gt=0)
    entry_gate_wait: DurationSec = DurationSec("5m")
    entry_gate_poll: DurationSec = Field(DurationSec("3s"), ge=1, le=10)
    first_as_prime: bool = False
    group_size: int | None = Field(None, ge=2, le=5)
    regroup_interval: DurationSec | None = None

    @model_validator(mode="before")
    @classmethod
    def _before(cls, values):
        if isinstance(values, dict):
            if "symbols" in values and "markets" in values:
                raise ValueError("Use `symbols` only; replace legacy `markets` with `symbols`")
            if "markets" in values:
                warnings.warn("`markets` is deprecated, use `symbols` instead")
                values["symbols"] = values.pop("markets")
            if "first_as_main" in values:
                # warnings.warn("`first_as_main` is deprecated, use `first_as_prime` instead`)
                values["first_as_prime"] = values.pop("first_as_main")
        return values

    @property
    def limit_wait_budget(self) -> int:
        """Worst-case wait for one limit order before fallback."""
        return int(self.limit_wait) * (1 + self.limit_wait_retries)


class StrategyPoolConfig(TradeConfig):
    """One named strategy's account membership and trading parameters."""

    accounts: list[str] = Field(..., min_length=2)

    @model_validator(mode="after")
    def _unique_accounts(self):
        duplicates = {name for name in self.accounts if self.accounts.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate pool accounts: {', '.join(sorted(duplicates))}")
        return self


class StrategyConfig(TradeConfig):
    """Legacy single-strategy config kept as a public runtime model."""

    accounts: list[AccountConfig]
    balance_transfer_delay: TimeRange = TimeRange(min=DurationSec("2m"), max=DurationSec("4m"))
    telegram: TgConfig = Field(default_factory=lambda: TgConfig())

    @model_validator(mode="after")
    def _unique_account_names(self):
        names = [account.name for account in self.accounts]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate account names: {', '.join(sorted(duplicates))}")
        return self

    @classmethod
    def load(cls, filepath: str) -> "StrategyConfig":
        return load_config(cls, filepath)


class AppConfig(BaseModel):
    """Exchange config supporting either one legacy strategy or named pools."""

    accounts: list[AccountConfig]
    pools: dict[str, StrategyPoolConfig] = Field(default_factory=dict)
    strategy: TradeConfig | None = Field(None, exclude=True, repr=False)
    balance_transfer_delay: TimeRange = TimeRange(min=DurationSec("2m"), max=DurationSec("4m"))
    telegram: TgConfig = Field(default_factory=lambda: TgConfig())

    @model_validator(mode="before")
    @classmethod
    def _extract_legacy_strategy(cls, values):
        if not isinstance(values, dict):
            return values

        values = values.copy()
        legacy_fields = set(TradeConfig.model_fields) | {"markets", "first_as_main"}
        configured_fields = legacy_fields.intersection(values)
        if "pools" in values:
            if not values["pools"]:
                raise ValueError("pools must contain at least one named pool")
            if configured_fields:
                fields = ", ".join(sorted(configured_fields))
                raise ValueError(f"top-level trading settings cannot be used with pools: {fields}")
        else:
            values["strategy"] = {field: values.pop(field) for field in configured_fields}

        return values

    @model_validator(mode="after")
    def _validate_accounts_and_pools(self):
        names = [account.name for account in self.accounts]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate account names: {', '.join(sorted(duplicates))}")

        if not self.pools:
            if self.strategy is None:
                raise ValueError("legacy config is missing trading settings")
            return self

        blank_names = [name for name in self.pools if not name.strip()]
        if blank_names:
            raise ValueError("pool names must not be empty")

        known = set(names)
        memberships: dict[str, list[str]] = {}
        for pool_name, pool in self.pools.items():
            for account_name in pool.accounts:
                memberships.setdefault(account_name, []).append(pool_name)

        unknown = set(memberships).difference(known)
        if unknown:
            raise ValueError(f"unknown pool accounts: {', '.join(sorted(unknown))}")

        reused = {
            name: pool_names for name, pool_names in memberships.items() if len(pool_names) > 1
        }
        if reused:
            details = ", ".join(
                f"{name} ({', '.join(pool_names)})" for name, pool_names in sorted(reused.items())
            )
            raise ValueError(f"accounts assigned to multiple pools: {details}")

        unassigned = {
            account.name
            for account in self.accounts
            if account.enabled and account.name not in memberships
        }
        if unassigned:
            raise ValueError(
                f"enabled accounts missing from pools: {', '.join(sorted(unassigned))}"
            )

        return self

    @classmethod
    def load(cls, filepath: str) -> "AppConfig":
        return load_config(cls, filepath)


def load_config[T: BaseModel](config_cls: type[T], filepath: str) -> T:
    """Load and validate a Pydantic config from a TOML file with user-friendly errors."""
    try:
        with open(filepath, "rb") as fp:
            obj = tomllib.load(fp)
    except FileNotFoundError:
        raise SystemExit(f"❌ Config file not found: {filepath}")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"❌ Invalid TOML syntax in {filepath}: {e}")

    try:
        return config_cls.model_validate(obj)
    except ValidationError as e:
        print(f"❌ Config validation failed for {filepath}\n", file=sys.stderr)
        errors = []
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"  • {field}: {msg}")
        print("\n".join(errors), file=sys.stderr)
        print(f"\n💡 Fix the errors above in {filepath}", file=sys.stderr)
        raise SystemExit(1)
