"""
Scrape the Forex correlation matrix from https://www.mataf.net/en/forex/tools/correlation

The page sits behind Cloudflare's "Just a moment..." managed (Turnstile) challenge, so a
plain requests/curl call returns HTTP 403. This scraper drives a real Chrome browser instead:

  * --attach HOST:PORT  -> attach to a Chrome you already started with
                           --remote-debugging-port (the challenge is solved in that window,
                           manually once if needed). Most reliable on locked-down IPs.
  * (default)           -> launch Chrome with a persistent profile dir. Once you solve the
                           Cloudflare check in that window a single time, the `cf_clearance`
                           cookie is cached in the profile and later runs load straight through
                           until it expires.

Output (in --out dir, default ./correlation_output):
  correlation_<timeframe>.csv  : 28x28 matrix, one file per timeframe
  correlation_long.csv         : tidy long format -> timeframe,pair_a,pair_b,correlation

Usage:
  # 1) Start Chrome yourself (recommended on a server / blocked IP), solve the CF check once:
  #    chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\cf_profile" \
  #               "https://www.mataf.net/en/forex/tools/correlation"
  python mataf_correlation.py --attach 127.0.0.1:9222

  # 2) Or let the script launch a persistent-profile Chrome and wait for you to solve CF:
  python mataf_correlation.py --profile "C:\\cf_profile"

  # Limit timeframes / change output dir:
  python mataf_correlation.py --attach 127.0.0.1:9222 --timeframes 4h daily --out data
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

URL = "https://www.mataf.net/en/forex/tools/correlation"

# Mataf renders one <table> per timeframe inside a tab pane whose id is correl_<seconds>.
TIMEFRAMES = {
    "daily":  "correl_86400",
    "4h":     "correl_14400",
    "hourly": "correl_3600",
    "15min":  "correl_900",
    "5min":   "correl_300",
}


# --------------------------------------------------------------------------- #
# Browser acquisition
# --------------------------------------------------------------------------- #
def _driver_attached(address: str) -> webdriver.Chrome:
    """Attach to an already-running Chrome started with --remote-debugging-port."""
    opts = Options()
    opts.add_experimental_option("debuggerAddress", address)
    return webdriver.Chrome(options=opts)


def _detect_chrome_major() -> int | None:
    """Best-effort detection of the installed Chrome major version (Windows).

    undetected-chromedriver otherwise grabs the *latest* chromedriver, which fails with a
    'only supports Chrome version N' error when the local Chrome lags behind.
    """
    # 1) Registry (most reliable, no file access needed).
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"Software\Google\Chrome\BLBeacon") as k:
                    version, _ = winreg.QueryValueEx(k, "version")
                    return int(version.split(".")[0])
            except OSError:
                continue
    except ImportError:
        pass
    # 2) chrome.exe file version.
    import subprocess

    for exe in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if os.path.exists(exe):
            try:
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
                    text=True, stderr=subprocess.DEVNULL,
                ).strip()
                if out:
                    return int(out.split(".")[0])
            except Exception:
                pass
    return None


def _driver_launch(profile_dir: str, headless: bool, chrome_version: int | None) -> webdriver.Chrome:
    """Launch Chrome with a persistent profile so a solved cf_clearance cookie is reused.

    Uses undetected-chromedriver when available (better at passing Cloudflare), otherwise
    falls back to plain Selenium.
    """
    profile_dir = os.path.abspath(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)
    try:
        import undetected_chromedriver as uc

        if chrome_version is None:
            chrome_version = _detect_chrome_major()
            if chrome_version:
                print(f"  Detected Chrome major version {chrome_version}.", file=sys.stderr)
        opts = uc.ChromeOptions()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--window-size=1920,1400")
        if headless:
            opts.add_argument("--headless=new")
        return uc.Chrome(options=opts, version_main=chrome_version)
    except ImportError:
        opts = Options()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--window-size=1920,1400")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        if headless:
            opts.add_argument("--headless=new")
        return webdriver.Chrome(options=opts)


def fetch_html(driver: webdriver.Chrome, wait: int = 120) -> str:
    """Navigate (if needed) and wait until the correlation tables are present.

    Pauses while the Cloudflare 'Just a moment...' page is showing so a human can solve the
    challenge in the visible window.
    """
    if "mataf.net" not in (driver.current_url or ""):
        driver.get(URL)

    deadline = time.time() + wait
    warned = False
    while time.time() < deadline:
        title = driver.title or ""
        html = driver.page_source
        if "EURUSD" in html and "Just a moment" not in title and "correl_14400" in html:
            return html
        if "Just a moment" in title and not warned:
            print("  Cloudflare challenge detected -> solve it in the Chrome window...",
                  file=sys.stderr)
            warned = True
        time.sleep(2)
    raise TimeoutError(
        f"Tables did not appear within {wait}s (still on Cloudflare or page failed to load)."
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _to_int(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_matrix(html: str, pane_id: str) -> pd.DataFrame:
    """Parse one timeframe table into a square DataFrame indexed/columned by currency pair.

    Cells carry data-inst1 (row pair) / data-inst2 (column pair); the diagonal is left blank.
    """
    soup = BeautifulSoup(html, "lxml")
    pane = soup.find(id=pane_id)
    if pane is None:
        raise ValueError(f"pane '{pane_id}' not found in page")
    table = pane if pane.name == "table" else pane.find("table")
    if table is None:
        raise ValueError(f"no <table> inside pane '{pane_id}'")

    # Column order from the header row.
    header = table.find("tr")
    cols = [th.get("data-inst2") for th in header.find_all("th") if th.get("data-inst2")]

    data: dict[str, dict[str, int]] = {}
    for tr in table.find_all("tr"):
        row_pair = tr.get("data-inst1")
        if not row_pair:
            continue
        row = {}
        for td in tr.find_all("td"):
            col_pair = td.get("data-inst2")
            if col_pair:
                row[col_pair] = _to_int(td.get_text())
        data[row_pair] = row

    df = pd.DataFrame.from_dict(data, orient="index")
    df = df.reindex(index=cols, columns=cols)  # keep mataf's pair ordering
    df.index.name = "pair"
    return df


def scrape(html: str, timeframes: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for tf in timeframes:
        pane = TIMEFRAMES[tf]
        out[tf] = parse_matrix(html, pane)
    return out


def save(matrices: dict[str, pd.DataFrame], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    long_rows = []
    for tf, df in matrices.items():
        df.to_csv(os.path.join(out_dir, f"correlation_{tf}.csv"))
        for a in df.index:
            for b in df.columns:
                v = df.loc[a, b]
                if a != b and pd.notna(v):
                    long_rows.append((tf, a, b, int(v)))
    long_df = pd.DataFrame(long_rows, columns=["timeframe", "pair_a", "pair_b", "correlation"])
    long_df.to_csv(os.path.join(out_dir, "correlation_long.csv"), index=False)
    print(f"Saved {len(matrices)} matrices + correlation_long.csv ({len(long_df)} rows) "
          f"to {os.path.abspath(out_dir)}")


# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="Scrape mataf.net forex correlation tables.")
    p.add_argument("--attach", metavar="HOST:PORT",
                   help="Attach to a Chrome already running with --remote-debugging-port.")
    p.add_argument("--profile", default="cf_profile",
                   help="Persistent Chrome profile dir when launching (default: cf_profile).")
    p.add_argument("--headless", action="store_true",
                   help="Launch headless (only works once cf_clearance is cached; CF usually "
                        "needs a visible window the first time).")
    p.add_argument("--chrome-version", type=int, default=None,
                   help="Pin undetected-chromedriver to this Chrome major version.")
    p.add_argument("--timeframes", nargs="+", default=list(TIMEFRAMES),
                   choices=list(TIMEFRAMES),
                   help="Timeframes to scrape (default: all).")
    p.add_argument("--out", default="correlation_output", help="Output directory.")
    p.add_argument("--wait", type=int, default=120,
                   help="Seconds to wait for tables / manual CF solve (default: 120).")
    args = p.parse_args(argv)

    own_driver = args.attach is None
    if args.attach:
        driver = _driver_attached(args.attach)
    else:
        driver = _driver_launch(args.profile, args.headless, args.chrome_version)

    try:
        html = fetch_html(driver, wait=args.wait)
    finally:
        if own_driver:
            driver.quit()  # leave an attached browser open

    matrices = scrape(html, args.timeframes)
    save(matrices, args.out)


if __name__ == "__main__":
    main()
