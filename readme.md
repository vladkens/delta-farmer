# delta-farmer

<p align="center"><img src=".github/logo.svg" width="200" /></p>

Automated delta-neutral trading for crypto points farming. Execute hedged strategies across perpetual DEXs to maximize airdrops with minimal directional risk.

## Features

- 🎯 Delta-neutral trading strategies
- 🔄 Multi-account position management
- 📊 Real-time P&L tracking
- 🔐 Encrypted private key storage
- 🎲 Configurable trade sizes and timing

## Supported Protocols

| Protocol                                | Tech   | Status      | Description                   |
| --------------------------------------- | ------ | ----------- | ----------------------------- |
| [Pacifica](https://pacifica.fi)         | Solana | Ready       | Perpetuals DEX                |
| [Omni](https://omni.variational.io)     | EVM    | Ready       | Perpetuals DEX by Variational |
| [Ethereal](https://app.ethereal.trade/) | EVM    | Coming Soon | —                             |
| [Nado](https://app.nado.xyz/)           | EVM    | Coming Soon | —                             |

## Installation

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer) package manager

### Setup

```bash
# Clone the repository
git clone https://github.com/vladkens/delta-farmer.git && cd delta-farmer

# Install dependencies
uv sync
```

## Quick Start

### Pacifica (Solana)

```bash
# Create config
cp configs.example/pacifica.toml configs/pacifica.toml
# Edit configs/pacifica.toml with your Solana private keys

# Encrypt private keys
uv run -m apps.pacifica config encrypt

# Start trading
uv run -m apps.pacifica trade
```

### Omni (EVM)

```bash
# Create config
cp configs.example/omni.toml configs/omni.toml
# Edit configs/omni.toml with your EVM private keys

# Encrypt private keys
uv run -m apps.omni config encrypt

# Start trading
uv run -m apps.omni trade
```

## Usage

All protocols share common commands:

```bash
# Replace <app> with: pacifica, omni

# Trading
uv run -m apps.<app> trade          # Start automated trading
uv run -m apps.<app> close          # Close all open positions
uv run -m apps.<app> info           # View account balances & points
uv run -m apps.<app> stats          # View trading statistics (cached 1h)
uv run -m apps.<app> stats last     # Show only previous period
uv run -m apps.<app> stats --sync   # Force refresh cached data
uv run -m apps.<app> clean          # Delete cached data

# Config management
uv run -m apps.<app> config encrypt  # Encrypt private keys
uv run -m apps.<app> config decrypt  # Decrypt to view keys

# Help
uv run -m apps.<app> --help
```

### Using Custom Configs

Use the `-c` flag to specify different config files:

```bash
# Run with custom config
uv run -m apps.pacifica -c configs/pacifica-alt.toml trade

# Run multiple instances
uv run -m apps.omni -c configs/omni-set1.toml trade
uv run -m apps.omni -c configs/omni-set2.toml trade
```

### Password Management

```bash
# Set password in .env to avoid prompts
echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

## Updating

To update to the latest version:

```bash
# Stop running instances (Ctrl+C or kill the process)

# Pull latest changes
git pull

# Update dependencies
uv sync

# Restart trading
uv run -m apps.<app> trade
```

## How It Works

Delta-neutral trading maintains zero directional exposure by opening equal but opposite positions:

1. Opens a LONG position on one account
2. Opens a SHORT position on another account
3. Positions offset each other, neutralizing price risk
4. Earns trading volume for points/airdrops
5. Closes positions after random duration
6. Repeats with configurable cooldown

## Recommended Services

- [**Digital Ocean**](https://m.do.co/c/a97fd963258f) – VPC for running bots 24/7 in background
- [**Proxy Shard**](https://proxyshard.com?ref=5406) – proxies for crypto activities

## Risk Disclaimer

**⚠️ USE AT YOUR OWN RISK**

- This software is for educational purposes
- Trading cryptocurrencies carries significant financial risk
- You may lose all deposited funds
- No guarantees of profit or airdrop eligibility
- Always test with small amounts first
- The authors are not responsible for any losses

## Contact & Feedback

I'd love to hear your feedback on usage, features, and improvements!

- **X/Twitter:** [@uid127](https://x.com/uid127)
- **Telegram:** [@eazyrekt](https://t.me/s/eazyrekt) - drop farming insights & updates
