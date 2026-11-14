import asyncio
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from lib.logger import logger
from lib.utils import find_safe_pair, round_to_tick_size

from .execution import fill_limit_order, wait_for_legs_entry_quality
from .models import Position, Side, TradeConfig, TradingClient, opposite_side, usd_to_qty

USD_TICK = Decimal("0.01")
SAFE_PCT = Decimal("0.96")  # leave 4% margin to avoid liquidation on leverage rounding
SIZE_DRIFT_LIMIT = Decimal("0.05")
QTY_IMBALANCE_WARN_USD = Decimal("0.10")
# A leg may send at most this much more than it planned: find_safe_pair sizes every leg to 90% of
# its account's margin, so a 10% overshoot still fits, and more is rejected for margin mid-open.
SIZE_OVERSHOOT_LIMIT = Decimal("0.10")


class PlanError(RuntimeError):
    """Plan cannot be traded as planned. Raised while planning, before any order is sent."""


class PartsTooSmall(ValueError):
    """Planned qty cannot be cut into this many parts; the caller may retry with fewer."""


def basket_lot_size(lots: Sequence[Decimal]) -> Decimal:
    """Finest step every leg's lot grid accepts: the exact least common multiple of the lots.

    Nested grids (0.01 under 0.1) keep the coarsest lot, which is what a single venue uses.
    Grids that only meet further out are still tradeable: 0.2 and 0.5 reject each other's lot
    but both accept 1.0. Lots are arbitrary API decimals (Nado `size_increment`, Rise
    `step_size`, Omni `qty_tick`, Pacifica `lot_size`), not powers of ten, so grids can also
    meet many coarse lots out, where no normal trade size fits the step any more. Whether a step
    is too coarse depends on price and trade size, so it is measured where those are known:
    `DeltaStrategy._lot_compatible_symbols` before the cycle, `load_qtys` on the real plan.

    Every failure here is a `PlanError`, including a lot that is not a positive finite number, so
    one broken venue response costs its own symbol instead of the whole cycle.
    """
    if not lots:
        raise PlanError("no lot sizes to size against")

    # NaN would raise from the comparison below, Infinity from the int() further down.
    bad = sorted(str(lot) for lot in lots if not lot.is_finite() or lot <= 0)
    if bad:
        raise PlanError(f"lot size must be a positive number, got {', '.join(bad)}")

    # Every Decimal is n*10^e, so the lots scale to whole units and their LCM is exact.
    scale = max(-cast(int, lot.as_tuple().exponent) for lot in lots)
    units = [int(lot.scaleb(scale)) for lot in lots]

    return Decimal(math.lcm(*units)).scaleb(-scale)


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


