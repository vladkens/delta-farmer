# Delta Farmer

Delta Farmer is an async Python CLI that farms exchange volume and points with delta-neutral perpetual positions across multiple live accounts. Commands can place orders and move real funds.

## Architecture

- `apps/<exchange>.py` files are intentionally separate but disposable entrypoint shells. They may construct clients, dispatch commands, and render exchange-specific output; never put reusable workflows or protocol logic there. The direction is fewer and smaller app files, not an app framework.
- Shared trading code targets the runtime-checkable structural `TradingClient` Protocol in `strategy/models.py`. Clients conform by implementing it and do not need a common base class. Use `isinstance(value, TradingClient)` when runtime recognition is needed, never a `*_like` heuristic.
- Do not grow `TradingClient` for one feature. Narrow capabilities belong in a workflow-local Protocol, as with `DepositClient`, `WithdrawalClient`, `LoginClient`, and `EntryQualityEstimator`.
- `clients/` owns exchange authentication, signing, wire formats, and conversion into common models. `strategy/` owns cross-exchange workflows. `lib/` owns generic HTTP, EVM, logging, storage, CLI, and table mechanics.
- Keep domain models flat. Trading behavior belongs in `strategy/cycle.py`, `strategy/trade.py`, and `strategy/execution.py`, not in data models.
- Generic EVM work, including RPC validation, balances, allowances, gas, transaction submission, and receipt polling, belongs in `lib/evm.py`; clients supply only exchange-specific metadata and calls.

## Project invariants

- Order quantities are positive base-asset `Decimal` values; `Side` carries direction. Do not use floats at exchange, price, quantity, token, or fee boundaries.
- `ProfileInfo.pnl` means net realized trading P/L after fees and funding. Do not substitute leaderboard, notional, or unrealized values; apps that display `Burn` negate this value.
- Runtime `Δ` is the balance change during one cycle. `Total P/L` is current total balance minus process-start balance. Deposits, withdrawals, transfers, funding, fees, and delayed exchange accounting affect these balance-derived values; they are not pure trade P/L.
- Deposit and withdrawal workflows plan all accounts before confirmation, then execute transfers sequentially. Deposits use randomized delays so multi-account transactions are not submitted together.
- Prefer exchange/API metadata over hardcoded networks, limits, market hours, or statuses when that metadata is available. Preserve the exchange's terminology for commands and fields instead of inventing aliases.
- Client methods already receive account log context through `bind_log_context`; do not repeat the account name. Logs and tables are operator interfaces: keep them compact, preserve useful API error details, and do not add redundant summaries, unexplained columns, or speculative cause lists.

## Change discipline

- Before adding a wrapper, helper, cache, lock, state field, config option, or compatibility layer, find a concrete repeated need in the current code. Do not design for hypothetical concurrent CLI runs or future abstractions.
- Compare neighboring clients and shared helpers before implementing exchange behavior. Reuse the established shape, but keep genuine protocol differences inside the client.
- Test deterministic shared behavior and focused regressions. Do not add mocks that merely freeze a mutable third-party API or authentication flow.
- Never use live `trade`, `close`, `deposit`, `withdraw`, forced login, claim, or migration commands as validation unless the user explicitly requests that operation.
