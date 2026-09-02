# delta-farmer

<p align="center">
  English · <a href="readme.ru.md">Русский</a> · <a href="readme.uk.md">Українська</a>
</p>

<p align="center"><img src=".github/logo.svg" width="200" /></p>

<div align="center">

[<img src="https://badges.ws/badge/-/%40uid127/000?icon=x&label" alt="x" />](https://x.com/uid127) [<img src="https://badges.ws/badge/-/Telegram%20Channel/2CA5E0?icon=telegram&label" alt="tg channel" />](https://t.me/+nkSWfo2QASdiOTI0) [<img src="https://badges.ws/badge/-/Telegram%20Chat/2CA5E0?icon=telegram&label" alt="tg chat" />](https://t.me/+JPqp0bteCWwzMDJk)

</div>

Automated delta-neutral trading for crypto points farming. Run classic two-sided hedges or balanced multi-symbol baskets across perpetual DEXs to maximize volume and points with limited directional risk.

- 🎯 **Delta-neutral by design** — matched long/short positions minimize directional exposure
- 🧩 **Multi-symbol basket mode** — trade 2–4 symbols in one cycle, each leg staying neutral
- 🔄 **Multi-account management** — one config file drives all your accounts simultaneously
- 👥 **Grouped trading** — split accounts into independent strategy groups
- 📊 **Real-time safety checks** — emergency close if ROI limits are breached
- 🔐 **Encrypted key storage** — private keys never sit in plaintext
- 📨 **Telegram notifications** — get alerts on trade start, stop, errors, and periodic reports
- 🎲 **Configurable sizing and timing** — randomized sizes and durations to vary on-chain patterns

---

## What is delta-farmer?

Delta-farmer is a trading bot that automatically opens matched long and short positions on perpetual DEXs. The idea is simple: by holding equal opposite-side trades, your net market exposure stays near zero — you're farming trading volume and protocol points rather than betting on price direction.

Each trading cycle, the bot:

1. Opens a **long** position on one account and a **short** on another (or splits across multiple assets)
2. Holds them for a configurable duration while monitoring risk
3. Closes everything cleanly and waits before the next cycle
4. Sends you a Telegram summary if configured

You control the size, timing, leverage, and which exchange to run on. The bot handles the rest.

---

## Supported Exchanges

| Name | Network | Link | Referral |
| --- | --- | --- | --- |
| Ethereal | EVM | [ethereal.trade](https://app.ethereal.trade/) | [Sign up](https://app.ethereal.trade/?ref=DSQ3BOJ65L3X) |
| HyENA | EVM | [hyena.trade](https://app.hyena.trade/) | [Sign up](https://app.hyena.trade/ref/VLADKENS) |
| Nado | EVM | [nado.xyz](https://app.nado.xyz/) | [Sign up](https://app.nado.xyz?join=yUAjz7a) |
| Omni | EVM | [variational.io](https://omni.variational.io) | [Sign up](https://omni.variational.io) |
| Pacifica | Solana | [pacifica.fi](https://app.pacifica.fi) | [Sign up](https://app.pacifica.fi?referral=uid127) |
| RiseX | EVM | [rise.trade](https://www.rise.trade/) | [Sign up](https://www.rise.trade/) |
| N1 | EVM | [n1.xyz](https://app.n1.xyz/) | [Sign up](https://app.n1.xyz/r/vladkens) |

### Exchange updates

- **01.xyz → N1** — the exchange moved to [n1.xyz](https://app.n1.xyz/).

---

## Installation

### Step 1 — Install prerequisites

#### macOS

Open **Terminal** (`Cmd + Space` → type "Terminal" → Enter) and run:

```bash
xcode-select --install
```

A dialog will pop up — click "Install". Once done, install uv ([official guide](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_1)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen Terminal so the `uv` command becomes available.

#### Windows

Open **PowerShell** (`Win + S` → type "PowerShell" → Enter) and run:

```powershell
winget install --id Git.Git -e --source winget
```

Then install uv ([official guide](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_2)):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen PowerShell so both `git` and `uv` become available.

### Step 2 — Download and run

```bash
git clone https://github.com/vladkens/delta-farmer.git
cd delta-farmer
```

That's it. Dependencies are installed automatically on the first run.

---

## Quick Start

Replace `<app>` with your exchange name throughout: `pacifica`, `omni`, `ethereal`, `nado`, `hyena`, `rise`, or `n1`.

**Step 1 — Create a config file**

```bash
uv run apps/<app>.py config new
```

This creates `configs/<app>.toml` pre-filled with sensible defaults. Open the file in any text editor.

**Step 2 — Add your private keys**

Find the `[[accounts]]` sections and paste your private keys:

```toml
[[accounts]]
name = "acc1"
privkey = "your-private-key-here"

[[accounts]]
name = "acc2"
privkey = "your-private-key-here"
```

You need at least **2 accounts** — one goes long, the other goes short.

**Step 3 — Encrypt your keys**

```bash
uv run apps/<app>.py config encrypt
```

You'll be prompted for a password. After this step, the raw keys are replaced with encrypted values in the file. You'll enter this password each time you start the bot (or set it in `.env` — see [Private Key Encryption & Passwords](#private-key-encryption--passwords)).

**Step 4 — Start trading**

```bash
uv run apps/<app>.py trade
```

---

## Commands

All exchanges share the same command structure. Replace `<app>` with your exchange name.

```bash
# Trading
uv run apps/<app>.py trade          # Start automated trading
uv run apps/<app>.py close          # Close all open positions
uv run apps/<app>.py info           # View account balances & points
uv run apps/<app>.py positions      # View current open positions
uv run apps/<app>.py login          # Check and restore all account logins
uv run apps/<app>.py login --force  # Force all accounts to log in again
uv run apps/<app>.py proxy          # Check configured proxies

# Statistics
uv run apps/<app>.py stats          # All cached periods (cached 1h)
uv run apps/<app>.py stats this     # Current period only
uv run apps/<app>.py stats last     # Previous period only
uv run apps/<app>.py stats W05      # Specific week/period prefix
uv run apps/<app>.py stats --force  # Force-refresh cached stats
uv run apps/<app>.py clean          # Delete all cached data

# Config management
uv run apps/<app>.py config new            # Create a new config file
uv run apps/<app>.py config new -c my.toml # Create at a custom path
uv run apps/<app>.py config encrypt        # Encrypt private keys in config
uv run apps/<app>.py config decrypt        # Decrypt to view raw keys

# Help
uv run apps/<app>.py --help
```

### Logs

By default, logs are printed only to the terminal. For trading runs, set `DF_LOG_FILE=1` to also write logs to `logs/<timestamp>-<app>.log`:

```bash
DF_LOG_FILE=1 uv run apps/<app>.py trade
```

### Weekly summary

`scripts/weekly.py` reads cached stats from `.cache`. Refresh exchange data first with `uv run apps/<app>.py stats --force` when you need fresh numbers.

```bash
uv run scripts/weekly.py                    # All-time summary by exchange
uv run scripts/weekly.py 0                  # Latest cached ISO week
uv run scripts/weekly.py -1                 # Previous ISO week
uv run scripts/weekly.py W14                # Specific week in the current year
uv run scripts/weekly.py 2026-W14           # Specific ISO week with year
uv run scripts/weekly.py --from W14 --to W22 # Summary for a week range
uv run scripts/weekly.py -P --from W14 --to W22 # Weekly detail inside the range
uv run scripts/weekly.py Hyena              # One exchange, all available periods
uv run scripts/weekly.py Hyena 0            # One exchange, latest cached ISO week
uv run scripts/weekly.py -e Hyena           # Legacy alias for one exchange
uv run scripts/weekly.py --burn             # Burn pivot by ISO week and exchange
uv run scripts/weekly.py --help             # Full weekly report help
```

### Exchange-specific commands

```bash
uv run apps/omni.py competition              # Show Omni competition status
uv run apps/omni.py competition --join       # Opt in all configured Omni accounts
uv run apps/hyena.py reward claim            # Claim Hyena rewards
uv run apps/hyena.py migrate                 # Migrate Hyena HyperLiquid accounts to unified mode
```

Omni competition commands check the active tournament window, join status, eligibility volume, and leaderboard places. The Hyena migration command switches HyperLiquid-backed accounts to Unified Account mode when the exchange reports a legacy account mode.

### Omni login issues

Some users report login issues with Omni. As a workaround, run `uv run apps/omni.py login`; it retries accounts sequentially every 30 seconds until they succeed. Use `login --force` for a fresh login, and try another proxy if the problem persists.

### Omni Cloudflare challenges

Omni handles Cloudflare challenges automatically. By default it uses the shared solver gateway, so no extra configuration is required. To use your own Astrum solver account instead, [register with Astrum](https://solver.astrum.foundation/), add funds, create an API key, and add it to `configs/omni.toml`:

```toml
captcha_key = "your-astrum-key"
```

`captcha_key` takes priority over the `CAPTCHA_KEY` value in `.env`. Leave it unset to use the shared gateway. Run `uv run apps/omni.py config encrypt` after adding the key.

---

## Configuration Reference

All settings live in your `configs/<app>.toml` file. Here is every available parameter:

### Core settings

| Parameter | Default | Description |
| --- | --- | --- |
| `leverage` | `10` | Leverage multiplier (1–49). Set it to the **lowest** max leverage across all your chosen symbols. |
| `symbols` | required | Trading pairs, e.g. `["BTC"]` or `["BTC", "ETH"]`. Check the exchange UI for available symbols. |
| `symbols_per_trade` | `1` | How many symbols to trade per cycle. `1` = classic mode and may sample one symbol from the list; `2`–`4` = basket mode and must match the length of `symbols`. |
| `market_hours` | `"auto"` | Market-hours pre-check mode: `"auto"` checks planned open only, `"strict"` checks planned open and close, `"off"` disables the pre-check. |
| `use_limit` | `false` | If `true`, the prime account opens with a limit order instead of a market order — reduces fees. |
| `first_as_prime` | `false` | If `true`, the first account in the list is always the prime (limit-side). If `false`, it rotates randomly each cycle. Ignored when `group_size` is set. |

### Trade sizing

Exactly one of these is required — you cannot use both.

| Parameter | Default | Description |
| --- | --- | --- |
| `trade_size_usd` | — | Total notional per cycle in USD, as a range: `{ min = 140, max = 160 }`. The amount is split 50% prime / 50% hedge. |
| `trade_size_pct` | — | Size as a fraction of account balance (e.g. `0.5` = 50%). The tightest account sets the binding constraint. |

### Timing

Durations accept seconds (`30`), strings like `"15s"`, `"5m"`, `"1h"`, `"3d"`, compound strings like `"1d2h30m"`, or a range `{ min = "15m", max = "20m" }`.

| Parameter         | Default  | Description                                          |
| ----------------- | -------- | ---------------------------------------------------- |
| `trade_duration`  | required | How long to hold positions each cycle.               |
| `trade_cooldown`  | required | Pause between cycles.                                |
| `trade_heartbeat` | `"15s"`  | How often safety checks run while holding positions. |

### Limit order settings

Only relevant when `use_limit = true`.

```toml
use_limit = true
limit_wait = "90s"
limit_wait_retries = 99
limit_market_fallback = true
```

| Parameter | Default | Description |
| --- | --- | --- |
| `limit_wait` | `"90s"` | How long to wait for a limit order to fill. |
| `limit_wait_retries` | `99` | Extra `limit_wait` windows to keep waiting while BBO stays near the original limit price. `0` = disabled. |
| `limit_market_fallback` | `true` | If the limit order times out, fall back to a market order. Set to `false` to abort the cycle instead. |

Maximum wait for one limit order is `limit_wait * (1 + limit_wait_retries)`. Higher retry counts favor maker fills, but require longer tradeability windows before opening and closing positions.

### Entry gate settings

Before opening a position, the bot can wait for acceptable entry spread/depth. Set `max_entry_spread_pct = null` to disable this gate.

```toml
max_entry_spread_pct = 0.25
entry_gate_wait = "5m"
entry_gate_poll = "3s"
```

| Parameter | Default | Description |
| --- | --- | --- |
| `max_entry_spread_pct` | `0.25` | Maximum estimated entry spread/depth percent before opening a position. |
| `entry_gate_wait` | `"5m"` | Maximum time to wait for acceptable entry quality before skipping. |
| `entry_gate_poll` | `"3s"` | How often to re-check entry quality while waiting. Must be 1–10 seconds. |

### Safety limits

| Parameter | Default | Description |
| --- | --- | --- |
| `position_roi_limit` | `0.8` | Emergency-close the full cycle if any single position reaches ±80% ROI. |
| `combined_roi_limit` | `0.1` | Emergency-close if the combined basket ROI reaches ±10%. |
| `max_failures` | `0` | Stop the strategy after this many consecutive cycle failures. `0` = never stop — retries indefinitely with exponential backoff (up to 1h between attempts). |

### Grouped trading

| Parameter | Default | Description |
| --- | --- | --- |
| `group_size` | — | Split accounts into independent groups. Must be 2–5. Total enabled accounts must be divisible by this number. |
| `regroup_interval` | — | Re-sort accounts by balance and restart groups on this interval. Only active when `group_size` is set. |

### Accounts

Add one `[[accounts]]` block per wallet.

| Parameter | Default | Description |
| --- | --- | --- |
| `name` | required | Display name shown in logs and stats. |
| `privkey` | required | Private key. Fill it in, then run `config encrypt`. |
| `proxy` | — | Optional HTTP proxy: `"http://user:pass@host:port"`. |
| `enabled` | `true` | Set to `false` to exclude this account from trading while keeping it in stats. |

### Telegram (optional)

Add a `[telegram]` block to enable notifications.

| Parameter | Default | Description |
| --- | --- | --- |
| `token` | — | Bot token from [@BotFather](https://t.me/BotFather). Run `config encrypt` after adding it. |
| `chat_id` | — | Your personal or group chat ID. Get it from [@userinfobot](https://t.me/userinfobot). |
| `notify` | all channels | List of notification channels to enable. Remove any to silence them: `"start"`, `"stop"`, `"errors"`, `"reports"`. |
| `report_interval` | `"1h"` | How often to send a periodic stats digest. |

---

## Trading Modes

### Classic mode (single symbol)

One cycle trades one symbol: one account goes long, the other goes short. If you configure multiple symbols with `symbols_per_trade = 1`, the bot samples one currently tradeable symbol per cycle.

```toml
symbols = ["BTC"]
symbols_per_trade = 1
trade_size_usd = { min = 140, max = 160 }
```

### Basket mode (multi-symbol)

One cycle trades multiple symbols simultaneously. Each symbol stays neutral, and each account also nets out across the full basket.

```toml
symbols = ["BTC", "ETH"]
symbols_per_trade = 2
trade_size_usd = { min = 140, max = 160 }
```

Rules:

- `symbols_per_trade` must exactly match the number of entries in `symbols`
- Maximum 4 symbols per trade
- Safety exits apply both per-position and combined basket ROI

### Grouped trading

Splits your accounts into independent strategy groups that run in parallel within one process.

```toml
group_size = 2
regroup_interval = "12h"
```

Rules:

- `group_size` must be between 2 and 5
- Total enabled account count must divide evenly by `group_size`
- `first_as_prime` is ignored when `group_size` is set
- `regroup_interval` re-balances groups by account balance and restarts them

---

## Safety Checks

Before opening a cycle, the bot can filter configured symbols by exchange market-hours data. With the default `market_hours = "auto"`, only the planned entry window is checked; planned close may land outside regular hours. Use `market_hours = "strict"` to require both planned entry and planned close to be inside regular trading hours, or `market_hours = "off"` to disable this pre-check. Symbols without market-hours metadata are treated as 24/7.

Every `trade_heartbeat` interval (default 15 seconds), the bot checks:

1. **Per-position ROI** — if any single leg's return crosses `±position_roi_limit` (default ±80%), all positions are closed immediately
2. **Combined basket ROI** — if the total basket return crosses `±combined_roi_limit` (default ±10%), all positions are closed immediately
3. **Position count** — if any symbol has an unexpected number of positions (e.g. one side was liquidated), all positions are closed immediately

These are last-resort protections. You should also use sensible leverage and trade sizes.

---

## Telegram Notifications

**Setup:**

1. Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot, copy the token
2. Message [@userinfobot](https://t.me/userinfobot) to get your chat ID
3. Add to your config:

```toml
[telegram]
token = "123456:ABC-DEF..."
chat_id = "123456789"
notify = ["start", "stop", "errors", "reports"]
report_interval = "1h"
```

4. Encrypt the token: `uv run apps/<app>.py config encrypt`
5. Test it: `uv run apps/<app>.py tgtest`

**Notification channels:**

| Channel   | When it fires                                  |
| --------- | ---------------------------------------------- |
| `start`   | A trade cycle opens (symbol, size, accounts)   |
| `stop`    | A trade cycle closes (PnL, duration)           |
| `errors`  | Cycle failures and crashes                     |
| `reports` | Periodic digest (trades, volume, burn, $/100k) |

Remove a channel from the `notify` list to silence it.

---

## Private Key Encryption & Passwords

Private keys in your config are encrypted using AES. After filling in raw keys, always run:

```bash
uv run apps/<app>.py config encrypt
```

The bot prompts for your password on startup. To skip the prompt, save the password in a `.env` file in the project folder:

```bash
echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

To view raw keys again (for backup or migration):

```bash
uv run apps/<app>.py config decrypt
```

---

## Running Multiple Instances / Custom Configs

Use the `-c` flag to point to a different config file:

```bash
uv run apps/pacifica.py -c configs/pacifica-set2.toml trade
```

This lets you run multiple independent instances of the same exchange with different accounts or settings:

```bash
# Terminal 1
uv run apps/omni.py -c configs/omni-set1.toml trade

# Terminal 2
uv run apps/omni.py -c configs/omni-set2.toml trade
```

---

## Updating

```bash
# Stop running instances (Ctrl+C or kill the process)

# Pull latest changes
git pull

# Install the locked dependency set
uv sync --locked

# Restart trading
uv run apps/<app>.py trade
```

---

## Recommended Services

- [**Digital Ocean**](https://m.do.co/c/a97fd963258f) — VPS for running the bot 24/7 in the background
- [**Proxy Shard**](https://proxyshard.com?ref=5406) — proxies for separating account traffic

---

## Telemetry

Delta-farmer collects anonymous usage statistics (exchange name, command used, technical config flags) to understand adoption and popular features. No wallet addresses, balances, or strategy parameters are ever sent.

Set `DF_TELEMETRY=0` to opt out completely.

## Environment Variables

| Variable                  | Description                                            |
| ------------------------- | ------------------------------------------------------ |
| `DF_CONFIG_PASSWORD`      | Config encryption password for non-interactive runs.   |
| `DF_LOG_FILE=1`           | Also write trade logs to `logs/<timestamp>-<app>.log`. |
| `DF_NO_UPDATE_NOTIFIER=1` | Disable release update checks.                         |
| `DF_TELEMETRY=0`          | Disable anonymous usage telemetry.                     |

## Risk Disclaimer

**USE AT YOUR OWN RISK**

- This software is for educational purposes only
- Trading cryptocurrencies carries significant financial risk
- You may lose all deposited funds
- No guarantees of profit or airdrop eligibility
- Always test with small amounts first
- The authors are not responsible for any losses

---

## Contact & Feedback

- **X/Twitter:** [@uid127](https://x.com/uid127)
- **Telegram channel:** [@eazyrekt](https://t.me/s/eazyrekt) — drop farming insights & updates
- **Telegram chat:** [Join the group](https://t.me/+JPqp0bteCWwzMDJk)
