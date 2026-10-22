'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let snapshot = {}, data = {}, selected = null, optionSignature = '', confirmation = null, searchRequest = '';
  let explorerSignature = '', streamState = 'connecting', streamLatency = null, latestReceived = 0;
  const EXPLORER_TYPES=[['FUT','Future'],['MLEG','Spread'],['OPT','Option'],['CS','Stock']];
  const explorer = {exchange:'CME', type:'MLEG', product:'', contract:'', filter:''};
  // The last exchange/type/product is a per-viewer convenience: it may be
  // unavailable (private window, blocked storage) and the page works without it.
  try{Object.assign(explorer,JSON.parse(localStorage.getItem('ft.explorer')||'{}'),{contract:'',filter:''});}catch(e){}
  const rememberExplorer=()=>{try{localStorage.setItem('ft.explorer',JSON.stringify({exchange:explorer.exchange,type:explorer.type,product:explorer.product}));}catch(e){}};
  // `CL BZ`, `CL-BZ`, `cl/bz` and `CL|BZ` are the same product: an
  // inter-commodity spread is filed here under its leg products, with a bar.
  const productOf=v=>String(v||'').trim().toUpperCase().replace(/[\s/:+,]+|-(?=[A-Z])/g,'|').replace(/^\|+|\|+$/g,'');
  window.addEventListener('error',event=>{const status=$('feed-status');if(status)status.textContent=`Display error: ${event.message}. Refresh the page; prices are not being presented as live.`;});
  const fmt = n => n === null || n === undefined || n === '' ? '—' : typeof n === 'number' ? Number(n.toFixed(8)).toString() : String(n);
  const clock = n => n ? new Date(n).toLocaleTimeString([], {hour12:false}) : '—';
  function quoteStatus(q) {
    if(!snapshot.engine?.alive)return 'Engine offline · historical prices';
    if(!q.timestamp)return q.error||'No quote received from Stellar';
    const age=Math.max(0,Math.floor((Date.now()-Date.parse(q.timestamp))/1000));
    const precise=Math.max(0,Date.now()-Date.parse(q.timestamp));
    const proof=q.fix_message_type?` · FIX ${q.fix_message_type}${q.fix_sequence?' seq '+q.fix_sequence:''}`:'';
    return q.stale?`Not fresh · last update ${age}s ago${proof}`:`Stellar FIX UAT · age ${precise} ms${proof}`;
  }
  function applyQuoteFrame(frame) {
    streamState=frame.source==='STELLAR_FIX'&&frame.simulated===false&&frame.connected?'verified Stellar FIX stream':'disconnected';
    // FIX-to-screen is measured on a NEW quote only — one Stellar sent just now
    // (published within 2 s of reaching our FIX session). An old quote
    // re-published says how old the quote is, not how fast the screen is:
    // that read "33289 ms" on a quiet market.
    const received=Object.values(frame.quotes||{}).reduce((latest,q)=>Math.max(latest,Number(q.received_ms)||0),0);
    if(received>latestReceived){
      latestReceived=received;
      if(Number(frame.published_ms)-received<2000)streamLatency=Math.max(0,Math.round(Date.now()-received));
    }
    for(const row of data.watchlist||[]) {
      const quote=frame.quotes?.[row.instrument.security_id];
      if(!quote)continue;
      row.quote={...row.quote,...quote};
      const age=quote.timestamp?Date.now()-Date.parse(quote.timestamp):Infinity;
      row.quote.stale=quote.integrity_ok===false||!frame.connected||age>15000;
      row.quote.spread=quote.ask!==null&&quote.ask!==undefined&&quote.bid!==null&&quote.bid!==undefined?quote.ask-quote.bid:null;
      row.quote.mid=quote.ask!==null&&quote.ask!==undefined&&quote.bid!==null&&quote.bid!==undefined?(quote.ask+quote.bid)/2:null;
    }
    renderFeed();
    renderWatch();
  }
  /* ONE line, ONE wording, from both the price stream and the page's
   * refresh — two writers with two wordings of different lengths wrapped
   * differently and shifted the page every half second. It never wraps
   * (CSS) and is rewritten only when it changed. */
  function renderFeed() {
    const engine=snapshot.engine||{};
    const md=engine.fix_connection?.sessions?.find(s=>s.name==='Market Data');
    const text=[`Source: Stellar ${engine.environment||'UAT'} FIX`,`Market data: ${md?.status||'DISCONNECTED'}`,
      `quote stream: ${streamState}`,`FIX-to-screen ${streamLatency===null?'—':streamLatency+' ms'}`,
      `last heartbeat ${clock(md?.last_heartbeat)}`].join(' · ');
    const el=$('feed-status');
    if(el.textContent!==text){el.textContent=text;el.title=text+'. FIX-to-screen: from our FIX session receiving a new Stellar quote to this screen showing it. A quote\'s age on its row is measured from Stellar\'s last update.';}
  }
  function connectQuoteStream() {
    if(!window.EventSource)return;
    const source=new EventSource('/api/quotes/stream');
    source.onopen=()=>streamState='streaming';
    source.onmessage=event=>{try{applyQuoteFrame(JSON.parse(event.data));}catch(error){streamState='invalid frame';}};
    source.onerror=()=>{streamState='reconnecting';};
  }
  const text = (tag, value, cls) => {const e = document.createElement(tag); e.textContent = value; if (cls) e.className = cls; return e;};
  function notice(message, error=false) { $('notice').textContent = message; $('notice').className = 'feedback' + (error ? ' error' : ''); }
  async function request(url, options) {
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),5000);
    let r;
    try{r=await fetch(url,{cache:'no-store',signal:controller.signal,...options});}
    catch(error){throw new Error(error.name==='AbortError'?'The local engine did not respond within 5 seconds.':error.message);}
    finally{clearTimeout(timeout);}
    if (!r.ok) throw new Error(`Request failed (${r.status}). Check the FIX engine.`);
    return r.json();
  }
  async function command(operation, args) {
    const queued = await request('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'terminal_'+operation, args})});
    if (!queued.ok) throw new Error(queued.error || 'Request refused');
    const deadline = Date.now()+45000;
    while (Date.now()<deadline) {
      const r = await request('/api/result/'+encodeURIComponent(queued.id));
      if (!r.pending) {
        if (!r.ok) throw new Error(r.error || 'Request failed');
        return r;
      }
      await new Promise(resolve=>setTimeout(resolve,40));
    }
    throw new Error('Engine reply timed out. Check status before retrying an order.');
  }
  function button(label, handler, cls='') {
    const b = text('button', label, cls); b.type='button';
    b.addEventListener('click', async()=>{b.disabled=true;try{await handler();}catch(e){notice(e.message,true);}finally{b.disabled=false;}});
    return b;
  }
  function empty(body, count, message) {const row=document.createElement('tr'), cell=text('td',message);cell.colSpan=count;row.append(cell);body.append(row);}
  function showInstrument(instrument) {
    selected=instrument;
    $('instrument-description').textContent=instrument.description || instrument.symbol;
    const dl=document.createElement('dl');
    for (const [name,key] of [['Stellar Security ID','security_id'],['Product symbol','symbol'],['Exchange','exchange'],['Type','security_type'],['Expiry month','maturity'],['Expiry date','expiry_date'],['Currency','currency'],['Tick size','tick_size'],['Tick value','tick_value'],['Point value','point_value'],['CFI code','cfi'],['Contract code','contract_code'],['Contract multiplier','multiplier'],['Minimum trade volume','min_qty'],['Strike','strike'],['Put / call (0 put, 1 call)','put_call'],['Strategy subtype','subtype']]) {
      const wrap=document.createElement('div');wrap.append(text('dt',name),text('dd',fmt(instrument[key])));dl.append(wrap);
    }
    $('instrument-details').replaceChildren(dl);
    $('legs').textContent=instrument.legs?.length ? instrument.legs.map((l,i)=>`${i+1}. ${l['600']||l['602']||'Leg'} · ${l['610']||''} · ${l['624']==='1'?'Buy':l['624']==='2'?'Sell':l['624']||'Side unspecified'} · ratio ${l['623']||'—'} · ID ${l['602']||'—'}`).join('\n') : 'No leg definition returned for this instrument.';
    const rows=document.createDocumentFragment();
    for (const [tag,value] of Object.entries(instrument.parameters||{})) {const row=document.createElement('tr');row.append(text('td',tag),text('td',value));rows.append(row);}
    $('parameters').replaceChildren(rows);
  }
  function ticketFor(instrument, side) {
    $('ticket-instrument').value=instrument.security_id;
    $('ticket-form').elements.side.value=side;
    showInstrument(instrument);
    const quote=data.watchlist?.find(w=>w.instrument.security_id===instrument.security_id)?.quote;
    if (quote && !quote.stale) {
      const value=side==='BUY'?quote.ask:quote.bid;
      if (value!==null && value!==undefined) $('ticket-form').elements.price.value=value;
    }
    $('ticket-form').scrollIntoView({behavior:'smooth',block:'center'});
  }
  function renderResults() {
    if (searchRequest && data.search?.request_id !== searchRequest) {
      $('results').replaceChildren(); empty($('results'),8,'Waiting for the new Stellar search…');
      $('search-status').textContent='Searching…'; return;
    }
    const rows=document.createDocumentFragment(), filter=$('filter').value.toLowerCase();
    const month=new Date().toISOString().slice(0,7).replace('-','');
    const found=(data.instruments||[]).filter(i=>[i.description,i.display_name,i.symbol,i.security_id,i.maturity].join(' ').toLowerCase().includes(filter)).sort((a,b)=>{
      const am=a.maturity||'',bm=b.maturity||'';
      return Number(am<month)-Number(bm<month)||am.localeCompare(bm);
    });
    for (const instrument of found.slice(0,200)) {
      const row=document.createElement('tr'), first=document.createElement('td');first.className='wrap';
      first.append(button(instrument.display_name||instrument.description||instrument.symbol,()=>showInstrument(instrument)));row.append(first);
      for (const key of ['symbol','exchange','security_type','maturity','security_id','currency']) row.append(text('td',fmt(instrument[key])));
      const action=document.createElement('td');
      action.append(button('Add + subscribe',async()=>{await command('add',{security_id:instrument.security_id});showInstrument(instrument);notice('Symbol saved. Waiting for Stellar market data.');}));row.append(action);rows.append(row);
    }
    $('results').replaceChildren(rows);
    if (!found.length) empty($('results'),8,'No matching instruments received. Check exchange/product and the search status.');
    const search=data.search||{};
    $('search-status').textContent=`${search.status||'Idle'} · ${(data.instruments||[]).length} received${found.length>200?' · showing first 200; use filter':''}${search.error?' · '+search.error:''}`;
  }
  function renderWatch() {
    const rows=document.createDocumentFragment();
    for (const {instrument:i,quote:q} of data.watchlist||[]) {
      const row=document.createElement('tr'), name=document.createElement('td');name.className='wrap';
      name.append(button(i.display_name||i.description||i.symbol,()=>showInstrument(i)),text('div',i.security_id+' / '+i.maturity,'tiny'));row.append(name);
      for (const key of ['bid_size','bid','ask','ask_size','last','mid','spread']) row.append(text('td',fmt(q[key]),q.stale?'stale':key==='bid'?'bid':key==='ask'?'ask':''));
      const stamp=text('td',clock(q.timestamp));
      stamp.append(text('div',quoteStatus(q),q.stale?'stale':'quote-live'));
      // Why there are no prices, in words: refused by Stellar, nobody quoting, no answer.
      const fs=q.feed_status;
      if (fs && !['LIVE','ONE_SIDED'].includes(fs.state)) stamp.append(text('div',fs.text,'quote-message'));
      else if (q.error) stamp.append(text('div',q.error,'quote-message'));
      row.append(stamp);
      const actions=document.createElement('td');actions.append(button('Buy',()=>ticketFor(i,'BUY'),'buy'),button('Sell',()=>ticketFor(i,'SELL'),'sell'),button('Remove',async()=>{await command('remove',{security_id:i.security_id});notice('Removed from the watchlist.');}),button('Algo desk…',()=>toAlgoDesk(i)));row.append(actions);rows.append(row);
    }
    $('watchlist').replaceChildren(rows);
    if (!data.watchlist?.length) empty($('watchlist'),10,'Add a returned Stellar instrument to subscribe to its prices.');
    const signature=(data.watchlist||[]).map(w=>w.instrument.security_id).join(',');
    if(signature!==optionSignature){
      const previous=$('ticket-instrument').value;optionSignature=signature;
      $('ticket-instrument').replaceChildren(new Option('Select an instrument',''));
      for(const {instrument:i} of data.watchlist||[]) $('ticket-instrument').append(new Option(`${i.display_name||i.description||i.symbol} · ${i.maturity||i.security_type} · ${i.security_id}`,i.security_id));
      if ([...$('ticket-instrument').options].some(o=>o.value===previous)) $('ticket-instrument').value=previous;
    }
  }
  function confirmDialog(title, content, label, callback) {
    $('review-title').textContent=title;$('review-content').replaceChildren(content);$('confirm').textContent=label;
    $('review-error').textContent='';confirmation=callback;$('confirm').disabled=false;$('review-dialog').showModal();
  }
  async function reviewClose(order) {
    const review=await command('preview_close',{order_id:order.id}), ticket=review.ticket;
    const content=document.createElement('div');
    content.append(text('h3',ticket.instrument.display_name||ticket.instrument.symbol),
      text('p',`${ticket.side} ${ticket.quantity} at MARKET · Account ${ticket.account} · Open/Close: CLOSE`),
      text('p',`Closes fills from ${order.id}. Market execution price is not guaranteed.`),
      text('p','Quantity is based on this ticket’s fills. Confirm it is still open in Stellar if you traded it elsewhere.'));
    confirmDialog('Review close position',content,'Confirm · Close position',()=>command('submit',{token:review.token,confirmed:true}));
  }
  function explorerMatches(){
    const catalogue=data.catalogue||[];
    const onVenue=catalogue.filter(i=>i.exchange===explorer.exchange&&i.security_type===explorer.type);
    // The same legs in either order are one product to look for.
    const same=(a,b)=>a===b||(a.includes('|')&&[...a.split('|')].sort().join('|')===[...b.split('|')].sort().join('|'));
    const inProduct=onVenue.filter(i=>same(i.product_key||i.symbol,explorer.product));
    const tokens=explorer.filter.toLowerCase().split(/\s+/).filter(Boolean);
    const contracts=inProduct.filter(i=>{const hay=[i.venue_name,i.structure,i.description,i.contract_code,i.display_name,i.security_id].join(' ').toLowerCase();return tokens.every(t=>hay.includes(t));})
      .sort((a,b)=>(a.leg_months||a.maturity||'').localeCompare(b.leg_months||b.maturity||'')||(a.venue_name||'').localeCompare(b.venue_name||''));
    return {catalogue,onVenue,inProduct,contracts};
  }
  // The algo desk trades what config.json lists. This fills in the contract
  // form from Stellar's own definition; the trader reviews it and presses Save.
  function toAlgoDesk(i){
    const q=new URLSearchParams({prefill:'1',name:i.venue_name||i.display_name||i.symbol,symbol:i.symbol,security_id:i.security_id,
      security_exchange:i.exchange,tick_size:i.tick_size||'',tick_value:i.tick_value||'',contract_multiplier:i.point_value||'',currency:i.currency||''});
    window.location.href='/exchanges?'+q.toString();
  }
  function renderExplorer(force=false) {
    const {catalogue,onVenue,inProduct,contracts}=explorerMatches();
    const signature=catalogue.length+'|'+(data.instruments||[]).length+'|'+JSON.stringify(data.search||{})+'|'+Math.floor(Date.now()/10000)+'|'+JSON.stringify(explorer);
    if(!force&&signature===explorerSignature)return;explorerSignature=signature;
    const options=(id, values, selected)=>{const el=$(id);el.replaceChildren();for(const [value,label,title] of values){const o=new Option(label,value);if(title)o.title=title;el.append(o);}el.value=selected;};
    options('explore-exchange',[...new Set([explorer.exchange,...catalogue.map(i=>i.exchange)])].filter(Boolean).sort().map(v=>[v,v]),explorer.exchange);
    if(document.activeElement!==$('explore-exchange-input'))$('explore-exchange-input').value=explorer.exchange;
    options('explore-type',EXPLORER_TYPES,explorer.type);
    // Products as Stellar files them, with how many contracts each holds, narrowed
    // by whatever is typed in the box above the list.
    const counts=new Map(), described=new Map();
    for(const i of onVenue){const k=i.product_key||i.symbol;counts.set(k,(counts.get(k)||0)+1);if(!described.has(k)&&i.description)described.set(k,i.description);}
    const typed=productOf($('explore-product-input').value), wanted=typed.split('|').filter(Boolean);
    // A product matches when it has every typed leg product, in any order:
    // `HO|CL` finds Stellar's `CL|HO`. When nothing matches, every product is
    // shown rather than an empty list, so what Stellar DID send is visible.
    const all=[...counts.keys()], parts=k=>k.split('|');
    const hits=all.filter(k=>k===explorer.product||wanted.every(w=>parts(k).some(p=>p.startsWith(w))));
    const products=(hits.length?hits:all).sort((a,b)=>parts(a).length-parts(b).length||a.localeCompare(b));
    options('explore-product',products.map(k=>[k,`${k}  (${counts.get(k)})`,described.get(k)]),explorer.product);
    if(document.activeElement!==$('explore-product-input'))$('explore-product-input').value=explorer.product;
    if(!contracts.some(i=>i.security_id===explorer.contract))explorer.contract='';
    options('explore-contract',contracts.slice(0,2000).map(i=>[i.security_id,i.venue_name||i.display_name||i.symbol,[i.structure,i.description,i.contract_code].filter(Boolean).join(' · ')]),explorer.contract);
    const pick=contracts.find(i=>i.security_id===explorer.contract), detail=$('explore-detail');
    detail.replaceChildren();
    if(pick){
      detail.append(text('b',pick.venue_name||pick.display_name));
      for(const part of [pick.structure,pick.description,pick.contract_code&&'code '+pick.contract_code,'Stellar Security ID '+pick.security_id,
                         pick.tick_size&&`tick ${pick.tick_size}`+(pick.tick_value?` = ${pick.tick_value} ${pick.currency||''}`:'')].filter(Boolean))detail.append(' · '+part);
    }else detail.textContent=explorer.product&&described.get(explorer.product)?`${explorer.product} — ${described.get(explorer.product)}`:'Select an instrument to see its legs, Stellar Security ID and tick value.';
    const search=data.search||{}, typeName=(EXPLORER_TYPES.find(t=>t[0]===explorer.type)||[])[1]||explorer.type;
    let status;
    if(!explorer.product)status=`${counts.size} ${typeName.toLowerCase()} products cached for ${explorer.exchange}. Choose one, or type a product and press Search Stellar.`;
    else if(!inProduct.length)status=`No ${explorer.product} ${typeName.toLowerCase()}s cached for ${explorer.exchange} yet. Press Search Stellar to fetch them`+(explorer.product.includes('|')?` (asks Stellar for the product ${explorer.product}, then for ${explorer.product.split('|').join(' and ')}).`:'.');
    else status=`${contracts.length} of ${inProduct.length} ${explorer.product} contracts`+(contracts.length>2000?' · showing the first 2000; type in the filter':'')+'. Double-click an instrument to subscribe.';
    // Stellar never says a search has finished, so its status is shown only while
    // one is recent enough to still be arriving.
    if(search.started&&Date.now()/1000-search.started<60){
      // What Stellar actually answered, so an empty list is never a mystery.
      status+=` · last Stellar search ${search.symbol||''} ${search.security_type||''}: ${search.status}, ${(data.instruments||[]).length} definitions received`;
      if(search.error)status+=` · Stellar said: ${search.error}`;
    }
    $('explore-status').textContent=status;
  }
  function renderOrders() {
    const rows=document.createDocumentFragment();
    for (const order of data.orders||[]) {
      const t=order.ticket, row=document.createElement('tr');row.append(text('td',clock(order.updated)));
      const name=text('td',t.instrument.display_name||t.instrument.description||t.instrument.symbol,'wrap');name.append(text('div',order.id,'tiny'));row.append(name);
      row.append(text('td',`${t.side} ${t.order_type}`),text('td',`${t.quantity} / ${fmt(order.filled_qty)}`),text('td',`${fmt(t.price)} / ${fmt(t.stop_price)}`),text('td',order.status+(order.pending?' · '+order.pending.kind+' requested':'')));
      const reason=text('td',order.venue_order_id||'Awaiting ID','wrap');reason.append(text('div',order.text||'','quote-message'));row.append(reason);
      const actions=document.createElement('td');
      if (order.close_available > 0) {
        actions.append(button('Close position', async()=>{
          const review=await command('preview_close',{order_id:order.id}), ticket=review.ticket;
          const content=document.createElement('div');
          content.append(text('h3',ticket.instrument.display_name||ticket.instrument.symbol),
            text('p',`${ticket.side} ${ticket.quantity} at MARKET · Account ${ticket.account} · Open/Close: CLOSE`),
            text('p',`Closes fills from ${order.id}. Market execution price is not guaranteed.`),
            text('p','This quantity is based on this ticket’s fills. Confirm the position is still open in Stellar if you traded it elsewhere.'));
          confirmDialog('Review close position',content,'Confirm · Close position',()=>command('submit',{token:review.token,confirmed:true}));
        }));
      }
      if (order.closed_qty > 0) actions.append(text('div',`Closed: ${fmt(order.closed_qty)}`,'tiny'));
      if (order.close_of) actions.append(text('div','Closing '+order.close_of,'tiny'));
      if (['NEW','PARTIALLY_FILLED','REPLACED'].includes(order.status)&&!order.pending) {
        actions.append(button('Cancel',()=>confirmDialog('Cancel UAT order',text('p',`Request cancellation of ${order.id}? It remains working until Stellar confirms cancellation.`),'Request cancel',()=>command('cancel',{order_id:order.id}))));
        if (!order.close_of) actions.append(button('Replace',()=>{
          const form=document.createElement('div');form.className='form-grid ticket-fields';
          for(const [key,label] of [['quantity','New total quantity'],['price','New limit price'],['stop_price','New stop trigger']]) {
            const l=text('label',label),input=document.createElement('input');input.type='number';input.step='any';input.name=key;input.value=t[key]??'';l.append(input);form.append(l);
          }
          confirmDialog('Review order replacement',form,'Send replacement',()=>{const args={order_id:order.id};form.querySelectorAll('input').forEach(i=>args[i.name]=i.value);return command('replace',args);});
        }));
      }
      row.append(actions);rows.append(row);
    }
    $('orders').replaceChildren(rows);if(!data.orders?.length)empty($('orders'),8,'No orders sent from this manual ticket.');
    const fills=document.createDocumentFragment();for(const f of data.fills||[]){const row=document.createElement('tr');for(const value of [clock(f.time),f.symbol,f.side,f.quantity,f.price,f.decision_price])row.append(text('td',fmt(value)));row.append(slipCell(f));row.append(text('td',fmt(f.exec_id)));fills.append(row);}
    $('fills').replaceChildren(fills);if(!data.fills?.length)empty($('fills'),8,'No executions received.');
    renderSlipSummary(data.slippage);
  }
  // Slippage: positive a cost (red), negative an improvement (green), and
  // blank where nothing was measured — never a zero.
  function slipCell(f) {
    const ticks=f.slippage_ticks, cell=document.createElement('td');
    if(ticks===null||ticks===undefined){cell.textContent='—';cell.title=f.decision_price==null?'no fresh Stellar quote when the ticket was sent':'';return cell;}
    cell.textContent=(ticks>0?'+':'')+Number(ticks).toFixed(2)+' t'+(f.slippage_money==null?'':' · '+(f.slippage_money<0?'-':'')+'$'+Math.abs(f.slippage_money).toFixed(2));
    cell.className=ticks>0?'slip-cost':ticks<0?'slip-better':'';
    return cell;
  }
  function renderSlipSummary(s) {
    const host=$('slip-summary');if(!host)return;
    if(!s||!s.counts||!s.counts.tickets){host.textContent='';return;}
    const part=(label,st)=>!st||!st.measured?`${label}: — (${st?st.unmeasured:0} unmeasured)`
      :`${label}: mean ${(st.ticks_mean>0?'+':'')+st.ticks_mean.toFixed(2)} t · worst ${(st.ticks_worst>0?'+':'')+st.ticks_worst.toFixed(2)} t · ${st.money_total==null?'—':(st.money_total<0?'-':'')+'$'+Math.abs(st.money_total).toFixed(2)} over ${st.measured} ticket(s)`+(st.unmeasured?` · ${st.unmeasured} unmeasured`:'');
    host.textContent=part('Entries',s.entry)+'   |   '+part('Closes',s.exit);
  }
  function renderRisk() {
    const risk=data.risk||{}, form=$('risk-form'), enabled=risk.trading_enabled!==false;
    if(!form)return;
    if(!form.dataset.loaded){for(const key of ['max_order_qty','max_open_qty_per_instrument','daily_loss_limit'])form.elements[key].value=risk[key]??0;form.elements.trading_enabled.checked=enabled;form.dataset.loaded='1';}
    $('risk-state').textContent=enabled?'NEW ORDERS ENABLED':'KILL SWITCH ON';
    $('risk-state').className=enabled?'risk-safe':'risk-blocked';
  }
  function conditionals() {
    const type=$('order-type').value, tif=$('tif').value, form=$('ticket-form');
    const limit=['LIMIT','STOP_LIMIT','LIMIT_ON_CLOSE','POST_ONLY'].includes(type), stop=['STOP','STOP_LIMIT'].includes(type);
    for(const [id,name,needed] of [['price-label','price',limit],['stop-label','stop_price',stop]]){
      $(id).hidden=!needed;form.elements[name].disabled=!needed;form.elements[name].required=needed;
    }
  }
  $('search-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;searchRequest='pending';$('filter').value='';renderResults();try{const result=await command('search',Object.fromEntries(new FormData(e.target)));searchRequest=result.request_id;notice('Search sent to Stellar. Results arrive asynchronously.');}catch(error){searchRequest='';notice(error.message,true);}finally{b.disabled=false;}});
  $('ticket-form').addEventListener('submit',async e=>{
    e.preventDefault();$('review-order').disabled=true;
    try{
      const args=Object.fromEntries(new FormData(e.target));
      const r=await command('preview',args), wrap=document.createElement('div'), dl=document.createElement('dl');
      for(const [key,value] of Object.entries(r.ticket)){if(key==='instrument')continue;const div=document.createElement('div');div.append(text('dt',key.replaceAll('_',' ')),text('dd',fmt(value)));dl.append(div);}
      const spread=r.book&&r.book.text?text('p',r.book.text):null;
      if(spread)spread.className='spread-line'+(r.book.wide?' wide':'');
      wrap.append(text('h3',r.ticket.instrument.display_name||r.ticket.instrument.description||r.ticket.instrument.symbol));
      if(spread)wrap.append(spread);
      wrap.append(dl,text('p','Review expires in 60 seconds. Confirming sends this order to Stellar UAT. Exchange acceptance is reported below.'));
      const details=document.createElement('details');details.append(text('summary','FIX fields to be sent'),text('pre',r.fields.map(([k,v])=>`${k}=${v}`).join('\n')));wrap.append(details);
      confirmDialog('Review UAT order',wrap,'Confirm · Send UAT order',()=>command('submit',{token:r.token,confirmed:true}));
    }catch(error){notice(error.message,true);}finally{$('review-order').disabled=false;}
  });
  $('risk-form').addEventListener('submit',async e=>{e.preventDefault();const args=Object.fromEntries(new FormData(e.target));args.trading_enabled=e.target.elements.trading_enabled.checked;try{const result=await command('risk',args);data.risk=result.risk;e.target.dataset.loaded='';renderRisk();notice('Manual trading safety limits saved.');}catch(error){notice(error.message,true);}});
  $('confirm').addEventListener('click',async()=>{if(!confirmation)return;$('confirm').disabled=true;try{const r=await confirmation();confirmation=null;$('review-dialog').close();notice(r&&r.message?r.message:`Request sent${r&&r.order_id?' · '+r.order_id:''}. Wait for Stellar acknowledgement.`);}catch(e){$('review-error').textContent=e.message;}finally{$('confirm').disabled=false;}});
  $('abort').addEventListener('click',()=>{$('review-dialog').close();confirmation=null;});
  $('filter').addEventListener('input',renderResults);
  $('order-type').addEventListener('change',conditionals);$('tif').addEventListener('change',conditionals);
  $('ticket-instrument').addEventListener('change',()=>{const i=data.watchlist?.find(w=>w.instrument.security_id===$('ticket-instrument').value)?.instrument;if(i)showInstrument(i);});
  async function poll(){
    try{
      const streamed=new Map((data.watchlist||[]).map(row=>[row.instrument.security_id,row.quote]));
      snapshot=await request('/api/snapshot');const engine=snapshot.engine||{};data=engine.manual_terminal||{};
      for(const row of data.watchlist||[]) {
        const newer=streamed.get(row.instrument.security_id);
        if(newer?.timestamp&&(!row.quote.timestamp||Date.parse(newer.timestamp)>Date.parse(row.quote.timestamp)))row.quote={...newer,feed_status:row.quote.feed_status};
      }
      renderFeed();
      $('session').textContent=!engine.alive?'Engine offline':`${engine.environment||''} · ${engine.session?.state||'DOWN'}`;
      $('session').className='tag '+(engine.alive&&engine.session?.state==='LOGGED_ON'?'good':'error');
      // Who is trading: a hand order is refused only on a contract whose
      // Algo TRADES it (the button on its ladder). The terminal refuses it
      // regardless; the screen says so before the trader fills a ticket.
      const trading=(snapshot.contracts||[]).filter(c=>c.algo_state==='PAPER'||c.algo_state==='LIVE');
      $('mode-text').textContent=!engine.alive?'Engine offline':trading.length
        ?'Hand orders are refused on the contracts the Algo is trading: '+trading.map(c=>c.name+' ('+c.algo_state+')').join(', ')+'. Everything else is yours. Closing and cancelling always work.'
        :'No Algo is trading — every instrument can be traded by hand.';
      $('mode-bar').className='mode-bar'+(trading.length?' algo':'');
      $('review-order').disabled=!engine.alive||engine.session?.state!=='LOGGED_ON';
      if(!engine.alive)notice('The engine is offline. Prices below are historical and orders are unavailable.',true);
      if(!engine.alive)for(const w of data.watchlist||[])w.quote.stale=true;
      if(!$('account').value&&data.account)$('account').value=data.account;
      renderResults();renderWatch();renderOrders();renderRisk();renderExplorer();
    }catch(error){for(const w of data.watchlist||[])w.quote.stale=true;if(snapshot.engine)snapshot.engine.alive=false;$('session').textContent='Engine unavailable';$('session').className='tag error';$('review-order').disabled=true;notice(error.message,true);renderWatch();}
    setTimeout(poll,1000);
  }
  $('explore-open').onclick=()=>{renderExplorer(true);$('explore-dialog').showModal();$('explore-product-input').focus();};
  $('explore-cancel').onclick=()=>$('explore-dialog').close();
  const pickExplorer=(key,value)=>{explorer[key]=value;if(key!=='contract'){explorer.contract='';explorer.filter='';$('explore-contract-filter').value='';}rememberExplorer();renderExplorer(true);};
  $('explore-exchange').onchange=()=>pickExplorer('exchange',$('explore-exchange').value);
  $('explore-type').onchange=()=>pickExplorer('type',$('explore-type').value);
  $('explore-product').onchange=()=>{$('explore-product-input').value='';pickExplorer('product',$('explore-product').value);};
  $('explore-contract').onchange=()=>pickExplorer('contract',$('explore-contract').value);
  $('explore-exchange-input').oninput=()=>pickExplorer('exchange',$('explore-exchange-input').value.trim().toUpperCase());
  // Typing narrows the product list; an exact match selects it, and a product
  // that is not cached yet becomes the one Search Stellar asks for.
  $('explore-product-input').oninput=()=>pickExplorer('product',productOf($('explore-product-input').value));
  $('explore-contract-filter').oninput=()=>{explorer.filter=$('explore-contract-filter').value;renderExplorer(true);};
  async function exploreSearch(){
    const legs=explorer.product.split('|').filter(Boolean);
    if(!legs.length)throw new Error('Type a product first, e.g. CL, BZ or CL|BZ');
    // Stellar's Security List Request names ONE symbol: each leg product is
    // asked for in turn, and a strategy listed under either is found.
    const symbols=legs;
    searchRequest='pending';
    // One Stellar search per leg product: an inter-commodity spread is defined
    // under its legs' products. The later searches APPEND, so the answers to
    // the first are not thrown away when the second is sent.
    for(const [n,symbol] of symbols.entries()){
      const r=await command('search',{exchange:explorer.exchange,security_type:explorer.type,symbol,append:n>0});
      searchRequest=r.request_id;
    }
    notice(`Asked Stellar for ${explorer.exchange} ${symbols.join(' + ')} definitions. They arrive over a few seconds.`);
  }
  $('explore-search').onclick=async()=>{try{await exploreSearch();}catch(e){searchRequest='';notice(e.message,true);$('explore-status').textContent=e.message;}};
  $('explore-product-input').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();if(!explorerMatches().inProduct.length)$('explore-search').click();}};
  async function exploreSubscribe(){
    if(!explorer.contract)throw new Error('Select an instrument first');
    await command('add',{security_id:explorer.contract});$('explore-dialog').close();
    notice('Instrument added to the watchlist and subscribed.');
  }
  $('explore-add').onclick=async()=>{try{await exploreSubscribe();}catch(e){$('explore-status').textContent=e.message;}};
  $('explore-contract').ondblclick=async()=>{explorer.contract=$('explore-contract').value;try{await exploreSubscribe();}catch(e){$('explore-status').textContent=e.message;}};
  $('explore-algo').onclick=()=>{
    const i=explorerMatches().contracts.find(c=>c.security_id===explorer.contract);
    if(!i){$('explore-status').textContent='Select an instrument first';return;}
    toAlgoDesk(i);
  };
  conditionals();connectQuoteStream();poll();
})();
