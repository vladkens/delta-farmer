import pytest

from lib.errors import AppError
from scripts.smoke import resolve_target, split_core_twin
from strategy import AppConfig

# The readme's Lighter RH/Core hedge pool: one account, whose wallet holds both legs.
HEDGE_CONFIG = """
[[accounts]]
name = "main"
privkey = "your-private-key"

[pools.hedge-crypto]
accounts = ["main"]
symbols = ["BTC"]
long_exchange = "lighter"
trade_size_usd = { min = 1000, max = 1200 }
trade_duration = { min = "2h", max = "4h" }
trade_cooldown = { min = "5m", max = "10m" }
"""

TWO_POOLS_CONFIG = """
[[accounts]]
name = "spare"
privkey = "k"
enabled = false

[[accounts]]
name = "one"
privkey = "k"

[[accounts]]
name = "two"
privkey = "k"

[[accounts]]
name = "three"
privkey = "k"

[[accounts]]
name = "four"
privkey = "k"

[pools.btc]
accounts = ["two", "one"]
symbols = ["BTC"]
trade_duration = [60, 60]
trade_cooldown = [10, 10]

[pools.eth]
accounts = ["three", "four"]
symbols = ["ETH", "AVAX"]
trade_duration = [60, 60]
trade_cooldown = [10, 10]
"""

FLAT_CONFIG = """
symbols = ["BTC", "ETH"]
trade_duration = [60, 60]
trade_cooldown = [10, 10]

[[accounts]]
name = "one"
privkey = "k"
enabled = false

[[accounts]]
name = "two"
privkey = "k"
"""


def load(tmp_path, text: str) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return AppConfig.load(str(path))


def test_hedge_pool_is_smoke_testable(tmp_path):
    cfg = load(tmp_path, HEDGE_CONFIG)
    target = resolve_target(cfg, "hedge-crypto", None)

    assert (target.account.name, target.symbols, target.pools) == (
        "main",
        ["BTC"],
        ["hedge-crypto"],
    )


@pytest.mark.parametrize(
    ("exchange", "name", "expected"),
    [
        ("lighter", "main@core", ("main", True)),
        ("lighter", "main", ("main", False)),
        ("lighter", None, (None, False)),
        ("nado", "main@core", ("main@core", False)),  # only Lighter has a Core twin
    ],
)
def test_core_twin_is_named_by_its_account(exchange, name, expected):
    assert split_core_twin(exchange, name) == expected


def test_core_twin_name_alone_finds_its_pool(tmp_path):
    # The readme tells the operator to pass `-a main@core`, without a pool name.
    cfg = load(tmp_path, HEDGE_CONFIG)
    name, core_twin = split_core_twin("lighter", "main@core")
    target = resolve_target(cfg, None, name)

    assert (target.account.name, core_twin) == ("main", True)
    assert (target.symbols, target.pools) == (["BTC"], ["hedge-crypto"])


def test_pool_and_core_twin_name_together(tmp_path):
    cfg = load(tmp_path, HEDGE_CONFIG)
    name, _ = split_core_twin("lighter", "main@core")
    target = resolve_target(cfg, "hedge-crypto", name)

    assert (target.account.name, target.symbols, target.pools) == (
        "main",
        ["BTC"],
        ["hedge-crypto"],
    )


def test_account_name_uses_only_its_own_pool(tmp_path):
    cfg = load(tmp_path, TWO_POOLS_CONFIG)
    target = resolve_target(cfg, None, "three")

    assert (target.symbols, target.pools) == (["ETH", "AVAX"], ["eth"])


def test_pools_config_never_guesses_the_account(tmp_path):
    # Picking the first enabled account of the whole config can aim real orders at another pool.
    cfg = load(tmp_path, HEDGE_CONFIG)

    with pytest.raises(AppError, match="name a pool with -p or an account with -a"):
        resolve_target(cfg, None, None)


def test_pool_picks_its_own_first_enabled_account(tmp_path):
    cfg = load(tmp_path, TWO_POOLS_CONFIG)
    target = resolve_target(cfg, "eth", None)

    assert (target.account.name, target.symbols, target.pools) == (
        "three",
        ["ETH", "AVAX"],
        ["eth"],
    )


def test_pool_account_order_follows_the_config(tmp_path):
    # `accounts = ["two", "one"]` must not reorder the account list smoke selects from.
    cfg = load(tmp_path, TWO_POOLS_CONFIG)
    target = resolve_target(cfg, "btc", None)

    assert target.account.name == "one"


def test_account_outside_the_named_pool_is_refused(tmp_path):
    cfg = load(tmp_path, TWO_POOLS_CONFIG)

    with pytest.raises(AppError, match="account 'three' is not in pool 'btc': two, one"):
        resolve_target(cfg, "btc", "three")


def test_unknown_pool_is_refused(tmp_path):
    cfg = load(tmp_path, TWO_POOLS_CONFIG)

    with pytest.raises(AppError, match="unknown pool 'hedge'; available: btc, eth"):
        resolve_target(cfg, "hedge", None)


def test_unknown_account_is_refused(tmp_path):
    cfg = load(tmp_path, FLAT_CONFIG)

    with pytest.raises(AppError, match="account 'three' not found; available: one, two"):
        resolve_target(cfg, None, "three")


def test_pool_name_needs_a_pools_config(tmp_path):
    cfg = load(tmp_path, FLAT_CONFIG)

    with pytest.raises(AppError, match="this config has no pools"):
        resolve_target(cfg, "btc", None)


def test_flat_config_takes_the_first_enabled_account(tmp_path):
    cfg = load(tmp_path, FLAT_CONFIG)
    target = resolve_target(cfg, None, None)

    assert (target.account.name, target.symbols, target.pools) == ("two", ["BTC", "ETH"], [])


def test_flat_config_account_name_selects_a_disabled_account(tmp_path):
    cfg = load(tmp_path, FLAT_CONFIG)
    target = resolve_target(cfg, None, "one")

    assert (target.account.name, target.account.enabled) == ("one", False)
    assert target.symbols == ["BTC", "ETH"]


def test_account_in_no_pool_avoids_every_pool_symbol(tmp_path):
    # A spare account trades nothing, so the symbol guard uses the whole config's symbols.
    cfg = load(tmp_path, TWO_POOLS_CONFIG)
    target = resolve_target(cfg, None, "spare")

    assert target.account.name == "spare"
    assert (target.symbols, target.pools) == (["BTC", "ETH", "AVAX"], ["btc", "eth"])
