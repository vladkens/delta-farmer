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