def split_qty(qty: Decimal, lot: Decimal, parts: int) -> list[Decimal]:
    """Split a lot-aligned qty into parts that differ by at most one lot and sum to qty."""
    units = qty / lot
    if units != units.to_integral_value():
        raise ValueError(f"qty {qty} is not a multiple of lot {lot}")

    units = int(units)
    return [lot * (units * (i + 1) // parts - units * i // parts) for i in range(parts)]


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


def smallest_leg_usd(total_usd: Decimal, symbols_per_trade: int, accounts: int) -> Decimal:
    """Smallest leg notional a cycle of this basket size can plan.

    `calc_symbol_sizes` splits the basket across the symbols, then `find_safe_pair` gives the prime
    account half of each symbol's size and splits the other half across the rest, with ±10% jitter.
    """
    names = [str(i) for i in range(symbols_per_trade)]
    per_symbol = min(size for size, _ in calc_symbol_sizes(total_usd, names, "bid").values())

    return per_symbol * Decimal("0.5") / max(accounts - 1, 1)


async def plan_delta_trades(
    accounts: Sequence[TradingClient],
    symbols: Sequence[str],
    total_size_usd: Decimal,
    leverage: int,
    balances: list[tuple[str, float]],
    long_exchange: str | None = None,
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

    if long_exchange is not None:
        # A cross-venue points boost pays only while the long leg sits on the venue named for the
        # week, so the side comes from each leg's exchange, not from the draw above (which then
        # only picks per-symbol sides, and _check_cfg keeps such a pool at one symbol).
        venues = dict.fromkeys(mapping[name].exchange for name, _ in pairs)
        shorts = [venue for venue in venues if venue != long_exchange]
        logger.info(f"Hedge direction: {long_exchange} long, {', '.join(shorts)} short")

    for symbol, (trade_size, trade_side) in symbol_sizes.items():
        ratio = trade_size / total_size
        legs: list[DeltaLeg] = []
        for j, (name, size) in enumerate(pairs):
            client = mapping[name]
            side: Side = trade_side if j == 0 else opposite_side(trade_side)
            if long_exchange is not None:
                side = "bid" if client.exchange == long_exchange else "ask"

            legs.append(DeltaLeg(client=client, side=side, size_usd=Decimal(str(size)) * ratio))

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
    lot_size: Decimal = Decimal(0)

    @property
    def legs(self) -> list[DeltaLeg]:
        return [self.lead] + self.rest

    def check_plan(self) -> None:
        """Every leg tradeable and the basket flat in base qty. Must hold before any order."""
        zero = [leg.client.name for leg in self.legs if leg.qty <= 0]
        if zero:
            raise PlanError(f"{self.symbol}: size is too small to trade on {', '.join(zero)}")

        bid = sum(leg.qty for leg in self.legs if leg.side == "bid")
        ask = sum(leg.qty for leg in self.legs if leg.side == "ask")

        if bid != ask:
            raise PlanError(
                f"{self.symbol}: qty imbalance {bid - ask}, basket is not delta-neutral "
                f"({bid} bid vs {ask} ask)"
            )

    def split(self, parts: int) -> list["DeltaTrade"]:
        """Plan equal entry parts. Each part's lead equals its hedges, so every part is neutral."""
        self.check_plan()  # also for one part: an unbalanced basket must never reach an order

        # Each part's lead is built as the sum of that part's hedges, which is neutral only while
        # the lead is the only leg on its side. `check_plan`'s bid total == ask total is weaker:
        # it holds for baskets this split would plan directional, so check the shape here too.
        crowd = [leg.client.name for leg in self.rest if leg.side == self.lead.side]
        if crowd:
            raise PlanError(
                f"{self.symbol}: lead cannot share its {self.lead.side} side with "
                f"{', '.join(crowd)}; parts are sized from the hedges"
            )

        if parts == 1:
            return [self]

        def part(leg: DeltaLeg, qty: Decimal) -> DeltaLeg:
            return DeltaLeg(leg.client, leg.side, leg.size_usd * qty / leg.qty, qty)

        rest_parts = [split_qty(leg.qty, self.lot_size, parts) for leg in self.rest]
        out: list[DeltaTrade] = []
        for i in range(parts):
            qtys = [qty[i] for qty in rest_parts]
            if not all(qtys):
                raise PartsTooSmall(f"{self.symbol}: size is too small for {parts} parts")

            lead = part(self.lead, sum(qtys, Decimal(0)))
            rest = [part(leg, qty) for leg, qty in zip(self.rest, qtys)]
            out.append(DeltaTrade(self.symbol, lead, rest, self.lot_size))

        return out

    def keep(self, parts: list["DeltaTrade"]) -> None:
        """Shrink the trade to the parts actually opened, so monitoring checks the real size."""
        for i, leg in enumerate(self.legs):
            leg.qty = sum((p.legs[i].qty for p in parts), Decimal(0))
            leg.size_usd = sum((p.legs[i].size_usd for p in parts), Decimal(0))

    async def load_qtys(self) -> None:
        async def quote(leg: DeltaLeg) -> tuple[Decimal, Decimal]:
            price, lot = await asyncio.gather(
                leg.client.get_price(self.symbol), leg.client.get_lot_size(self.symbol)
            )
            return price, lot

        legs = self.legs
        quotes = await asyncio.gather(*[quote(leg) for leg in legs])
        prices, lots = [price for price, _ in quotes], [lot for _, lot in quotes]

        # Legs may sit on different venues: each sizes on its own price, the whole basket on one
        # step every lot grid accepts, which keeps split() aligned on every venue.
        lot_size = self.lot_size = basket_lot_size(lots)

        for leg, price in zip(legs, prices):
            leg.qty = usd_to_qty(leg.size_usd, price, lot_size)

        bid_qty = sum(leg.qty for leg in legs if leg.side == "bid")
        ask_qty = sum(leg.qty for leg in legs if leg.side == "ask")
        delta = bid_qty - ask_qty  # positive → bid side is larger

        if delta != 0:
            larger_side = "bid" if delta > 0 else "ask"
            last_leg = next(leg for leg in reversed(legs) if leg.side == larger_side)
            usd_imbalance = abs(delta) * prices[0]  # lead price: one scale for the whole basket
            if abs(delta) >= last_leg.qty:
                # Leaving it unbalanced would hold a directional position for the whole trade:
                # nothing downstream measures it, because every check reads these planned qtys.
                raise PlanError(
                    f"{self.symbol}: qty imbalance {delta} ({usd_imbalance:.2f} USD) does not fit "
                    f"the last {larger_side} leg ({last_leg.qty}); size up or use a finer market"
                )

            last_leg.qty -= abs(delta)
            if usd_imbalance > QTY_IMBALANCE_WARN_USD:
                logger.debug(f"qty imbalance adjusted: {delta} qty = {usd_imbalance:.2f} USD")

        # A leg trades whole basket lots, so its notional can sit up to one lot off the requested
        # size. The exchange minimum, the plan log and the reported volume all have to use what
        # will really be sent, not what was asked for.
        wants = [leg.size_usd for leg in legs]
        for leg, price in zip(legs, prices):
            leg.size_usd = leg.qty * price

        # Rounding to the basket step lifts a leg as readily as it drops it, and the overshoot is
        # paid out of that leg's margin. Half of a leg's own lot is the most its venue's own grid
        # can move it and is traded today; only what the shared basket step adds on top of that
        # is refused here, so a single-venue basket can never reach this raise. The slack is each
        # leg's own: a finer venue's leg must not borrow the coarse venue's lot.
        over = [
            f"{leg.client.name} {leg.size_usd:.2f}/{want:.2f}"
            for leg, want, price, lot in zip(legs, wants, prices, lots)
            if leg.size_usd - want > max(want * SIZE_OVERSHOOT_LIMIT, lot / 2 * price)
        ]
        if over:
            raise PlanError(
                f"{self.symbol}: basket step {lot_size} sends more than "
                f"{SIZE_OVERSHOOT_LIMIT:.0%} over the planned size ({', '.join(over)} USD); "
                "size up or use a finer market"
            )

        self.check_plan()

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

    async def below_min(self) -> list[tuple[DeltaLeg, Decimal]]:
        """Legs whose order notional misses their own venue minimum. `size_usd` is qty x price
        once `load_qtys` has run, so this measures the order, not the requested size."""
        mins = await asyncio.gather(*[x.client.get_min_trade_usd(self.symbol) for x in self.legs])
        return [(act, val) for act, val in zip(self.legs, mins) if act.size_usd < val]

    async def check_min_sizes(self):
        vals = await self.below_min()
        if not vals:
            return

        for act, val in vals:
            name, size = act.client.name, act.size_usd
            logger.warning(f"{name}: {size:.2f} < min {val:.2f} USD for {self.symbol}")

        failed_accs = ", ".join(act.client.name for act, _ in vals)
        raise RuntimeError(f"Trade size below minimum for: {failed_accs}")

    async def gate(self, cfg: TradeConfig) -> bool:
        legs = [(leg.client, leg.side, leg.qty) for leg in self.legs]
        rs = await wait_for_legs_entry_quality(legs, self.symbol, cfg)
        return rs is not None

    async def open(self, cfg: TradeConfig) -> bool:
        if cfg.use_limit:
            clt, side, qty = self.lead.client, self.lead.side, self.lead.qty
            order = await _fill_limit_order(clt, self.symbol, side, qty, cfg)
            if order is None:
                raise RuntimeError(f"Limit order failed for {clt.name} on {self.symbol}")
        elif cfg.hedge_delay:
            clt, side, qty = self.lead.client, self.lead.side, self.lead.qty
            await clt.market_order(self.symbol, side, qty)
            logger.bind(account=clt.name).debug(f"Market {side} {qty} {self.symbol} filled")

        legs = self.rest if cfg.use_limit or cfg.hedge_delay else self.legs
        if cfg.hedge_delay:  # opt-in: hedges follow the lead after a pause, unhedged meanwhile
            await asyncio.sleep(cfg.hedge_delay.sample())

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

    async def reduce(self, cfg: TradeConfig, remaining: int, use_limit=False) -> None:
        """Close 1/remaining of each leg's position; a final close() takes the exact rest."""

        # Legs on several exchanges opened on the basket step and reduce on it too: a finer-lot
        # leg rounding on its own lot would leave the pair directional.
        mixed = len({leg.client.exchange for leg in self.legs}) > 1

        async def _reduce(leg: DeltaLeg, limit: bool) -> None:
            if mixed and self.lot_size:
                lot = self.lot_size
            else:
                lot = await leg.client.get_lot_size(self.symbol)
            for pos in await self._positions(leg.client):
                qty = round_to_tick_size(pos.size / remaining, lot)
                if qty <= 0:
                    continue

                side = opposite_side(pos.side)
                if limit:
                    clt = leg.client
                    order = await _fill_limit_order(
                        clt, self.symbol, side, qty, cfg, reduce_only=True
                    )
                    if order is None:  # never reduce hedges past an unreduced lead
                        raise RuntimeError(f"Limit reduce failed for {clt.name} on {self.symbol}")
                else:
                    await leg.client.market_order(self.symbol, side, qty, reduce_only=True)

                log = logger.bind(account=leg.client.name)
                log.debug(f"Reduced {qty} {self.symbol} of {pos.size}")

        if use_limit:
            await _reduce(self.lead, limit=True)

        legs = self.legs if not use_limit else self.rest
        await asyncio.gather(*[_reduce(leg, limit=False) for leg in legs])

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
            drift = pos.side != leg.side or (
                bool(leg.qty) and abs(pos.size - leg.qty) / leg.qty > SIZE_DRIFT_LIMIT
            )
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
