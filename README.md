# goldbot — XAUUSD M5 research & execution toolkit

A trading bot for spot gold on the 5-minute chart, built around one idea:

> **The hard part is not writing a strategy. The hard part is building a test
> that can tell you your strategy is worthless — and then believing it.**

Most of this repository is that test. The strategy itself is about 200 lines;
the machinery that tries to disprove it is most of the rest.

---

## الخلاصة بالعربي

هذا المشروع بوت تداول للذهب على فريم 5 دقائق. لكن الجزء الأكبر منه **مو
الاستراتيجية** — الجزء الأكبر هو نظام اختبار مصمّم عشان يثبتلك إذا كانت
الاستراتيجية فاشلة.

ثلاث نقاط مهمة قبل ما تبدي:

1. **المحرك يفترض الأسوأ دائماً.** إذا الشمعة لمست وقف الخسارة والهدف بنفس
   الوقت، يحسبها خسارة. أغلب برامج الاختبار تفترض العكس، وهذا بالضبط الشي
   اللي يخلي استراتيجية خاسرة تبيّن رابحة.

2. **أكو فاحص إعادة رسم (repainting).** يحسب المؤشرات على البيانات الكاملة،
   ثم يعيد حسابها على البيانات المقطوعة عند نفس الشمعة، ويقارن. أي اختلاف
   معناه المؤشر يقرأ المستقبل. شغّل `audit` قبل أي شي ثاني.

3. **الاختبار الوحيد اللي يهم هو `walkforward`.** يحسّن المعاملات على فترة،
   ويتداول فيها على فترة **ما شافها أبداً**. النتيجة اللي تطلع منه هي
   الشي الوحيد اللي له علاقة بالمستقبل.

ابدأ من هنا:

```bash
pip install -r requirements.txt
python -m goldbot validate            # على بيانات اصطناعية (اختبار للكود فقط)
python -m goldbot --data data/XAUUSD_M5.csv validate   # على بياناتك الحقيقية
```

النتيجة المتوقعة على البيانات الاصطناعية: **FAILED**. وهذا صحيح ومقصود —
البيانات الاصطناعية ما بيها أي أفضلية، فلازم النظام يقول "ما أكو أفضلية".
نظام اختبار يطلّع أرباح على بيانات عشوائية هو نظام معطوب.

---

## Two ways to run this

| | `goldbot/` (Python) | `mt5/XauTrend_M5.mq5` (MQL5) |
|---|---|---|
| Purpose | research, backtesting, validation | live trading on MetaTrader 5 |
| Runs on | any OS | MT5 terminal (Windows / VPS) |
| Install | `pip install -r requirements.txt` | copy one file into `MQL5\Experts` |
| Use it to | decide whether the strategy is worth trading | trade it once you have decided |

The EA is a direct port of the Python strategy — same entry conditions, same
stop placement, same sizing formula, same risk limits. `tests/test_mql5_parity.py`
parses the `.mq5` source and asserts both that every default matches the Python
dataclass and that a transliteration of the EA's entry logic makes the identical
decision on every bar. If the two ever drift apart, the suite fails.

### MetaTrader 5 (Exness)

**Arabic install guide: [`docs/XauTrend_Exness_MT5_AR.pdf`](docs/XauTrend_Exness_MT5_AR.pdf)** —
9 pages covering installation, Exness-specific settings, the full parameter
reference, Strategy Tester setup, troubleshooting and a pre-flight checklist.
It is generated from the EA source (`python3 tools/make_guide_pdf.py`), so the
parameter tables cannot drift out of date.

**One-click install (Windows):** run `mt5/installer/Install-XauTrend.bat`
next to the `.mq5`. It finds every MT5 data folder under
`%APPDATA%\MetaQuotes\Terminal`, removes earlier copies from `Experts`,
`Scripts` and `Indicators`, copies the source into `Experts`, then locates
that terminal's MetaEditor (via its `origin.txt`, falling back to a Program
Files search) and compiles it from the command line. If MetaEditor cannot be
found it says so and leaves you one `F7` away.

