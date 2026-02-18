#!/usr/bin/env python3
"""
Indian Stock Market Trend Monitor (Production)

Designed for LIVE market hours: signals follow volume trend (large participation =
many people buying or selling) and price direction. News-driven moves typically
show up as both high volume and sharp price change, which this logic captures.

- Volume surge: current volume vs recent average → more people trading.
- Price % change: session direction → buying vs selling pressure.
- Score = volume_ratio * |price_pct| → strength of trend; alerts when > 3.0.
- Risk level (Low/Medium/High) from score, volatility and volume to gauge risk.

Config from config.json; web UI with Start/Stop monitoring; logs and CSV; graceful shutdown.
"""

import csv
import json
import logging
import os
import signal
import smtplib
import threading
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Config (loaded from config.json)
# ---------------------------------------------------------------------------
CONFIG: Dict[str, Any] = {}
REQUIRED_KEYS = [
    "email_id",
    "email_pass",
    "recipient",
    "volume_threshold",
    "price_threshold",
    "check_interval_min",
]

# Full list of NSE stocks (loaded from config: stocks_list_file or stocks_list)
STOCKS_FULL_LIST: List[Dict[str, str]] = []
MAX_SYMBOLS_PER_RUN = 50  # limit to avoid timeouts
STOCKS_LIST_PATH = "stocks_list.json"  # default filename

# Constants not in config
INTRADAY_INTERVAL = "5m"
PERIOD = "1d"
AVG_VOLUME_PERIODS = 10
MAX_EMAILS_PER_HOUR = 5
ALERT_SCORE_THRESHOLD = 3.0  # score = volume_ratio * abs(price_pct)
LOG_FILE = "stock_monitor.log"
CSV_FILE = "trends.csv"
CONFIG_PATH = "config.json"
NOTIFICATION_EMAILS_FILE = "notification_emails.json"

# State
last_signals: Dict[str, str] = {}
email_sent_times: List[float] = []
shutdown_requested = False
MOCK_EMAIL_SEND = False

# Continuous monitoring: run cycle every N minutes, send email + show on page
monitoring_active = False
monitoring_thread: Optional[threading.Thread] = None
last_results: List[Dict[str, Any]] = []
last_run_time: Optional[str] = None
_monitoring_lock = threading.Lock()
# Selected symbols from frontend (used for run and monitoring)
current_watchlist: List[str] = []
# Default selection for UI (from config watchlist if present, else first 20)
default_selection: List[str] = []

# Logging (configured after load_config)
logger: Optional[logging.Logger] = None


# ---------------------------------------------------------------------------
# Config load and validation
# ---------------------------------------------------------------------------
def load_config(config_path: Optional[str] = None) -> None:
    """Load config from config.json and validate required keys. Load stocks list from config."""
    global CONFIG, logger, STOCKS_FULL_LIST
    path = Path(config_path or CONFIG_PATH)
    if not path.is_file():
        raise FileNotFoundError("Config file not found: {}".format(path.resolve()))
    config_dir = path.resolve().parent
    with open(path, "r", encoding="utf-8") as f:
        CONFIG.update(json.load(f))
    missing = [k for k in REQUIRED_KEYS if k not in CONFIG]
    if missing:
        raise ValueError("config.json missing required keys: {}".format(missing))
    # Load stocks list: from CONFIG["stocks_list"] or from file CONFIG["stocks_list_file"]
    stocks_loaded = []
    if isinstance(CONFIG.get("stocks_list"), list):
        for item in CONFIG["stocks_list"]:
            if isinstance(item, dict) and item.get("symbol") and item.get("name"):
                stocks_loaded.append({"symbol": str(item["symbol"]), "name": str(item["name"])})
    if not stocks_loaded and CONFIG.get("stocks_list_file"):
        file_path = Path(CONFIG["stocks_list_file"])
        if not file_path.is_absolute():
            file_path = config_dir / file_path
        if file_path.is_file():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict) and item.get("symbol") and item.get("name"):
                            stocks_loaded.append({"symbol": str(item["symbol"]), "name": str(item["name"])})
            except Exception as e:
                if logger:
                    logger.warning("Could not load stocks list from %s: %s", file_path, e)
    if not stocks_loaded:
        file_path = config_dir / STOCKS_LIST_PATH
        if file_path.is_file():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict) and item.get("symbol") and item.get("name"):
                            stocks_loaded.append({"symbol": str(item["symbol"]), "name": str(item["name"])})
            except Exception as e:
                if logger:
                    logger.warning("Could not load stocks list from %s: %s", file_path, e)
    if stocks_loaded:
        STOCKS_FULL_LIST[:] = stocks_loaded
    else:
        # Minimal fallback so app still runs
        STOCKS_FULL_LIST[:] = [
            {"symbol": "RELIANCE.NS", "name": "Reliance"},
            {"symbol": "TCS.NS", "name": "TCS"},
            {"symbol": "HDFCBANK.NS", "name": "HDFC Bank"},
        ]
    # Optional watchlist in config = default selection in UI (no longer required)
    global default_selection, current_watchlist
    valid_symbols = {s["symbol"] for s in STOCKS_FULL_LIST}
    wl = CONFIG.get("watchlist")
    if isinstance(wl, list) and all(isinstance(x, str) for x in wl):
        default_selection = [s for s in wl[:MAX_SYMBOLS_PER_RUN] if s in valid_symbols]
    if not default_selection:
        default_selection = [s["symbol"] for s in STOCKS_FULL_LIST[:20]]
    if not current_watchlist:
        current_watchlist = list(default_selection)
    # Setup logging after we know we have config
    _setup_logging()
    logger.info("Config loaded from %s", path.resolve())


