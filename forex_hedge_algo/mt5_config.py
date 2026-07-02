"""
MetaTrader 5 connection + trading settings.

Fill in your DEMO account details below. You get these from the MT5 terminal:
  Tools > Options > or the "File > Login to Trade Account" dialog shows the
  Login (account number) and Server; the password is the one you set.
"""

# --- Account login -----------------------------------------------------------
# Account number (integer). Leave as 0 to just attach to an already logged-in terminal.
LOGIN = 25687228
# Trade/master password for the account above.
PASSWORD = "Dhairya7@"
# Trade server name exactly as shown in MT5, e.g. "MetaQuotes-Demo", "Exness-MT5Trial9".
SERVER = "VantageMarkets-Demo"

# --- Terminal ----------------------------------------------------------------
# Path to terminal64.exe. The default install path is filled in below.
# Set to None to let the MetaTrader5 package auto-locate a running terminal.
TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# --- Symbols -----------------------------------------------------------------
# If your broker appends a suffix to symbol names (e.g. EURUSD shows as
# "EURUSD.m", "EURUSDx", "EURUSD.raw"), put the suffix here. Leave "" for none.
# The trader also auto-detects symbols at runtime, so this is just a hint/override.
SYMBOL_SUFFIX = ""

# --- Order execution ---------------------------------------------------------
DEVIATION = 20        # max allowed slippage in points for market orders
MAGIC = 555001        # tag applied to trades opened by this tool
COMMENT = "corr_hedge"  # order comment
