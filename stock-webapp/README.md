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

## Automatic runs
The stock scanner runs every 30 minutes on weekdays and also whenever the web-app files are updated. Manual runs remain available from GitHub Actions.

## Dashboard hosting
GitHub Pages serves the repository and `/stock-webapp/` is the scanner dashboard path.

## Future additions
Alerts, Telegram integration, custom strategy rules, NIFTY universe, forex/gold/BTC, intraday timeframes, backtesting, saved scans, custom watchlists.
