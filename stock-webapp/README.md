# Rupak Stock Scanner Web App

Browser-only stock scanner. Nothing needs to be installed on the user's PC.

## Current features
- NSE stock watchlist from `stock_symbols.txt`
- Cloud scan with GitHub Actions every 30 minutes on weekdays
- Price, daily change, SMA20, SMA50, RSI14, volume ratio
- 20-day breakout/support zone tagging
- Bullish/Bearish/Mixed trend classification
- Signal scoring and browser filters
- Modular files so more rules/markets can be added later

## Run manually
Open GitHub > Actions > Stock Web App Scan > Run workflow.

## Dashboard hosting
Enable GitHub Pages for the repository and use `/stock-webapp/` as the app path if serving from the repository root via Pages. If Pages is configured to deploy from `/docs`, copy this folder there instead.

## Future additions
Alerts, Telegram integration, custom strategy rules, NIFTY universe, forex/gold/BTC, intraday timeframes, backtesting, saved scans, custom watchlists.
