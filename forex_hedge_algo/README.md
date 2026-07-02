# Forex Correlation Hedge Trader

Scrapes the Mataf forex correlation table and trades the most negatively-correlated
pair on a MetaTrader 5 account.

## Setup

1. Install Python 3.11+ and Google Chrome (Windows).
2. Install the MetaTrader 5 terminal and log into your (demo) account.
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Open `mt5_config.py` and fill in your account details:
   - `LOGIN`  – account number (leave `0` to attach to an already logged-in terminal)
   - `PASSWORD` – trade password
   - `SERVER` – trade server name (e.g. `MetaQuotes-Demo`)
   - `TERMINAL_PATH` – path to `terminal64.exe`
   - `SYMBOL_SUFFIX` – broker symbol suffix if any (e.g. `.m`), otherwise leave `""`

## Usage

```
python mataf_correlation.py     # scrape correlation data -> correlation_output/*.csv
python correlation_trade.py     # interactive: pick timeframe, direction, lot
```

The trader asks for:
- **Timeframe** (5min / 15min / hourly / 4h / daily)
- **Entry type** – `long` = buy both legs, `short` = sell both legs
- **Lot size** – applied in full to EACH instrument

It then finds the most negatively-correlated pair for that timeframe, opens both
positions, holds for the timeframe duration, and closes both.

Useful flags:
- `--dry-run` – run everything except sending orders
- `--scrape` – scrape fresh data instead of reading the CSVs
- `--timeframe 15min --direction long --lot 0.60` – skip the prompts
- `--hold-seconds N` – override the hold duration (handy for testing)

## Files
| File | Purpose |
|------|---------|
| `mataf_correlation.py` | Scrapes the correlation matrices (handles the Cloudflare challenge) |
| `correlation_trade.py` | Interactive trading workflow |
| `mt5_trader.py` | MetaTrader 5 connect / open / close helpers |
| `mt5_config.py` | Your account + execution settings (fill these in) |

## Notes
- The Mataf page is behind Cloudflare. The first scrape opens a visible Chrome window;
  solve the "Verify you are human" check once and the session is cached in `cf_profile/`.
- Daily / 4h selections hold positions for a long time (24h / 4h). Use `--hold-seconds`
  to test with a shorter hold.
