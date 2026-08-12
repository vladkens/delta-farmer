# delta-farmer

<p align="center">
  <a href="readme.md">English</a> · <a href="readme.ru.md">Русский</a> · Українська
</p>

<p align="center"><img src=".github/logo.svg" width="200" /></p>

<div align="center">

[<img src="https://badges.ws/badge/-/%40uid127/000?icon=x&label" alt="x" />](https://x.com/uid127)
[<img src="https://badges.ws/badge/-/Telegram%20Channel/2CA5E0?icon=telegram&label" alt="tg channel" />](https://t.me/+nkSWfo2QASdiOTI0)
[<img src="https://badges.ws/badge/-/Telegram%20Chat/2CA5E0?icon=telegram&label" alt="tg chat" />](https://t.me/+JPqp0bteCWwzMDJk)

</div>

Автоматична delta-neutral торгівля для фармінгу points у крипті. Запускайте класичні двосторонні хеджі або збалансовані кошики з кількох символів на perpetual DEX, щоб набирати обсяг і points з обмеженим напрямним ризиком.

- 🎯 **Delta-neutral за задумом** — узгоджені long/short позиції зменшують напрямну експозицію
- 🧩 **Кошики з кількох символів** — торгівля 2-4 символами за цикл, з нейтральністю по кожній нозі
- 🔄 **Керування кількома акаунтами** — один config керує всіма акаунтами одночасно
- 👥 **Групова торгівля** — розділення акаунтів на незалежні групи стратегії
- 📊 **Перевірки ризику в реальному часі** — аварійне закриття при пробитті ROI-лімітів
- 🔐 **Шифрування ключів** — приватні ключі не зберігаються у відкритому вигляді
- 📨 **Telegram-сповіщення** — алерти про старт, зупинку, помилки та періодичні звіти
- 🎲 **Налаштовувані розміри й таймінги** — рандомізація розмірів і тривалостей для варіативності on-chain патернів

---

## Що таке delta-farmer?

Delta-farmer автоматично відкриває узгоджені long і short позиції на perpetual DEX. Ідея проста: коли протилежні позиції рівні, чиста ринкова експозиція залишається близькою до нуля — ви фармите торговий обсяг і protocol points, а не ставите на напрям ціни.

Кожен торговий цикл бот:

1. Відкриває **long** на одному акаунті та **short** на іншому (або розподіляє позиції між кількома активами)
2. Утримує позиції заданий час і моніторить ризик
3. Акуратно закриває все й чекає перед наступним циклом
4. Надсилає Telegram-зведення, якщо його налаштовано

Ви задаєте розмір, таймінги, leverage і exchange. Решту робить бот.

---

## Підтримувані біржі

| Назва    | Мережа  | Посилання                                    | Referral                                                           |
| -------- | ------- | --------------------------------------------- | ------------------------------------------------------------------ |
| Ethereal | EVM     | [ethereal.trade](https://app.ethereal.trade/) | [Sign up](https://app.ethereal.trade/?ref=DSQ3BOJ65L3X)            |
| HyENA    | EVM     | [hyena.trade](https://app.hyena.trade/)       | [Sign up](https://app.hyena.trade/ref/VLADKENS)                    |
| Nado     | EVM     | [nado.xyz](https://app.nado.xyz/)             | [Sign up](https://app.nado.xyz?join=yUAjz7a)                       |
| Omni     | EVM     | [variational.io](https://omni.variational.io) | [Sign up](https://omni.variational.io)                             |
| Onyx     | EVM     | [onyx.live](https://app.onyx.live/)           | [Sign up](https://app.onyx.live/?ref=BB7M4BW3)                     |
| Pacifica | Solana  | [pacifica.fi](https://app.pacifica.fi)        | [Sign up](https://app.pacifica.fi?referral=uid127)                 |
| RiseX    | EVM     | [rise.trade](https://www.rise.trade/)         | [Sign up](https://www.rise.trade/)                                 |
| N1       | EVM     | [n1.xyz](https://app.n1.xyz/)                 | [Sign up](https://app.n1.xyz/r/vladkens)                           |

### Оновлення бірж

- **01.xyz → N1** — біржа переїхала на [n1.xyz](https://app.n1.xyz/).

---

## Встановлення

### Крок 1 — встановіть залежності

#### macOS

Відкрийте **Terminal** (`Cmd + Space` → введіть "Terminal" → Enter) і виконайте:

```bash
xcode-select --install
```

З'явиться вікно — натисніть "Install". Після цього встановіть uv ([офіційна інструкція](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_1)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Закрийте й знову відкрийте Terminal, щоб команда `uv` стала доступною.

#### Windows

Відкрийте **PowerShell** (`Win + S` → введіть "PowerShell" → Enter) і виконайте:

```powershell
winget install --id Git.Git -e --source winget
```

Потім встановіть uv ([офіційна інструкція](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_2)):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Закрийте й знову відкрийте PowerShell, щоб `git` і `uv` стали доступними.

### Крок 2 — завантажте й запустіть

```bash
git clone https://github.com/vladkens/delta-farmer.git
cd delta-farmer
```

Готово. Залежності встановляться автоматично під час першого запуску.

---

## Швидкий старт

Усюди нижче замінюйте `<app>` на назву exchange: `pacifica`, `omni`, `ethereal`, `nado`, `hyena`, `onyx`, `rise` або `n1`.

**Крок 1 — створіть config**

```bash
uv run apps/<app>.py config new
```

Команда створить `configs/<app>.toml` з базовими налаштуваннями. Відкрийте файл у будь-якому текстовому редакторі.

**Крок 2 — додайте приватні ключі**

Знайдіть секції `[[accounts]]` і вставте приватні ключі:

```toml
[[accounts]]
name = "acc1"
privkey = "your-private-key-here"

[[accounts]]
name = "acc2"
privkey = "your-private-key-here"
```

Потрібно мінімум **2 акаунти** — один іде long, інший short.

**Крок 3 — зашифруйте ключі**

```bash
uv run apps/<app>.py config encrypt
```

Команда попросить пароль. Після цього raw-ключі будуть замінені зашифрованими значеннями в config. Цей пароль треба вводити під час кожного старту бота (або зберегти в `.env` — див. [Шифрування ключів і паролі](#шифрування-ключів-і-паролі)).

**Крок 4 — запустіть торгівлю**

```bash
uv run apps/<app>.py trade
```

---

## Команди

В усіх exchanges однакова базова структура команд. Замініть `<app>` на назву exchange.

```bash
# Trading
uv run apps/<app>.py trade          # Запустити автоматичну торгівлю
uv run apps/<app>.py close          # Закрити всі відкриті позиції
uv run apps/<app>.py info           # Показати баланси акаунтів і points
uv run apps/<app>.py positions      # Показати поточні відкриті позиції
uv run apps/<app>.py login          # Перевірити й відновити логіни всіх акаунтів
uv run apps/<app>.py login --force  # Примусово перелогінити всі акаунти
uv run apps/<app>.py proxy          # Перевірити налаштовані proxies

# Statistics
uv run apps/<app>.py stats          # Усі кешовані періоди (кеш 1h)
uv run apps/<app>.py stats this     # Тільки поточний період
uv run apps/<app>.py stats last     # Тільки попередній період
uv run apps/<app>.py stats W05      # Конкретний тиждень/period prefix
uv run apps/<app>.py stats --force  # Примусово оновити cached stats
uv run apps/<app>.py clean          # Видалити всі cached data

# Config management
uv run apps/<app>.py config new            # Створити новий config file
uv run apps/<app>.py config new -c my.toml # Створити config за custom path
uv run apps/<app>.py config encrypt        # Зашифрувати private keys у config
uv run apps/<app>.py config decrypt        # Розшифрувати, щоб побачити raw keys

# Help
uv run apps/<app>.py --help
```

### Логи

За замовчуванням логи виводяться тільки в термінал. Для торгових запусків встановіть
`DF_LOG_FILE=1`, щоб додатково писати логи в `logs/<timestamp>-<app>.log`:

```bash
DF_LOG_FILE=1 uv run apps/<app>.py trade
```

### Тижневе зведення

`scripts/weekly.py` читає кешовані stats з `.cache`. Якщо потрібні свіжі дані, спочатку оновіть exchange через `uv run apps/<app>.py stats --force`.

```bash
uv run scripts/weekly.py                    # Загальне зведення по exchanges за весь час
uv run scripts/weekly.py 0                  # Остання cached ISO week
uv run scripts/weekly.py -1                 # Попередня ISO week
uv run scripts/weekly.py W14                # Конкретний тиждень поточного року
uv run scripts/weekly.py 2026-W14           # Конкретна ISO week із роком
uv run scripts/weekly.py --from W14 --to W22 # Зведення за діапазон тижнів
uv run scripts/weekly.py -P --from W14 --to W22 # Деталізація тижнів у діапазоні
uv run scripts/weekly.py Hyena              # Один exchange, усі доступні періоди
uv run scripts/weekly.py Hyena 0            # Один exchange, остання cached ISO week
uv run scripts/weekly.py -e Hyena           # Старий alias для одного exchange
uv run scripts/weekly.py --burn             # Burn pivot за ISO week і exchange
uv run scripts/weekly.py --help             # Повна довідка weekly report
```

### Команди окремих exchanges

```bash
uv run apps/omni.py competition              # Показати статус Omni competition
uv run apps/omni.py competition --join       # Зареєструвати всі налаштовані Omni accounts
uv run apps/hyena.py reward claim            # Забрати Hyena rewards
uv run apps/hyena.py migrate                 # Перевести Hyena HyperLiquid accounts в unified mode
uv run apps/onyx.py migrate                  # Перевести Onyx HyperLiquid accounts в unified mode
```

Команди Omni competition показують активне вікно турніру, статус участі, eligibility volume і місця в leaderboard. Команди Hyena та Onyx `migrate` переводять HyperLiquid-backed акаунти в Unified Account mode, якщо exchange повідомляє legacy account mode.

### Проблеми зі входом в Omni

Деякі користувачі повідомляють про проблеми зі входом в Omni. Як workaround запустіть `uv run apps/omni.py login`: команда послідовно повторює вхід кожні 30 секунд до успіху. Для свіжого входу використовуйте `login --force`; якщо проблема залишається, змініть proxy.

---

## Налаштування

Усі налаштування лежать у `configs/<app>.toml`.

### Основні налаштування

| Параметр           | За замовчуванням | Опис                                                                                                                                                   |
| ------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `leverage`          | `10`     | Множник leverage (1-49). Ставте **мінімальний** max leverage серед вибраних symbols.                                                                           |
| `symbols`           | required | Торгові пари, наприклад `["BTC"]` або `["BTC", "ETH"]`. Доступні symbols перевіряйте в UI exchange.                                                           |
| `symbols_per_trade` | `1`      | Скільки symbols торгувати за цикл. `1` = classic mode і може вибирати один symbol зі списку; `2`-`4` = basket mode і має збігатися з довжиною `symbols`.        |
| `market_hours`      | `"auto"` | Режим market-hours pre-check: `"auto"` перевіряє тільки planned open, `"strict"` перевіряє planned open і close, `"off"` вимикає перевірку.                    |
| `use_limit`         | `false`  | Якщо `true`, prime account відкривається limit order замість market order — це знижує fees.                                                                    |
| `first_as_prime`    | `false`  | Якщо `true`, перший акаунт у списку завжди prime (limit-side). Якщо `false`, prime вибирається випадково кожен цикл. Ігнорується, якщо задано `group_size`.     |

### Розмір угоди

Потрібен рівно один із цих параметрів — одночасно використовувати обидва не можна.

| Параметр         | За замовчуванням | Опис                                                                                                                   |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `trade_size_usd` | —       | Total notional за цикл у USD, діапазоном: `{ min = 140, max = 160 }`. Розмір ділиться 50% prime / 50% hedge.                 |
| `trade_size_pct` | —       | Розмір як частка балансу акаунта (наприклад `0.5` = 50%). Обмежувальним стає найтісніший акаунт.                             |

### Таймінги

Durations приймають секунди (`30`), рядки на кшталт `"15s"`, `"5m"`, `"1h"`, `"3d"`, складені рядки на кшталт `"1d2h30m"` або діапазон `{ min = "15m", max = "20m" }`.

| Параметр          | За замовчуванням | Опис                                  |
| ----------------- | -------- | -------------------------------------------- |
| `trade_duration`  | required | Скільки тримати позиції в кожному циклі.     |
| `trade_cooldown`  | required | Пауза між циклами.                           |
| `trade_heartbeat` | `"15s"`  | Як часто запускати safety checks під час утримання позицій. |

### Limit order налаштування

Актуально тільки при `use_limit = true`.

```toml
use_limit = true
limit_wait = "90s"
limit_wait_retries = 99
limit_market_fallback = true
```

| Параметр                | За замовчуванням | Опис                                                                                                      |
| ----------------------- | ------- | --------------------------------------------------------------------------------------------------------------------- |
| `limit_wait`            | `"90s"` | Скільки чекати fill limit order.                                                                                      |
| `limit_wait_retries`    | `99`    | Додаткові вікна `limit_wait`, доки BBO залишається близько до початкової limit price. `0` = вимкнено.                |
| `limit_market_fallback` | `true`  | Якщо limit order не виконався вчасно, перейти на market order. `false` = перервати цикл.                             |

Максимальне очікування одного limit order: `limit_wait * (1 + limit_wait_retries)`. Більші значення підвищують шанс maker fill, але потребують більшого tradeability window перед відкриттям і закриттям позицій.

### Entry gate налаштування

Перед відкриттям позиції бот може чекати прийнятний entry spread/depth. Щоб вимкнути gate, встановіть `max_entry_spread_pct = null`.

```toml
max_entry_spread_pct = 0.25
entry_gate_wait = "5m"
entry_gate_poll = "3s"
```

| Параметр               | За замовчуванням | Опис                                                                     |
| ---------------------- | ------- | ---------------------------------------------------------------------------------- |
| `max_entry_spread_pct` | `0.25`  | Максимальний розрахунковий entry spread/depth percent перед відкриттям позиції.    |
| `entry_gate_wait`      | `"5m"`  | Максимальний час очікування прийнятного entry quality перед skip.                  |
| `entry_gate_poll`      | `"3s"`  | Як часто повторно перевіряти entry quality під час очікування. Має бути 1-10 секунд. |

### Ліміти безпеки

| Параметр             | За замовчуванням | Опис                                                                                                                                           |
| -------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `position_roi_limit` | `0.8`   | Аварійно закрити весь цикл, якщо будь-яка окрема позиція досягла ±80% ROI.                                                                                     |
| `combined_roi_limit` | `0.1`   | Аварійно закрити, якщо combined basket ROI досяг ±10%.                                                                                                         |
| `max_failures`       | `0`     | Зупинити стратегію після такої кількості поспіль cycle failures. `0` = не зупиняти, повторювати з exponential backoff до 1h між спробами.                      |

### Групова торгівля

| Параметр           | За замовчуванням | Опис                                                                                         |
| ------------------ | ------- | ----------------------------------------------------------------------------------------------------------- |
| `group_size`       | —       | Ділить акаунти на незалежні групи. Має бути 2-5. Кількість enabled accounts має ділитися на це число.       |
| `regroup_interval` | —       | Періодично сортує accounts за балансом і перезапускає групи. Працює тільки при заданому `group_size`.       |

### Акаунти

Додайте один блок `[[accounts]]` на кожен wallet.

| Параметр  | За замовчуванням | Опис                                                                 |
| --------- | -------- | ------------------------------------------------------------------------------ |
| `name`    | required | Ім'я для логів і stats.                                                        |
| `privkey` | required | Приватний ключ. Заповніть його, потім виконайте `config encrypt`.              |
| `proxy`   | —        | Опціональний HTTP proxy: `"http://user:pass@host:port"`.                       |
| `enabled` | `true`   | `false` виключає акаунт із торгівлі, але залишає його в stats.                 |

### Telegram (опціонально)

Додайте блок `[telegram]`, щоб увімкнути сповіщення.

| Параметр          | За замовчуванням | Опис                                                                                                  |
| ----------------- | ------------ | ------------------------------------------------------------------------------------------------------------------- |
| `token`           | —            | Bot token від [@BotFather](https://t.me/BotFather). Після додавання виконайте `config encrypt`.                    |
| `chat_id`         | —            | Ваш особистий або груповий chat ID. Можна отримати у [@userinfobot](https://t.me/userinfobot).                     |
| `notify`          | all channels | Список каналів сповіщень: `"start"`, `"stop"`, `"errors"`, `"reports"`. Приберіть непотрібні, щоб їх вимкнути.     |
| `report_interval` | `"1h"`       | Як часто надсилати періодичний stats digest.                                                                       |

---

## Режими торгівлі

### Classic mode (один symbol)

Один цикл торгує один symbol: один акаунт іде long, інший short. Якщо налаштувати кілька symbols при `symbols_per_trade = 1`, бот вибирає один tradeable symbol за цикл.

```toml
symbols = ["BTC"]
symbols_per_trade = 1
trade_size_usd = { min = 140, max = 160 }
```

### Basket mode (кілька symbols)

Один цикл торгує кілька symbols одночасно. Кожен symbol залишається neutral, і кожен акаунт також нетиться по всьому кошику.

```toml
symbols = ["BTC", "ETH"]
symbols_per_trade = 2
trade_size_usd = { min = 140, max = 160 }
```

Правила:

- `symbols_per_trade` має точно збігатися з кількістю елементів у `symbols`
- Максимум 4 symbols за trade
- Safety exits працюють і по окремій позиції, і по combined basket ROI

### Групова торгівля

Розділяє акаунти на незалежні strategy groups, які працюють паралельно в одному процесі.

```toml
group_size = 2
regroup_interval = "12h"
```

Правила:

- `group_size` має бути від 2 до 5
- Загальна кількість enabled accounts має ділитися на `group_size`
- `first_as_prime` ігнорується, якщо задано `group_size`
- `regroup_interval` перебалансовує групи за balance і перезапускає їх

---

## Перевірки безпеки

Перед відкриттям циклу бот може фільтрувати налаштовані symbols за exchange market-hours data. З дефолтним `market_hours = "auto"` перевіряється тільки planned entry window; planned close може потрапити поза regular hours. Використовуйте `market_hours = "strict"`, щоб вимагати planned entry і planned close всередині regular trading hours, або `market_hours = "off"`, щоб вимкнути цю попередню перевірку. Symbols без market-hours metadata вважаються 24/7.

Кожні `trade_heartbeat` (за замовчуванням 15 секунд) бот перевіряє:

1. **Per-position ROI** — якщо дохідність будь-якої ноги перетинає `±position_roi_limit` (за замовчуванням ±80%), усі позиції закриваються негайно
2. **Combined basket ROI** — якщо сумарна дохідність кошика перетинає `±combined_roi_limit` (за замовчуванням ±10%), усі позиції закриваються негайно
3. **Position count** — якщо в будь-якого symbol неочікувана кількість позицій (наприклад, одну сторону ліквідувало), усі позиції закриваються негайно

Це last-resort protection. Все одно використовуйте розумний leverage і trade sizes.

---

## Telegram-сповіщення

**Налаштування:**

1. Напишіть [@BotFather](https://t.me/BotFather) у Telegram, створіть bot і скопіюйте token
2. Напишіть [@userinfobot](https://t.me/userinfobot), щоб отримати chat ID
3. Додайте в config:

```toml
[telegram]
token = "123456:ABC-DEF..."
chat_id = "123456789"
notify = ["start", "stop", "errors", "reports"]
report_interval = "1h"
```

4. Зашифруйте token: `uv run apps/<app>.py config encrypt`
5. Перевірте: `uv run apps/<app>.py tgtest`

**Канали сповіщень:**

| Канал     | Коли спрацьовує                               |
| --------- | --------------------------------------------- |
| `start`   | Відкрито торговий цикл (symbol, size, accounts) |
| `stop`    | Торговий цикл закрито (PnL, duration)         |
| `errors`  | Помилки циклів і crashes                      |
| `reports` | Періодичний digest (trades, volume, burn, $/100k) |

Видаліть channel зі списку `notify`, щоб вимкнути його.

---

## Шифрування ключів і паролі

Приватні ключі в config шифруються через AES. Після заповнення raw keys завжди виконуйте:

```bash
uv run apps/<app>.py config encrypt
```

Під час старту бот попросить пароль. Щоб не вводити його вручну, збережіть пароль у `.env` у папці проєкту:

```bash
echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

Щоб знову побачити raw keys для backup або migration:

```bash
uv run apps/<app>.py config decrypt
```

---

## Кілька instances / custom configs

Використовуйте прапорець `-c`, щоб вказати інший config file:

```bash
uv run apps/pacifica.py -c configs/pacifica-set2.toml trade
```

Так можна запускати кілька незалежних instances одного exchange з різними accounts або settings:

```bash
# Terminal 1
uv run apps/omni.py -c configs/omni-set1.toml trade

# Terminal 2
uv run apps/omni.py -c configs/omni-set2.toml trade
```

---

## Оновлення

```bash
# Зупиніть запущені instances (Ctrl+C або kill process)

# Отримайте останні зміни
git pull

# Встановіть locked dependency set
uv sync --locked

# Перезапустіть торгівлю
uv run apps/<app>.py trade
```

---

## Рекомендовані сервіси

- [**Digital Ocean**](https://m.do.co/c/a97fd963258f) — VPS для роботи бота 24/7
- [**Proxy Shard**](https://proxyshard.com?ref=5406) — proxy для розділення account traffic

---

## Телеметрія

Delta-farmer збирає анонімну usage statistics (exchange name, command, technical config flags), щоб розуміти adoption і популярність features. Wallet addresses, balances і strategy parameters не надсилаються.

Встановіть `DF_TELEMETRY=0`, щоб повністю вимкнути telemetry.

## Змінні оточення

| Змінна                    | Опис                                                  |
| ------------------------- | ----------------------------------------------------- |
| `DF_CONFIG_PASSWORD`      | Пароль шифрування config для non-interactive runs.   |
| `DF_LOG_FILE=1`           | Додатково писати trade logs у `logs/<timestamp>-<app>.log`. |
| `DF_NO_UPDATE_NOTIFIER=1` | Вимкнути checks нових releases.                       |
| `DF_TELEMETRY=0`          | Вимкнути anonymous usage telemetry.                   |

## Попередження про ризики

**ВИКОРИСТОВУЙТЕ НА ВЛАСНИЙ РИЗИК**

- Це software тільки для освітніх цілей
- Торгівля криптовалютами має суттєвий фінансовий ризик
- Ви можете втратити всі внесені кошти
- Немає гарантій прибутку або eligibility для airdrop
- Завжди спочатку тестуйте на невеликих сумах
- Автори не несуть відповідальності за збитки

---

## Контакти та feedback

- **X/Twitter:** [@uid127](https://x.com/uid127)
- **Telegram channel:** [@eazyrekt](https://t.me/s/eazyrekt) — drop farming insights & updates
- **Telegram chat:** [Join the group](https://t.me/+JPqp0bteCWwzMDJk)
