"""
Correlation hedge trader.

Flow:
  1. Load the Mataf forex correlation matrices (from the CSVs produced by
     mataf_correlation.py, or scrape fresh with --scrape).
  2. Ask which timeframe to trade (5min / 15min / hourly / 4h / daily).
  3. Find the MOST NEGATIVELY correlated pair-of-pairs in that timeframe
     (e.g. EURUSD vs USDCHF at -98) whose symbols are tradable on your broker.
  4. Ask the entry direction (long = buy both, short = sell both).
  5. Ask the lot size (applied in full to EACH instrument).
  6. Open both market positions on your MT5 account, hold for the timeframe
     duration, then close both.

Run:
  python mataf_correlation.py            # (optional) refresh the correlation CSVs first
  python correlation_trade.py            # interactive: pick timeframe, direction, lot
  python correlation_trade.py --scrape   # scrape fresh inside this tool instead of reading CSVs
  python correlation_trade.py --dry-run  # do everything except actually send orders

Fill in mt5_config.py with your demo account login/password/server first.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd

import mt5_trader as trader
import mt5_config as cfg

# timeframe -> (csv label, hold duration in seconds)
TIMEFRAMES = {
    "5min":   ("5min",   300),
    "15min":  ("15min",  900),
    "hourly": ("hourly", 3600),
    "4h":     ("4h",     14400),
    "daily":  ("daily",  86400),
}
MENU = ["5min", "15min", "hourly", "4h", "daily"]


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def ask_choice(prompt: str, options: list[str]) -> str:
    while True:
        print(prompt)
        for i, opt in enumerate(options, 1):
            print(f"   {i}) {opt}")
        raw = input("Enter number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw.lower() in options:
            return raw.lower()
        print("  Invalid choice, try again.\n")


def ask_lot() -> float:
    while True:
        raw = input("Enter lot size per instrument (e.g. 0.60): ").strip()
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
        print("  Invalid lot size, try again.")


def ask_yes_no(prompt: str) -> bool:
    return input(prompt + " [y/N]: ").strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_matrix(timeframe: str, scrape: bool, out_dir: str) -> pd.DataFrame:
    label = TIMEFRAMES[timeframe][0]
    if scrape:
        import mataf_correlation as mc
        print("Scraping fresh correlation data (a Chrome window will open)...")
        driver = mc._driver_launch(out_dir_profile(), headless=False, chrome_version=None)
        try:
            html = mc.fetch_html(driver)
        finally:
            driver.quit()
        df = mc.parse_matrix(html, mc.TIMEFRAMES[label])
        return df
    path = os.path.join(out_dir, f"correlation_{label}.csv")
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} not found. Run 'python mataf_correlation.py' first, "
                 f"or pass --scrape.")
    df = pd.read_csv(path, index_col=0)
    return df


def out_dir_profile() -> str:
    return "cf_profile"


def negative_candidates(df: pd.DataFrame) -> list[tuple[str, str, int]]:
    """Unique unordered pairs sorted most-negative-correlation first."""
    seen = set()
    rows = []
    for a in df.index:
        for b in df.columns:
            if a == b:
                continue
            v = df.loc[a, b]
            if pd.isna(v):
                continue
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            rows.append((key[0], key[1], int(v)))
    rows.sort(key=lambda r: r[2])  # ascending => most negative first
    return rows


# --------------------------------------------------------------------------- #
# Trading flow
# --------------------------------------------------------------------------- #
def wait_with_countdown(seconds: int) -> None:
    end = time.time() + seconds
    while True:
        rem = end - time.time()
        if rem <= 0:
            break
        r = int(round(rem))
        h, m = divmod(r, 3600)
        m, s = divmod(m, 60)
        print(f"\r  Holding positions... {h:02d}:{m:02d}:{s:02d} remaining ", end="", flush=True)
        time.sleep(min(1.0, max(0.0, end - time.time())))
    print()


def close_all_opened() -> None:
    positions = trader.open_positions_by_magic()
    if not positions:
        print("  No tagged positions to close.")
        return
    for p in positions:
        try:
            res = trader.close_position(p)
            print(f"  Closed {p.symbol} #{p.ticket} vol={p.volume} -> profit {p.profit:+.2f}")
        except Exception as e:
            print(f"  WARNING: failed to close {p.symbol} #{p.ticket}: {e}", file=sys.stderr)


def run(timeframe: str, direction: str, lot: float, df: pd.DataFrame,
        dry_run: bool, hold_override: int | None) -> None:
    hold = hold_override if hold_override is not None else TIMEFRAMES[timeframe][1]

    # Connect first so we can validate which symbols are actually tradable.
    acct = trader.connect()
    print(f"\nConnected: account {acct.login} on {acct.server} | "
          f"balance {acct.balance:.2f} {acct.currency} | "
          f"trade_allowed={acct.trade_allowed}")

    # Pick the most-negative pair whose BOTH legs resolve to tradable symbols.
    chosen = None
    for a, b, corr in negative_candidates(df):
        sa = trader.resolve_symbol(a, cfg.SYMBOL_SUFFIX)
        sb = trader.resolve_symbol(b, cfg.SYMBOL_SUFFIX)
        if sa and sb:
            chosen = (a, b, corr, sa, sb)
            break
        print(f"  Skipping {a}/{b} (corr {corr}) - "
              f"{'%s missing ' % a if not sa else ''}{'%s missing' % b if not sb else ''}")
    if chosen is None:
        trader.shutdown()
        sys.exit("ERROR: no negatively-correlated pair could be mapped to tradable symbols.")

    a, b, corr, sa, sb = chosen
    print("\n" + "=" * 60)
    print(f"  Timeframe        : {timeframe}  (hold {hold} s)")
    print(f"  Most -corr pair  : {a} / {b}   correlation = {corr}")
    print(f"  Broker symbols   : {sa} , {sb}")
    print(f"  Direction        : {direction.upper()}  "
          f"({'BUY' if direction == 'long' else 'SELL'} both)")
    print(f"  Lot per symbol   : {lot}")
    print(f"  Mode             : {'DRY-RUN (no orders)' if dry_run else 'LIVE'}")
    print("=" * 60)

    if not ask_yes_no("Proceed?"):
        trader.shutdown()
        sys.exit("Aborted by user.")

    if dry_run:
        print(f"\n[DRY-RUN] Would {direction} {lot} {sa} and {lot} {sb}, "
              f"hold {hold}s, then close both.")
        trader.shutdown()
        return

    # --- open both legs; roll back leg 1 if leg 2 fails ---------------------
    try:
        r1 = trader.open_position(sa, direction, lot)
        print(f"  Opened {sa}: {('BUY' if direction=='long' else 'SELL')} "
              f"{r1.volume} @ {r1.price}  (deal #{r1.deal})")
        try:
            r2 = trader.open_position(sb, direction, lot)
            print(f"  Opened {sb}: {('BUY' if direction=='long' else 'SELL')} "
                  f"{r2.volume} @ {r2.price}  (deal #{r2.deal})")
        except Exception as e:
            print(f"  ERROR opening second leg ({sb}): {e}", file=sys.stderr)
            print("  Rolling back first leg...")
            close_all_opened()
            raise
        # --- hold, then guarantee close even on Ctrl+C / error --------------
        try:
            wait_with_countdown(hold)
        finally:
            print("Closing positions...")
            close_all_opened()
    finally:
        acct = trader.account_info_safe()
        if acct:
            print(f"Final balance: {acct.balance:.2f} {acct.currency}")
        trader.shutdown()


# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="Correlation hedge trader (MT5).")
    p.add_argument("--scrape", action="store_true",
                   help="Scrape fresh correlation data instead of reading CSVs.")
    p.add_argument("--out", default="correlation_output",
                   help="Directory with correlation_<tf>.csv (default: correlation_output).")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything except actually send orders.")
    p.add_argument("--timeframe", choices=MENU, help="Skip the timeframe prompt.")
    p.add_argument("--direction", choices=["long", "short"], help="Skip the direction prompt.")
    p.add_argument("--lot", type=float, help="Skip the lot-size prompt.")
    p.add_argument("--hold-seconds", type=int, default=None,
                   help="Override the hold duration (default: the timeframe length).")
    args = p.parse_args(argv)

    timeframe = args.timeframe or ask_choice("\nSelect correlation timeframe:", MENU)
    df = load_matrix(timeframe, args.scrape, args.out)

    direction = args.direction or ask_choice(
        "\nEntry type:", ["long", "short"])
    lot = args.lot if args.lot is not None else ask_lot()

    run(timeframe, direction, lot, df, args.dry_run, args.hold_seconds)


if __name__ == "__main__":
    main()
