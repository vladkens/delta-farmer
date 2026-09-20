# delta-farmer

<p align="center">
  English · <a href="readme.ru.md">Русский</a> · <a href="readme.uk.md">Українська</a>
</p>

<p align="center"><img src=".github/logo.png" width="320" alt="delta-farmer dung beetle mascot" /></p>

<div align="center">

[![Follow @uid127 on X](https://badges.ws/badge/-/Follow%20%40uid127/000?icon=x&label)](https://x.com/uid127) [![Project updates on Telegram](https://badges.ws/badge/-/Project%20Updates/2CA5E0?icon=telegram&label)](https://t.me/eazyrekt) [![Community chat on Telegram](https://badges.ws/badge/-/Community%20Chat/2CA5E0?icon=telegram&label)](https://t.me/+JPqp0bteCWwzMDJk) [![Support or subscribe](https://badges.ws/badge/-/Support%20%26%20Subscribe/FF6B35?icon=telegram&label)](https://t.me/deltafarm_bot)

</div>

Automated delta-neutral trading for crypto points farming. Run two-sided hedges or balanced multi-symbol baskets across perpetual exchanges to generate volume and maintain open interest with limited directional exposure.

- 🎯 **Delta-neutral by design** — balanced long and short positions reduce directional exposure
- 🧩 **Single markets or baskets** — trade one symbol or a neutral basket of two to four symbols
- 🔄 **Multi-account management** — run multiple accounts from one encrypted configuration
- 🎛️ **Flexible strategy controls** — configure markets, size, leverage, timing, entry rules, and risk limits
- 👥 **Pools and parallel groups** — run independent strategies and periodically regroup accounts by balance
- 🛡️ **Safer execution** — use market or maker-first orders with preflight checks and full-cycle cleanup
- 💸 **Funding automation** — deposit and withdraw on Omni or Lighter, move assets across EVM networks, or send them to a CEX
- 📊 **Monitoring and alerts** — track balances, positions, points, volume, and P/L with Telegram notifications

---

## What is delta-farmer?

Delta-farmer is a CLI trading bot that opens matched long and short perpetual positions across your accounts. Instead of betting on market direction, it aims to keep net exposure close to zero while generating trading volume and maintaining open interest for points programs.

Each trading cycle:

1. Selects the configured market or basket and validates the complete action plan.
2. Opens a prime position and balances it with opposite-side positions on the other accounts.
3. Holds the hedge for the configured duration while monitoring execution, position health, and risk limits.
4. Closes every leg, waits for the cooldown, and starts the next cycle.

You control the accounts, markets, position size, leverage, timing, order type, and safety limits. Named pools can run completely different strategies from the same config, while account groups can execute in parallel.

> [!WARNING]
> Delta-neutral does not mean risk-free. Fees, funding, spread, slippage, liquidation, exchange failures, bridge failures, and an incomplete hedge can all lose money. Start small and supervise the first runs.

## Supported exchanges

Each exchange has a launcher under `apps/`. Use the launcher name as `<app>` in the commands below.

| App        | Exchange                                                          | Wallet | Deposit / withdraw | Extra tools                   |
| ---------- | ----------------------------------------------------------------- | ------ | ------------------ | ----------------------------- |
| `lighter`  | [Lighter on Robinhood Chain](https://robinhoodchain.lighter.xyz/) | EVM    | Yes                | Referral codes                |
| `n1`       | [N1](https://app.n1.xyz/r/vladkens)                               | EVM    | —                  | —                             |
| `nado`     | [Nado](https://app.nado.xyz?join=yUAjz7a)                         | EVM    | —                  | —                             |
| `omni`     | [Omni](https://omni.variational.io)                               | EVM    | Yes                | Competition status and opt-in |
| `pacifica` | [Pacifica](https://app.pacifica.fi?referral=uid127)               | Solana | —                  | —                             |
| `rise`     | [RISE](https://www.rise.trade/)                                   | EVM    | —                  | —                             |

Delta-farmer previously supported Ethereal and HyENA, but both projects have since closed.

## Installation

You need [Git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/getting-started/installation/). Uv installs the pinned Python version and locked dependencies for the project.

Install uv on macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, install Git and uv from PowerShell:

```powershell
winget install --id Git.Git -e --source winget
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Clone the project and install its locked environment:

```bash
git clone https://github.com/vladkens/delta-farmer.git
cd delta-farmer
uv sync --locked
```

## Quick start

The examples use Omni. Replace `omni` with any active launcher from the table above.

Create a config from the bundled template:

```bash
uv run apps/omni.py config new
```

Open `configs/omni.toml` and configure a strategy with at least two accounts:

```toml
leverage = 10
symbols = ["BTC", "ETH"]
symbols_per_trade = 1
use_limit = true
first_as_prime = true

trade_size_usd = { min = 140, max = 160 }
trade_duration = { min = "15m", max = "20m" }
trade_cooldown = { min = "3m", max = "6m" }

[[accounts]]
name = "acc1"
privkey = "your-private-key"
# proxy = "http://user:pass@host:port"
# cex_addr = "0xYourExchangeDepositAddress"

[[accounts]]
name = "acc2"
privkey = "your-private-key"
```

Encrypt the private keys immediately:

```bash
uv run apps/omni.py config encrypt
```

Check the configuration and account state before trading:

```bash
uv run apps/omni.py login
uv run apps/omni.py info
uv run apps/omni.py positions
# Only needed when accounts use proxies
uv run apps/omni.py proxy
```

Start the strategy:

```bash
uv run apps/omni.py trade
```

Press `Ctrl+C` once for a graceful stop. The strategy closes its active positions before exiting. To cancel open orders and close every position without starting a trading loop, run:

```bash
uv run apps/omni.py close
```

## Commands

| Command                                      | Available on                         | Purpose                                                                          |
| -------------------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------- |
| `trade [POOL]`                               | All active launchers                 | Start automated trading, optionally using a named pool.                          |
| `close`                                      | All active launchers                 | Cancel open orders and close all positions on enabled accounts.                  |
| `positions`                                  | All active launchers                 | Show current positions and risk metrics.                                         |
| `info`                                       | All active launchers                 | Show balances and exchange-specific account metrics.                             |
| `stats [FILTER] [-g {week,day}] [--force]`   | All active launchers                 | Show period statistics and optionally refresh cached history.                    |
| `addrs`                                      | All active launchers                 | Show the wallet address derived for every configured account.                    |
| `login [--force]`                            | All active launchers                 | Check account sessions and retry failed logins; `--force` starts fresh sessions. |
| `proxy`                                      | All active launchers                 | Check configured proxies, public IPs, and latency.                               |
| `move [SOURCE] [TARGET] [-a ACCOUNT] [-q QUANTITY]` | Lighter, N1, Nado, Omni, and RISE | Show wallet balances or execute an EVM move or CEX transfer.                     |
| `deposit [-a NAME]`                          | Omni and Lighter                     | Top exchange balances up toward their configured targets.                        |
| `withdraw [-a NAME] [--full]`                | Omni and Lighter                     | Withdraw toward configured targets or withdraw the full safe balance.            |
| `useref CODE [-a NAME]`                      | Lighter                              | Apply a referral code to every enabled account or one selected account.           |
| `competition`                                | Omni                                 | Show the active competition and offer to join eligible accounts.                 |
| `clean`                                      | All active launchers                 | Delete locally cached exchange history.                                          |
| `config new`                                 | All active launchers                 | Create a config from `config.example.toml`.                                      |
| `config encrypt`                             | All active launchers                 | Encrypt private keys and other supported secrets in place.                       |
| `config decrypt`                             | All active launchers                 | Restore plaintext secrets for inspection or migration.                           |
| `license`                                    | All active launchers                 | Show Supporter status and offer activation when no key is configured.            |

Lighter currently uses its account summary for `stats`; the other active launchers provide period reports. Run `uv run apps/<app>.py --help` or append `--help` to a public subcommand for its exact options. Use `-c` before or after a subcommand to select another config file.

## Configuration

### Accounts and secrets

Every `[[accounts]]` block accepts these fields:

| Field      | Required | Description                                                                                                         |
| ---------- | -------- | ------------------------------------------------------------------------------------------------------------------- |
| `name`     | Yes      | Unique name used in logs, tables, pools, and `-a` selectors.                                                        |
| `privkey`  | Yes      | Wallet private key. Encrypt the config after entering it.                                                           |
| `proxy`    | No       | Account-specific HTTP proxy, including credentials when required.                                                   |
| `cex_addr` | No       | Checksummed EVM deposit address used by `move ... cex`.                                                             |
| `enabled`  | No       | Defaults to `true`; disabled accounts stay visible in reports but are excluded from trading and balance operations. |

`config encrypt` protects private keys, Telegram tokens, and supported app-specific secrets. The password is requested when the config is opened. For unattended runs, place it in the environment or a `.env` file in the project directory:

```dotenv
DF_CONFIG_PASSWORD=your-config-password
```

Keep the password and a separate backup of the original keys away from the machine running the bot.

### Strategy basics

Set `leverage` no higher than the lowest maximum supported by every configured symbol. Configure exactly one sizing mode:

```toml
# Fixed total notional for the complete cycle
trade_size_usd = { min = 140, max = 160 }

# Or a fraction of available balance; 0.5 means 50%
# trade_size_pct = 0.5
```

`trade_duration` controls how long positions remain open, while `trade_cooldown` controls the pause after a completed cycle. Both accept ranges and sample a new value each cycle. Durations may be seconds (`30`) or strings such as `"15s"`, `"5m"`, `"1h"`, or `"1d2h30m"`.

Classic mode selects one tradeable symbol from the configured list:

```toml
symbols = ["BTC", "ETH", "SOL"]
symbols_per_trade = 1
```

Basket mode trades two to four symbols in the same cycle. Here `symbols_per_trade` must equal the number of configured symbols:

```toml
symbols = ["BTC", "ETH"]
symbols_per_trade = 2
```

### Named pools

Named pools let one exchange config hold independent strategies with different account sets and trading parameters:

```toml
[pools.majors]
accounts = ["acc1", "acc2"]
symbols = ["BTC", "ETH"]
trade_size_usd = { min = 140, max = 160 }
trade_duration = { min = "15m", max = "20m" }
trade_cooldown = { min = "3m", max = "6m" }

[pools.stocks]
accounts = ["acc3", "acc4"]
symbols = ["HOOD", "CRCL"]
trade_size_usd = { min = 100, max = 120 }
trade_duration = { min = "10m", max = "15m" }
trade_cooldown = { min = "5m", max = "8m" }
```

Every enabled account must belong to exactly one pool, and a pool needs at least two accounts. Top-level trading settings cannot be mixed with pools; app-level balance settings, `[telegram]`, and `[[accounts]]` remain outside them.

Pass the pool name to `trade`. One process runs one selected pool, so start separate processes when pools should trade concurrently:

```bash
uv run apps/<app>.py trade majors
uv run apps/<app>.py trade stocks -c configs/<app>.toml
```

### Parallel groups

Within a single strategy or named pool, `group_size` divides accounts into groups that trade in parallel:

```toml
group_size = 2
regroup_interval = "12h"
```

Each group must contain two to five accounts, and the enabled account count must divide evenly by `group_size`. Without grouping, a strategy supports up to five enabled accounts. When `regroup_interval` is set, accounts are periodically sorted by balance and assigned to new groups. `first_as_prime` is ignored in grouped mode.

### Orders and entry quality

Market orders are the default. With `use_limit = true`, the prime account starts with a maker-first limit order and the remaining accounts hedge it:

```toml
use_limit = true
limit_wait = "90s"
limit_wait_retries = 99
limit_market_fallback = true
first_as_prime = true
```

`limit_wait_retries` adds another wait window while the best price remains near the original limit. If the order still does not fill, `limit_market_fallback = true` permits a market order; set it to `false` to abort the cycle instead.

Before opening, the strategy estimates spread and available order-book depth for the planned size:

```toml
max_entry_spread_pct = 0.25
entry_gate_wait = "5m"
entry_gate_poll = "3s"
```

### Safety settings

```toml
trade_heartbeat = "15s"
position_roi_limit = 0.8
combined_roi_limit = 0.1
max_failures = 0
market_hours = "auto"
```

While a cycle is open, the strategy checks position count, missing legs, size drift, per-position ROI, and combined ROI. A failed cycle cleans up every account before retrying. `max_failures = 0` retries indefinitely with backoff; a positive value stops after that many consecutive failures.

`market_hours = "auto"` checks the planned entry window, `"strict"` checks both entry and close windows, and `"off"` disables the schedule check. Markets without schedule metadata are treated as continuously tradeable.

These checks reduce operational risk, but they cannot guarantee a complete hedge or prevent losses.

### Telegram notifications

Create a bot with [BotFather](https://t.me/BotFather), obtain the destination chat ID, and add:

```toml
[telegram]
token = "123456:ABC-DEF"
chat_id = "123456789"
notify = ["start", "stop", "errors", "reports"]
report_interval = "1h"
```

Run `config encrypt` again after adding the token.

## Funding and wallet tools

The `deposit` and `withdraw` commands manage exchange balances on Omni and Lighter. The `move` command is available through every EVM launcher and handles wallet balances, cross-network routes, and transfers to per-account CEX deposit addresses. Each operation shows the complete plan and asks for confirmation before sending anything.

### The `move` command

Run `move` without arguments to scan enabled wallets for stablecoins and native gas across every supported network:

```bash
uv run apps/lighter.py move
uv run apps/lighter.py move -a acc1
```

Sources and destinations use `network:asset`, such as `arb:usdc` or `base:usdc`. The amount defaults to the maximum available balance for every selected account; use `-q` for a fixed quantity per account and `-a` to select one account.

Move an entire token balance between networks:

```bash
uv run apps/lighter.py move arb:usdc base:usdc
```

Use a bare source network to collect every supported stablecoin found there into one destination asset. A fixed quantity cannot be combined with a bare network source:

```bash
uv run apps/lighter.py move bsc arb:usdc
```

Move a fixed amount for one account:

```bash
uv run apps/lighter.py move arb:usdc base:usdc -a acc1 -q 100
```

Use `cex` as the destination to transfer an asset directly to each account's configured `cex_addr` on the source network:

```bash
uv run apps/lighter.py move arb:usdc cex -a acc1 -q 100
```

Before using `cex`, verify that the exchange accepts that exact asset on that exact network. A valid address does not prove that the destination supports the deposit.

| Code   | Network         | Assets           |
| ------ | --------------- | ---------------- |
| `eth`  | Ethereum        | USDC, USDT, ETH  |
| `op`   | Optimism        | USDC, USDT, ETH  |
| `bsc`  | BNB Smart Chain | USDC, USDT, BNB  |
| `base` | Base            | USDC, USDT, ETH  |
| `arb`  | Arbitrum        | USDC, USDT, ETH  |
| `rh`   | Robinhood Chain | USDG, ETH        |
| `hevm` | HyperEVM        | USDC, USDT, HYPE |

Cross-network and cross-asset moves use Relay. Routes longer than 10 minutes or with a worst-case loss above the larger of $0.25 and 2% are rejected. Keep enough native gas on the source wallet; when needed and available, the route also tops up destination gas.

### Deposits and withdrawals

Automated deposits and withdrawals are currently supported only by the Omni and Lighter clients.

#### Deposits

Automated deposit plans top exchange balances up to a configured target. Add these app-level settings before any `[pools.*]` tables:

```toml
balance_target = 500
balance_target_jitter_pct = 2
deposit_min_amount = 10
balance_transfer_delay = { min = "2m", max = "4m" }
```

The target is randomized independently for each account within `±balance_target_jitter_pct`; set it to `0` for an exact target. Before confirmation, the plan shows the exchange-provided network and token, current exchange and wallet balances, target, minimum, and amount.

```bash
# All enabled accounts
uv run apps/omni.py deposit
uv run apps/lighter.py deposit

# One enabled account
uv run apps/omni.py deposit -a acc1
```

The first Lighter deposit registers the account. Run `uv run apps/lighter.py login` after that deposit is credited.

#### Withdrawals

Without `--full`, withdrawals reduce each exchange balance toward `balance_target`. With `--full`, they withdraw the safe available balance and do not require a target:

```bash
uv run apps/lighter.py withdraw
uv run apps/lighter.py withdraw -a acc1 --full
uv run apps/omni.py withdraw -a acc1
uv run apps/omni.py withdraw --full
```

The plan accounts for exchange-reported availability and fees. Accounts with open positions or orders are identified before execution; a full withdrawal stops if any selected account is blocked.

## Exchange-specific commands

### Lighter referral codes

Apply a referral code to every enabled Lighter account, or select one enabled account with `-a`:

```bash
uv run apps/lighter.py useref CODE
uv run apps/lighter.py useref CODE -a ACCOUNT
```

### Omni competition

Check the active Omni competition, account eligibility, volume, and rankings. If eligible accounts have not joined, the command asks for confirmation before opting them in:

```bash
uv run apps/omni.py competition
```

Omni handles Cloudflare challenges through the shared solver by default. To use a personal [Astrum](https://solver.astrum.foundation/) account, add `captcha_key = "your-key"` to the Omni config and encrypt it, or set `CAPTCHA_KEY` in the environment.

## Reports and logs

Exchange statistics are cached locally to avoid downloading complete histories on every run:

```bash
uv run apps/omni.py stats
uv run apps/omni.py stats this
uv run apps/omni.py stats W05
uv run apps/omni.py stats -g day --force
```

Build a combined report from the cached exchange data with `scripts/weekly.py`:

```bash
uv run scripts/weekly.py
uv run scripts/weekly.py 0
uv run scripts/weekly.py 2026-W14
uv run scripts/weekly.py --from W14 --to W22
uv run scripts/weekly.py --burn
```

Run `uv run scripts/weekly.py --help` for all filters. Refresh the relevant launcher with `stats --force` first when the report must include the latest history.

Terminal logging is the default. Set `DF_LOG_FILE=1` to also write trading logs under `logs/`:

```bash
DF_LOG_FILE=1 uv run apps/omni.py trade
```

## Multiple configs

Use `-c` to select another config. The flag works before or after a subcommand:

```bash
uv run apps/omni.py -c configs/omni-set1.toml trade
uv run apps/omni.py trade -c configs/omni-set2.toml
```

Separate configs can run in separate terminals or services. Do not place the same account in concurrent trading processes.

## Updating

Stop active trading gracefully, then update the checkout and locked environment:

```bash
git pull --ff-only
uv sync --locked
```

Read [changelog.md](changelog.md), then rerun `info` and `positions` before restarting unattended instances.

## Privacy and Supporter subscription

Delta-farmer sends anonymous operational telemetry containing a hashed machine identifier, launcher and command names, platform and version information, and coarse feature flags. It does not include private keys, wallet addresses, balances, symbol names, or trade sizes. Set `DF_TELEMETRY=0` to disable it.

You can [buy a Supporter subscription or make a donation through @deltafarm_bot](https://t.me/deltafarm_bot). The subscription helps fund maintenance, while every MIT-licensed feature remains usable without payment. The app keeps hashed wallet identities locally for a rolling 30-day account count and shows a short reminder when no suitable Supporter key is active.

```bash
uv run apps/omni.py license
```

The command shows the current status and offers key activation when none is configured. Supporter checks fail open: a service outage never blocks trading, position closure, deposits, withdrawals, or other existing commands. An active subscription that covers the local account count can set `DF_NO_BANNER=1` to hide the startup banner.

## Environment variables

| Variable                  | Purpose                                                                               |
| ------------------------- | ------------------------------------------------------------------------------------- |
| `DF_CONFIG_PASSWORD`      | Supply the config encryption password for unattended runs.                            |
| `DF_LOG_FILE=1`           | Write trading logs to a timestamped file under `logs/`.                               |
| `DF_NO_UPDATE_NOTIFIER=1` | Disable release update checks.                                                        |
| `DF_TELEMETRY=0`          | Disable anonymous operational telemetry.                                              |
| `DF_ACCOUNTS_CONCURRENCY` | Limit concurrent account requests; defaults to `3`.                                   |
| `DF_NO_BANNER=1`          | Hide the banner when an active Supporter subscription covers the local account count. |
| `CAPTCHA_KEY`             | Use a personal Astrum solver key for Omni.                                            |
| `LOGURU_LEVEL`            | Set the log level, for example `INFO` or `DEBUG`.                                     |

## Risk disclosure

Delta-farmer is provided as-is, without a guarantee of profit, points, airdrop eligibility, order execution, withdrawal availability, or preservation of funds. You are responsible for private-key security, account permissions, leverage, balances, gas, proxies, exchange rules, tax obligations, and every transaction approved through the tool. Never deposit more than you can afford to lose.

## Support and license

- Follow [@uid127 on X](https://x.com/uid127) for project updates.
- Read the [Telegram channel](https://t.me/eazyrekt) for release notes and farming updates.
- Ask questions in the [Telegram chat](https://t.me/+JPqp0bteCWwzMDJk).
- [Support development or buy a Supporter subscription](https://t.me/deltafarm_bot).
- Report reproducible defects through [GitHub Issues](https://github.com/vladkens/delta-farmer/issues).

Delta-farmer is released under the [MIT License](LICENSE).
