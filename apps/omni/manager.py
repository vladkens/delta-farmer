# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Works on my machine ¯\_(ツ)_/¯
import asyncio
import random
import time
from dataclasses import dataclass
from decimal import Decimal

from core import logger, utils

from .client import Client, OrderSide
from .config import Config


@dataclass
class Act:
    acc: Client
    side: OrderSide
    size: Decimal


class Manager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.accs = [Client.from_config(x) for x in cfg.accounts if x.enabled]
        self.initial_bal = 0.0

    async def warmup(self, accs: list[Client]) -> list[Client]:
        fn = lambda r: isinstance(r, Exception)  # noqa: E731
        rs = await asyncio.gather(*[acc.warmup() for acc in accs], return_exceptions=True)
        cc = [acc.name for acc, r in zip(accs, rs) if fn(r)]
        logger.error(f"Warmup failed: {', '.join(cc)}") if cc else None
        return [acc for acc, r in zip(accs, rs) if not fn(r)]

    async def registered(self, accs: list[Client]) -> list[Client]:
        fn = lambda r: isinstance(r, Exception) or not r  # noqa: E731
        rs = await asyncio.gather(*[acc.is_registered() for acc in accs], return_exceptions=True)
        cc = [acc.name for acc, r in zip(accs, rs) if fn(r)]
        logger.error(f"Not registered: {', '.join(cc)}") if cc else None
        return [acc for acc, r in zip(accs, rs) if not fn(r)]

    async def get_bals(self, accs: list[Client]):
        bals = await asyncio.gather(*[acc.balance() for acc in accs])
        return list(zip([acc.name for acc in accs], [float(bal) for bal in bals]))

    async def ensure_leverage(self, accs: list[Client], asset: str):
        for acc in accs:
            await acc.set_leverage(asset, self.cfg.leverage)

    async def run_trade(self):
        if not (2 <= len(self.accs) <= 5):
            logger.error(f"Accounts for trading must be between 2 and 5, got {len(self.accs)}")
            exit(1)

        accs1 = await self.warmup(self.accs)
        if len(accs1) != len(self.accs):
            logger.error("Warmup failed for some accounts, cannot continue.")
            exit(1)

        accs2 = await self.registered(accs1)
        if len(accs2) != len(self.accs):
            logger.error("Some accounts are not registered, cannot continue.")
            exit(1)

        self.initial_bal = float(sum(x[1] for x in await self.get_bals(self.accs)))

        async def loop():
            await self.close(self.accs)

            while True:
                try:
                    print("-" * 60)
                    await self.trade(self.accs)

                    wait_sec = self.cfg.trade_cooldown.sample()
                    logger.info(utils.wait_msg(wait_sec))
                    await asyncio.sleep(wait_sec)
                except Exception as e:
                    logger.warning(f"Trade cycle failed {type(e)}: {e}")
                    await self.close(self.accs)
                    break

        while True:
            try:
                await loop()
            except Exception as e:
                wait_sec = 60 * 3
                logger.error(f"Trade failed with {type(e)}: {e} {utils.wait_msg(wait_sec)}")
                await asyncio.sleep(wait_sec)

    async def close(self, accs: list[Client], market: str | None = None):
        closed_orders, closed_positions = 0, 0

        for acc in accs:
            for order in await acc.orders(market=market):
                await acc.cancel_order(order.id)
                closed_orders += 1

        for acc in accs:
            for pos in await acc.positions(market=market):
                await acc.market_order(pos.symbol, -pos.qty, reduce_only=True)
                closed_positions += 1

        logger.info(f"Canceled {closed_orders} open orders") if closed_orders > 0 else None
        logger.info(f"Closed {closed_positions} open positions") if closed_positions > 0 else None

    async def _trade_check(self, accs: list[Client], market: str) -> bool:
        for acc in accs:
            positions = await acc.positions(market)
            if len(positions) != 1:
                logger.warning(f"{len(positions)} positions for {market} on {acc.name}, closing...")
                return False

            pos = positions[0]
            current_price = await acc.get_indicative(market, 1)
            mark_price = current_price.mark_price

            entry_cost = abs(pos.qty) * pos.entry_price
            current_cost = abs(pos.qty) * mark_price

            # Calculate ROI: positive qty = long, negative = short
            roi = (current_cost / entry_cost - 1) * (1 if pos.qty > 0 else -1)

            if abs(roi) >= self.cfg.pnl_limit:
                tmp = f"{roi:.2%} ({entry_cost:.2f} -> {current_cost:.2f})"
                logger.info(f"Position {market} hit stop loss at {tmp}, closing...")
                return False

        return True

    async def _trade_wait(self, accs: list[Client], market: str):
        wait_sec = self.cfg.trade_duration.sample()
        logger.info(utils.wait_msg(wait_sec))

        until_sec = time.time() + wait_sec
        while time.time() < until_sec:
            skip_sec = min(self.cfg.trade_heartbeat, until_sec - time.time())
            await asyncio.sleep(skip_sec)

            try:
                if not await self._trade_check(accs, market):
                    return False
            except Exception as e:
                logger.warning(f"Position safety check failed {type(e)}: {e}, continuing wait...")

        return True

    async def trade(self, accs: list[Client]):
        accs = accs[:1] + utils.shuffle(accs[1:]) if self.cfg.first_as_main else utils.shuffle(accs)
        accs_map = {acc.name: acc for acc in accs}
        assert len(accs) >= 2, "At least two accounts are required."
        # logger.debug(f"accs: {', '.join(acc.name for acc in accs)}")

        was = await self.get_bals(accs)
        bal_str = " | ".join([f"{name} {bal:.2f}" for name, bal in was])
        bal_str = f"{sum(bal for _, bal in was):.2f} = " + bal_str
        logger.info(f"Balances: {bal_str}")

        size_usd = self.cfg.trade_size_usd.sample()
        acts = utils.find_safe_pair(was, size_usd, leverage=self.cfg.leverage)
        assert acts is not None, "No valid account combination found for trading."

        market = random.choice(self.cfg.markets)

        left_side: OrderSide = random.choice(["ask", "bid"])
        rest_side: OrderSide = "bid" if left_side == "ask" else "ask"
        acts = [
            Act(accs_map[name], left_side if i == 0 else rest_side, size)
            for i, (name, size) in enumerate(acts)
        ]

        # debug trade size calculation
        size_usd = sum(x.size for x in acts)
        rest_sizes = " ".join([str(x.size) for x in acts[1:]])
        rest_sizes = f"{sum(x.size for x in acts[1:])} ({rest_sizes})"
        logger.info(f"Trade {market}: {size_usd} = {acts[0].size} + {rest_sizes}")

        await self.ensure_leverage(accs, market)

        # Convert USD to asset quantity for each account
        qty_tasks = [act.acc.usd_to_qty(market, act.size) for act in acts]
        qtys = await asyncio.gather(*qty_tasks)

        # Execute orders with proper sign (+ for buy, - for sell)
        order_tasks = []
        for act, qty in zip(acts, qtys):
            signed_qty = qty if act.side == "bid" else -qty
            order_tasks.append(act.acc.market_order(market, signed_qty))

        await asyncio.gather(*order_tasks)

        await self._trade_wait(accs, market)
        await self.close(accs, market)

        now = await self.get_bals(accs)
        diff_sum = sum(x[1] for x in now) - sum(x[1] for x in was)
        diff_str = [(x[0], x[1] - y[1]) for x, y in zip(now, was)]
        diff_str = " | ".join([f"{name} {diff:+.2f}" for name, diff in diff_str])
        total_pnl = sum(x[1] for x in now) - self.initial_bal
        logger.info(f"Δ {diff_sum:+.2f} ~ {diff_str}; Total P/L: {total_pnl:+.2f}")