Manual install: `File → Open Data Folder → MQL5 → Experts`, drop the `.mq5` in,
press `F4`, compile, refresh the Navigator, attach to an XAUUSD **M5** chart,
enable AutoTrading.

> MetaEditor decides the program *type* from the folder: `Experts` needs
> `OnTick()`, `Scripts` needs `OnStart()`, `Indicators` needs `OnCalculate()`.
> A file in the wrong folder fails with `event handling function not found`
> even though the code is perfectly valid.

Nothing broker-specific is hardcoded. Contract size, tick value, lot step,
minimum stop distance and order fill policy are all read from the symbol at run
time, so the same file works on Exness Standard, Cent, Raw Spread or Zero, and
on `XAUUSD`, `XAUUSDm` or any suffixed gold symbol, with no edits.

> **Check one line in the log before you trade it.** The EA prints the server's
> detected GMT offset and the session hours it translates to. Broker server time
> is not GMT and shifts with daylight saving. If that line is wrong, the bot
> trades the Asian session instead of the London/NY overlap — a different system
> than the one you tested.

## Install

```bash
git clone <this repo> && cd -
pip install -r requirements.txt
python -m pytest tests/ -q          # 112 tests
```

Python 3.11+. Research needs only numpy, pandas and PyYAML.

## Quickstart

```bash
# The full gauntlet. Start here, always.
python -m goldbot validate

# Individual stages
python -m goldbot audit                       # look-ahead / repaint detector
python -m goldbot backtest --out out/trades.csv
python -m goldbot walkforward                 # the only test that means anything
python -m goldbot montecarlo --sims 10000

# With your own data
python -m goldbot --data data/XAUUSD_M5.csv --config config/default.yaml validate
```

With no `--data`, synthetic bars are generated and every report says so. They
exercise the code. They tell you nothing about gold.

## The validation gauntlet

`validate` runs four stages in falsification order and stops at the first
failure.

### 1. Causality audit — *can the strategy see the future?*

Features are computed on the full history, then recomputed on a truncated
prefix ending at bar `k`. If the strategy is causal, every value at bar `k` must
be **bit-for-bit identical**, because the prefix is exactly what it will have
live.

This catches **repainting** — the reason so many indicators look flawless on a
chart and fall apart in real time. A repainting indicator changes its past
values as new bars arrive, so the chart shows you signals that were never
knowable at the time.

`tests/test_lookahead_detection.py` builds two deliberately cheating strategies
(a `shift(-1)`, and a whole-series normalisation) and asserts the auditor
catches both. An auditor that never fails anything is not an auditor.

### 2. In-sample backtest — *is there anything here at all?*

Reports expectancy in R, and a **t-statistic**. A positive expectancy with
`t < 2` is indistinguishable from luck, and the report says so rather than
printing a profit figure and letting you draw your own conclusion.

### 3. Walk-forward — *does it survive data it was not fitted to?*

Optimise on a training window, trade forward on unseen data, roll, repeat. Two
numbers matter more than the equity curve:

- **efficiency** — pooled out-of-sample expectancy ÷ pooled in-sample
  expectancy. Below ~0.4 means the optimiser was fitting noise.
- **parameter stability** — if every fold wants different parameters, there is
  no edge, just a different curve fit each window.

### 4. Monte Carlo — *how bad does a plausible bad run get?*

Your backtest shows one accidental ordering of trades. Resampling thousands of
orderings gives the drawdown distribution. **Read the 5th percentile, not the
median.** If that drawdown would make you quit, your position size is too big.

## Why the engine is deliberately pessimistic

| Situation | What this engine does | What flatters you |
|---|---|---|
| Bar touches both stop and target | Takes the **stop** | Takes the target |
| Bar opens through the stop | Fills at the **open** (the gap) | Fills at the stop price |
| Long entry | Buys the **ask** | Uses the raw close |
| Short exit | Measured on the **ask** | Measured on the bid |
| Stop triggered | Market order, **takes slippage** | Exact fill |
| Spread | Hour-of-day model, **blows out at rollover** | A flat 0.1 |
| Position size | Floored to lot step | Rounded (over-risks every trade) |

