const originalExpress = require('express');

const mt5Stores = new Map();
const MAX_TRADES_PER_KEY = 5000;

function validKey(key) {
  return /^[A-Za-z0-9_-]{16,128}$/.test(String(key || ''));
}

function normalizeTrade(x = {}) {
  const sourceId = String(x.source_id || x.sourceId || x.deal || '').slice(0, 120);
  if (!sourceId) return null;
  const closeTime = Number(x.close_time || x.closeTime || 0);
  return {
    sourceId,
    account: String(x.account || '').slice(0, 40),
    symbol: String(x.symbol || '').slice(0, 40),
    side: String(x.side || '').toLowerCase() === 'sell' ? 'Sell' : 'Buy',
    entry: Number(x.entry || 0),
    sl: Number(x.sl || 0),
    target: Number(x.target || x.tp || 0),
    exit: Number(x.exit || 0),
    qty: Number(x.qty || x.volume || 0),
    pnl: Number(x.pnl || 0),
    r: Number(x.r || 0),
    result: Number(x.pnl || 0) > 0 ? 'Win' : Number(x.pnl || 0) < 0 ? 'Loss' : 'BE',
    closeTime,
    date: closeTime > 0 ? new Date(closeTime * 1000).toISOString().slice(0, 10) : '',
    setup: String(x.setup || '').slice(0, 120),
    tf: String(x.tf || '').slice(0, 30),
    notes: String(x.notes || x.comment || '').slice(0, 500),
    commission: Number(x.commission || 0),
    swap: Number(x.swap || 0),
    positionId: String(x.position_id || x.positionId || '').slice(0, 80),
    deal: String(x.deal || '').slice(0, 80),
    syncedAt: Date.now()
  };
}

function installMt5Routes(app) {
  app.get('/mt5/journal/:key/status', (req, res) => {
    const key = String(req.params.key || '');
    if (!validKey(key)) return res.status(400).json({ ok: false, error: 'invalid_sync_key' });
    const store = mt5Stores.get(key);
    res.json({ ok: true, trades: store ? store.size : 0, memory_only: true });
  });

  app.get('/mt5/journal/:key', (req, res) => {
    const key = String(req.params.key || '');
    if (!validKey(key)) return res.status(400).json({ ok: false, error: 'invalid_sync_key' });
    const store = mt5Stores.get(key) || new Map();
    const trades = [...store.values()].sort((a, b) => (b.closeTime || 0) - (a.closeTime || 0));
    res.json({ ok: true, trades, count: trades.length, memory_only: true });
  });

  app.post('/mt5/journal/:key', (req, res) => {
    const key = String(req.params.key || '');
    if (!validKey(key)) return res.status(400).json({ ok: false, error: 'invalid_sync_key' });
    const input = Array.isArray(req.body?.trades) ? req.body.trades : (Array.isArray(req.body) ? req.body : [req.body]);
    if (input.length > 1000) return res.status(413).json({ ok: false, error: 'too_many_trades' });
    let store = mt5Stores.get(key);
    if (!store) { store = new Map(); mt5Stores.set(key, store); }
    let accepted = 0;
    for (const raw of input) {
      const t = normalizeTrade(raw);
      if (!t || !t.symbol) continue;
      store.set(t.sourceId, t);
      accepted++;
    }
    if (store.size > MAX_TRADES_PER_KEY) {
      const ordered = [...store.values()].sort((a, b) => (b.closeTime || 0) - (a.closeTime || 0)).slice(0, MAX_TRADES_PER_KEY);
      store = new Map(ordered.map(t => [t.sourceId, t]));
      mt5Stores.set(key, store);
    }
    res.json({ ok: true, accepted, count: store.size });
  });
}

function wrappedExpress(...args) {
  const app = originalExpress(...args);
  const realListen = app.listen.bind(app);
  app.listen = (...listenArgs) => {
    installMt5Routes(app);
    return realListen(...listenArgs);
  };
  return app;
}
Object.assign(wrappedExpress, originalExpress);
require.cache[require.resolve('express')].exports = wrappedExpress;
require('./server');
