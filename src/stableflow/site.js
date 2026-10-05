(() => {
  'use strict';
  const DATA = JSON.parse(document.getElementById('stableflow-data').textContent);
  const $ = id => document.getElementById(id);
  const colors = {cex:'#345c64', dex:'#168c78', lending:'#9eb7a1', bridge:'#ca9655', unknown:'#b7bfba'};
  const categoryName = key => ({cex:'CEX',dex:'DEX',lending:'Lending',bridge:'Bridge',unknown:'Unknown',all:'All categories'}[key] || String(key));
  const n = value => value == null ? 0 : Number(value);
  const sum = (rows, key) => rows.reduce((total, row) => total + n(row[key]), 0);
  const f = value => Number.isFinite(value) ? Intl.NumberFormat('en-US',{maximumFractionDigits:2}).format(value) : '—';
  const compact = value => Number.isFinite(value) ? Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(value) : '—';
  const pct = value => value == null || !Number.isFinite(value) ? '—' : `${(value*100).toFixed(1)}%`;
  const day = value => String(value || '').slice(0,10);
  const utcLabel = value => String(value || '').replace('T',' ').replace('Z',' UTC');
  const exclusiveEnd = day(DATA.manifest.end);
  const lastDate = exclusiveEnd ? new Date(new Date(exclusiveEnd+'T00:00:00Z').valueOf()-86400000).toISOString().slice(0,10) : '';
  const initial = {token:'USDC', start:day(DATA.manifest.start), end:lastDate, category:'all', internal:true, direction:'inflow'};
  let filtered = {}, currentView = 'overview';
  const state = () => ({token:$('token-filter').value,start:$('start-filter').value,end:$('end-filter').value,category:$('category-filter').value,internal:$('internal-toggle').checked,direction:$('direction-filter').value});
  const inRange = (row, s) => {const date=day(row.hour || row.date || row.start || row.start_hour);return (!s.start || date>=s.start) && (!s.end || date<=s.end);};
  const matches = (row,s) => row.token===s.token && inRange(row,s) && (s.category==='all' || row.category===s.category);
  const safeLink = value => {try {const url=new URL(String(value));return ['https:','http:'].includes(url.protocol)?url.href:null;}catch {return null;}};
  function el(tag, text, className) {const node=document.createElement(tag);if(text!=null)node.textContent=text;if(className)node.className=className;return node;}
  function text(id,value){$(id).textContent=value;}
  function emptyChart(id,message){const chart=$(id);if(chart.data && window.Plotly)Plotly.purge(chart);chart.replaceChildren(el('div',message,'chart-empty'));}
  const layout = (extra={}) => ({paper_bgcolor:'transparent',plot_bgcolor:'transparent',font:{family:'Inter, Segoe UI, Arial, sans-serif',size:10,color:'#8b9b91'},margin:{l:52,r:18,t:18,b:39},xaxis:{showgrid:false,zeroline:false,linecolor:'#edf0e9',tickfont:{size:9},ticks:'',automargin:true},yaxis:{gridcolor:'#edf1e9',zeroline:false,tickfont:{size:9},automargin:true},showlegend:false,hovermode:'x unified',...extra});
  function plot(id,traces,extra={}) {
    if(!window.Plotly){emptyChart(id,'The bundled Plotly file could not be loaded. Inspect the visible tables or export CSV.');return;}
    $(id).replaceChildren();
    Plotly.newPlot($(id),traces,layout(extra),{responsive:true,displayModeBar:false,scrollZoom:false,staticPlot:false});
  }
  function table(id,rows,columns) {
    const tbody=$(id).querySelector('tbody');tbody.replaceChildren();
    if(!rows.length){const tr=el('tr'),td=el('td','No rows in this selection.','empty-row');td.colSpan=columns.length;tr.append(td);tbody.append(tr);return;}
    rows.forEach(row=>{const tr=el('tr');columns.forEach(col=>{const td=el('td',null,col.numeric?'numeric':'');const value=col.render?col.render(row):row[col.key];if(value instanceof Node)td.append(value);else td.textContent=value==null?'—':String(value);tr.append(td);});tbody.append(tr);});
  }
  function groupedEdges(rows){
    const grouped=new Map();
    rows.forEach(row=>{const key=JSON.stringify([row.from_category,row.to_category]);const group=grouped.get(key)||{from_category:row.from_category,to_category:row.to_category,amount:0,transfer_count:0};group.amount+=n(row.amount);group.transfer_count+=n(row.transfer_count);grouped.set(key,group);});
    return [...grouped.values()].sort((a,b)=>b.amount-a.amount);
  }
  function dailyRows(rows,s){
    const grouped=new Map();rows.forEach(row=>{const key=day(row.date);const group=grouped.get(key)||{date:key,token:s.token,category:s.category,amount:0,known_amount:0,transfer_count:0};group.amount+=n(row.amount);if(row.from_category!=='unknown'||row.to_category!=='unknown')group.known_amount+=n(row.amount);group.transfer_count+=n(row.transfer_count);grouped.set(key,group);});
    const totals=DATA.totals.filter(row=>row.token===s.token && inRange(row,s));
    // Missing days stay absent. Observed complete days without transfers can show zero.
    totals.forEach(row=>{const key=day(row.date||row.hour);if(!grouped.has(key) && row.complete===true)grouped.set(key,{date:key,token:s.token,category:s.category,amount:0,known_amount:0,transfer_count:0});});
    const byDate=new Map();DATA.hourly.filter(row=>row.token===s.token&&inRange(row,s)&&(s.category==='all'||row.category===s.category)).forEach(row=>{const key=day(row.hour);byDate.set(key,(byDate.get(key)??true)&&row.complete!==false);});
    if(!grouped.size)return [];
    const dates=[...grouped.keys()].sort(),start=s.start||dates[0],end=s.end||dates[dates.length-1],result=[];
    for(let cursor=new Date(start+'T00:00:00Z');cursor.toISOString().slice(0,10)<=end;cursor=new Date(cursor.valueOf()+86400000)){
      const date=cursor.toISOString().slice(0,10),row=grouped.get(date)||{date,token:s.token,category:s.category,amount:null,known_amount:null,transfer_count:null};
      result.push({...row,coverage:row.amount?row.known_amount/row.amount:null,complete:row.amount!==null&&(byDate.get(date)??true)});
    }
    return result;
  }
  function focusedCategory(s){return s.category==='all'?(DATA.manifest.categories||[]).find(c=>c!=='unknown') || 'dex':s.category;}
  function filterRows(){
    const s=state();
    const eligible=DATA.edges.filter(row=>row.token===s.token&&inRange(row,s)&&!['zero_address','self','zero_amount'].includes(row.treatment)&&(s.category==='all'||row.from_category===s.category||row.to_category===s.category));
    const edges=eligible.filter(row=>s.internal||row.treatment!=='internal');
    const hourly=DATA.hourly.filter(row=>matches(row,s));
    const predictions=DATA.predictions.filter(row=>matches(row,s)&&(row.seed==null||Number(row.seed)===42));
    filtered={s,edges,eligible,hourly,predictions,daily:dailyRows(edges,s),metrics:metricRows(predictions.filter(row=>row.split==='test'))};
    return filtered;
  }
  function metricRows(rows){
    const groups=new Map();rows.forEach(row=>{for(const direction of ['inflow','outflow']){const key=JSON.stringify([row.model,row.category,direction]);const group=groups.get(key)||{model:row.model,category:row.category,direction,count:0,absolute:0,denominator:0,log_error:0};const actual=n(row['actual_'+direction]), predicted=n(row['predicted_'+direction]);group.count++;group.absolute+=Math.abs(actual-predicted);group.denominator+=Math.abs(actual);group.log_error+=Math.abs(Math.log1p(actual)-Math.log1p(predicted));groups.set(key,group);}});
    return [...groups.values()].map(g=>({model:g.model,category:g.category,direction:g.direction,mae:g.absolute/g.count,wape:g.denominator?g.absolute/g.denominator:null,log_mae:g.log_error/g.count,hours:g.count})).sort((a,b)=>a.model.localeCompare(b.model)||a.category.localeCompare(b.category)||a.direction.localeCompare(b.direction));
  }
  function qualityRow(label,value){const div=el('div');div.append(el('dt',label),el('dd',value));$('quality-list').append(div);}
  function renderOverview(){
    const {s,edges,eligible,hourly,daily}=filtered, amount=sum(edges,'amount'), known=sum(edges.filter(row=>row.from_category!=='unknown'||row.to_category!=='unknown'),'amount');
    const hasData=DATA.edges.length>0;
    text('stat-volume',hasData?compact(amount):'—');text('stat-volume-detail',`${s.token} · ${s.internal?'internal included':'internal excluded'}`);
    text('stat-coverage',amount?pct(known/amount):'—');text('stat-count',hasData?compact(sum(edges,'transfer_count')):'—');
    const status=DATA.experiment.status==='complete'?'Complete':'Pending';text('stat-model',status);text('stat-model-detail',status==='Complete'?'GRU · frozen test · seed 42':'Awaiting complete research dataset');
    if(!daily.length)emptyChart('volume-chart','No documented transfers in this selection.');else plot('volume-chart',[
      {x:daily.map(r=>r.date),y:daily.map(r=>r.complete?r.amount:null),type:'scatter',mode:'lines',name:'Transfer volume',line:{color:'#168c78',width:2.2},fill:'tozeroy',fillcolor:'rgba(22,140,120,.045)',hovertemplate:`%{y:,.2f} ${s.token}<extra>Eligible volume</extra>`,connectgaps:false},
      {x:daily.map(r=>r.date),y:daily.map(r=>r.complete?r.known_amount:null),type:'scatter',mode:'lines',name:'Labeled portion',line:{color:'#b1d7c4',width:1.6,dash:'dot'},hovertemplate:`%{y:,.2f} ${s.token}<extra>Labeled volume</extra>`,connectgaps:false}
    ],{yaxis:{...layout().yaxis,title:{text:s.token+' native units',font:{size:9}}}});
    text('trend-scope',`${s.token} native units · UTC`);
    $('findings').replaceChildren();
    const findings=[];
    if(!edges.length)findings.push('Research findings are pending a documented dataset for this selection.');
    else {
      findings.push(`${f(amount)} ${s.token} moved across ${f(sum(edges,'transfer_count'))} eligible transfer events in this selection.`);
      findings.push(`${pct(known/amount)} of selected volume touched a verified endpoint; ${pct(1-known/amount)} had two unknown endpoints.`);
      const top=groupedEdges(edges)[0];findings.push(`${categoryName(top.from_category)} → ${categoryName(top.to_category)} was the largest observed category pair, accounting for ${pct(top.amount/amount)} of selected volume.`);
    }
    findings.forEach((value,index)=>{const div=el('div',null,'finding');div.append(el('span',String(index+1).padStart(2,'0'),'finding-index'));div.append(el('p',value));$('findings').append(div);});
    $('quality-list').replaceChildren();
    qualityRow('Evidence status',DATA.manifest.kind==='synthetic-test'?'Synthetic test · no on-chain claims':hasData?'Documented snapshot':'Pending actual data');
    const hours=new Map();hourly.forEach(row=>hours.set(row.hour,(hours.get(row.hour)??true)&&row.complete!==false));
    qualityRow('Complete observed hours',`${[...hours.values()].filter(Boolean).length} / ${hours.size}`);
    qualityRow('Complete months',(DATA.manifest.complete_months||[]).length || 'Not established');
    qualityRow('Unknown-to-unknown volume',amount?pct(1-known/amount):'—');
    qualityRow('Internal volume excluded',s.internal?'Included in selection':`${compact(sum(eligible.filter(r=>r.treatment==='internal'),'amount'))} ${s.token}`);
    qualityRow('Model categories',(DATA.manifest.categories||[]).filter(c=>c!=='unknown').map(categoryName).join(' · ')||'Uncovered');
  }
  function renderFlows(){
    const {s,edges,eligible,hourly}=filtered, pairs=groupedEdges(edges), total=sum(edges,'amount');
    if(!pairs.length)emptyChart('sankey-chart','No category movement in this selection.');else{
      const categories=['cex','dex','lending','bridge','unknown'].filter(c=>pairs.some(r=>r.from_category===c||r.to_category===c));
      const nodes=[...categories,...categories];
      plot('sankey-chart',[{type:'sankey',orientation:'h',arrangement:'snap',node:{pad:22,thickness:12,line:{color:'transparent',width:0},label:nodes.map(categoryName),color:nodes.map(c=>colors[c]),x:nodes.map((c,i)=>i<categories.length?.01:.99)},link:{source:pairs.map(r=>categories.indexOf(r.from_category)),target:pairs.map(r=>categories.length+categories.indexOf(r.to_category)),value:pairs.map(r=>r.amount),color:pairs.map(r=>r.from_category==='unknown'?'rgba(170,183,174,.15)':'rgba(44,130,106,.17)'),customdata:pairs.map(r=>f(r.transfer_count)),hovertemplate:`%{source.label} → %{target.label}<br>%{value:,.2f} ${s.token}<br>%{customdata} events<extra></extra>`}}],{margin:{l:8,r:8,t:20,b:20},hovermode:'closest'});
    }
    text('sankey-scope',`${s.token} native units · ${s.internal?'internal included':'internal excluded'}`);
    const covered=DATA.manifest.categories||[];
    const usage=['cex','dex','lending','bridge'].map(category=>({category,amount:sum(edges.filter(r=>r.from_category===category||r.to_category===category),'amount'),covered:covered.includes(category)}));
    if(!edges.length)emptyChart('usage-chart','Usage structure is pending transfer evidence.');else plot('usage-chart',[{type:'bar',orientation:'h',x:usage.map(r=>r.covered?r.amount/total*100:0),y:usage.map(r=>categoryName(r.category)+(r.covered?'':' · uncovered')),marker:{color:usage.map(r=>colors[r.category])},width:.42,text:usage.map(r=>r.covered?pct(r.amount/total):'Uncovered'),textposition:'outside',cliponaxis:false,hovertemplate:'%{y}<br>%{x:.1f}% of selected volume<extra></extra>'}],{margin:{l:70,r:60,t:9,b:28},xaxis:{...layout().xaxis,range:[0,Math.max(100,...usage.map(r=>r.amount/Math.max(total,1)*100))],ticksuffix:'%',showgrid:true,gridcolor:'#edf1e9'},yaxis:{...layout().yaxis,autorange:'reversed',showgrid:false},bargap:.55,hovermode:'closest'});
    table('flows-table',pairs,[{render:r=>categoryName(r.from_category)},{render:r=>categoryName(r.to_category)},{render:r=>f(r.amount),numeric:true},{render:r=>f(r.transfer_count),numeric:true}]);
    const internal=sum(eligible.filter(r=>r.treatment==='internal'),'amount');text('internal-impact',`Known entity internal movements: ${f(internal)} ${s.token}. The toggle changes the map and every matching volume denominator.`);
    const category=focusedCategory(s), focus=hourly.filter(r=>r.category===category).sort((a,b)=>a.hour.localeCompare(b.hour));
    text('hourly-scope',`${categoryName(category)} boundary flows${s.category==='all'?' · focus shown for the first covered category':''}. Inflow and outflow are separate.`);
    if(!focus.length)emptyChart('hourly-chart','No covered hourly series for this category.');else plot('hourly-chart',[
      {type:'scatter',mode:'lines',x:focus.map(r=>r.hour),y:focus.map(r=>r.complete?r.inflow:null),name:'Inflow',line:{color:'#168c78',width:1.6},hovertemplate:`%{y:,.2f} ${s.token}<extra>Inflow</extra>`,connectgaps:false},
      {type:'scatter',mode:'lines',x:focus.map(r=>r.hour),y:focus.map(r=>r.complete?r.outflow:null),name:'Outflow',line:{color:'#ca9655',width:1.3},hovertemplate:`%{y:,.2f} ${s.token}<extra>Outflow</extra>`,connectgaps:false}
    ],{yaxis:{...layout().yaxis,title:{text:s.token+' native units',font:{size:9}}}});
  }
  function bestBaseline(category){const value=DATA.experiment.best_baseline;if(typeof value==='string')return value;if(value&&typeof value==='object')return value[filtered.s.token+'|'+category]||value[category]||value.model||value.name;return null;}
  function modelName(value){return ({gru:'GRU · seed 42',gru_flow_only:'GRU · flow only',previous_day:'Previous day',previous_week:'Previous week',seasonal_median:'4-week median',ridge:'Ridge'}[value]||String(value).replaceAll('_',' '));}
  function renderForecast(){
    const {s,predictions,metrics}=filtered, category=focusedCategory(s), focus=predictions.filter(r=>r.category===category&&r.split==='test'), direction=s.direction;
    const main=focus.filter(r=>r.model==='gru').sort((a,b)=>a.hour.localeCompare(b.hour));
    let baseline=bestBaseline(category);
    if(!baseline){const candidates=metrics.filter(r=>r.category===category&&r.direction===direction&&!String(r.model).startsWith('gru'));baseline=candidates[0]?.model;}
    const baseRows=focus.filter(r=>r.model===baseline).sort((a,b)=>a.hour.localeCompare(b.hour));
    text('experiment-status',String(DATA.experiment.status||'pending').toUpperCase());
    text('forecast-scope',main.length?`${categoryName(category)} · ${s.token} ${direction} · final test only${s.category==='all'?' · first covered category shown':''}`:'Awaiting a completed frozen test for this selection.');
    if(!main.length)emptyChart('forecast-chart','No evaluated GRU predictions in this selection.');else plot('forecast-chart',[
      {type:'scatter',mode:'lines',x:main.map(r=>r.hour),y:main.map(r=>r['actual_'+direction]),name:'Actual',line:{color:'#2f4d57',width:1.8},hovertemplate:`%{y:,.2f} ${s.token}<extra>Actual</extra>`},
      {type:'scatter',mode:'lines',x:main.map(r=>r.hour),y:main.map(r=>r['predicted_'+direction]),name:'GRU seed 42',line:{color:'#168c78',width:1.5},hovertemplate:`%{y:,.2f} ${s.token}<extra>GRU</extra>`},
      {type:'scatter',mode:'lines',x:baseRows.map(r=>r.hour),y:baseRows.map(r=>r['predicted_'+direction]),name:modelName(baseline),line:{color:'#ca9655',width:1.1,dash:'dot'},hovertemplate:`%{y:,.2f} ${s.token}<extra>Baseline</extra>`}
    ],{yaxis:{...layout().yaxis,title:{text:s.token+' native units',font:{size:9}}}});
    table('metrics-table',metrics,[{render:r=>modelName(r.model)},{render:r=>categoryName(r.category)},{render:r=>r.direction},{render:r=>f(r.mae),numeric:true},{render:r=>pct(r.wape),numeric:true},{render:r=>f(r.log_mae),numeric:true}]);
    const seedRows=[7,42,2026].map(seed=>{
      const rows=(DATA.experiment.metrics||[]).filter(r=>r.model==='gru'&&Number(r.seed)===seed&&r.split==='test'&&r.token===s.token&&(s.category==='all'||r.category===s.category));
      const wape=rows.map(r=>r.wape).filter(v=>v!=null&&Number.isFinite(Number(v))),log=rows.map(r=>r.log_mae).filter(v=>v!=null&&Number.isFinite(Number(v)));
      return {seed,wape:wape.length?wape.reduce((a,b)=>a+Number(b),0)/wape.length:null,log_mae:log.length?log.reduce((a,b)=>a+Number(b),0)/log.length:null,role:seed===42?'Preselected primary':'Seed sensitivity'};
    });
    table('seeds-table',seedRows,[{key:'seed'},{render:r=>pct(r.wape),numeric:true},{render:r=>r.log_mae==null?'—':f(r.log_mae),numeric:true},{key:'role'}]);
    renderAnomalies();
  }
  function renderAnomalies(){
    const {s}=filtered;
    const selectionStart=s.start?new Date(s.start+'T00:00:00Z').valueOf():-Infinity,selectionEnd=s.end?new Date(s.end+'T00:00:00Z').valueOf()+86400000:Infinity;
    const events=(DATA.experiment.anomalies||[]).filter(row=>{
      const eventStart=new Date(row.start||row.start_hour||row.hour).valueOf(),eventEnd=row.end||row.end_hour?new Date(row.end||row.end_hour).valueOf():eventStart+3600000;
      return row.token===s.token&&(s.category==='all'||row.category===s.category)&&eventStart<selectionEnd&&eventEnd>selectionStart;
    }).sort((a,b)=>n(b.score)-n(a.score)).slice(0,20);
    const container=$('anomalies');container.replaceChildren();
    if(!events.length){container.append(el('p',DATA.experiment.status==='complete'?'No calibrated events fall inside this selection.':'Anomaly investigations are pending a completed experiment.','quiet-note'));return;}
    events.forEach((event,index)=>{
      const details=el('details',null,'anomaly-item'),summary=el('summary');
      summary.append(el('span',String(index+1).padStart(2,'0'),'finding-index'),el('strong',`${categoryName(event.category)} · ${event.direction||(event.directions||[]).join(' / ')||'flow'} deviation`),el('span',utcLabel(event.start||event.start_hour||event.hour),'event-date'));
      details.append(summary);const body=el('div',null,'event-body');
      const stats=el('div',null,'event-stats');
      const dir=event.direction||(event.directions||[])[0]||s.direction;
      const actual=event.actual??event.actual_amount??event.actual_total??event['actual_'+dir], predicted=event.predicted??event.predicted_amount??event.predicted_total??event['predicted_'+dir];
      [['Actual',actual==null?'See hourly evidence':`${f(n(actual))} ${s.token}`],['Predicted',predicted==null?'See hourly evidence':`${f(n(predicted))} ${s.token}`],['Residual score',event.score==null?'—':f(n(event.score))]].forEach(([label,value])=>{const span=el('span',label);span.append(el('strong',value));stats.append(span);});
      body.append(stats);
      body.append(el('p',`Investigation: ${event.conclusion||event.classification||event.investigation||'Unexplained pending independent evidence'}. ${event.explanation||event.notes||event.note||'A forecast deviation alone does not identify its cause.'}`));
      const evidence=event.evidence||event.transfers||[];
      if(!evidence.length)body.append(el('p','Contributing transaction evidence is pending. No business explanation has been assigned.'));
      else evidence.forEach(transfer=>{const p=el('p');const hash=transfer.tx_hash||transfer.transaction_hash;const amount=transfer.amount??(transfer.amount_raw==null?null:n(transfer.amount_raw)/1e6);p.append(el('span',`${amount==null?'Amount pending':f(n(amount))+' '+s.token} · `));if(/^0x[a-fA-F0-9]{64}$/.test(hash||'')){const link=el('a',hash.slice(0,12)+'…'+hash.slice(-6));link.href='https://etherscan.io/tx/'+hash;link.target='_blank';link.rel='noopener noreferrer';p.append(link);}else p.append(el('span','Transaction hash unavailable'));body.append(p);});
      if(event.coverage!=null)body.append(el('p',`Labeled volume share: ${pct(n(event.coverage))}.`));
      details.append(body);container.append(details);
    });
  }
  function renderMethod(){
    table('labels-table',DATA.labels,[
      {render:r=>{const node=el('div');node.append(el('strong',r.entity||'Unknown'),el('div',categoryName(r.category),'quiet-note'));return node;}},
      {render:r=>{const node=el('code',r.address);node.title=r.address;return node;}},
      {render:r=>{const url=safeLink(r.source_url);if(!url)return 'Source pending';const link=el('a','Source ↗');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.title=r.evidence||url;return link;}},
      {render:r=>el('span',r.verified?'Verified':'Pending','category-tag')}
    ]);
    const provenance=$('provenance');provenance.replaceChildren();
    const source=DATA.manifest.source||{};
    const values=[['Snapshot kind',DATA.manifest.kind||'Pending'],['Source',typeof source==='string'?source:source.table||source.dataset||source.name||JSON.stringify(source)],['Data window',`${DATA.manifest.start||'—'} → ${DATA.manifest.end||'—'} (exclusive end)`],['Snapshot schema',String(DATA.manifest.schema_version||'—')],['Reproducibility',DATA.manifest.kind==='synthetic-test'?'Test artifact only; no investment or on-chain research claims':'Frozen snapshot · saved queries · SHA-256 file checksums']];
    values.forEach(([label,value])=>{const p=el('p');p.append(el('strong',label+': '),el('span',value));provenance.append(p);});
    const files=DATA.manifest.files||{};
    if(Object.keys(files).length){const details=el('details');details.append(el('summary','Inspect file checksums'));Object.entries(files).forEach(([name,digest])=>details.append(el('p',`${name}: ${digest}`)));provenance.append(details);}
    const boundaries=DATA.experiment.split_boundaries;
    if(boundaries)text('split-description','Frozen split boundaries (UTC): '+JSON.stringify(boundaries));
  }
  function csvValue(value){if(value==null)return '';let string=String(value);if(typeof value==='string'&&/^\s*[=+\-@]/.test(string))string="'"+string;return '"'+string.replaceAll('"','""')+'"';}
  function downloadCSV(kind){
    const rows=kind==='edges'?filtered.edges:kind==='hourly'?filtered.hourly:kind==='predictions'?filtered.predictions:kind==='metrics'?filtered.metrics:filtered.daily;
    const columns=[...new Set(rows.flatMap(row=>Object.keys(row)))];
    const synthetic=DATA.manifest.kind==='synthetic-test';if(synthetic)columns.unshift('evidence_notice');
    const lines=[columns.map(csvValue).join(','),...rows.map(row=>columns.map(key=>csvValue(key==='evidence_notice'?'Synthetic test data — not on-chain evidence':row[key])).join(','))];
    const content='\ufeff'+lines.join('\r\n');
    const url=URL.createObjectURL(new Blob([content],{type:'text/csv;charset=utf-8'}));const link=el('a');link.href=url;link.download=`stableflow-${DATA.manifest.kind==='synthetic-test'?'synthetic-test-':''}${kind}-${filtered.s.token}-${filtered.s.start}-${filtered.s.end}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function render(){
    filterRows();
    const {s}=filtered;
    text('filter-summary',`${s.token} · ${s.start||'any start'} through ${s.end||'any end'} inclusive · UTC · ${categoryName(s.category)} · ${s.internal?'internal included':'internal excluded'} · special transfers excluded`);
    if(s.start&&s.end&&s.start>s.end)text('filter-summary','The start date is later than the end date. Choose a valid date range.');
    renderOverview();
    if(currentView==='flows')renderFlows();
    if(currentView==='forecast')renderForecast();
  }
  function switchView(name,focus=false){
    currentView=name;
    document.querySelectorAll('.nav-tab').forEach(button=>{const active=button.dataset.tab===name;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;if(active&&focus)button.focus();});
    document.querySelectorAll('.view').forEach(view=>view.hidden=view.id!==name);
    text('breadcrumb',{overview:'Overview',flows:'Flows & Usage',forecast:'Forecast & Anomalies',method:'Method & Evidence'}[name]);
    if(name==='flows')renderFlows();if(name==='forecast')renderForecast();
    requestAnimationFrame(()=>document.querySelectorAll('.view:not([hidden]) .js-plotly-plot').forEach(chart=>Plotly.Plots.resize(chart)));
  }
  ['start','end'].forEach(key=>{$(key+'-filter').value=initial[key];if(initial.start)$(key+'-filter').min=initial.start;if(initial.end)$(key+'-filter').max=initial.end;});
  const categories=[...new Set([...(DATA.manifest.categories||[]),...DATA.edges.flatMap(row=>[row.from_category,row.to_category])])];
  categories.filter(Boolean).forEach(category=>{const option=el('option',categoryName(category));option.value=category;$('category-filter').append(option);});
  const warning=$('evidence-warning');warning.hidden=DATA.manifest.kind!=='synthetic-test';
  text('data-window',initial.start&&exclusiveEnd?`${initial.start} → ${exclusiveEnd}`:'Awaiting snapshot');
  text('snapshot-kind',DATA.manifest.kind==='synthetic-test'?'SYNTHETIC TEST ONLY':DATA.edges.length?'FIXED SNAPSHOT':'PENDING EVIDENCE');
  $('empty-state').hidden=DATA.edges.length>0;
  $('filters').addEventListener('submit',event=>event.preventDefault());
  $('filters').addEventListener('change',render);$('direction-filter').addEventListener('change',render);
  $('reset-filters').addEventListener('click',()=>{$('token-filter').value=initial.token;$('start-filter').value=initial.start;$('end-filter').value=initial.end;$('category-filter').value=initial.category;$('internal-toggle').checked=true;render();});
  document.querySelectorAll('[data-download]').forEach(button=>button.addEventListener('click',()=>downloadCSV(button.dataset.download)));
  const tabs=[...document.querySelectorAll('.nav-tab')];tabs.forEach((button,index)=>{button.addEventListener('click',()=>switchView(button.dataset.tab));button.addEventListener('keydown',event=>{if(!['ArrowDown','ArrowUp','ArrowRight','ArrowLeft','Home','End'].includes(event.key))return;event.preventDefault();let next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowDown'||event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;switchView(tabs[next].dataset.tab,true);});});
  document.querySelector('.brand').addEventListener('click',event=>{event.preventDefault();switchView('overview');});
  window.addEventListener('resize',()=>{if(window.Plotly)document.querySelectorAll('.view:not([hidden]) .js-plotly-plot').forEach(chart=>Plotly.Plots.resize(chart));});
  renderMethod();render();
})();
