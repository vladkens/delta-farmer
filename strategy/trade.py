import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from lib.logger import logger
from lib.utils import find_safe_pair, round_to_tick_size

from .execution import fill_limit_order, wait_for_entry_quality
from .models import Position, Side, TradeConfig, TradingClient, opposite_side, usd_to_qty

USD_TICK = Decimal("0.01")
SAFE_PCT = Decimal("0.96")  # leave 4% margin to avoid liquidation on leverage rounding
SIZE_DRIFT_LIMIT = Decimal("0.05")
QTY_IMBALANCE_WARN_USD = Decimal("0.10")


async def _fill_limit_order(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    cfg: TradeConfig,
    reduce_only=False,
):
    return await fill_limit_order(
        client,
        symbol,
        side,
        qty,
        reduce_only=reduce_only,
        timeout=cfg.limit_wait,
        use_market_fallback=cfg.limit_market_fallback,
        max_wait_retries=cfg.limit_wait_retries,
    )


def calc_symbol_sizes(
    total: Decimal, symbols: Sequence[str], lead_side: Side
) -> dict[str, tuple[Decimal, Side]]:  # {symbol: (size, side)}
    if not symbols:
        raise ValueError("at least one symbol is required")

    if len(symbols) > 4:
        raise ValueError("up to 4 symbols are supported")

    if len(symbols) == 1:
        return {symbols[0]: (total, lead_side)}

    n_lead = len(symbols) // 2
    n_rest = len(symbols) - n_lead
    half = total * Decimal("0.5")
    out: dict[str, tuple[Decimal, Side]] = {}

    for i, symbol in enumerate(symbols):
        if i < n_lead:
            size = round_to_tick_size(half / n_lead, USD_TICK)
            side = lead_side
        else:
            size = round_to_tick_size(half / n_rest, USD_TICK)
            side = opposite_side(lead_side)
        out[symbol] = (size, side)

    return out


async def plan_delta_trades(
    accounts: Sequence[TradingClient],
    symbols: Sequence[str],
    total_size_usd: Decimal,
    leverage: int,
    balances: list[tuple[str, float]],
) -> list["DeltaTrade"] | None:
    pairs = find_safe_pair(balances, float(total_size_usd), leverage)
    if pairs is None:
        return None

    lead_side: Side = random.choice(["bid", "ask"])
    total_size = Decimal(sum(size for _, size in pairs))
    if total_size <= 0:
        logger.warning("Trade size is zero; skipping cycle")
        return None

    symbol_sizes = calc_symbol_sizes(total_size, symbols, lead_side)

    mapping = {acc.name: acc for acc in accounts}
    trades: list[DeltaTrade] = []

    for symbol, (trade_size, trade_side) in symbol_sizes.items():
        ratio = trade_size / total_size
        legs: list[DeltaLeg] = [
            DeltaLeg(
                client=mapping[name],
                side=trade_side if j == 0 else opposite_side(trade_side),
                size_usd=Decimal(str(size)) * ratio,
            )
            for j, (name, size) in enumerate(pairs)
        ]
        trades.append(DeltaTrade(symbol=symbol, lead=legs[0], rest=legs[1:]))

    return trades


# MARK: Models


@dataclass
class DeltaLeg:
    client: TradingClient
    side: Side
    size_usd: Decimal
    qty: Decimal = Decimal(0)


@dataclass
class DeltaTradeSummary:
    symbol: str
    total_pnl: Decimal
    total_entry_cost: Decimal
    combined_roi: Decimal
    max_abs_leg_roi: Decimal
    leg_count: int
    open_leg_count: int
    has_size_drift: bool
    roi_breach: bool
    healthy: bool

    @property
    def close_reason(self) -> str:
        if self.open_leg_count != self.leg_count:
            return f"missing positions ({self.open_leg_count}/{self.leg_count})"
        if self.has_size_drift:
            return "position size drift"
        if self.roi_breach:
            return f"leg ROI hit {self.max_abs_leg_roi:.2%}"
        return "unknown reason"


