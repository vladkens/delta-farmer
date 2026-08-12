# delta-farmer

<p align="center">
  <a href="readme.md">English</a> · Русский · <a href="readme.uk.md">Українська</a>
</p>

<p align="center"><img src=".github/logo.svg" width="200" /></p>

<div align="center">

[<img src="https://badges.ws/badge/-/%40uid127/000?icon=x&label" alt="x" />](https://x.com/uid127)
[<img src="https://badges.ws/badge/-/Telegram%20Channel/2CA5E0?icon=telegram&label" alt="tg channel" />](https://t.me/+nkSWfo2QASdiOTI0)
[<img src="https://badges.ws/badge/-/Telegram%20Chat/2CA5E0?icon=telegram&label" alt="tg chat" />](https://t.me/+JPqp0bteCWwzMDJk)

</div>

Автоматическая delta-neutral торговля для фарминга points в крипте. Запускайте классические двусторонние хеджи или сбалансированные корзины из нескольких символов на perpetual DEX, чтобы набирать объем и points с ограниченным направленным риском.

- 🎯 **Delta-neutral по умолчанию** — совпадающие long/short позиции снижают направленную экспозицию
- 🧩 **Корзины из нескольких символов** — торговля 2-4 символами за цикл, с нейтральностью по каждой ноге
- 🔄 **Управление несколькими аккаунтами** — один config управляет всеми аккаунтами одновременно
- 👥 **Групповая торговля** — разделение аккаунтов на независимые группы стратегии
- 📊 **Проверки риска в реальном времени** — аварийное закрытие при пробое ROI-лимитов
- 🔐 **Шифрование ключей** — приватные ключи не лежат в открытом виде
- 📨 **Telegram-уведомления** — алерты о старте, остановке, ошибках и периодических отчетах
- 🎲 **Настраиваемые размеры и тайминги** — рандомизация размеров и длительностей для вариативности on-chain паттернов

---

## Что такое delta-farmer?

Delta-farmer автоматически открывает совпадающие long и short позиции на perpetual DEX. Идея простая: при равных противоположных позициях чистая рыночная экспозиция остается около нуля — вы фармите торговый объем и protocol points, а не делаете ставку на направление цены.

Каждый торговый цикл бот:

1. Открывает **long** на одном аккаунте и **short** на другом (или распределяет позиции по нескольким активам)
2. Держит позиции заданное время и мониторит риск
3. Аккуратно закрывает все позиции и ждет перед следующим циклом
4. Отправляет Telegram-сводку, если она настроена

Вы задаете размер, тайминги, leverage и exchange. Остальное делает бот.

---

## Поддерживаемые биржи

| Название | Сеть    | Ссылка                                        | Referral                                                           |
| -------- | ------- | --------------------------------------------- | ------------------------------------------------------------------ |
| Ethereal | EVM     | [ethereal.trade](https://app.ethereal.trade/) | [Sign up](https://app.ethereal.trade/?ref=DSQ3BOJ65L3X)            |
| HyENA    | EVM     | [hyena.trade](https://app.hyena.trade/)       | [Sign up](https://app.hyena.trade/ref/VLADKENS)                    |
| Nado     | EVM     | [nado.xyz](https://app.nado.xyz/)             | [Sign up](https://app.nado.xyz?join=yUAjz7a)                       |
| Omni     | EVM     | [variational.io](https://omni.variational.io) | [Sign up](https://omni.variational.io)                             |
| Onyx     | EVM     | [onyx.live](https://app.onyx.live/)           | [Sign up](https://app.onyx.live/?ref=BB7M4BW3)                     |
| Pacifica | Solana  | [pacifica.fi](https://app.pacifica.fi)        | [Sign up](https://app.pacifica.fi?referral=uid127)                 |
| RiseX    | EVM     | [rise.trade](https://www.rise.trade/)         | [Sign up](https://www.rise.trade/)                                 |
| N1       | EVM     | [n1.xyz](https://app.n1.xyz/)                 | [Sign up](https://app.n1.xyz/r/vladkens)                           |

### Обновления бирж

- **01.xyz → N1** — биржа переехала на [n1.xyz](https://app.n1.xyz/).

---

## Установка

### Шаг 1 — установите зависимости

#### macOS

Откройте **Terminal** (`Cmd + Space` → введите "Terminal" → Enter) и выполните:

```bash
xcode-select --install
```

Появится окно — нажмите "Install". После установки поставьте uv ([официальная инструкция](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_1)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Закройте и снова откройте Terminal, чтобы команда `uv` стала доступна.

#### Windows

Откройте **PowerShell** (`Win + S` → введите "PowerShell" → Enter) и выполните:

```powershell
winget install --id Git.Git -e --source winget
```

Затем установите uv ([официальная инструкция](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_2)):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Закройте и снова откройте PowerShell, чтобы `git` и `uv` стали доступны.

### Шаг 2 — скачайте и запустите

```bash
git clone https://github.com/vladkens/delta-farmer.git
cd delta-farmer
```

Готово. Зависимости установятся автоматически при первом запуске.

---

## Быстрый старт

Везде ниже заменяйте `<app>` на имя exchange: `pacifica`, `omni`, `ethereal`, `nado`, `hyena`, `onyx`, `rise` или `n1`.

**Шаг 1 — создайте config**

```bash
uv run apps/<app>.py config new
```

Команда создаст `configs/<app>.toml` с базовыми настройками. Откройте файл в любом текстовом редакторе.

**Шаг 2 — добавьте приватные ключи**

Найдите секции `[[accounts]]` и вставьте приватные ключи:

```toml
[[accounts]]
name = "acc1"
privkey = "your-private-key-here"

[[accounts]]
name = "acc2"
privkey = "your-private-key-here"
```

Нужно минимум **2 аккаунта** — один идет long, другой short.

**Шаг 3 — зашифруйте ключи**

```bash
uv run apps/<app>.py config encrypt
```

Команда попросит пароль. После этого raw-ключи будут заменены зашифрованными значениями в config. Этот пароль нужно вводить при каждом старте бота (или сохранить в `.env` — см. [Шифрование ключей и пароли](#шифрование-ключей-и-пароли)).

**Шаг 4 — запустите торговлю**

```bash
uv run apps/<app>.py trade
```

---

## Команды

У всех exchanges одинаковая базовая структура команд. Замените `<app>` на имя exchange.

```bash
# Trading
uv run apps/<app>.py trade          # Запустить автоматическую торговлю
uv run apps/<app>.py close          # Закрыть все открытые позиции
uv run apps/<app>.py info           # Показать балансы аккаунтов и points
uv run apps/<app>.py positions      # Показать текущие открытые позиции
uv run apps/<app>.py login          # Проверить и восстановить логины всех аккаунтов
uv run apps/<app>.py login --force  # Принудительно перелогинить все аккаунты
uv run apps/<app>.py proxy          # Проверить настроенные proxies

# Statistics
uv run apps/<app>.py stats          # Все кэшированные периоды (кэш 1h)
uv run apps/<app>.py stats this     # Только текущий период
uv run apps/<app>.py stats last     # Только предыдущий период
uv run apps/<app>.py stats W05      # Конкретная неделя/period prefix
uv run apps/<app>.py stats --force  # Принудительно обновить cached stats
uv run apps/<app>.py clean          # Удалить все cached data

# Config management
uv run apps/<app>.py config new            # Создать новый config file
uv run apps/<app>.py config new -c my.toml # Создать config по custom path
uv run apps/<app>.py config encrypt        # Зашифровать private keys в config
uv run apps/<app>.py config decrypt        # Расшифровать, чтобы увидеть raw keys

# Help
uv run apps/<app>.py --help
```

### Логи

По умолчанию логи выводятся только в терминал. Для торговых запусков установите
`DF_LOG_FILE=1`, чтобы дополнительно писать логи в `logs/<timestamp>-<app>.log`:

```bash
DF_LOG_FILE=1 uv run apps/<app>.py trade
```

### Недельная сводка

`scripts/weekly.py` читает кэшированные stats из `.cache`. Если нужны свежие данные, сначала обновите exchange через `uv run apps/<app>.py stats --force`.

```bash
uv run scripts/weekly.py                    # Общая сводка по exchanges за все время
uv run scripts/weekly.py 0                  # Последняя cached ISO week
uv run scripts/weekly.py -1                 # Предыдущая ISO week
uv run scripts/weekly.py W14                # Конкретная неделя текущего года
uv run scripts/weekly.py 2026-W14           # Конкретная ISO week с годом
uv run scripts/weekly.py --from W14 --to W22 # Сводка за диапазон недель
uv run scripts/weekly.py -P --from W14 --to W22 # Детализация недель внутри диапазона
uv run scripts/weekly.py Hyena              # Один exchange, все доступные периоды
uv run scripts/weekly.py Hyena 0            # Один exchange, последняя cached ISO week
uv run scripts/weekly.py -e Hyena           # Старый alias для одного exchange
uv run scripts/weekly.py --burn             # Burn pivot по ISO week и exchange
uv run scripts/weekly.py --help             # Полная справка weekly report
```

### Команды отдельных exchanges

```bash
uv run apps/omni.py competition              # Показать статус Omni competition
uv run apps/omni.py competition --join       # Зарегистрировать все настроенные Omni accounts
uv run apps/hyena.py reward claim            # Забрать Hyena rewards
uv run apps/hyena.py migrate                 # Перевести Hyena HyperLiquid accounts в unified mode
uv run apps/onyx.py migrate                  # Перевести Onyx HyperLiquid accounts в unified mode
```

Команды Omni competition показывают активное окно турнира, статус участия, eligibility volume и места в leaderboard. Команды Hyena и Onyx `migrate` переводят HyperLiquid-backed аккаунты в Unified Account mode, если exchange сообщает legacy account mode.

### Проблемы со входом в Omni

Некоторые пользователи сообщают о проблемах со входом в Omni. В качестве workaround запустите `uv run apps/omni.py login`: команда последовательно повторяет вход каждые 30 секунд до успеха. Для свежего входа используйте `login --force`; если проблема остаётся, смените proxy.

---

## Настройки

Все настройки лежат в `configs/<app>.toml`.

### Основные настройки

| Параметр           | По умолчанию | Описание                                                                                                                                                       |
| ------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `leverage`          | `10`     | Множитель leverage (1-49). Ставьте **минимальный** max leverage среди выбранных symbols.                                                                        |
| `symbols`           | required | Торговые пары, например `["BTC"]` или `["BTC", "ETH"]`. Доступные symbols проверяйте в UI exchange.                                                            |
| `symbols_per_trade` | `1`      | Сколько symbols торговать за цикл. `1` = classic mode и может выбирать один symbol из списка; `2`-`4` = basket mode и должно совпадать с длиной `symbols`.      |
| `market_hours`      | `"auto"` | Режим market-hours pre-check: `"auto"` проверяет только planned open, `"strict"` проверяет planned open и close, `"off"` отключает проверку.                  |
| `use_limit`         | `false`  | Если `true`, prime account открывается limit order вместо market order — это снижает fees.                                                                     |
| `first_as_prime`    | `false`  | Если `true`, первый аккаунт в списке всегда prime (limit-side). Если `false`, prime выбирается случайно каждый цикл. Игнорируется, если задан `group_size`.     |

### Размер сделки

Нужен ровно один из этих параметров — одновременно использовать оба нельзя.

| Параметр         | По умолчанию | Описание                                                                                                                |
| ---------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `trade_size_usd` | —       | Total notional за цикл в USD, диапазоном: `{ min = 140, max = 160 }`. Размер делится 50% prime / 50% hedge.                  |
| `trade_size_pct` | —       | Размер как доля баланса аккаунта (например `0.5` = 50%). Ограничивающим становится самый тесный аккаунт.                      |

### Тайминги

Durations принимают секунды (`30`), строки вроде `"15s"`, `"5m"`, `"1h"`, `"3d"`, составные строки вроде `"1d2h30m"` или диапазон `{ min = "15m", max = "20m" }`.

| Параметр          | По умолчанию | Описание                                  |
| ----------------- | -------- | -------------------------------------------- |
| `trade_duration`  | required | Сколько держать позиции в каждом цикле.      |
| `trade_cooldown`  | required | Пауза между циклами.                         |
| `trade_heartbeat` | `"15s"`  | Как часто запускать safety checks во время удержания позиций. |

### Limit order настройки

Актуально только при `use_limit = true`.

```toml
use_limit = true
limit_wait = "90s"
limit_wait_retries = 99
limit_market_fallback = true
```

| Параметр                | По умолчанию | Описание                                                                                                         |
| ----------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------- |
| `limit_wait`            | `"90s"` | Сколько ждать fill limit order.                                                                                         |
| `limit_wait_retries`    | `99`    | Дополнительные окна `limit_wait`, пока BBO остается близко к исходной limit price. `0` = отключено.                    |
| `limit_market_fallback` | `true`  | Если limit order не исполнился вовремя, перейти на market order. `false` = прервать цикл.                              |

Максимальное ожидание одного limit order: `limit_wait * (1 + limit_wait_retries)`. Большие значения повышают шанс maker fill, но требуют большего tradeability window перед открытием и закрытием позиций.

### Entry gate настройки

Перед открытием позиции бот может ждать приемлемый entry spread/depth. Чтобы отключить gate, установите `max_entry_spread_pct = null`.

```toml
max_entry_spread_pct = 0.25
entry_gate_wait = "5m"
entry_gate_poll = "3s"
```

| Параметр               | По умолчанию | Описание                                                                   |
| ---------------------- | ------- | -------------------------------------------------------------------------------- |
| `max_entry_spread_pct` | `0.25`  | Максимальный расчетный entry spread/depth percent перед открытием позиции.       |
| `entry_gate_wait`      | `"5m"`  | Максимальное время ожидания приемлемого entry quality перед skip.                |
| `entry_gate_poll`      | `"3s"`  | Как часто перепроверять entry quality во время ожидания. Должно быть 1-10 секунд. |

### Лимиты безопасности

| Параметр             | По умолчанию | Описание                                                                                                                                               |
| -------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `position_roi_limit` | `0.8`   | Аварийно закрыть весь цикл, если любая отдельная позиция достигла ±80% ROI.                                                                                    |
| `combined_roi_limit` | `0.1`   | Аварийно закрыть, если combined basket ROI достиг ±10%.                                                                                                        |
| `max_failures`       | `0`     | Остановить стратегию после такого числа подряд идущих cycle failures. `0` = не останавливать, повторять с exponential backoff до 1h между попытками.           |

### Групповая торговля

| Параметр           | По умолчанию | Описание                                                                                           |
| ------------------ | ------- | --------------------------------------------------------------------------------------------------------- |
| `group_size`       | —       | Делит аккаунты на независимые группы. Должно быть 2-5. Число enabled accounts должно делиться на это число. |
| `regroup_interval` | —       | Периодически сортирует accounts по балансу и перезапускает группы. Работает только при заданном `group_size`. |

### Аккаунты

Добавьте один блок `[[accounts]]` на каждый wallet.

| Параметр  | По умолчанию | Описание                                                                 |
| --------- | -------- | ------------------------------------------------------------------------------ |
| `name`    | required | Имя для логов и stats.                                                         |
| `privkey` | required | Приватный ключ. Заполните его, затем выполните `config encrypt`.               |
| `proxy`   | —        | Опциональный HTTP proxy: `"http://user:pass@host:port"`.                       |
| `enabled` | `true`   | `false` исключает аккаунт из торговли, но оставляет его в stats.               |

### Telegram (опционально)

Добавьте блок `[telegram]`, чтобы включить уведомления.

| Параметр          | По умолчанию | Описание                                                                                                      |
| ----------------- | ------------ | ------------------------------------------------------------------------------------------------------------------- |
| `token`           | —            | Bot token от [@BotFather](https://t.me/BotFather). После добавления выполните `config encrypt`.                     |
| `chat_id`         | —            | Ваш личный или групповой chat ID. Можно получить у [@userinfobot](https://t.me/userinfobot).                        |
| `notify`          | all channels | Список каналов уведомлений: `"start"`, `"stop"`, `"errors"`, `"reports"`. Уберите ненужные, чтобы их заглушить.    |
| `report_interval` | `"1h"`       | Как часто отправлять периодический stats digest.                                                                    |

---

## Режимы торговли

### Classic mode (один symbol)

Один цикл торгует один symbol: один аккаунт идет long, другой short. Если настроить несколько symbols при `symbols_per_trade = 1`, бот выбирает один tradeable symbol за цикл.

```toml
symbols = ["BTC"]
symbols_per_trade = 1
trade_size_usd = { min = 140, max = 160 }
```

### Basket mode (несколько symbols)

Один цикл торгует несколько symbols одновременно. Каждый symbol остается neutral, и каждый аккаунт также неттится по всей корзине.

```toml
symbols = ["BTC", "ETH"]
symbols_per_trade = 2
trade_size_usd = { min = 140, max = 160 }
```

Правила:

- `symbols_per_trade` должен точно совпадать с числом элементов в `symbols`
- Максимум 4 symbols за trade
- Safety exits работают и по отдельной позиции, и по combined basket ROI

### Групповая торговля

Разделяет аккаунты на независимые strategy groups, которые работают параллельно в одном процессе.

```toml
group_size = 2
regroup_interval = "12h"
```

Правила:

- `group_size` должен быть от 2 до 5
- Общее число enabled accounts должно делиться на `group_size`
- `first_as_prime` игнорируется, если задан `group_size`
- `regroup_interval` перебалансирует группы по balance и перезапускает их

---

## Проверки безопасности

Перед открытием цикла бот может фильтровать настроенные symbols по exchange market-hours data. С дефолтным `market_hours = "auto"` проверяется только planned entry window; planned close может попасть вне regular hours. Используйте `market_hours = "strict"`, чтобы требовать planned entry и planned close внутри regular trading hours, или `market_hours = "off"`, чтобы отключить эту предварительную проверку. Symbols без market-hours metadata считаются 24/7.

Каждые `trade_heartbeat` (по умолчанию 15 секунд) бот проверяет:

1. **Per-position ROI** — если доходность любой ноги пересекает `±position_roi_limit` (по умолчанию ±80%), все позиции закрываются немедленно
2. **Combined basket ROI** — если суммарная доходность корзины пересекает `±combined_roi_limit` (по умолчанию ±10%), все позиции закрываются немедленно
3. **Position count** — если у любого symbol неожиданное число позиций (например, одну сторону ликвидировало), все позиции закрываются немедленно

Это last-resort protection. Все равно используйте разумный leverage и trade sizes.

---

## Telegram-уведомления

**Настройка:**

1. Напишите [@BotFather](https://t.me/BotFather) в Telegram, создайте bot и скопируйте token
2. Напишите [@userinfobot](https://t.me/userinfobot), чтобы получить chat ID
3. Добавьте в config:

```toml
[telegram]
token = "123456:ABC-DEF..."
chat_id = "123456789"
notify = ["start", "stop", "errors", "reports"]
report_interval = "1h"
```

4. Зашифруйте token: `uv run apps/<app>.py config encrypt`
5. Проверьте: `uv run apps/<app>.py tgtest`

**Каналы уведомлений:**

| Канал     | Когда срабатывает                              |
| --------- | ---------------------------------------------- |
| `start`   | Открыт торговый цикл (symbol, size, accounts) |
| `stop`    | Торговый цикл закрыт (PnL, duration)           |
| `errors`  | Ошибки циклов и crashes                        |
| `reports` | Периодический digest (trades, volume, burn, $/100k) |

Удалите channel из списка `notify`, чтобы отключить его.

---

## Шифрование ключей и пароли

Приватные ключи в config шифруются через AES. После заполнения raw keys всегда выполняйте:

```bash
uv run apps/<app>.py config encrypt
```

При старте бот попросит пароль. Чтобы не вводить его вручную, сохраните пароль в `.env` в папке проекта:

```bash
echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

Чтобы снова увидеть raw keys для backup или migration:

```bash
uv run apps/<app>.py config decrypt
```

---

## Несколько instances / custom configs

Используйте флаг `-c`, чтобы указать другой config file:

```bash
uv run apps/pacifica.py -c configs/pacifica-set2.toml trade
```

Так можно запускать несколько независимых instances одного exchange с разными accounts или settings:

```bash
# Terminal 1
uv run apps/omni.py -c configs/omni-set1.toml trade

# Terminal 2
uv run apps/omni.py -c configs/omni-set2.toml trade
```

---

## Обновление

```bash
# Остановите запущенные instances (Ctrl+C или kill process)

# Получите последние изменения
git pull

# Установите locked dependency set
uv sync --locked

# Перезапустите торговлю
uv run apps/<app>.py trade
```

---

## Рекомендуемые сервисы

- [**Digital Ocean**](https://m.do.co/c/a97fd963258f) — VPS для работы бота 24/7
- [**Proxy Shard**](https://proxyshard.com?ref=5406) — proxy для разделения account traffic

---

## Телеметрия

Delta-farmer собирает анонимную usage statistics (exchange name, command, technical config flags), чтобы понимать adoption и популярность features. Wallet addresses, balances и strategy parameters не отправляются.

Установите `DF_TELEMETRY=0`, чтобы полностью отключить telemetry.

## Переменные окружения

| Переменная                | Описание                                              |
| ------------------------- | ----------------------------------------------------- |
| `DF_CONFIG_PASSWORD`      | Пароль шифрования config для non-interactive runs.   |
| `DF_LOG_FILE=1`           | Дополнительно писать trade logs в `logs/<timestamp>-<app>.log`. |
| `DF_NO_UPDATE_NOTIFIER=1` | Отключить checks новых releases.                      |
| `DF_TELEMETRY=0`          | Отключить anonymous usage telemetry.                  |

## Предупреждение о рисках

**ИСПОЛЬЗУЙТЕ НА СВОЙ РИСК**

- Это software только для образовательных целей
- Торговля криптовалютами несет существенный финансовый риск
- Вы можете потерять все внесенные средства
- Нет гарантий прибыли или eligibility для airdrop
- Всегда сначала тестируйте на небольших суммах
- Авторы не несут ответственности за убытки

---

## Контакты и feedback

- **X/Twitter:** [@uid127](https://x.com/uid127)
- **Telegram channel:** [@eazyrekt](https://t.me/s/eazyrekt) — drop farming insights & updates
- **Telegram chat:** [Join the group](https://t.me/+JPqp0bteCWwzMDJk)
