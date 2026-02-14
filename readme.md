# delta-farmer

<p align="center"><img src=".github/logo.svg" width="200" /></p>

Automated delta-neutral trading for crypto points farming. Execute hedged strategies across perpetual DEXs to maximize airdrops with minimal directional risk.

## Features

- 🎯 Delta-neutral trading strategies
- 🔄 Multi-account position management
- 📊 Real-time P&L tracking
- 🔐 Encrypted private key storage
- 🎲 Configurable trade sizes and timing

## Supported Platforms

- [Pacifica.fi](https://pacifica.fi) - Solana perpetuals DEX

**In Progress:**

- Variational - Coming soon
- Ethereal - Coming soon

More platforms coming soon (perp DEXs, prediction markets, etc.)

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

# Create and configure your config file
cp configs.example/pacifica.toml configs/pacifica.toml
# Edit configs/pacifica.toml with your settings

# Encrypt private keys (will prompt for password)
uv run -m apps.pacifica config encrypt

# Optional: Set password in .env to avoid prompts
# echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

## Usage

All apps share common commands for trading and configuration:

```bash
# Replace <app> with: pacifica, or other supported platforms

# Core trading commands
uv run -m apps.<app> trade   # Start automated trading
uv run -m apps.<app> close   # Close all open positions
uv run -m apps.<app> stats   # View trading statistics

# Configuration management
uv run -m apps.<app> config encrypt  # Encrypt private keys in config
uv run -m apps.<app> config decrypt  # Decrypt to view keys

# View app-specific commands
uv run -m apps.<app> --help
```

**Example:**

```bash
uv run -m apps.pacifica trade
uv run -m apps.pacifica --help
```

### Using Custom Configs

Use the `-c` flag to specify different config files. Each config contains both strategy parameters and accounts:

```bash
# Test different strategies
uv run -m apps.pacifica -c configs/pacifica-strategy1.toml trade
uv run -m apps.pacifica -c configs/pacifica-strategy2.toml trade

# Run multiple instances with different configs
uv run -m apps.pacifica -c configs/pacifica-set1.toml trade
uv run -m apps.pacifica -c configs/pacifica-set2.toml trade
```

## How It Works

Delta-neutral trading maintains zero directional exposure by opening equal but opposite positions:

1. Opens a LONG position on one account
2. Opens a SHORT position on another account
3. Positions offset each other, neutralizing price risk
4. Earns trading volume for points/airdrops
5. Closes positions after random duration
6. Repeats with configurable cooldown

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