def _setup_logging() -> None:
    """Configure logging to stock_monitor.log (INFO/WARNING/ERROR)."""
    global logger
    log_path = Path(LOG_FILE)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    # Reduce console noise: only WARNING and above to console if you prefer file-only for INFO
    for h in logging.root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.INFO)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    # Reduce third-party log noise in our file
    logging.getLogger("yfinance").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
def _signal_handler(signum: int, frame: Any) -> None:
    """Handle Ctrl+C: set flag for graceful shutdown."""
    global shutdown_requested
    shutdown_requested = True
    if logger:
        logger.info("Shutdown requested (signal %s)", signum)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_stock_data(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch 1-day intraday (5m) OHLCV for one NSE symbol. Returns None if unavailable."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=PERIOD, interval=INTRADAY_INTERVAL, timeout=10)
        if df is None or df.empty or len(df) < 2:
            return None
        required = ["Open", "High", "Low", "Close", "Volume"]
        if not all(col in df.columns for col in required):
            return None
        return df
    except Exception as e:
        if logger:
            logger.error("Fetch failed for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# Trend analysis (volume + price = buying/selling pressure; risk level)
# ---------------------------------------------------------------------------
def _risk_level(volume_surge_ratio: float, pct_change: float, score: float) -> str:
    """
    Derive risk level from trend strength and volatility.
    High volume + large move = higher momentum but also reversal/volatility risk.
    """
    abs_pct = abs(pct_change)
    if score >= 6 or abs_pct > 5 or volume_surge_ratio > 2.5:
        return "High"   # Strong move or climax; higher volatility/reversal risk
    if score < 2 and abs_pct < 2:
        return "Low"    # Mild move, low participation
    return "Medium"     # Normal trend


def analyze_trend(data: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Analyze OHLCV for volume trend (participation) and price direction (buying vs selling).
    Suited to live market: large volume + price move often reflects news or crowd behaviour.
    Returns: current_price, volume_surge_ratio, pct_change, signal, score, risk_level.
    """
    if data is None or len(data) < 2:
        return None
    vol_th = CONFIG.get("volume_threshold", 1.5)
    price_th = CONFIG.get("price_threshold", 2.0)
    try:
        current = data.iloc[-1]
        current_price = float(current["Close"])
        current_volume = float(current["Volume"])
        first_open = float(data.iloc[0]["Open"])
        if first_open <= 0:
            return None
        pct_change = ((current_price - first_open) / first_open) * 100
        vol_slice = data["Volume"].iloc[-AVG_VOLUME_PERIODS - 1 : -1]
        if vol_slice.empty or vol_slice.sum() == 0:
            avg_volume = current_volume
        else:
            avg_volume = float(vol_slice.mean())
        volume_surge_ratio = (current_volume / avg_volume) if avg_volume > 0 else 1.0

        # Signal: volume surge + price direction = buying or selling pressure
        if volume_surge_ratio >= vol_th and pct_change > price_th:
            signal = "BUYING"
        elif volume_surge_ratio >= vol_th and pct_change < -price_th:
            signal = "SELLING"
        else:
            signal = "Neutral"

        score = round(volume_surge_ratio * abs(pct_change), 2)
        risk = _risk_level(volume_surge_ratio, pct_change, score)
        return {
            "current_price": current_price,
            "volume_surge_ratio": round(volume_surge_ratio, 2),
            "pct_change": round(pct_change, 2),
            "signal": signal,
            "score": score,
            "risk_level": risk,
        }
    except Exception as e:
        if logger:
            logger.error("Analyze trend failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CSV log (append daily trends)
# ---------------------------------------------------------------------------
def _append_trend_csv(row: Dict[str, Any]) -> None:
    """Append one row to trends.csv. Columns: timestamp, stock, price, volume_ratio, pct_change, signal, risk_level."""
    path = Path(CSV_FILE)
    file_exists = path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "stock", "price", "volume_ratio", "pct_change", "signal", "risk_level"])
        if not file_exists:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------
def _prune_old_email_times() -> None:
    global email_sent_times
    cutoff = time.time() - 3600
    email_sent_times = [t for t in email_sent_times if t > cutoff]


def _can_send_email() -> bool:
    _prune_old_email_times()
    return len(email_sent_times) < MAX_EMAILS_PER_HOUR


def _get_notification_emails() -> List[str]:
    """Load list of emails from notification_emails.json (users who added themselves for alerts)."""
    path = Path(NOTIFICATION_EMAILS_FILE)
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(e).strip().lower() for e in data if e and "@" in str(e)]
    except Exception:
        pass
    return []


def _save_notification_emails(emails: List[str]) -> None:
    """Save list of notification emails to file."""
    path = Path(NOTIFICATION_EMAILS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(emails), f, indent=2)


def _all_recipients() -> List[str]:
    """Config recipient + all notification emails (no duplicates)."""
    main = (CONFIG.get("recipient") or "").strip() or (CONFIG.get("email_id") or "").strip()
    extra = _get_notification_emails()
    seen = set()
    out = []
    if main and main not in seen:
        seen.add(main)
        out.append(main)
    for e in extra:
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def send_email(signal_data: Dict[str, Any]) -> bool:
    """Send alert via Gmail SMTP to all recipients (config + notification list)."""
    if MOCK_EMAIL_SEND:
        if logger:
            logger.warning("Mock email: %s %s", signal_data["signal"], signal_data["stock"])
        return True
    email_id = CONFIG.get("email_id") or ""
    email_pass = CONFIG.get("email_pass") or ""
    if not email_id or not email_pass:
        return False
    recipients = _all_recipients()
    if not recipients:
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = email_id
        msg["To"] = recipients[0]
        msg["Subject"] = "Stock Alert: {} in {} [{}]".format(
            signal_data["signal"], signal_data["stock"], signal_data.get("risk_level", "")
        )
        body = (
            "Stock: {stock}\n"
            "Signal: {signal}\n"
            "Risk level: {risk}\n"
            "Price: ₹{price}\n"
            "Volume Surge: {ratio}x\n"
            "Change: {pct}%\n"
            "Time: {now}"
        ).format(
            stock=signal_data["stock"],
            signal=signal_data["signal"],
            risk=signal_data.get("risk_level", "-"),
            price=signal_data["current_price"],
            ratio=signal_data["volume_surge_ratio"],
            pct=signal_data["pct_change"],
            now=signal_data["time_str"],
        )
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_id, email_pass)
            for to in recipients:
                server.sendmail(email_id, to, msg.as_string())
        email_sent_times.append(time.time())
        if logger:
            logger.warning("Email sent: %s %s to %d recipient(s)", signal_data["signal"], signal_data["stock"], len(recipients))
        return True
    except smtplib.SMTPAuthenticationError as e:
        if logger:
            logger.error("SMTP auth failed (check Gmail App Password): %s", e)
        return False
    except smtplib.SMTPException as e:
        if logger:
            logger.error("SMTP error: %s", e)
        return False
    except Exception as e:
        if logger:
            logger.error("Email error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Run one cycle: fetch, analyze, log, CSV, email (score > 3.0 and new BUYING/SELLING)
# ---------------------------------------------------------------------------
def run_one_cycle(symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Run full scan for selected symbols. Log to file, append CSV, send emails when score > 3.0
    and signal is new BUYING/SELLING. Returns list of result dicts for UI.
    Uses symbols if provided, else current_watchlist (from frontend selection).
    """
    global last_signals
    watchlist = symbols if symbols is not None else current_watchlist
    valid = {s["symbol"] for s in STOCKS_FULL_LIST}
    watchlist = [s for s in watchlist if s in valid][:MAX_SYMBOLS_PER_RUN]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results: List[Dict[str, Any]] = []

    if logger:
        logger.info("Starting trend check at %s", now_str)

    for symbol in watchlist:
        data = fetch_stock_data(symbol)
        if data is None:
            continue
        result = analyze_trend(data)
        if result is None:
            continue
        stock_name = symbol.replace(".NS", "")
        result["stock"] = stock_name
        result["symbol"] = symbol
        results.append(result)

        # CSV log every row
        _append_trend_csv({
            "timestamp": now_str,
            "stock": stock_name,
            "price": result["current_price"],
            "volume_ratio": result["volume_surge_ratio"],
            "pct_change": result["pct_change"],
            "signal": result["signal"],
            "risk_level": result.get("risk_level", ""),
        })

        signal_val = result["signal"]
        score = result.get("score", 0)
        prev = last_signals.get(symbol)
        last_signals[symbol] = signal_val

        # Alert only when score > 3.0 and new BUYING/SELLING
        if signal_val in ("BUYING", "SELLING") and signal_val != prev and score > ALERT_SCORE_THRESHOLD:
            if logger:
                logger.warning("Signal %s for %s (score %.2f)", signal_val, stock_name, score)
            if _can_send_email():
                signal_data = {
                    "stock": stock_name,
                    "signal": signal_val,
                    "risk_level": result.get("risk_level", "Medium"),
                    "current_price": result["current_price"],
                    "volume_surge_ratio": result["volume_surge_ratio"],
                    "pct_change": result["pct_change"],
                    "time_str": now_str,
                }
                if send_email(signal_data):
                    result["email_sent"] = True

    if logger:
        logger.info("Check complete: %d stocks", len(results))
    return results


def _serialize_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make result dicts JSON-serializable (no numpy types)."""
    out = []
    for r in results:
        row = {}
        for k, v in r.items():
            if isinstance(v, (np.floating, float)):
                row[k] = float(v)
            elif isinstance(v, (np.integer, int)) and not isinstance(v, bool):
                row[k] = int(v)
            else:
                row[k] = v
        out.append(row)
    return out


def _monitoring_loop() -> None:
    """Background loop: run cycle every check_interval_min, update last_results; exit when monitoring_active is False."""
    global last_results, last_run_time, monitoring_active
    interval_min = max(1, int(CONFIG.get("check_interval_min", 10)))
    interval_sec = interval_min * 60
    if logger:
        logger.info("Monitoring loop started (interval %s min)", interval_min)
    while True:
        with _monitoring_lock:
            if not monitoring_active:
                break
        try:
            results = run_one_cycle()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with _monitoring_lock:
                last_results = _serialize_results(results)
                last_run_time = now_str
        except Exception as e:
            if logger:
                logger.error("Monitoring cycle failed: %s", e)
        # Sleep in 1-sec chunks so we can stop promptly
        for _ in range(interval_sec):
            with _monitoring_lock:
                if not monitoring_active:
                    if logger:
                        logger.info("Monitoring loop stopped")
                    return
            time.sleep(1)
    if logger:
        logger.info("Monitoring loop stopped")


# ---------------------------------------------------------------------------
# Web UI (Flask): Start/Stop monitoring, latest results below
# ---------------------------------------------------------------------------
def create_app():
    """Create Flask app: Start/Stop monitoring, latest results below; optional one-off Run check."""
    try:
        from flask import Flask, jsonify, render_template_string, request
    except ImportError:
        raise ImportError("Install Flask: pip install flask")

    app = Flask(__name__)

    INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NSE Stock Monitor</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
    h1 { margin-bottom: 0.5rem; }
    .meta { color: #666; font-size: 0.9rem; margin-bottom: 1rem; }
    .buttons { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }
    button { padding: 0.6rem 1.2rem; font-size: 1rem; cursor: pointer; border: none; border-radius: 6px; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    #btnStart { background: #0a0; color: #fff; }
    #btnStart:hover:not(:disabled) { background: #080; }
    #btnStop { background: #c00; color: #fff; }
    #btnStop:hover:not(:disabled) { background: #a00; }
    #btnRun { background: #0d6efd; color: #fff; }
    #btnRun:hover:not(:disabled) { background: #0b5ed7; }
    .status { margin-bottom: 0.5rem; font-weight: 600; }
    .status.on { color: #0a0; }
    .status.off { color: #666; }
    #results { margin-top: 1rem; }
    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
    th, td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #ddd; }
    th { background: #f5f5f5; }
    .signal-BUYING { color: #0a0; }
    .signal-SELLING { color: #c00; }
    .risk-Low { color: #0a0; font-weight: 500; }
    .risk-Medium { color: #b8860b; font-weight: 500; }
    .risk-High { color: #c00; font-weight: 600; }
    .email-sent { font-size: 0.85rem; color: #06c; }
    .error { color: #c00; margin-top: 1rem; }
    .loading { color: #666; }
    .stocks-section { margin-bottom: 1rem; }
    .stocks-section h3 { margin: 0 0 0.5rem 0; font-size: 1rem; }
    .stocks-search-wrap { margin-bottom: 0.5rem; }
    .stocks-search-wrap input { width: 100%; max-width: 320px; padding: 0.5rem 0.75rem; font-size: 0.95rem; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }
    .stocks-search-wrap input:focus { outline: none; border-color: #0d6efd; }
    .stocks-toolbar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; flex-wrap: wrap; }
    .stocks-toolbar button { padding: 0.4rem 0.75rem; font-size: 0.9rem; cursor: pointer; border: 1px solid #ccc; border-radius: 4px; background: #fff; }
    .stocks-toolbar button:hover { background: #f0f0f0; }
    .stocks-toolbar span { font-size: 0.9rem; color: #666; }
    #stocksContainer { max-height: 280px; overflow-y: auto; border: 1px solid #ddd; border-radius: 6px; padding: 0.5rem; background: #fafafa; display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 0.35rem; }
    #stocksContainer label { display: flex; align-items: center; gap: 0.35rem; cursor: pointer; font-size: 0.9rem; white-space: nowrap; }
    #stocksContainer label.hidden-by-search { display: none; }
    #stocksContainer input[type=checkbox] { margin: 0; flex-shrink: 0; }
    .sym-muted { color: #999; font-size: 0.85em; }
    .stocks-empty-msg { color: #666; padding: 0.5rem; }
    .help-section { margin-bottom: 1rem; padding: 0.5rem; background: #f8f9fa; border-radius: 6px; border: 1px solid #eee; }
    .help-section summary { cursor: pointer; }
    .help-list { margin: 0.5rem 0 0 1.2rem; padding: 0; }
    .help-list li { margin-bottom: 0.35rem; }
    .notify-section { margin-bottom: 1rem; padding: 0.75rem; background: #f0f8ff; border-radius: 6px; border: 1px solid #cce5ff; }
    .notify-section h3 { margin: 0 0 0.5rem 0; font-size: 1rem; }
    .notify-row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.5rem; }
    .notify-row input[type=email] { padding: 0.4rem 0.6rem; width: 220px; max-width: 100%; border: 1px solid #ccc; border-radius: 4px; }
    .notify-list { margin-top: 0.5rem; }
    .notify-list span { display: inline-block; margin-right: 0.5rem; margin-bottom: 0.25rem; padding: 0.2rem 0.5rem; background: #fff; border-radius: 4px; font-size: 0.9rem; }
    .notify-list button { margin-left: 0.25rem; font-size: 0.85rem; cursor: pointer; color: #c00; background: none; border: none; }
    .notify-list button:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>NSE Stock Trend Monitor</h1>
  <p class="meta">Live market: signals follow <strong>volume trend</strong> (large participation = many people buying/selling) and <strong>price direction</strong>. News-driven moves show as high volume + sharp price change. Risk level: Low = mild move, Medium = normal trend, High = strong momentum / higher volatility.</p>
  <details class="help-section">
    <summary><strong>What do Vol ratio, % Chg and Score mean? How are BUYING/SELLING signals decided?</strong></summary>
    <ul class="help-list">
      <li><strong>Vol ratio</strong>: Current 5‑minute volume ÷ average volume over the last 10 periods. &gt; 1 means more trading than usual (participation surge).</li>
      <li><strong>% Chg</strong>: (Current price − session open price) ÷ session open × 100. Positive = up from open, negative = down from open.</li>
      <li><strong>Score</strong>: Vol ratio × |% Chg|. Combines volume surge and size of price move. Email alerts are sent only when Score &gt; 3.0 and the signal is BUYING or SELLING.</li>
      <li><strong>BUYING</strong>: Vol ratio ≥ threshold (default 1.5) <em>and</em> % Chg &gt; price threshold (default +2%).</li>
      <li><strong>SELLING</strong>: Vol ratio ≥ threshold <em>and</em> % Chg &lt; −2%.</li>
      <li><strong>Neutral</strong>: Otherwise (e.g. low volume or small price change).</li>
    </ul>
  </details>
  <div class="stocks-section">
    <h3>Stocks to monitor (select one or more)</h3>
    <div class="stocks-search-wrap">
      <input type="text" id="stockSearch" placeholder="Search by name or symbol…" autocomplete="off">
    </div>
    <div class="stocks-toolbar">
      <button type="button" id="btnSelectAll">Select all</button>
      <button type="button" id="btnDeselectAll">Deselect all</button>
      <button type="button" id="btnSelectVisible">Select visible</button>
      <button type="button" id="btnDeselectVisible">Deselect visible</button>
      <span id="selectedCount">0 selected</span>
    </div>
    <div id="stocksContainer"><p class="loading">Loading stocks…</p></div>
  </div>
  <div class="buttons">
    <button id="btnStart">Start monitoring</button>
    <button id="btnStop">Stop monitoring</button>
    <button id="btnRun">Run check now</button>
  </div>
  <div class="notify-section">
    <h3>Get notification signals by email</h3>
    <p class="meta" style="margin-bottom:0.5rem;">Enter your email to receive BUYING/SELLING alerts (when Score &gt; 3.0). Multiple users can add their emails.</p>
    <div class="notify-row">
      <input type="email" id="notifyEmail" placeholder="your@email.com">
      <button type="button" id="btnAddEmail">Add email</button>
    </div>
    <div class="notify-list" id="notifyList"></div>
  </div>
  <p class="status off" id="status">Monitoring: OFF</p>
  <p class="meta" id="lastRun"></p>
  <div id="results"></div>
  <script>
    var pollTimer = null;
    var allStocks = [];
    function escapeHtml(s) {
      if (!s) return '';
      var d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }
    function getSelectedSymbols() {
      var out = [];
      document.querySelectorAll('#stocksContainer input[type=checkbox]:checked').forEach(function(cb) { out.push(cb.value); });
      return out;
    }
    function updateCount() {
      var n = getSelectedSymbols().length;
      document.getElementById('selectedCount').textContent = n + ' selected';
    }
    function getVisibleCheckboxes() {
      return document.querySelectorAll('#stocksContainer label:not(.hidden-by-search) input[type=checkbox]');
    }
    function applySearchFilter() {
      var q = (document.getElementById('stockSearch').value || '').trim().toLowerCase();
      document.querySelectorAll('#stocksContainer label[data-search]').forEach(function(label) {
        label.classList.toggle('hidden-by-search', q && label.getAttribute('data-search').toLowerCase().indexOf(q) === -1);
      });
    }
    function renderStocks(data) {
      if (!data || !data.stocks || !data.stocks.length) return;
      allStocks = data.stocks;
      var defaultSel = (data.selected_symbols && data.selected_symbols.length) ? data.selected_symbols : (data.default_selection || []);
      var set = new Set(defaultSel);
      var html = '';
      data.stocks.forEach(function(s) {
        var sym = escapeHtml(s.symbol);
        var name = escapeHtml(s.name || s.symbol);
        var searchText = (s.symbol + ' ' + (s.name || '')).toLowerCase();
        var checked = set.has(s.symbol) ? ' checked' : '';
        html += '<label data-search="' + escapeHtml(searchText) + '"><input type="checkbox" value="' + sym + '"' + checked + '> ' + name + ' <span class="sym-muted">(' + sym.replace(/\\.NS$/, '') + ')</span></label>';
      });
      document.getElementById('stocksContainer').innerHTML = html;
      document.querySelectorAll('#stocksContainer input').forEach(function(cb) { cb.addEventListener('change', updateCount); });
      document.getElementById('stockSearch').addEventListener('input', applySearchFilter);
      document.getElementById('stockSearch').addEventListener('keyup', function(e) { if (e.key === 'Escape') { this.value = ''; applySearchFilter(); } });
      updateCount();
    }
    function renderResults(data) {
      var statusEl = document.getElementById('status');
      var lastRunEl = document.getElementById('lastRun');
      var out = document.getElementById('results');
      if (data.monitoring) {
        var min = (data.interval_min != null) ? data.interval_min : 10;
        statusEl.textContent = 'Monitoring: ON (checks every ' + min + ' min)';
        statusEl.className = 'status on';
      } else {
        statusEl.textContent = 'Monitoring: OFF';
        statusEl.className = 'status off';
      }
      if (data.last_run_time) lastRunEl.textContent = 'Last run: ' + data.last_run_time;
      else lastRunEl.textContent = '';
      if (!data.results || data.results.length === 0) {
        out.innerHTML = '<p class="meta">No results yet. Start monitoring or run a check.</p>';
        return;
      }
      var html = '<table><thead><tr><th>Stock</th><th>Price</th><th>Vol ratio</th><th>% Chg</th><th>Score</th><th>Signal</th><th>Risk</th><th>Email</th></tr></thead><tbody>';
      data.results.forEach(function(row) {
        var signalClass = 'signal-' + (row.signal || '');
        var risk = row.risk_level || '-';
        var riskClass = 'risk-' + risk;
        var emailText = row.email_sent ? '<span class="email-sent">Sent</span>' : '';
        html += '<tr><td>' + (row.stock || '') + '</td><td>₹' + Number(row.current_price).toFixed(2) + '</td><td>' + row.volume_surge_ratio + 'x</td><td>' + (row.pct_change >= 0 ? '+' : '') + row.pct_change + '%</td><td>' + (row.score != null ? row.score : '-') + '</td><td class="' + signalClass + '">' + (row.signal || '') + '</td><td class="' + riskClass + '">' + risk + '</td><td>' + emailText + '</td></tr>';
      });
      html += '</tbody></table>';
      out.innerHTML = html;
    }
    function poll() {
      fetch('/results').then(function(r) { return r.json(); }).then(renderResults).catch(function() {});
    }
    function startPolling() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(poll, 60000);
    }
    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
    fetch('/stocks').then(function(r) { return r.json(); }).then(renderStocks).catch(function() { document.getElementById('stocksContainer').innerHTML = '<p class="error">Failed to load stocks.</p>'; });
    poll();
    function loadNotificationEmails() {
      fetch('/notification-emails').then(function(r) { return r.json(); }).then(function(data) {
        var list = document.getElementById('notifyList');
        var emails = data.emails || [];
        if (emails.length === 0) { list.innerHTML = '<span class="meta">No emails added yet.</span>'; return; }
        list.innerHTML = emails.map(function(e) {
          return '<span>' + escapeHtml(e) + ' <button type="button" data-email="' + escapeHtml(e) + '">Remove</button></span>';
        }).join('');
        list.querySelectorAll('button').forEach(function(btn) {
          btn.onclick = function() { removeNotifyEmail(btn.getAttribute('data-email')); };
        });
      }).catch(function() { document.getElementById('notifyList').innerHTML = ''; });
    }
    function addNotifyEmail(email) {
      if (!email || email.indexOf('@') === -1) { alert('Enter a valid email address.'); return; }
      fetch('/notification-emails', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.trim() }) })
        .then(function(r) { return r.json(); })
        .then(function(d) { if (d.added !== false) { document.getElementById('notifyEmail').value = ''; loadNotificationEmails(); } else { alert(d.error || 'Could not add.'); } })
        .catch(function() { alert('Request failed.'); });
    }
    function removeNotifyEmail(email) {
      fetch('/notification-emails', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ remove: email }) })
        .then(function(r) { return r.json(); })
        .then(function() { loadNotificationEmails(); })
        .catch(function() {});
    }
    loadNotificationEmails();
    document.getElementById('btnAddEmail').onclick = function() { addNotifyEmail(document.getElementById('notifyEmail').value); };
    document.getElementById('notifyEmail').onkeydown = function(e) { if (e.key === 'Enter') { e.preventDefault(); addNotifyEmail(this.value); } };
    document.getElementById('btnSelectAll').onclick = function() {
      document.querySelectorAll('#stocksContainer input[type=checkbox]').forEach(function(cb) { cb.checked = true; });
      updateCount();
    };
    document.getElementById('btnDeselectAll').onclick = function() {
      document.querySelectorAll('#stocksContainer input[type=checkbox]').forEach(function(cb) { cb.checked = false; });
      updateCount();
    };
    document.getElementById('btnSelectVisible').onclick = function() {
      getVisibleCheckboxes().forEach(function(cb) { cb.checked = true; });
      updateCount();
    };
    document.getElementById('btnDeselectVisible').onclick = function() {
      getVisibleCheckboxes().forEach(function(cb) { cb.checked = false; });
      updateCount();
    };
    document.getElementById('btnStart').onclick = async function() {
      var symbols = getSelectedSymbols();
      if (symbols.length === 0) { alert('Select at least one stock to monitor.'); return; }
      var btn = this;
      btn.disabled = true;
      try {
        var r = await fetch('/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbols: symbols }) });
        var d = await r.json();
        if (r.ok && d.started) { poll(); startPolling(); }
      } finally { btn.disabled = false; }
    };
    document.getElementById('btnStop').onclick = async function() {
      var btn = this;
      btn.disabled = true;
      try {
        await fetch('/stop', { method: 'POST' });
        stopPolling();
        poll();
      } finally { btn.disabled = false; }
    };
    document.getElementById('btnRun').onclick = async function() {
      var symbols = getSelectedSymbols();
      if (symbols.length === 0) { alert('Select at least one stock to run a check.'); return; }
      var btn = this;
      var out = document.getElementById('results');
      btn.disabled = true;
      out.innerHTML = '<p class="loading">Running check…</p>';
      try {
        var r = await fetch('/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbols: symbols }) });
        var data = await r.json();
        if (!r.ok) { out.innerHTML = '<p class="error">' + (data.error || r.status) + '</p>'; return; }
        renderResults({ monitoring: false, last_run_time: data.timestamp, results: data.results || [] });
      } catch (e) { out.innerHTML = '<p class="error">' + e.message + '</p>'; }
      btn.disabled = false;
    };
  </script>
</body>
</html>
"""

    @app.route("/")
    def index():
        return render_template_string(INDEX_HTML)

    @app.route("/stocks")
    def stocks():
        """Return full stock list and default/current selection for the multi-select UI."""
        with _monitoring_lock:
            return jsonify({
                "stocks": list(STOCKS_FULL_LIST),
                "default_selection": list(default_selection),
                "selected_symbols": list(current_watchlist),
            })

    @app.route("/notification-emails", methods=["GET", "POST"])
    def notification_emails():
        """GET: return list of emails. POST: body {"email": "x@y.com"} to add, {"remove": "x@y.com"} to remove."""
        if request.method == "GET":
            return jsonify({"emails": _get_notification_emails()})
        payload = request.get_json(silent=True) or {}
        remove = payload.get("remove")
        if remove is not None:
            email = str(remove).strip().lower()
            current = _get_notification_emails()
            current = [e for e in current if e != email]
            _save_notification_emails(current)
            return jsonify({"removed": True, "emails": current})
        email = (payload.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return jsonify({"added": False, "error": "Enter a valid email address"}), 400
        current = _get_notification_emails()
        if email in current:
            return jsonify({"added": True, "emails": current})
        current.append(email)
        _save_notification_emails(current)
        return jsonify({"added": True, "emails": current})

    @app.route("/results")
    def results():
        """Return current monitoring status, selected symbols, and latest results."""
        with _monitoring_lock:
            interval = max(1, int(CONFIG.get("check_interval_min", 10)))
            return jsonify({
                "monitoring": monitoring_active,
                "interval_min": interval,
                "last_run_time": last_run_time,
                "selected_symbols": list(current_watchlist),
                "results": list(last_results),
            })

    @app.route("/start", methods=["POST"])
    def start_monitoring():
        """Start continuous monitoring; body may include {"symbols": ["RELIANCE.NS", ...]}."""
        global monitoring_active, monitoring_thread, current_watchlist
        payload = request.get_json(silent=True) or {}
        symbols = payload.get("symbols")
        valid = {s["symbol"] for s in STOCKS_FULL_LIST}
        if isinstance(symbols, list) and symbols:
            current_watchlist = [s for s in symbols if s in valid][:MAX_SYMBOLS_PER_RUN]
        with _monitoring_lock:
            if monitoring_active:
                return jsonify({"started": True, "message": "Already running"})
            monitoring_active = True
            monitoring_thread = threading.Thread(target=_monitoring_loop, daemon=True)
            monitoring_thread.start()
        return jsonify({"started": True})

    @app.route("/stop", methods=["POST"])
    def stop_monitoring():
        """Stop continuous monitoring."""
        global monitoring_active
        with _monitoring_lock:
            monitoring_active = False
        return jsonify({"stopped": True})

    @app.route("/run", methods=["GET", "POST"])
    def run():
        """One-off run; body may include {"symbols": ["RELIANCE.NS", ...]}."""
        global last_results, last_run_time, current_watchlist
        payload = request.get_json(silent=True) or {}
        symbols = payload.get("symbols")
        valid = {s["symbol"] for s in STOCKS_FULL_LIST}
        if isinstance(symbols, list) and symbols:
            current_watchlist = [s for s in symbols if s in valid][:MAX_SYMBOLS_PER_RUN]
        try:
            results = run_one_cycle()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            out = _serialize_results(results)
            # Update last_results so /results and page show this run too
            with _monitoring_lock:
                last_results.clear()
                last_results.extend(out)
                last_run_time = now_str
            return jsonify({"timestamp": now_str, "results": out})
        except Exception as e:
            if logger:
                logger.error("Run cycle failed: %s", e)
            return jsonify({"error": str(e)}), 500

    return app


# ---------------------------------------------------------------------------
# CLI entry: load_config() then main()
# ---------------------------------------------------------------------------
def main() -> None:
    """Load config, register signal handler, start web server."""
    signal.signal(signal.SIGINT, _signal_handler)
    # Optional: run from same dir as config
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)

    load_config()
    app = create_app()
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    if logger:
        logger.info("Starting web UI at http://%s:%s", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Tests (optional, for dev)
# ---------------------------------------------------------------------------
def test_reliance() -> None:
    """Single-stock test (RELIANCE.NS). Requires load_config() to have been called first."""
    symbol = "RELIANCE.NS"
    print("Test:", symbol)
    data = fetch_stock_data(symbol)
    if data is None:
        print("  No data")
        return
    result = analyze_trend(data)
    if result:
        print("  ", result)
    else:
        print("  Analyze failed")


if __name__ == "__main__":
    import sys
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        load_config(script_dir / CONFIG_PATH)
        test_reliance()
    else:
        main()