On a $3 stop, a 0.25 spread is **8% of your risk on every single trade**. Over
800 trades that is not a rounding error — it is the whole result.

## Strategy: session trend-pullback

Gold on M5 is mostly noise, so the strategy's main job is declining to trade.
Four independent filters, then a trigger:

1. **Direction** — EMA(21/55) alignment plus price beyond EMA(200).
2. **Trend strength** — ADX ≥ threshold. The chop filter.
3. **Volatility regime** — ATR vs its own rolling median, with a floor *and a
   ceiling*. The floor skips dead tape the spread would eat; the ceiling skips
   post-news expansion where stops explode and fills go unreliable.
4. **Pullback then resumption** — RSI dips into pullback territory, then
   recovers out of it, with the bar closing beyond the prior bar's extreme.

Stops sit beyond a recent swing extreme with an ATR buffer — never a fixed
distance, which is arbitrary and gets swept. Targets are a multiple of the
realised stop distance. Break-even at 1R, ATR trail from 1.5R.

**This is a starting point, not a finished edge.** Every filter is a named
parameter that walk-forward is free to find worthless.

## XauFlash — seconds scalper (`mt5/XauFlash_S.mq5`)

بوت سكالب يفتح ويسكّر الصفقة خلال ثواني. يراقب كل تِك، وإذا الذهب تحرّك
حركة قوية باتجاه واحد خلال آخر 3 ثواني يدخل وياه، ويطلع عند الهدف أو
الوقف أو بعد 15 ثانية كحد أقصى — أيهم يصير أول.

| | |
|---|---|
| Entry | bid moves ≥ 0.60 in the last 3000 ms, on ≥ 6 ticks, ≥ 70% of steps one way, and ≥ 3× the live spread |
| Exit | TP 0.80 / SL 0.80 **sent to the server with the order**, or market close after `MaxHoldSeconds` (15 s) |
| Timer | a 200 ms timer enforces the hold limit even when no ticks arrive |
| Guards | 0.5% risk per trade, 3% daily loss, 5-loss streak, 40 trades/day, 20 s cooldown, 500-point spread cap (0.50 USD on a 3-digit feed), 07–20 GMT |
| Install | `mt5/installer/Install-XauFlash.bat`, attach to any XAUUSD chart (the timeframe does not matter) |

Honest caveats, also printed in the file header:

- **There is no backtest of this one in `goldbot/`.** The Python engine works
  on M5 bars and cannot see a 3-second burst. The only valid test is the MT5
  Strategy Tester with **"Every tick based on real ticks"**, then a demo.
- **Spread and slippage are most of the game.** A 0.25 spread is 31% of a 0.80
  target. The panel shows average slippage per fill — if it is eating the
  edge on demo, it will on live.
- **Latency matters.** Run it on a VPS close to the broker's server.
- **Broker rules.** Some brokers and most prop firms restrict trades held
  under a minimum time. Check before running it.

## XauSpike — two-core spike catcher (`mt5/XauSpike.mq5`)

بوت يصطاد الحركات العنيفة بالذهب بنظامين، وطريقة الخروج مأخوذة من تقرير
Strategy Tester لبوت تجاري. شرط الدخول مقدّر، لأن التقرير ما يبيّنه.

| | Core A (seconds) | Core B (minutes) |
|---|---|---|
| Trigger (estimated) | ≥ 3.00 in 5 s, ≥ 8 ticks, ≥ 65% one-way | ≥ 6.00 in 60 s, ≥ 30 ticks, ≥ 58% one-way |
| Stop / target | SL 5, TP 50 (far) | SL 15, TP 15 |
| Exit | trail from +1.50, 1.00 behind | at +5.00 the stop jumps to +1.50 |
| Max hold / cooldown | 30 min / 120 s | 90 min / 300 s |
| Magic | 770644 | 770655 |

