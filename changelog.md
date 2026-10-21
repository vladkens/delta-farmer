## v0.11.0 – 2026-10-21

### Features

- Added `trade_parts` to enter and exit positions in several equal parts; every part stays delta-neutral, and a trade uses fewer parts when a part would fall below the exchange minimum.
- Added an optional `hedge_delay` that opens the hedges a random pause after the prime account's entry.
- Added `stats` for Lighter with weekly or daily volume, fees, and burn.
- Extended Ethereal `claim` to unwrap WUSDe rewards to USDe and bridge them to USDC on another network after a quote review.

### Fixes

- Fixed Omni login after Variational changed its sign-in flow.
- Fixed limit orders waiting behind the market: they now move to the best price when the market leaves them by more than the spread, and with `limit_market_fallback = false` they keep waiting at the best price instead of failing the cycle.
- Fixed trading on markets with trading hours: positions are held through closures and closed after the market reopens, and `market_hours = "strict"` follows Omni's trading sessions.
- Fixed a failed cleanup, such as one on a closed market, letting the next trade open on top of positions that were still open; cleanup now retries before trading again.
- Fixed a position flipped to the opposite side not being detected as drift.
- Fixed an encrypted Omni `captcha_key` being sent to the captcha solver still encrypted.
- Fixed configs with `leverage = 50` being rejected.
- Fixed lowercase tickers in configs not matching exchange markets.
- Fixed N1 burn by calculating it from position history.
- Fixed HyENA burn to include trading fees.

### Improvements

- The version banner shows the commits since the last release, e.g. `v0.10.1-15-g6fb8e7b`.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.10.1...v0.11.0

---

## v0.10.1 – 2026-09-26

### Fixes

- Fixed Lighter points reporting to use leaderboard totals.

### Other

- Added a standalone script to check and claim the Big Short airdrop on BSC, with optional swaps from SHORT to USDT or BNB.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.10.0...v0.10.1

---

## v0.10.0 – 2026-09-20

### Features

- Added Lighter support on Robinhood Chain with account setup, account information, positions, trading, closing, deposits, and withdrawals.
- Added balance-targeted deposit and withdrawal plans for Omni and Lighter, including per-account selection and full-balance withdrawals.
- Added shared `addrs` and `move` commands to inspect wallets and move supported assets across EVM networks or to configured exchange deposit addresses.
- Added selectable named trading pools with independent account membership and strategy settings.
- Added Ethereal reward discovery and claiming.
- Added optional Supporter subscription activation and status commands without restricting existing features.

### Improvements

- Deprecated the closed HyENA and Ethereal integrations ahead of their removal.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.9.1...v0.10.0

---

## v0.9.1 – 2026-09-04

### Features

- Added trading support for Omni swap markets.

### Improvements

- Added clear shutdown progress messages while open positions are being closed.
- Improved shared CAPTCHA solver reliability by limiting concurrent tasks per client.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.9.0...v0.9.1

---

## v0.9.0 – 2026-09-02

### Breaking Changes

- Replaced the `zero1` integration with `n1` after 01.xyz migrated to N1.
- Removed the defunct Onyx integration.

### Features

- Added N1 account info, positions, statistics, closing, and trading support.
- Added shared `login` and `login --force` commands with automatic retries for all exchange apps.
- Added Rise points and rank reporting to account info, statistics, and weekly summaries.
- Added referral points boost reporting to Omni account info.

### Fixes

- Fixed Nado points retrieval after the rewards API moved to a new endpoint.
- Fixed Omni login and Cloudflare solver compatibility, including updated Astrum clearance responses and clearer out-of-funds guidance.
- Fixed long Omni history syncs triggering Cloudflare challenges by pacing paginated requests and warning about lengthy downloads.
- Fixed multi-exchange `foreach` commands stopping after the first failed exchange.

### Improvements

- Improved multi-account commands with bounded parallel execution and clearer account failure reporting.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.8.3...v0.9.0

---

## v0.8.3 – 2026-07-15

### Features

- Added optional `captcha_key` configuration for using a personal Astrum solver account with Omni.

### Fixes

- Fixed Omni requests failing on Cloudflare challenges by refreshing the required clearance automatically.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.8.2...v0.8.3

---

## v0.8.2 – 2026-07-14

### Fixes

- Fixed Omni login flow.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.8.1...v0.8.2

---

## v0.8.1 – 2026-06-14

### Features

- Added a unified command bridge for routing common commands and exchange-specific tools.
- Added positional exchange filters to weekly reports, so a single exchange can be shown for a selected week.
- Added burn efficiency per $100k of volume to weekly reports.
- Added weekly report sorting by name, volume, or burn.

### Fixes

- Fixed 01.xyz login and points authentication.
- Fixed RiseX open order parsing.
- Fixed weekly reports to include RiseX cached trading stats.
- Fixed Omni info volume reporting for accounts with their own referral code.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.8.0...v0.8.1

---

## v0.8.0 – 2026-06-06

### Features

- Added RiseX exchange support with account info, positions, statistics, closing, and trading.

### Improvements

- Improved limit order execution by repricing drifted orders before using market fallback.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.7.1...v0.8.0

---

## v0.7.1 – 2026-06-01

