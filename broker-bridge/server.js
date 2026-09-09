const express = require('express');
const cors = require('cors');
const crypto = require('crypto');
const UpstoxClient = require('upstox-js-sdk');

const app = express();
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(cors({ origin: true, credentials: false }));

const PORT = process.env.PORT || 3000;
const FRONTEND_URL = process.env.FRONTEND_URL || '';
const ENABLE_TRADING = String(process.env.ENABLE_TRADING || 'false').toLowerCase() === 'true';

const cfg = {
  upstox: {
    key: process.env.UPSTOX_CLIENT_ID || '',
    secret: process.env.UPSTOX_CLIENT_SECRET || '',
    redirect: process.env.UPSTOX_REDIRECT_URI || '',
  },
  zerodha: {
    key: process.env.ZERODHA_API_KEY || '',
    secret: process.env.ZERODHA_API_SECRET || '',
    redirect: process.env.ZERODHA_REDIRECT_URI || '',
  },
  angel: {
    key: process.env.ANGEL_API_KEY || '',
    redirect: process.env.ANGEL_REDIRECT_URI || '',
    clientCode: process.env.ANGEL_CLIENT_CODE || '',
    publicIp: process.env.ANGEL_PUBLIC_IP || '127.0.0.1',
    localIp: process.env.ANGEL_LOCAL_IP || '127.0.0.1',
    mac: process.env.ANGEL_MAC || '00:00:00:00:00:00',
  },
  dhan: {
    clientId: process.env.DHAN_CLIENT_ID || '',
    key: process.env.DHAN_API_KEY || '',
    secret: process.env.DHAN_API_SECRET || '',
    redirect: process.env.DHAN_REDIRECT_URI || '',
  },
};

const sessions = {
  upstox: { accessToken: process.env.UPSTOX_ACCESS_TOKEN || '', obtainedAt: null, profile: null },
  zerodha: { accessToken: process.env.ZERODHA_ACCESS_TOKEN || '', obtainedAt: null, profile: null },
  angel: { accessToken: process.env.ANGEL_ACCESS_TOKEN || '', feedToken: process.env.ANGEL_FEED_TOKEN || '', obtainedAt: null, profile: null },
  dhan: { accessToken: process.env.DHAN_ACCESS_TOKEN || '', obtainedAt: null, profile: null },
};

const oauthStates = new Map();
const sseClients = new Set();
let streamer = null;
let activeKeys = new Set();
let activeMode = 'ltpc';
let liveBroker = 'upstox';

function providerInfo(name) {
  const connected = !!sessions[name]?.accessToken;
  const configured = name === 'upstox' ? !!(cfg.upstox.key && cfg.upstox.secret && cfg.upstox.redirect)
    : name === 'zerodha' ? !!(cfg.zerodha.key && cfg.zerodha.secret)
    : name === 'angel' ? !!cfg.angel.key
    : !!(cfg.dhan.clientId && cfg.dhan.key && cfg.dhan.secret);
  return {
    id: name,
    name: name === 'upstox' ? 'Upstox' : name === 'zerodha' ? 'Zerodha Kite' : name === 'angel' ? 'Angel One' : 'Dhan',
    configured,
    connected,
    realtime: name === 'upstox' ? !!streamer : false,
    account: true,
    trading: ENABLE_TRADING,
  };
}

