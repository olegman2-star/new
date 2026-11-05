# Guardian: trading-day portfolio protection

Guardian sends a risk report for Oleg's IBKR account to Telegram every U.S.
trading day, 60 minutes before the open. An optional second report comes 15
minutes after the close. It **advises only**: it reads positions through a
read-only IBKR Flex token and has no code that can place, change or cancel an
order.

## How a run works

| Step | What | Where |
|---|---|---|
| 1 | Time check: NY time vs the NYSE calendar (holidays skipped, early closes noted) | `guardian_core/schedule.py` |
| 2 | Positions, cost basis, account value from IBKR Flex | `guardian_core/ibkr_flex.py` |
| 3 | Prices, 13w/26w/52w levels, 30-day volatility, earnings dates (yfinance) | `guardian_core/market.py` |
| 4 | Stocktwits trending + bull/bear ratio | `guardian_core/sentiment.py` |
| 5 | Option quotes (Tradier, else delayed yfinance) and hedge math | `guardian_core/options.py` |
| 6 | **Rule engine:** sector filter, stop audit, never-lower ratchet, open risk, status floor | `guardian_core/analysis.py` |
| 7 | Claude writes the report from that data (+ web search for headlines) | `guardian_core/report.py` |
| 8 | Code enforces the format: status floor, no em dashes, closing lines | `guardian_core/report.py` |
| 9 | Telegram message + dated `.md` attachment | `guardian_core/report.py` |

The numbers behind the hard rules are computed by code, not by the model. That
covers stop levels, the never-lower ratchet, uncovered shares, open risk %,
sector FAIL and the RED trigger. Claude explains and prioritizes, but it cannot
move a stop or soften the status. If Claude writes GREEN when the code
says RED, the status line is replaced.

The system prompt is `prompts/system.md`. Edit it to change the voice or the
routine.

### Stop rule, exactly
1. Levels: 13w, 26w and 52w lows (prior bars, excluding today), nearest first.
2. The candidate stop is 0.3% under a level. It must sit at least 1.5x the daily volatility below the last price. If a level is too close, try the next one. If no level works, use the 1.5x-volatility distance.
3. Nudge it off round numbers (.00/.25/.50/.75), always downward.
4. **Ratchet:** suggested = max(computed, live stop, last suggestion). It never goes down.
5. If the last price is at or below that level, the row is flagged BREACHED ("plan says exit") and status is RED.

Open risk = (last − stop) × shares. Covered shares count at their live stop. Uncovered shares count at the suggested stop, which is the risk once Oleg places it. Above 8% of net liquidation, status is RED.

## Important: open orders and the public repo

**IBKR Flex does not report open orders.** In headless runs, the stop book is
the `manual_stops` list in `config.yaml`, and every report labels it **MANUAL,
NOT LIVE**. Update that list whenever you place, raise or cancel a stop. If
your Flex query ever includes an order section, the parser uses it instead.

**This repository is public.** Account data is kept out of everything
public:
- Reports go only to Telegram (message + `.md` file). `reports/` and `state/` are git-ignored.
- The Action prints only the status word, never the report.
- The ratchet and "already sent" markers live in the Actions cache, not in git.

Even so, `config.yaml` lists your tickers, share counts and stops, and it is
committed. **Recommended: move `guardian/` to a private repository** (or make
this one private).

## Setup

### 1. IBKR Flex Web Service (positions)

1. Log in to **IBKR Client Portal** → **Performance & Reports** → **Flex Queries**.
2. Under **Activity Flex Query**, click **+** (create).
3. Name it `guardian`. Add these sections:
   - **Open Positions**: options **Summary** (Guardian ignores lot rows anyway). Select at least:
     Symbol, Asset Class, Quantity, Mark Price, Position Value, Cost Basis Price,
     Cost Basis Money, FIFO Unrealized P/L, Put/Call, Strike, Expiry,
     Multiplier, Underlying Symbol, Currency, Level of Detail.
     (Selecting all fields is fine.)
   - **Net Asset Value (NAV) in Base**: needed for the account total. Without it, open risk % is "unknown".
   - **Cash Report** (optional): cash balance.
