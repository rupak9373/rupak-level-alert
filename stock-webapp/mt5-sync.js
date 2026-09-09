(()=>{
  const JOURNAL_KEY='rupak_trading_journal_v1';
  const SYNC_KEY_NAME='rupak_mt5_journal_sync_key_v1';
  const BRIDGE_NAME='rupak_mt5_bridge_url_v1';
  const DEFAULT_BRIDGE='https://rupak-level-alert.onrender.com';

  function makeKey(){
    const a=new Uint8Array(24); crypto.getRandomValues(a);
    return Array.from(a,b=>b.toString(16).padStart(2,'0')).join('');
  }
  let syncKey=localStorage.getItem(SYNC_KEY_NAME);
  if(!syncKey){syncKey=makeKey();localStorage.setItem(SYNC_KEY_NAME,syncKey)}

  // Force the verified live Render service URL so stale browser storage cannot keep an old placeholder.
  localStorage.setItem(BRIDGE_NAME,DEFAULT_BRIDGE);

  const panel=document.createElement('section');
  panel.className='panel';
  panel.innerHTML=`<div class="head" style="margin-bottom:10px"><div><h2 style="margin:0">🔄 MT5 Auto Sync</h2><div class="muted" id="mt5Status">MT5 connection setup pending</div></div><button class="btn primary" id="mt5SyncNow">Sync Now</button></div>
  <div class="grid"><div class="field wide"><label>Bridge URL</label><input id="mt5Bridge" readonly value="${DEFAULT_BRIDGE}"></div><div class="field wide"><label>MT5 Sync Key</label><input id="mt5Key" readonly value="${syncKey}"></div></div>
  <div class="actions"><button class="btn" id="mt5CopyKey">Copy Sync Key</button><button class="btn" id="mt5CopyUrl">Copy Bridge URL</button></div>
  <div class="hint">EA MT5 se closed trades bhejega; journal har 30 sec me automatically sync karega. Setup/timeframe/notes MT5 me available na ho to blank rahenge.</div>`;
  const main=document.querySelector('main.wrap');
  if(main) main.insertBefore(panel,main.querySelector('.panel'));

  const status=document.getElementById('mt5Status');
  const bridge=document.getElementById('mt5Bridge');
  const keyEl=document.getElementById('mt5Key');
  const setStatus=(s,ok=false)=>{status.textContent=s;status.style.color=ok?'#55d58a':'#8fa4c3'};
  document.getElementById('mt5CopyKey').onclick=async()=>{await navigator.clipboard.writeText(keyEl.value);setStatus('Sync Key copied',true)};
  document.getElementById('mt5CopyUrl').onclick=async()=>{await navigator.clipboard.writeText(DEFAULT_BRIDGE);setStatus('Bridge URL copied',true)};

  function marketOf(symbol){const s=String(symbol||'').toUpperCase();if(s.includes('XAU'))return 'Gold';if(/BTC|ETH|SOL|XRP|DOGE/.test(s))return 'Crypto';if(/NIFTY|BANKNIFTY|SENSEX/.test(s))return 'Index';return 'Forex'}
  async function sync(){
    const base=DEFAULT_BRIDGE;
    try{
      setStatus('MT5 trades sync ho rahe hain...');
      const r=await fetch(base+'/mt5/journal/'+encodeURIComponent(syncKey),{cache:'no-store'});
      if(!r.ok)throw new Error('HTTP '+r.status);
      const j=await r.json();
      if(!j.ok)throw new Error(j.error||'sync failed');
      const remote=Array.isArray(j.trades)?j.trades:[];
      let local=[];try{local=JSON.parse(localStorage.getItem(JOURNAL_KEY)||'[]')}catch(_){local=[]}
      const map=new Map(local.map(x=>[String(x.sourceId||x.id),x]));
      let added=0;
      remote.forEach(t=>{
        const k=String(t.sourceId||''); if(!k)return;
        const old=map.get(k);
        const row={id:old?.id||Date.now()+Math.floor(Math.random()*100000),sourceId:k,date:t.date||'',market:old?.market||marketOf(t.symbol),symbol:t.symbol||'',side:t.side||'Buy',setup:old?.setup||t.setup||'',tf:old?.tf||t.tf||'',entry:t.entry||'',sl:t.sl||'',target:t.target||'',exit:t.exit||'',qty:t.qty||'',pnl:t.pnl??'',result:t.result||'BE',r:t.r||'',notes:old?.notes||t.notes||'',mt5:true,account:t.account||'',deal:t.deal||'',positionId:t.positionId||'',commission:t.commission||0,swap:t.swap||0};
        if(!old)added++; map.set(k,row);
      });
      const merged=[...map.values()];
      localStorage.setItem(JOURNAL_KEY,JSON.stringify(merged));
      if(typeof trades!=='undefined'){trades=merged;if(typeof render==='function')render()}
      setStatus(`MT5 connected • ${remote.length} synced • ${added} new`,true);
    }catch(e){setStatus('MT5 sync disconnected: '+e.message)}
  }
  document.getElementById('mt5SyncNow').onclick=sync;
  sync(); setInterval(sync,30000);
})();