function saveState(broker) {
  const state = crypto.randomBytes(18).toString('hex');
  oauthStates.set(state, { broker, expires: Date.now() + 10 * 60 * 1000 });
  return state;
}
function validState(state, broker) {
  const x = oauthStates.get(String(state || ''));
  if (!x || x.broker !== broker || x.expires < Date.now()) return false;
  oauthStates.delete(String(state));
  return true;
}
function frontendRedirect(broker, connected = true, error = '') {
  const target = FRONTEND_URL || '/status';
  const q = new URLSearchParams({ broker, connected: connected ? '1' : '0' });
  if (error) q.set('error', error);
  return `${target}${target.includes('?') ? '&' : '?'}${q.toString()}`;
}
function sendSse(payload) {
  const msg = `data: ${JSON.stringify(payload)}\n\n`;
  for (const res of sseClients) {
    try { res.write(msg); } catch (_) { sseClients.delete(res); }
  }
}
async function jsonFetch(url, opts = {}) {
  const r = await fetch(url, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch (_) { data = { raw: text }; }
  if (!r.ok) {
    const err = new Error(data?.message || data?.error || `HTTP ${r.status}`);
    err.status = r.status; err.payload = data; throw err;
  }
  return data;
}
function authHeaders(broker) {
  const token = sessions[broker]?.accessToken;
  if (!token) throw Object.assign(new Error('broker_not_connected'), { status: 401 });
  if (broker === 'upstox') return { Accept: 'application/json', Authorization: `Bearer ${token}` };
  if (broker === 'zerodha') return { Accept: 'application/json', 'X-Kite-Version': '3', Authorization: `token ${cfg.zerodha.key}:${token}` };
  if (broker === 'dhan') return { Accept: 'application/json', 'Content-Type': 'application/json', 'access-token': token };
  return {
    Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}`,
    'X-UserType': 'USER', 'X-SourceID': 'WEB', 'X-ClientLocalIP': cfg.angel.localIp,
    'X-ClientPublicIP': cfg.angel.publicIp, 'X-MACAddress': cfg.angel.mac, 'X-PrivateKey': cfg.angel.key,
  };
}
function requireTrading(res) {
  if (ENABLE_TRADING) return true;
  res.status(403).json({ ok: false, error: 'trading_disabled', message: 'Set ENABLE_TRADING=true on the private bridge only after you are ready to place real orders.' });
  return false;
}
function resetStreamer() {
  if (streamer) { try { streamer.disconnect(); } catch (_) {} streamer = null; }
}
function ensureUpstoxStreamer() {
  if (!sessions.upstox.accessToken || streamer || !activeKeys.size) return;
  const defaultClient = UpstoxClient.ApiClient.instance;
  defaultClient.authentications['OAUTH2'].accessToken = sessions.upstox.accessToken;
  streamer = new UpstoxClient.MarketDataStreamerV3([...activeKeys], activeMode);
  streamer.autoReconnect(true, 5, 50);
  liveBroker = 'upstox';
  streamer.on('open', () => sendSse({ type: 'bridge_status', broker: 'upstox', status: 'live_connected', keys: [...activeKeys], mode: activeMode, ts: Date.now() }));
  streamer.on('message', (data) => {
    let parsed;
    try { parsed = JSON.parse(Buffer.isBuffer(data) ? data.toString('utf-8') : String(data)); }
    catch (_) { parsed = { raw: Buffer.isBuffer(data) ? data.toString('base64') : String(data) }; }
    sendSse({ type: 'market_data', broker: 'upstox', payload: parsed, ts: Date.now() });
  });
  streamer.on('error', e => sendSse({ type: 'bridge_error', broker: 'upstox', error: String(e?.message || e), ts: Date.now() }));
  streamer.on('close', () => { streamer = null; sendSse({ type: 'bridge_status', broker: 'upstox', status: 'disconnected', ts: Date.now() }); });
  streamer.on('reconnecting', m => sendSse({ type: 'bridge_status', broker: 'upstox', status: 'reconnecting', detail: String(m || ''), ts: Date.now() }));
  streamer.connect();
}

app.get('/health', (_req, res) => res.json({ ok: true, trading_enabled: ENABLE_TRADING, brokers: Object.keys(sessions).map(providerInfo) }));
app.get('/brokers', (_req, res) => res.json({ ok: true, trading_enabled: ENABLE_TRADING, brokers: Object.keys(sessions).map(providerInfo) }));
app.get('/status', (_req, res) => res.json({
  connected_brokers: Object.keys(sessions).filter(x => sessions[x].accessToken),
  brokers: Object.fromEntries(Object.keys(sessions).map(x => [x, providerInfo(x)])),
  trading_enabled: ENABLE_TRADING,
  live_broker: liveBroker,
  live_stream: !!streamer,
  mode: activeMode,
  subscriptions: [...activeKeys],
}));

app.get('/auth/upstox', (_req, res) => {
  if (!cfg.upstox.key || !cfg.upstox.redirect) return res.status(500).send('Upstox app not configured');
  const state = saveState('upstox');
  const q = new URLSearchParams({ response_type: 'code', client_id: cfg.upstox.key, redirect_uri: cfg.upstox.redirect, state });
  res.redirect(`https://api.upstox.com/v2/login/authorization/dialog?${q}`);
});
app.get('/auth/upstox/callback', async (req, res) => {
  if (!validState(req.query.state, 'upstox') || !req.query.code) return res.status(400).send('Invalid or expired OAuth state');
  try {
    const body = new URLSearchParams({ code: String(req.query.code), client_id: cfg.upstox.key, client_secret: cfg.upstox.secret, redirect_uri: cfg.upstox.redirect, grant_type: 'authorization_code' });
    const j = await jsonFetch('https://api.upstox.com/v2/login/authorization/token', { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' }, body });
    sessions.upstox.accessToken = j.access_token; sessions.upstox.obtainedAt = new Date().toISOString();
    resetStreamer(); ensureUpstoxStreamer(); res.redirect(frontendRedirect('upstox'));
  } catch (e) { res.redirect(frontendRedirect('upstox', false, e.message)); }
});

app.get('/auth/zerodha', (_req, res) => {
  if (!cfg.zerodha.key) return res.status(500).send('Zerodha app not configured');
  const state = saveState('zerodha');
  const q = new URLSearchParams({ v: '3', api_key: cfg.zerodha.key, state });
  res.redirect(`https://kite.zerodha.com/connect/login?${q}`);
});
app.get('/auth/zerodha/callback', async (req, res) => {
  const stateOk = req.query.state ? validState(req.query.state, 'zerodha') : true;
  if (!stateOk || !req.query.request_token) return res.status(400).send('Invalid Zerodha callback');
  try {
    const checksum = crypto.createHash('sha256').update(cfg.zerodha.key + String(req.query.request_token) + cfg.zerodha.secret).digest('hex');
    const body = new URLSearchParams({ api_key: cfg.zerodha.key, request_token: String(req.query.request_token), checksum });
    const j = await jsonFetch('https://api.kite.trade/session/token', { method: 'POST', headers: { 'X-Kite-Version': '3', 'Content-Type': 'application/x-www-form-urlencoded' }, body });
    sessions.zerodha.accessToken = j.data?.access_token || j.access_token || ''; sessions.zerodha.profile = j.data || null; sessions.zerodha.obtainedAt = new Date().toISOString();
    res.redirect(frontendRedirect('zerodha', !!sessions.zerodha.accessToken));
  } catch (e) { res.redirect(frontendRedirect('zerodha', false, e.message)); }
});

app.get('/auth/angel', (_req, res) => {
  if (!cfg.angel.key || !cfg.angel.redirect) return res.status(500).send('Angel One app not configured');
  const state = saveState('angel');
  const q = new URLSearchParams({ api_key: cfg.angel.key, redirect_url: cfg.angel.redirect, state });
  res.redirect(`https://smartapi.angelone.in/publisher-login?${q}`);
});
app.get('/auth/angel/callback', (req, res) => {
  if (!validState(req.query.state, 'angel') || !req.query.auth_token) return res.status(400).send('Invalid Angel One callback');
  sessions.angel.accessToken = String(req.query.auth_token); sessions.angel.feedToken = String(req.query.feed_token || ''); sessions.angel.obtainedAt = new Date().toISOString();
  res.redirect(frontendRedirect('angel'));
});

app.get('/auth/dhan', async (_req, res) => {
  if (!cfg.dhan.clientId || !cfg.dhan.key || !cfg.dhan.secret) return res.status(500).send('Dhan app not configured');
  try {
    const j = await jsonFetch(`https://auth.dhan.co/app/generate-consent?client_id=${encodeURIComponent(cfg.dhan.clientId)}`, { method: 'POST', headers: { app_id: cfg.dhan.key, app_secret: cfg.dhan.secret } });
    if (!j.consentAppId) throw new Error('Dhan consentAppId missing');
    res.redirect(`https://auth.dhan.co/login/consentApp-login?consentAppId=${encodeURIComponent(j.consentAppId)}`);
  } catch (e) { res.redirect(frontendRedirect('dhan', false, e.message)); }
});
app.get('/auth/dhan/callback', async (req, res) => {
  if (!req.query.tokenId) return res.status(400).send('Dhan tokenId missing');
  try {
    const j = await jsonFetch(`https://auth.dhan.co/app/consumeApp-consent?tokenId=${encodeURIComponent(String(req.query.tokenId))}`, { headers: { app_id: cfg.dhan.key, app_secret: cfg.dhan.secret } });
    sessions.dhan.accessToken = j.accessToken || ''; sessions.dhan.profile = j; sessions.dhan.obtainedAt = new Date().toISOString();
    res.redirect(frontendRedirect('dhan', !!sessions.dhan.accessToken));
  } catch (e) { res.redirect(frontendRedirect('dhan', false, e.message)); }
});

async function accountCall(broker, kind) {
  const h = authHeaders(broker);
  if (broker === 'upstox') {
    const map = { profile: '/v2/user/profile', funds: '/v3/user/get-funds-and-margin', holdings: '/v2/portfolio/long-term-holdings', positions: '/v2/portfolio/short-term-positions', orders: '/v2/order/retrieve-all' };
    return jsonFetch(`https://api.upstox.com${map[kind]}`, { headers: { ...h, ...(kind === 'funds' ? { 'Api-Version': '3.0' } : {}) } });
  }
  if (broker === 'zerodha') {
    const map = { profile: '/user/profile', funds: '/user/margins', holdings: '/portfolio/holdings', positions: '/portfolio/positions', orders: '/orders' };
    return jsonFetch(`https://api.kite.trade${map[kind]}`, { headers: h });
  }
  if (broker === 'dhan') {
    const map = { profile: '/profile', funds: '/fundlimit', holdings: '/holdings', positions: '/positions', orders: '/orders' };
    return jsonFetch(`https://api.dhan.co/v2${map[kind]}`, { headers: h });
  }
  const map = {
    profile: '/rest/secure/angelbroking/user/v1/getProfile', funds: '/rest/secure/angelbroking/user/v1/getRMS',
    holdings: '/rest/secure/angelbroking/portfolio/v1/getAllHolding', positions: '/rest/secure/angelbroking/order/v1/getPosition', orders: '/rest/secure/angelbroking/order/v1/getOrderBook'
  };
  return jsonFetch(`https://apiconnect.angelone.in${map[kind]}`, { headers: h });
}
for (const kind of ['profile', 'funds', 'holdings', 'positions', 'orders']) {
  app.get(`/account/:broker/${kind}`, async (req, res) => {
    const broker = String(req.params.broker || '').toLowerCase();
    if (!sessions[broker]) return res.status(404).json({ ok: false, error: 'unsupported_broker' });
    try { res.json({ ok: true, broker, data: await accountCall(broker, kind) }); }
    catch (e) { res.status(e.status || 502).json({ ok: false, broker, error: e.message, detail: e.payload || null }); }
  });
}

app.post('/orders/:broker', async (req, res) => {
  if (!requireTrading(res)) return;
  const broker = String(req.params.broker || '').toLowerCase();
  if (!sessions[broker]?.accessToken) return res.status(401).json({ ok: false, error: 'broker_not_connected' });
  try {
    let data;
    if (broker === 'upstox') {
      data = await jsonFetch('https://api-hft.upstox.com/v3/order/place', { method: 'POST', headers: { ...authHeaders(broker), 'Content-Type': 'application/json' }, body: JSON.stringify(req.body) });
    } else if (broker === 'dhan') {
      data = await jsonFetch('https://api.dhan.co/v2/orders', { method: 'POST', headers: authHeaders(broker), body: JSON.stringify(req.body) });
    } else if (broker === 'angel') {
      data = await jsonFetch('https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder', { method: 'POST', headers: authHeaders(broker), body: JSON.stringify(req.body) });
    } else if (broker === 'zerodha') {
      const variety = req.body.variety || 'regular';
      const body = new URLSearchParams(req.body.params || req.body);
      data = await jsonFetch(`https://api.kite.trade/orders/${encodeURIComponent(variety)}`, { method: 'POST', headers: { ...authHeaders(broker), 'Content-Type': 'application/x-www-form-urlencoded' }, body });
    } else return res.status(404).json({ ok: false, error: 'unsupported_broker' });
    res.json({ ok: true, broker, data });
  } catch (e) { res.status(e.status || 502).json({ ok: false, broker, error: e.message, detail: e.payload || null }); }
});

app.delete('/orders/:broker/:orderId', async (req, res) => {
  if (!requireTrading(res)) return;
  const broker = String(req.params.broker || '').toLowerCase();
  const id = encodeURIComponent(String(req.params.orderId));
  try {
    let data;
    if (broker === 'upstox') data = await jsonFetch(`https://api-hft.upstox.com/v3/order/cancel?order_id=${id}`, { method: 'DELETE', headers: authHeaders(broker) });
    else if (broker === 'dhan') data = await jsonFetch(`https://api.dhan.co/v2/orders/${id}`, { method: 'DELETE', headers: authHeaders(broker) });
    else if (broker === 'angel') data = await jsonFetch('https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/cancelOrder', { method: 'POST', headers: authHeaders(broker), body: JSON.stringify({ variety: req.query.variety || 'NORMAL', orderid: req.params.orderId }) });
    else if (broker === 'zerodha') data = await jsonFetch(`https://api.kite.trade/orders/${encodeURIComponent(req.query.variety || 'regular')}/${id}`, { method: 'DELETE', headers: authHeaders(broker) });
    else return res.status(404).json({ ok: false, error: 'unsupported_broker' });
    res.json({ ok: true, broker, data });
  } catch (e) { res.status(e.status || 502).json({ ok: false, broker, error: e.message, detail: e.payload || null }); }
});

app.post('/subscribe', (req, res) => {
  const broker = String(req.body?.broker || 'upstox').toLowerCase();
  if (broker !== 'upstox') return res.status(501).json({ ok: false, error: 'realtime_adapter_not_enabled_for_broker', broker });
  if (!sessions.upstox.accessToken) return res.status(401).json({ ok: false, error: 'broker_not_connected' });
  const keys = Array.isArray(req.body?.instrumentKeys) ? req.body.instrumentKeys.filter(Boolean).map(String) : [];
  const mode = ['ltpc', 'full', 'option_greeks', 'full_d30'].includes(req.body?.mode) ? req.body.mode : 'ltpc';
  if (!keys.length) return res.status(400).json({ ok: false, error: 'instrumentKeys_required' });
  keys.forEach(k => activeKeys.add(k)); activeMode = mode;
  if (streamer) { try { streamer.subscribe(keys, activeMode); } catch (_) { resetStreamer(); ensureUpstoxStreamer(); } }
  else ensureUpstoxStreamer();
  res.json({ ok: true, broker: 'upstox', mode: activeMode, instrumentKeys: [...activeKeys] });
});
app.post('/unsubscribe', (req, res) => {
  const keys = Array.isArray(req.body?.instrumentKeys) ? req.body.instrumentKeys.filter(Boolean).map(String) : [];
  if (streamer && keys.length) { try { streamer.unsubscribe(keys); } catch (_) {} }
  keys.forEach(k => activeKeys.delete(k)); res.json({ ok: true, instrumentKeys: [...activeKeys] });
});
app.get('/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream'); res.setHeader('Cache-Control', 'no-cache, no-transform'); res.setHeader('Connection', 'keep-alive'); res.flushHeaders?.();
  sseClients.add(res); res.write(`data: ${JSON.stringify({ type: 'bridge_status', status: streamer ? 'live_connected' : 'connected', broker: liveBroker, ts: Date.now() })}\n\n`);
  req.on('close', () => sseClients.delete(res));
});
app.post('/disconnect/:broker?', (req, res) => {
  const broker = String(req.params.broker || req.body?.broker || 'all').toLowerCase();
  const targets = broker === 'all' ? Object.keys(sessions) : [broker];
  targets.forEach(x => { if (sessions[x]) { sessions[x].accessToken = ''; sessions[x].profile = null; } });
  if (targets.includes('upstox')) { activeKeys.clear(); resetStreamer(); }
  sendSse({ type: 'bridge_status', status: 'broker_disconnected', broker, ts: Date.now() });
  res.json({ ok: true, broker });
});

app.listen(PORT, () => console.log(`Multi-broker bridge listening on ${PORT}; trading=${ENABLE_TRADING}`));