4. Delivery configuration: **Format: XML**, **Period: Last Business Day**. Save.
5. Note the **Query ID** shown next to the query.
6. On the Flex Queries page, open **Flex Web Service Configuration** (gear
   icon), enable the service and **generate a token**. Choose the longest
   expiry. Leave IP restriction off, because GitHub runners have changing IPs.
7. Calendar reminder: the token expires. When it does, runs fail with an IBKR
   error, and the Action turns red after still sending a report labeled
   with baseline holdings.

Test locally:
```bash
export IBKR_FLEX_TOKEN=... IBKR_FLEX_QUERY_ID=...
python guardian.py --dry-run --force --no-llm | head -40
```
The `positions_source` field should say `IBKR Flex (live, ...)`.

### 2. Telegram bot (delivery)

1. In Telegram, open **@BotFather** → `/newbot` → choose a name and a
   username ending in `bot`. BotFather replies with the **bot token**.
2. Open a chat with your new bot and send it any message (e.g. `hi`). Bots
   cannot message you first.
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates`.
   Find `"chat":{"id": 123456789, ...}`: that number is the **chat ID**.
   (For a group: add the bot to the group, post a message, use the negative id.)
4. Test:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" -d chat_id=<CHAT_ID> -d text=test
   ```

### 3. Option quotes (optional but recommended)

Without `TRADIER_TOKEN`, hedges are priced from delayed yfinance chains and
flagged as possibly stale. For real bid/ask, create a Tradier account and
copy the API access token from its settings. A sandbox token gives delayed
quotes: set the repository **variable** `TRADIER_SANDBOX=true` in that case.

### 4. GitHub secrets

Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | From |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | yes | BotFather |
| `TELEGRAM_CHAT_ID` | yes | getUpdates |
| `IBKR_FLEX_TOKEN` | yes | Flex Web Service Configuration |
| `IBKR_FLEX_QUERY_ID` | yes | Flex Queries list |
| `TRADIER_TOKEN` | no | Tradier settings |

Then run it once by hand: **Actions → Guardian Risk Report → Run workflow**,
with `force` ticked.

## Running locally

```bash
cd guardian
pip install -r requirements.txt
python guardian.py --dry-run --force             # full report, printed, nothing sent
python guardian.py --dry-run --force --no-llm    # just the collected data (no API cost)
python guardian.py --dry-run --force --baseline --account-value 150000   # no IBKR needed
python guardian.py --session close --dry-run --force   # end-of-day stop review
python -m pytest tests -q                        # rule tests, offline
```

`--dry-run` never sends, never writes state, and ignores the "already sent"
check. `--force` skips the time window.

## Schedule

GitHub cron is UTC with no DST, so the workflow has two slots per report
(EDT and EST). `guardian.py` computes the target from New York time
(`open − 60 min`, `close + 15 min`, early closes included) and skips the slot
that does not apply, plus weekends and NYSE holidays. GitHub can start
scheduled runs late. A run is accepted from 15 minutes before to 50
minutes after the pre-market target, and a sent marker prevents duplicates.

## Configuration (`config.yaml`)

- `holding_sector`: map each new holding to its sector ETF. Unmapped holdings are checked against SPY, and the report calls them out.
- `manual_stops`: your live stop book (see above). **Keep it current.**
- `proposed_stops`: seeds the ratchet. After live runs, the cached state takes over. If the cache is evicted (7 days without runs), the ratchet falls back to this list.
- `hedges.candidates`: structures to price every day; `auto_sector_spread` sizes an ETF put spread to each sector cluster of positions under 100 shares.
- `macro.events`: known dates (FOMC). The model also searches the week's calendar.
- `llm.model` / `effort`: report model (default `claude-opus-5-5`, effort `high`). The request opts into server-side refusal fallback.

Not financial advice. Oleg makes every decision.