Shared guards: 1% risk per core, 5% daily loss, 3-loss streak, 10 trades/day,
no entries 21–22 GMT (rollover), flat from 20:00 GMT Friday, 500-point spread
cap. Spike windows are maintained incrementally, so a 60 s window costs the
same per tick as a 5 s one in the tester. A skip-reason report per core is
printed daily and on stop.

The same caveats as XauFlash apply, with more force: the tester modifies
trailing stops with zero latency, and live spikes slip.

## Risk controls

Sizing is always derived from stop distance, never a fixed lot count — a fixed
0.10 lots risks $30 on a quiet morning and $300 through a CPI print.

- risk % of equity per trade (default 0.5%)
- daily loss limit → stops trading for the day
- consecutive-loss breaker
- max trades per day, one position at a time
- session windows in UTC, flat before the close, never carry M5 risk overnight
- news blackout windows

## Getting real data

You need M5 OHLC history. The loader handles MetaTrader `<DATE>/<TIME>` exports,
Dukascopy, and generic `time,open,high,low,close,volume` CSVs.

- **MT5**: Tools → Options → Charts → set bars, then View → Symbols → Bars → Export.
- **Dukascopy**: free historical tick/bar downloader.

> **Set `data.timezone` correctly.** MT5 exports are in *broker* server time,
> not UTC. If this is wrong your session filters are silently shifted by hours
> and you are testing a different system than you will trade.

Aim for 3+ years, covering both trending and ranging regimes.

## Going live

```python
from goldbot.config import load
from goldbot.live.runner import LiveRunner, PaperExecutor
from goldbot.live.mt5 import Mt5Feed, initialize

initialize()                      # credentials from env vars, never the config file
runner = LiveRunner(cfg=load("config/default.yaml"),
                    feed=Mt5Feed(),
                    executor=PaperExecutor(starting_equity=10_000))
runner.run(poll_seconds=20)
```

The runner **only acts on closed bars**. The newest row from any broker feed is
the bar still forming; its high, low and close are still moving, so a signal
derived from it is not reproducible. `MarketFeed.closed_bars` drops it
unconditionally.

Live routing (`Mt5Executor`) additionally requires `confirm_live=True`. Run
`PaperExecutor` for weeks first and compare its fills against the backtest.
There will be divergence. That divergence is your honest error bar.

## Project layout

```
mt5/
  XauTrend_M5.mq5        the Expert Advisor you install into MetaTrader 5
  installer/
    Install-XauTrend.bat      one-click Windows installer (copies + compiles)
docs/
  XauTrend_Exness_MT5_AR.pdf  Arabic install & operation guide (generated)
tools/
  pdf_rtl.py             right-to-left PDF layout engine
  make_guide_pdf.py      builds the guide from the EA source
goldbot/
  contract.py            instrument spec, sizing and rounding
  indicators.py          causal indicators (EMA, RSI, ATR, ADX, Wilder RMA)
  risk.py                sizing, daily limits, session windows
  metrics.py             expectancy, t-stat, drawdown, Sharpe
  config.py              YAML loading
  cli.py                 command line entry point
  data/
    loader.py            CSV loading, validation, gap reporting
    synthetic.py         test bars (NOT for evaluating strategies)
  strategy/
    base.py              Strategy interface
    trend_pullback.py    the gold M5 strategy
  backtest/
    broker.py            spread, slippage, commission, intrabar fills
    engine.py            bar loop and event sequencing
  validation/
    lookahead.py         repaint / causality auditor
    walkforward.py       rolling out-of-sample validation
    montecarlo.py        trade-sequence resampling
  live/
    runner.py            paper & live loop (closed bars only)
    mt5.py               MetaTrader 5 adapter
```

## What this cannot do

- It cannot give you an edge. It can only test one you propose.
- Backtests assume your fills resemble the model. Under news, they will not.
- Monte Carlo assumes trades are independent. Real losses cluster, so real risk
  is **worse** than the percentiles suggest.
- A passing walk-forward is the weakest claim worth acting on — not a promise.

If the validation says no, it is not a bug to work around. That is the tool
doing the single most valuable thing it can do.
