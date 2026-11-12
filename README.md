# Stock Finder India 📈

A free, self-updating stock research app for NSE (Indian) stocks, built for a retired investor who wants
**clear, rule-based ideas** instead of tips and noise. It runs entirely on GitHub (no server, no cost) and
works on phone and laptop. You can install it on a phone's home screen like an app.

**Made for a very cautious, non-expert investor with a small amount (₹20,000 suggested).**
Nothing to press: the **Today** screen and a phone message simply say what to do at the next market open:
🔴 **SELL** · 🛒 **BUY** (how many shares, the most to pay, the most you could lose) · ✋ **HOLD**, or
**"Nothing to do today"**, which is the most common answer. Three big buttons: **Today**, **Results**, **More**.

### Safe mode (on by default)
| Rule | Why |
|---|---|
| Only **Nifty 50** companies (India's 50 largest), calmer than average, in rising long-term trends | No small or risky stocks |
| Only **one method**: buying a short dip in such a company, and **only while the whole market is rising** | On 5 years of real data: made money in **66%** of 476 trades, gains 1.5× losses, still profitable over the last 12 months |
| A **safety gate**: the method is used only while its own 5-year record shows ≥40 trades, ≥60% winners, gains ≥1.2× losses, and no loss over the last 12 months | If it stops working, the app pauses buying by itself |
| **One trade at a time** with the whole amount, whole shares only, the strongest qualifying stock first, no stock with bad news | On real data, 1 × ₹20,000 earned about 7× more than 2 × ₹10,000 (fixed broker charges hurt small trades), with a smaller worst dip |
| Every trade has a **safety sell price**, with a phone alert the moment it's reached, even on the day of buying | Losses stay small |

Typical trade: bought at 9:15 AM, sold within about 5 days. No method is ever 100% sure. Expect some losing trades;
the edge comes from many small, controlled trades. To switch safe mode off (all ideas), add the repository variable `SAFE_MODE` = `off`.
The share counts in phone messages use `TRADE_AMOUNT` (default ₹20,000; set the repository variable to his real amount).

### What every suggestion shows
Stock, how many shares, the most to pay, the sell-if-it-falls price, **how long to hold**, and **what to expect**: a bad case, a typical case
and a good case in rupees (with the sell price), taken from similar past cases after charges, plus exact steps to place the order
(NSE, Delivery, Limit order, then a stop-loss/GTT sell order). Long-term holdings also get a safety price that **rises as the price rises**
(25% below the highest price since buying; 10% for the index fund), so gains are protected. A tighter 12% rule was tested on real data and rejected: it sold winners on ordinary dips (typical result +0.6% against +4.8% for the 25% rule, with no better worst case). Suggestions are valid for 3 trading days and are re-checked every weekday evening.

### Honest expectations with ₹20,000 (5 years of real prices, after all charges)
| | |
|---|---|
| Trades | about 20 a year (one at a time, usually sold within ~5 days) |
| Made money | about 65 out of 100 trades |
| Total profit | about **₹3,800 in 5 years** (≈ ₹760 a year, ≈ 3.5% a year); one year was +₹4,300, two years were slightly negative |
| Worst dip along the way | about ₹2,000 (≈ 10%); the Nifty itself fell more |
| Money actually in a trade | only ~30% of the time |

That is **low risk but modest profit**, less than a fixed deposit on its own. Keeping the idle money in a liquid fund or liquid ETF
(for example LIQUIDBEES) and moving it only on buy days raised the total to about **7% a year** in the same test, at the cost of a few extra orders.
Below about ₹15,000 the fixed broker charges take most of the profit (₹10,000 split into two trades lost money in the test), and the app warns about it.
The live track record on the Results screen will show real results as they come in.

**What it gives you every day**

| | |
|---|---|
| **Market mood** | Is it a good time to buy at all? (Bullish / Cautious / Bearish, based on Nifty's 50 and 200-day averages and market breadth) |
| **Live Intelligence** | Near-live price, change, VWAP, 5/15-minute momentum, session range, quote freshness, and distance to each stop/target — without turning watch-only stocks into buys |
| **Swing ideas** | Specific stocks to buy at the next open, with the **buy limit, stop-loss, target, how many shares to buy for your capital, and the maximum loss in ₹** |
| **Long-term portfolio** | 15 strong large/mid-cap stocks to hold, reviewed monthly, with quantities for your amount and a staged investing plan |
| **Proof** | Backtests of every rule (after costs) **plus a live track record** of every idea the app has ever given, with real outcomes |
| **News** | Latest headlines for each stock and the market, with automatic red-flag detection (SEBI action, fraud, pledges, defaults…) |
| **Long-term champions** (More → Advanced) | Shares that rose in most years and got back to their old price fastest after every crash since 2008 (2008, 2011, 2016, COVID 2020, 2022…), with a crash recovery race, a report card for any Nifty 200 share, and an honest back-test: champions picked using only older prices, then held. Built weekly from ~19 years of prices (`engine/champions.py`) |
| **Alerts** | In-app/browser alerts, plus optional free phone push (ntfy), Telegram or Gmail for stop-loss and target hits and new ideas |

> ⚠️ **Honest expectations.** No system wins every time, and anyone promising that is not being truthful.
> The goal here is a *positive edge after costs*: winners that are bigger or more frequent than losers,
> with every loss kept small by a stop-loss. The app shows its own historical and live win rates so you can
> judge it on evidence. This is an educational tool, **not SEBI-registered investment advice**.

---

## How the ideas are chosen (the research behind it)

The rules use only well-documented, robust effects. They are kept deliberately simple, because complex,
over-fitted systems tend to look great in backtests and then fail with real money.

1. **Market regime filter.** No new swing buys while the Nifty 50 is below its 200-day average; half size when it's weak.
   This one rule avoids most of the damage of bear markets.
2. **Swing setups** (held for days to a few weeks), only in liquid Nifty 200 stocks that are in long-term uptrends and stronger than the market:
   * **Dip in an uptrend:** a 2-day RSI below 10 in a strong stock (a Connors-style short-term mean reversion). It wins often and makes small gains.
   * **Breakout with volume:** a new 50-day high on 1.5× volume from a top-30% relative-strength stock, with a trailing stop. It wins less often but the wins are bigger.
   * **Bounce from support:** a pullback to the 20-day EMA followed by a bullish reversal day. Target is 2× the risk.
3. **Long-term momentum portfolio.** This is the method behind NSE's **Nifty200 Momentum 30** index (6 and 12-month returns divided by volatility).
   NSE's [2026 whitepaper](https://www.niftyindices.com/docs/default-source/indices/nifty200-momentum-30/momentum-strategy-whitepaper_2026.pdf)
   reports 19.2% a year for that index since 2005, against 15.1% for the Nifty 200, with deeper falls in crashes.
   On top of that idea this app adds an uptrend filter, a debt and loss filter, a cap of 4 stocks per industry, and monthly reviews with a buffer that reduces churn.
4. **Scoring.** Each signal gets a 0-100 score from its setup's backtested win rate and average gain, relative strength, volume,
   NSE delivery % (when available), news sentiment, company quality and market regime. Only High/Medium scores without red flags become **BUY**.
5. **Self-policing.** If a setup's own backtest is not profitable after costs (profit factor below 1.1), it is automatically switched to "watch only".
6. **Position sizing.** Each swing trade risks at most 1% of your swing money (you can change this). Quantity = risk amount ÷ (entry − stop),
   capped at 20% of swing money per stock. This is how professionals survive losing streaks.

Open-source projects reviewed for ideas: [PKScreener](https://github.com/pkjmesra/PKScreener) (a large NSE scanner suite),
[NSE-Stock-Scanner](https://github.com/soodrohit/NSE-Stock-Scanner) and
[KJStockScreener](https://github.com/85599/KJStockScreener). This app differs from them in three ways:
it **backtests and live-tracks every rule it recommends**, it **sizes positions for you**, and it needs **no server**.

**Data:** Yahoo Finance via `yfinance` (free, split-adjusted daily data plus 1-minute bars for the monitored live list), with NSE's official daily files used for delivery % and
Nifty P/E when reachable, and as a backup price source. The 1-minute bars are summarised into change, VWAP, short momentum and session-range position, then published about every 15 minutes.
This is **near-live, not exchange-grade real time**: GitHub scheduling, Yahoo and Pages deployment can all add delay. NSE distributes licensed real-time/tick data through
[its data service and authorised vendors](https://www.nseindia.com/static/market-data/real-time-data-subscription). Always confirm the executable price in your broker app.

---

## Set it up on GitHub (one time, ~10 minutes)

1. **Create a repository** on GitHub (for example `indian_stock_finder`) and push this folder to it.
   It must be **public** for free GitHub Pages hosting (or use a paid plan for private Pages).
2. **Allow the bot to save data:** repo **Settings → Actions → General → Workflow permissions →** select
   **"Read and write permissions"** → Save.
3. **Turn on the website:** **Settings → Pages →** Source: *Deploy from a branch*, Branch: `main`, Folder: `/docs` → Save.
   Your app will be at `https://<your-username>.github.io/<repo-name>/`.
4. **Run the first scan:** **Actions** tab → **Daily scan** → **Run workflow**. Wait about 5 minutes, then open the app.

### What runs, and what you start

There are only **three** workflows (GitHub also shows its own automatic "pages build and deployment" job; ignore it).

| Workflow | What it does | Who starts it |
|---|---|---|
| **Live prices** | **Starts itself each trading morning** (about 8:40 AM, once the timer below is set up). Then the whole day runs by itself: morning reminder, live prices refreshed every minute or two until 3:40 PM, stop-loss alerts, and, once the day's data is final (about 4:20 PM), the evening scan | The **Google timer** (see "Automatic start" below), or by hand: Actions → Live prices → Run workflow. Tick "Only send a test alert" to check your phone |
| **Daily scan** | Downloads 5 years of data, runs the backtests, finds the next day's suggestions, updates the long-term list, reads news, records the track record, sends the plan | Started automatically by **Live prices** at about 4:20 PM, whenever the engine code changes, or by hand |
| **Tests** | Checks the engine (including a test that the backtest never "peeks" at future prices) | Automatically on every code change |

**Why does something need to start it?** GitHub's free scheduler does not fire for this repository (a test job that should have run every 10 minutes ran zero times in two hours),
so GitHub cannot start the day on its own. Workflows started by hand, by a push, or by another workflow work reliably, so one start chains everything.

### Automatic start (one-time setup, about 10 minutes, then nothing to do ever again)

A free Google timer starts the day for you at about 8:40 AM on weekdays (and backs up the evening scan at 4:40 PM). Google's timers are dependable and the access token stays inside your own Google account.

1. **Make a GitHub access token.** GitHub → your profile picture → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
   Name it `stock-finder-timer`, expiration **1 year**, *Repository access* → **Only select repositories** → `indian_stock_finder`,
   *Repository permissions* → **Actions: Read and write**. Generate and copy the token (it starts with `github_pat_`).
2. **Create the timer.** Go to <https://script.google.com> → **New project**. Delete the sample code and paste in everything from `automation/google-apps-script.gs` (in this repository).
3. **Add the token.** Left sidebar → **Project Settings** (gear) → *Script properties* → **Add script property**: name `GITHUB_TOKEN`, value = the token. Also tick **Show "appsscript.json" manifest file**, open it in the editor and set `"timeZone": "Asia/Kolkata"`.
4. **Switch it on.** Back in **Editor**, choose the function **createTriggers** in the drop-down and press **Run**. Approve the permissions Google asks for (it only needs to call the web).
5. **Test.** Choose **startLivePrices** and press **Run**. The log should say `HTTP 204`, and the **Actions** tab shows a new **Live prices** run.

Renew the token once a year (the same steps 1 and 3). Weekends are skipped automatically. If you ever want to start it manually, use Actions → Live prices → Run workflow, or edit `automation/start-live.txt` and push.

Live prices are published to the `live-data` branch (a single commit that is overwritten, so no history growth and no site rebuilds).
Free Yahoo prices are usually 1 to 3 minutes behind the exchange; always confirm the price in your broker app.

### Install on the phone
Open the app link → **Android/Chrome:** ⋮ → *Add to Home screen*. **iPhone/Safari:** Share → *Add to Home Screen*.
Then open **⚙ Settings** in the app and enter the capital and split (for example 70% long-term, 30% swing).
These settings stay on that phone only.

---

## Alerts when the app is closed (optional, free)

Add any of these in **Settings → Secrets and variables → Actions → New repository secret**:

| Channel | Secrets | How |
|---|---|---|
| **Phone push (easiest)** | `NTFY_TOPIC` | Install the free **ntfy** app (Android/iOS), subscribe to a hard-to-guess topic name such as `sf-alerts-7k2p9q`, and put the same name in the secret. No account needed. Anyone who knows the topic name can read it, so keep it unusual. |
| **Telegram** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Create a bot with @BotFather, send it a message, then get your chat id from `https://api.telegram.org/bot<token>/getUpdates`. |
| **Gmail** | `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `ALERT_EMAIL_TO` | Turn on 2-step verification in Google, create an **App password**, and use it here (never your real password). |
| **WhatsApp** | – | Not built in: WhatsApp's official API needs a Meta Business account. Telegram or ntfy give the same result for free. |

### AI research notes (optional, the only paid feature)
Each daily scan can ask Claude to write a short, plain-English note for each recommended stock and for the market.
The notes only summarise the technicals, backtest numbers and headlines the engine already found; they invent nothing.
It costs a few cents a day, so it needs **two** deliberate steps to switch on: add the secret `ANTHROPIC_API_KEY` **and**
the repository variable `AI_NOTES` = `on`. If either is missing, it stays off and everything else still works.

---

## Cost: ₹0 (how it stays free)

| Part | Cost | Why |
|---|---|---|
| GitHub Actions (the engine) | Free | Unlimited minutes for **public** repositories |
| GitHub Pages (the app) | Free | Free for public repositories |
| Market data (Yahoo Finance, NSE files) and news (RSS) | Free | Public sources, no API keys |
| ntfy / Telegram / Gmail alerts | Free | All free services |
| AI notes | Paid, **off by default** | See above: needs a key *and* `AI_NOTES=on` |

Safety nets:
* **Keep the repository public.** If it were private, Actions would use the 2,000 free minutes a month, the 15-minute
  live updates use most of those, and Pages would need a paid plan.
* Under **Settings → Billing and licensing → Budgets and alerts**, keep the Actions budget at **₹0 / $0** with
  "stop usage when budget is reached". GitHub then stops the runs instead of charging. That's the default if you never added a payment method.
* **Kill switch:** to pause all automatic runs, add the repository variable `AUTO_RUN` = `off`
  (Settings → Secrets and variables → Actions → Variables). Delete it to resume.
* Each run has a time limit (20 min daily, 8 min live), so a stuck run can't keep running.

---

## Using it sensibly (please read)

* **Follow the plan exactly.** Buy only up to the price shown, and sell when the app says SELL. If you skip a buy, just ignore that stock's later messages.
* **Follow the stop-loss every time.** Place it in your broker app right after buying (a GTT/stop order). This is what keeps losses small.
* **Expect losing streaks.** The Results tab shows each setup's worst historical streak. Risking 1% per trade means even 10 losses in a row cost only ~10% of swing money.
* **No trade is a valid answer.** On many days, and in bear markets, the app suggests nothing. That is a feature.
* **Give it time.** Judge the system on the live track record after 30+ closed trades, not on a single week.
* **Backtests are optimistic.** They use today's Nifty 200 members (survivorship bias) and can't model every real-world friction. Expect live results to be somewhat lower.
* Keep an emergency fund and fixed income outside this app. Swing trading should be a small part of a retiree's money.

---

## For developers

```bash
pip install -r requirements.txt pytest
python -m pytest -q                              # engine tests
python -m engine.run --mode full --demo           # offline run on synthetic data (writes to data/cache/demo/)
python -m engine.run --mode full                  # real run (writes docs/data/)
python -m engine.run --mode live --force          # live price refresh
cd docs && python -m http.server 8000             # view the app at http://localhost:8000
```

```
engine/            Python engine (runs in GitHub Actions)
  config.py        all tunable settings (costs, risk, universe, filters)
  data.py          Yahoo Finance / NSE bhavcopy / demo data loaders
  live_analysis.py Quote freshness, feed health, coverage and stale-data protection
  strategies.py    market regime, swing setups, momentum scoring
  backtest.py      trade-by-trade swing backtest + monthly portfolio backtest
  tracker.py       live track record of every recommendation
  news.py          RSS news, sentiment, red flags
  fundamentals.py  quality filter (ROE, debt, losses, P/E)
  notify.py        ntfy / Telegram / Gmail alerts
  ai_notes.py      optional Claude research notes
  run.py           orchestrator (full / live modes)
docs/              the web app (GitHub Pages): index.html, app.js, app.css, sw.js
docs/data/         JSON published by the engine (latest, live, backtest, track record, news)
tests/             pytest suite
```

## License
See [LICENSE](LICENSE).
