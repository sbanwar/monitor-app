# NSE Stock Monitor – Setup Instructions

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires: `yfinance`, `pandas`, `numpy`, `flask`.

## 2. Configuration (`config.json`)

Create or edit `config.json` in the same folder as the script with:

| Key | Description |
|-----|-------------|
| `stocks_list_file` | **Optional.** Path to a JSON file listing all stocks (e.g. `stocks_list.json`). File must contain an array of `{"symbol": "RELIANCE.NS", "name": "Reliance Industries"}`. Default: `stocks_list.json` in the same folder as config. Top 200 NSE stocks are provided in `stocks_list.json`. |
| `watchlist` | **Optional.** List of NSE symbols used only as default selection in the UI. Stocks to monitor are chosen on the web page (search + multi-select). |
| `email_id` | Your Gmail address |
| `email_pass` | Gmail **App Password** (see below) |
| `recipient` | Email where alerts are sent (leave empty to use `email_id`) |
| `volume_threshold` | Volume surge multiplier for signal (default `1.5`) |
| `price_threshold` | Price change % for BUYING/SELLING (default `2.0`) |
| `check_interval_min` | Not used by web UI; kept for reference (minutes) |

All keys except `watchlist` must be present. Empty strings for `email_id` / `email_pass` are valid (alerts will be skipped).

## 3. Gmail App Password (for email alerts)

Gmail no longer allows “less secure apps”. You must use an **App Password**:

1. Go to [Google Account](https://myaccount.google.com/) → **Security**.
2. Turn on **2-Step Verification** if it is off.
3. Under “How you sign in to Google”, open **App passwords**.
4. Select app: **Mail**, device: **Other** (e.g. “Stock Monitor”), then **Generate**.
5. Copy the 16-character password (no spaces).
6. Put it in `config.json` as `"email_pass": "xxxx xxxx xxxx xxxx"` (you can use spaces or not).

Never commit this password to version control. Keep `config.json` in `.gitignore` if the repo is shared.

## 4. Run the app

From the project directory:

```bash
python3 indian_stock_monitor.py
```

- Opens the **web UI** at `http://127.0.0.1:5000`.
- **Start monitoring**: runs continuously. Every **10 minutes** (or `check_interval_min` from config) it fetches live data, runs trend analysis, sends email alerts (when score > 3.0), and updates the **same results** shown on the page below the buttons.
- **Stop monitoring**: stops the loop. You can turn it off manually at any time.
- **Run check now**: run a single check once (does not start or stop continuous monitoring). Results appear below and are also used for “Latest run”.
- Logs go to `stock_monitor.log`; daily trend rows are appended to `trends.csv`.

Optional:

- `FLASK_HOST=0.0.0.0 FLASK_PORT=8080 python3 indian_stock_monitor.py` to listen on all interfaces and port 8080.
- `python3 indian_stock_monitor.py --test` to run a single-stock test (RELIANCE.NS) after loading config.

## 5. Parameters: Vol ratio, % Chg, Score and signals

- **Vol ratio**: Current 5‑minute volume ÷ average volume over the last 10 periods. &gt; 1 = more trading than usual (participation surge).
- **% Chg**: (Current price − session open) ÷ session open × 100. Positive = up from open, negative = down.
- **Score**: Vol ratio × |% Chg|. Combines volume surge and price move size. **Email alerts are sent only when Score &gt; 3.0** and the signal is BUYING or SELLING.
- **BUYING**: Vol ratio ≥ threshold (e.g. 1.5) and % Chg &gt; price threshold (e.g. +2%).
- **SELLING**: Vol ratio ≥ threshold and % Chg &lt; −2%.
- **Neutral**: Otherwise (e.g. low volume or small price change).

## 6. Multiple users: add your email for notifications

On the web page, use **“Get notification signals by email”**: enter an email and click **Add email**. Multiple people can add their addresses; alerts (when Score &gt; 3.0 and BUYING/SELLING) are sent to the config **recipient** plus all added emails. The list is stored in `notification_emails.json` in the project folder. Each user can **Remove** their email from the list.

## 7. How the logic works (live market)

The script is meant for **live market hours**. It follows:

- **Volume trend**: Current volume vs recent average → high ratio means more people trading (buying or selling).
- **Price direction**: Session % change → positive = buying pressure, negative = selling pressure.
- **News-style moves** usually show up as both high volume and a sharp price move; the **score** (volume_ratio × |price_change%|) measures that strength.

**Risk level** (shown per stock and in emails):

- **Low**: Mild move, low participation (score &lt; 2, small % change).
- **Medium**: Normal trend (moderate score and volume).
- **High**: Strong momentum or climax (score ≥ 6, or large % move, or very high volume) — higher volatility/reversal risk.

## 8. Alert rule (trending score)

An **email alert** is sent only when:

- Signal is **BUYING** or **SELLING**, and  
- **Trending score** > 3.0, where  
  `score = volume_ratio × |price_change%|`  
- And the signal is **new** (different from the previous one for that stock).

So both volume/price conditions and the score threshold must be satisfied.

## 9. Files produced

| File | Purpose |
|------|---------|
| `stock_monitor.log` | Application log (INFO checks, WARNING signals, ERROR failures) |
| `trends.csv` | One row per stock per run: timestamp, stock, price, volume_ratio, pct_change, signal, risk_level |
| `notification_emails.json` | Emails added on the web page to receive alerts (optional) |

## 10. Graceful shutdown

Press **Ctrl+C** in the terminal to stop the server. The script uses a signal handler for a clean exit.