### Features

- Added configurable Nado market-hours checks for planned trades.

### Fixes

- Fixed Nado market-hours handling for open and reduce-only markets.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.7.0...v0.7.1

---

## v0.7.0 – 2026-05-31

### Features

- Added a proxy check command for configured accounts.
- Added order book inspection support for exchange clients.
- Added an entry spread gate to skip openings when market depth is too wide.
- Added limit order wait retries.
- Added optional trade log files.
- Added automatic release update notifications.
- Added experimental Omni competition status and join commands.
- Added Hyena reward claiming.
- Added experimental HyperLiquid account migration and legacy account warnings for Hyena and Onyx.
- Added experimental Nado market-hours tradeability checks.
- Added support for day-based duration strings.

### Fixes

- Fixed HyperLiquid unified account balances.
- Fixed Ethereal order book parsing and client error diagnostics.
- Fixed Omni support for RWA perpetual instruments.
- Fixed Omni entry quality estimates from RFQ quotes.
- Fixed configured symbol validation before trading.
- Fixed total limit order wait time reporting.
- Fixed Nado order submissions for accounts with minor clock drift.

### Improvements

- Improved Onyx burn attribution with builder fill archive data.
- Improved trade balancing by adjusting the final leg quantity.
- Improved weekly report period labels.
- Reduced repeated limit order waiting logs.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.5...v0.7.0

---

## v0.6.5 – 2026-05-14

### Fixes

- Fixed 01.xyz authentication for the latest login flow.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.4...v0.6.5

---

## v0.6.4 – 2026-05-06

### Fixes

- Fixed the 01.xyz login flow.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.3...v0.6.4

---

## v0.6.3 – 2026-05-01

### Fixes

- Fixed API synchronization for Omni and 01.xyz.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.2...v0.6.3

---

## v0.6.2 – 2026-04-05

### Fixes

- Fixed 01.xyz points authentication discovery, pagination, and fee rates.
- Fixed the displayed Git version.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.1...v0.6.2

---

## v0.6.1 – 2026-04-05

### Features

- Added configurable failure backoff for trade cycles.
- Kept resting limit orders open while the best bid and offer remain stable.

### Fixes

- Fixed Nado margin calculations and balance reporting for short positions.

### Improvements

- Improved private key error messages.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.0...v0.6.1

---

## v0.6.0 – 2026-03-30

### Features

- Added fills caching and statistics for Hyena and Onyx.
- Added a weekly burn report script.
- Added 01.xyz points and history synchronization.

### Fixes

- Fixed Nado balance and isolated margin calculations.
- Fixed inconsistent info and stats output across clients.
- Fixed Omni points reporting to use leaderboard v2 and show points rank.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.5.1...v0.6.0

---

## v0.5.1 – 2026-03-22

### Fixes

- Fixed Hyena and Onyx positions to show only the configured DEX.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.5.0...v0.5.1

---

## v0.5.0 – 2026-03-21

### Breaking Changes

- Renamed strategy and config references from `main` to `prime`.

### Features

- Added ZeroOne (01.xyz) exchange support.
- Added Onyx exchange support.
- Added HyperLiquid and Hyena exchange support.
- Added a positions command with margin metrics.
- Added Omni limit orders.
- Added balance-based trade sizing with `trade_size_pct`.

### Fixes

- Fixed trading to stop early when position size differs from the expected size.
- Fixed Nado partial fill handling so limit orders are not canceled too early.

### Improvements

- Improved API error messages.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.4.1...v0.5.0

---

## v0.4.1 – 2026-03-18

### Features

- Added isolated symbol support for Nado.
- Added referral code display in Omni info.
- Added balances to trade stop notifications.
- Added date ranges to period labels.

### Fixes

- Fixed Nado period labels and a table rendering crash.

### Improvements

- Unified statistics output across apps.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.4.0...v0.4.1

---

## v0.4.0 – 2026-03-12

### Features

- Added Telegram notifications.
- Added multi-symbol balanced mode.
- Added per-exchange minimum trade size enforcement.
- Added an OPN claim script.

### Fixes

- Fixed Pacifica balance display in info output.
- Fixed limit order polling and Nado live/archive gap handling.
- Fixed Ethereal points authentication and point totals.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.3.0...v0.4.0

---

## v0.3.0 – 2026-03-04

### Features

- Added anonymous usage telemetry with PostHog.
- Added grouped trading mode with configurable regrouping.
- Added Nado exchange support.
- Added Ethereal exchange support.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.2.0...v0.3.0

---

## v0.2.0 – 2026-02-21

### Features

- Added Omni app support.
- Added Pacifica daily report grouping and filtering.

### Fixes

- Fixed Omni genesis date handling to match the app UI and Discord.

### Improvements

- Improved config validation and error handling.

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.1.0...v0.2.0

---

## v0.1.0 – 2026-02-14

### Features

- Added the initial delta-neutral trading app for Pacifica.
- Added multi-account position management for hedged long and short trades.
- Added trading, closing, statistics, and config encryption commands.
- Added configurable trade sizing, timing, and custom config file support.

**Full Changelog**: https://github.com/vladkens/delta-farmer/commits/v0.1.0

---
