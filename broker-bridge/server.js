const express = require('express');
const cors = require('cors');
const crypto = require('crypto');
const UpstoxClient = require('upstox-js-sdk');

const app = express();
app.use(express.json());
app.use(cors({ origin: true, credentials: false }));

const PORT = process.env.PORT || 3000;
const CLIENT_ID = process.env.UPSTOX_CLIENT_ID || '';
const CLIENT_SECRET = process.env.UPSTOX_CLIENT_SECRET || '';
const REDIRECT_URI = process.env.UPSTOX_REDIRECT_URI || '';
const FRONTEND_URL = process.env.FRONTEND_URL || '';

let accessToken = process.env.UPSTOX_ACCESS_TOKEN || '';
let tokenObtainedAt = null;
let streamer = null;
let activeKeys = new Set();
let activeMode = 'ltpc';
const sseClients = new Set();
const oauthStates = new Set();

function sendSse(payload) {
  const msg = `data: ${JSON.stringify(payload)}\n\n`;
  for (const res of sseClients) {
    try { res.write(msg); } catch (_) { sseClients.delete(res); }
  }
}

function resetStreamer() {
  if (streamer) {
    try { streamer.disconnect(); } catch (_) {}
    streamer = null;
  }
}

function ensureStreamer() {
  if (!accessToken || streamer || !activeKeys.size) return;
  const defaultClient = UpstoxClient.ApiClient.instance;
  const oauth = defaultClient.authentications['OAUTH2'];
  oauth.accessToken = accessToken;

  streamer = new UpstoxClient.MarketDataStreamerV3([...activeKeys], activeMode);
  streamer.autoReconnect(true, 5, 50);

  streamer.on('open', () => sendSse({ type: 'bridge_status', status: 'live_connected', keys: [...activeKeys], mode: activeMode, ts: Date.now() }));
  streamer.on('message', (data) => {
    let parsed = null;
    try {
      const text = Buffer.isBuffer(data) ? data.toString('utf-8') : String(data);
      parsed = JSON.parse(text);
    } catch (_) {
      parsed = { raw: Buffer.isBuffer(data) ? data.toString('base64') : String(data) };
    }
    sendSse({ type: 'market_data', broker: 'upstox', payload: parsed, ts: Date.now() });
  });
  streamer.on('error', (error) => sendSse({ type: 'bridge_error', error: String(error?.message || error), ts: Date.now() }));
  streamer.on('close', () => { streamer = null; sendSse({ type: 'bridge_status', status: 'disconnected', ts: Date.now() }); });
  streamer.on('reconnecting', (m) => sendSse({ type: 'bridge_status', status: 'reconnecting', detail: String(m || ''), ts: Date.now() }));
  streamer.connect();
}

app.get('/health', (_req, res) => res.json({ ok: true, broker: 'upstox', connected: !!accessToken, stream_active: !!streamer, subscriptions: activeKeys.size }));

app.get('/auth/upstox', (_req, res) => {
  if (!CLIENT_ID || !REDIRECT_URI) return res.status(500).send('UPSTOX_CLIENT_ID / UPSTOX_REDIRECT_URI missing');
  const state = crypto.randomBytes(18).toString('hex');
  oauthStates.add(state);
  setTimeout(() => oauthStates.delete(state), 10 * 60 * 1000).unref();
  const q = new URLSearchParams({ response_type: 'code', client_id: CLIENT_ID, redirect_uri: REDIRECT_URI, state });
  res.redirect(`https://api.upstox.com/v2/login/authorization/dialog?${q.toString()}`);
});

app.get('/auth/upstox/callback', async (req, res) => {
  const { code, state } = req.query;
  if (!code || !state || !oauthStates.has(state)) return res.status(400).send('Invalid or expired OAuth state');
  oauthStates.delete(state);
  if (!CLIENT_SECRET) return res.status(500).send('UPSTOX_CLIENT_SECRET missing');
  try {
    const body = new URLSearchParams({ code: String(code), client_id: CLIENT_ID, client_secret: CLIENT_SECRET, redirect_uri: REDIRECT_URI, grant_type: 'authorization_code' });
    const r = await fetch('https://api.upstox.com/v2/login/authorization/token', {
      method: 'POST', headers: { 'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' }, body
    });
    const j = await r.json();
    if (!r.ok || !j.access_token) return res.status(502).send(`Token exchange failed: ${JSON.stringify(j)}`);
    accessToken = j.access_token;
    tokenObtainedAt = new Date().toISOString();
    resetStreamer();
    ensureStreamer();
    const target = FRONTEND_URL || '/status';
    res.redirect(`${target}${target.includes('?') ? '&' : '?'}broker=upstox&connected=1`);
  } catch (e) {
    res.status(500).send(`OAuth callback failed: ${e.message}`);
  }
});

app.get('/status', (_req, res) => res.json({ broker: 'upstox', connected: !!accessToken, token_obtained_at: tokenObtainedAt, live_stream: !!streamer, mode: activeMode, subscriptions: [...activeKeys] }));

app.post('/subscribe', (req, res) => {
  if (!accessToken) return res.status(401).json({ ok: false, error: 'broker_not_connected' });
  const keys = Array.isArray(req.body?.instrumentKeys) ? req.body.instrumentKeys.filter(Boolean).map(String) : [];
  const mode = ['ltpc', 'full', 'option_greeks', 'full_d30'].includes(req.body?.mode) ? req.body.mode : 'ltpc';
  if (!keys.length) return res.status(400).json({ ok: false, error: 'instrumentKeys_required' });
  keys.forEach(k => activeKeys.add(k));
  const modeChanged = activeMode !== mode;
  activeMode = mode;
  if (streamer) {
    try {
      if (modeChanged) streamer.changeMode([...activeKeys], activeMode);
      else streamer.subscribe(keys, activeMode);
    } catch (_) {
      resetStreamer();
      ensureStreamer();
    }
  } else ensureStreamer();
  res.json({ ok: true, mode: activeMode, instrumentKeys: [...activeKeys] });
});

app.post('/unsubscribe', (req, res) => {
  const keys = Array.isArray(req.body?.instrumentKeys) ? req.body.instrumentKeys.filter(Boolean).map(String) : [];
  if (streamer && keys.length) {
    try { streamer.unsubscribe(keys); } catch (_) {}
  }
  keys.forEach(k => activeKeys.delete(k));
  res.json({ ok: true, instrumentKeys: [...activeKeys] });
});

app.get('/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders?.();
  sseClients.add(res);
  res.write(`data: ${JSON.stringify({ type: 'bridge_status', status: accessToken ? (streamer ? 'live_connected' : 'broker_connected') : 'broker_disconnected', ts: Date.now() })}\n\n`);
  req.on('close', () => sseClients.delete(res));
});

app.post('/disconnect', (_req, res) => {
  accessToken = '';
  tokenObtainedAt = null;
  activeKeys.clear();
  resetStreamer();
  sendSse({ type: 'bridge_status', status: 'broker_disconnected', ts: Date.now() });
  res.json({ ok: true });
});

app.listen(PORT, () => console.log(`Broker bridge listening on ${PORT}`));
