# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Bugs are features in disguise
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

    async def ips(self):
        for acc in self.accs:
            ip = await acc.ip()
            logger.info(f"{acc.name}: {ip}")

    async def get_bals(self, accs: list[Client]):
        bals: list[float] = []
        for acc in accs:
            bals.append(float(await acc.balance()))

        return list(zip([acc.name for acc in accs], bals))

    async def ensure_leverage(self, accs: list[Client], market: str):
        for acc in accs:
            await acc.set_leverage(market, self.cfg.leverage)

    async def close(self, accs: list[Client] | None = None, log_close=True):
        accs = accs or self.accs
        rs1 = await asyncio.gather(*(acc.cancel_all_orders() for acc in accs))
        rs2 = await asyncio.gather(*(acc.cancel_all_positions() for acc in accs))
        if (sum(rs1) + sum(rs2) > 0) and log_close:
            logger.debug(f"Closed {sum(rs1)} orders and {sum(rs2)} positions")

    async def _trade_check(self, accs: list[Client], market: str) -> bool:
        # logger.debug(f"Checking position safety for {market}...")
        for acc in accs:
            res = await acc.positions()
            if len(res) != 1:
                logger.warning(f"{len(res)} positions for {market} on {acc.name}, closing...")
                return False

            pos = utils.first([x for x in res if x.symbol == market])
            assert pos is not None, "Position should exist here."

            aprice = pos.entry_price
            bprice = await acc.vwap_price(market, "bid" if pos.side == "ask" else "ask")

            acost, bcost = pos.amount * aprice, pos.amount * bprice
            roi = (bcost / acost - 1) * (1 if pos.side == "bid" else -1)
            # pnl = bcost - acost if pos.side == "bid" else acost - bcost

            if abs(roi) >= self.cfg.pnl_limit:
                tmp = f"{roi:.2%} ({acost:.2f} -> {bcost:.2f})"
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
                is_safe = await self._trade_check(accs, market)
                if not is_safe:
                    return False
            except Exception as e:
                logger.warning(f"Position safety check failed {type(e)}: {e}, continuing wait...")

        return True

    async def trade(self, accs: list[Client]):
        accs = utils.shuffle(accs)
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

        if not self.cfg.use_limit:
            tasks = [act.acc.market_order(market, act.side, qsize=act.size) for act in acts]
            await asyncio.gather(*tasks)
        else:
            order_id = await acts[0].acc.limit_order(market, acts[0].side, qsize=acts[0].size)
            if not await acts[0].acc.wait_order_filled(order_id, self.cfg.limit_wait):
                await self.close(accs, log_close=False)
                return

            tasks = [act.acc.market_order(market, act.side, qsize=act.size) for act in acts[1:]]
            await asyncio.gather(*tasks)

        success = await self._trade_wait(accs, market)

        # close first account with limit if no error during wait and limit orders enabled
        if self.cfg.use_limit and success:
            close_side = "bid" if acts[0].side == "ask" else "ask"
            order_id = await acts[0].acc.limit_order(
                market, close_side, qsize=acts[0].size, reduce_only=True
            )
            await acts[0].acc.wait_order_filled(order_id, self.cfg.limit_wait)

        # close all other orders as market
        await asyncio.gather(*(acc.cancel_all_positions() for acc in accs))
        # await self.close(accs, log_close=False)

        now = await self.get_bals(accs)
        diff_sum = sum(x[1] for x in now) - sum(x[1] for x in was)
        diff_str = [(x[0], x[1] - y[1]) for x, y in zip(now, was)]
        diff_str = " | ".join([f"{name} {diff:+.2f}" for name, diff in diff_str])
        total_pnl = sum(x[1] for x in now) - self.initial_bal
        logger.info(f"Δ {diff_sum:+.2f} ~ {diff_str}; Total P/L: {total_pnl:+.2f}")

    async def run_trade(self):
        if not (2 <= len(self.accs) <= 5):
            logger.error(f"Accounts for trading must be between 2 and 5, got {len(self.accs)}")
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