@dataclass
class DeltaTrade:
    symbol: str
    lead: DeltaLeg
    rest: list[DeltaLeg] = field(default_factory=list)

    @property
    def legs(self) -> list[DeltaLeg]:
        return [self.lead] + self.rest

    async def load_qtys(self) -> None:
        price = await self.lead.client.get_price(self.symbol)
        lot_size = await self.lead.client.get_lot_size(self.symbol)
        for leg in self.legs:
            leg.qty = usd_to_qty(leg.size_usd, price, lot_size)

        bid_qty = sum(leg.qty for leg in self.legs if leg.side == "bid")
        ask_qty = sum(leg.qty for leg in self.legs if leg.side == "ask")
        delta = bid_qty - ask_qty  # positive → bid side is larger

        if delta != 0:
            larger_side = "bid" if delta > 0 else "ask"
            last_leg = next(leg for leg in reversed(self.legs) if leg.side == larger_side)
            usd_imbalance = abs(delta) * price
            if abs(delta) >= last_leg.qty:
                logger.warning(
                    f"qty imbalance {delta} ({usd_imbalance:.2f} USD) >= "
                    f"last leg qty {last_leg.qty} — skipping adjustment"
                )
            else:
                last_leg.qty -= abs(delta)
                if usd_imbalance > QTY_IMBALANCE_WARN_USD:
                    logger.debug(f"qty imbalance adjusted: {delta} qty = {usd_imbalance:.2f} USD")

    async def log_plan(self) -> None:
        total_usd = sum(leg.size_usd for leg in self.legs)
        lead_size = self.lead.size_usd
        rest_size = " ".join(f"{leg.size_usd:.2f}" for leg in self.rest)
        rest_size = f"{sum(leg.size_usd for leg in self.rest):.2f} ({rest_size})"
        logger.info(f"Trade {self.symbol}: {total_usd:.2f} = {lead_size:.2f} + {rest_size}")

    async def check_leverage(self, leverage: int) -> None:
        async def _ensure(acc: TradingClient) -> None:
            current = await acc.get_leverage(self.symbol)
            if current is None or current != leverage:
                await acc.set_leverage(self.symbol, leverage)

        await asyncio.gather(*[_ensure(leg.client) for leg in self.legs])

    async def check_min_sizes(self):
        mins = await asyncio.gather(*[x.client.get_min_trade_usd(self.symbol) for x in self.legs])
        vals = [(act, val) for act, val in zip(self.legs, mins) if act.size_usd < val]
        if not vals:
            return

        for act, val in vals:
            name, size = act.client.name, act.size_usd
            logger.warning(f"{name}: {size:.2f} < min {val:.2f} USD for {self.symbol}")

        failed_accs = ", ".join(act.client.name for act, _ in vals)
        raise RuntimeError(f"Trade size below minimum for: {failed_accs}")

    async def gate(self, cfg: TradeConfig) -> bool:
        rs = await wait_for_entry_quality(
            self.lead.client, self.symbol, [(leg.side, leg.qty) for leg in self.legs], cfg
        )
        return rs is not None

    async def open(self, cfg: TradeConfig) -> bool:
        if cfg.use_limit:
            clt, side, qty = self.lead.client, self.lead.side, self.lead.qty
            order = await _fill_limit_order(clt, self.symbol, side, qty, cfg)
            if order is None:
                raise RuntimeError(f"Limit order failed for {clt.name} on {self.symbol}")

        legs = self.legs if not cfg.use_limit else self.rest
        vals = await asyncio.gather(
            *[leg.client.market_order(self.symbol, leg.side, leg.qty) for leg in legs]
        )

        for leg, order in zip(legs, vals):
            log = logger.bind(account=leg.client.name)
            log.debug(f"Market {leg.side} {leg.qty} {self.symbol} filled")

        # todo: report opened log with spread info
        return True

    async def close(self, cfg: TradeConfig, use_limit=False) -> None:
        if use_limit:
            for pos in await self._positions(self.lead.client):
                clt, side = self.lead.client, opposite_side(pos.side)
                await _fill_limit_order(clt, pos.symbol, side, pos.size, cfg, reduce_only=True)

        async def _close(leg: DeltaLeg) -> None:
            for pos in await self._positions(leg.client):
                await leg.client.close_position(pos)
                log = logger.bind(account=leg.client.name)
                log.debug(f"Closed {pos.size} {self.symbol} with market order")

        legs = self.legs if not use_limit else self.rest
        await asyncio.gather(*[_close(leg) for leg in legs])
        # todo: report closed log with spread info

    # MARK: Monitoring

    async def _positions(self, client: TradingClient) -> list[Position]:
        return [p for p in await client.positions() if p.symbol == self.symbol]

    async def state(self, cfg: TradeConfig) -> DeltaTradeSummary:
        roi_limit = Decimal(str(cfg.position_roi_limit))

        async def leg_data(leg: DeltaLeg) -> tuple[Decimal, Decimal, Decimal, bool] | None:
            positions = await self._positions(leg.client)
            if len(positions) != 1 or positions[0].size == 0:
                return None
            pos = positions[0]
            price = await leg.client.get_price(self.symbol)
            entry_cost = pos.size * pos.entry_price
            sign = Decimal(1) if pos.side == "bid" else Decimal(-1)
            pnl = (pos.size * price - entry_cost) * sign
            roi = pnl / entry_cost if entry_cost else Decimal(0)
            drift = bool(leg.qty) and abs(pos.size - leg.qty) / leg.qty > SIZE_DRIFT_LIMIT
            return pnl, entry_cost, roi, drift

        data = [d for d in await asyncio.gather(*[leg_data(leg) for leg in self.legs]) if d]
        total_pnl = sum((d[0] for d in data), Decimal(0))
        total_entry_cost = sum((d[1] for d in data), Decimal(0))
        max_abs_roi = max((abs(d[2]) for d in data), default=Decimal(0))
        has_drift = any(d[3] for d in data)
        roi_breach = max_abs_roi >= roi_limit

        return DeltaTradeSummary(
            symbol=self.symbol,
            total_pnl=total_pnl,
            total_entry_cost=total_entry_cost,
            combined_roi=total_pnl / total_entry_cost if total_entry_cost else Decimal(0),
            max_abs_leg_roi=max_abs_roi,
            leg_count=len(self.legs),
            open_leg_count=len(data),
            has_size_drift=has_drift,
            roi_breach=roi_breach,
            healthy=len(data) == len(self.legs) and not has_drift and not roi_breach,
        )
