const $ = (selector) => document.querySelector(selector);
const key = new URLSearchParams(location.search).get('key') || sessionStorage.getItem('sounddetect-admin-key') || '';
if (key) sessionStorage.setItem('sounddetect-admin-key', key);
let state = null;
let selectedSensor = null;
let selectedFurniture = null;
let selectedEvent = null;
let dragging = null;
let lastEventCount = 0;
let lastIncidentCount = 0;
const SVG = 'http://www.w3.org/2000/svg';

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = (iso) => { const date = new Date(iso); return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}); };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function toast(message) { const el = $('#toast'); el.textContent = message; el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3000); }
async function api(path, options={}) {
  const url = new URL(path, location.origin);
  url.searchParams.set('key', key);
  const response = await fetch(url, { ...options, headers: {'Content-Type':'application/json','X-Admin-Key':key,...options.headers} });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
async function refresh({editor=false}={}) {
  try {
    state = await api('/api/state');
    if (!selectedEvent && state.events.length) selectedEvent = state.events[state.events.length - 1].id;
    if (!selectedSensor && state.sensors.length) selectedSensor = state.sensors[0].id;
    renderAll(editor);
    if (state.incidents.length > lastIncidentCount && lastIncidentCount) {
      const incident = state.incidents[state.incidents.length - 1];
      toast(`Event triggered: ${incident.ruleName}`);
    } else if (state.events.length > lastEventCount && lastEventCount) toast('New sound event received');
    lastEventCount = state.events.length;
    lastIncidentCount = state.incidents.length;
  } catch (error) {
    toast(error.message);
    if (!key) $('#room-map').innerHTML = '<text x="500" y="300" text-anchor="middle" fill="#9ccbd7">Open the dashboard URL printed by the server.</text>';
  }
}
function renderAll(editor=false) {
  const room = state.room;
  $('#room-title').textContent = room.name;
  $('#metric-sensors').textContent = String(state.sensors.filter(isOnline).length).padStart(2,'0');
  $('#metric-events').textContent = String(state.events.length + (state.incidents?.length || 0)).padStart(2,'0');
  $('#metric-area').textContent = (room.width * room.depth).toFixed(1);
  $('#classifier-status').textContent = state.classifier;
  $('#sensor-count').textContent = String(state.sensors.length).padStart(2,'0');
  $('#network-mode').textContent = state.networkMode === 'manual' ? 'MANUAL ADDRESS' : 'AUTO / WI-FI';
  const baseAddress = new URL(state.baseUrl);
  $('#network-address').textContent = baseAddress.hostname === '127.0.0.1' ? 'Waiting for Wi-Fi…' : state.baseUrl;
  $('#map-scale').textContent = `${room.width} × ${room.depth} m`;
  if (editor || document.activeElement?.closest('#room-form') == null) {
    $('#room-name').value = room.name;
    $('#room-width').value = room.width;
    $('#room-depth').value = room.depth;
  }
  renderMap(); renderSensors(); renderEvents(); renderIncidents(); renderEventRules(); renderPhoto();
  if (editor) { renderSensorEditor(); renderFurnitureEditor(); }
}
function isOnline(sensor) { return sensor.active && sensor.lastSeen && Date.now() - new Date(sensor.lastSeen).getTime() < 15000; }
function geometry() {
  const {width:w, depth:d} = state.room;
  const scale = Math.min(650 / (w+d), 390 / ((w+d)*.48));
  return {w,d,scale,ox:500,oy:195};
}
function project(x,y,z=0,g=geometry()) { return [g.ox+(x-y)*g.scale, g.oy+(x+y)*g.scale*.48-z*g.scale*.78]; }
function inverse(sx,sy,g=geometry()) { const u=(sx-g.ox)/g.scale,v=(sy-g.oy)/(g.scale*.48); return [(u+v)/2,(v-u)/2]; }
const point = (p) => p.map((n)=>n.toFixed(1)).join(',');
const points = (arr) => arr.map(point).join(' ');
function poly(arr, cls, fill='') { return `<polygon class="${cls}" points="${points(arr)}" ${fill ? `fill="${fill}"`:''}/>`; }
function renderMap() {
  if (!state) return;
  const g=geometry(), p=(x,y,z=0)=>project(x,y,z,g);
  const floor=[p(0,0),p(g.w,0),p(g.w,g.d),p(0,g.d)];
  let html=`<defs><linearGradient id="floorShade" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#173b51"/><stop offset="1" stop-color="#0c2235"/></linearGradient><linearGradient id="furnTop"><stop stop-color="#3c7180"/><stop offset="1" stop-color="#235166"/></linearGradient></defs>`;
  html+=poly([p(0,0,1.25),p(g.w,0,1.25),p(g.w,0),p(0,0)],'room-wall','#102d43');
  html+=poly([p(0,0,1.25),p(0,g.d,1.25),p(0,g.d),p(0,0)],'room-wall','#0e293b');
  html+=poly(floor,'room-floor','url(#floorShade)');
  for(let x=1;x<g.w;x++) html+=`<line class="floor-grid" x1="${p(x,0)[0]}" y1="${p(x,0)[1]}" x2="${p(x,g.d)[0]}" y2="${p(x,g.d)[1]}"/>`;
  for(let y=1;y<g.d;y++) html+=`<line class="floor-grid" x1="${p(0,y)[0]}" y1="${p(0,y)[1]}" x2="${p(g.w,y)[0]}" y2="${p(g.w,y)[1]}"/>`;
  html+=`<polyline class="room-edge" points="${points([p(0,g.d,1.25),p(0,0,1.25),p(g.w,0,1.25)])}"/>`;
  html+=`<polyline class="room-edge" points="${points([p(0,g.d),p(g.w,g.d),p(g.w,0)])}"/>`;
  html+=`<line class="room-edge" x1="${p(0,g.d,1.25)[0]}" y1="${p(0,g.d,1.25)[1]}" x2="${p(0,g.d)[0]}" y2="${p(0,g.d)[1]}"/>`;
  html+=`<line class="room-edge" x1="${p(g.w,0,1.25)[0]}" y1="${p(g.w,0,1.25)[1]}" x2="${p(g.w,0)[0]}" y2="${p(g.w,0)[1]}"/>`;
  const heights={sofa:.55,table:.43,bed:.35,desk:.64,chair:.58,cabinet:1.1,plant:.78};
  const furniture=[...state.furniture].sort((a,b)=>(a.x+a.y)-(b.x+b.y));
  for(const item of furniture){
    const x=clamp(item.x,0,g.w),y=clamp(item.y,0,g.d),w=Math.min(item.w,g.w-x),d=Math.min(item.d,g.d-y),h=heights[item.type]||.5;
    const a=p(x,y,h),b=p(x+w,y,h),c=p(x+w,y+d,h),e=p(x,y+d,h),bf=p(x+w,y),cf=p(x+w,y+d),ef=p(x,y+d);
    const topColor=item.type==='plant'?'#47765a':item.type==='bed'?'#5b6d82':'url(#furnTop)';
    html+=`<g class="furniture-object ${item.id===selectedFurniture?'selected':''}" data-kind="furniture" data-id="${escapeHtml(item.id)}">${poly([e,c,cf,ef],'furniture-face','#1d4054')}${poly([b,c,cf,bf],'furniture-face','#244d5d')}${poly([a,b,c,e],'furniture-top',topColor)}<text class="furniture-label" x="${p(x+w/2,y+d/2,h)[0]}" y="${p(x+w/2,y+d/2,h)[1]+3}">${escapeHtml(item.type.toUpperCase())}</text></g>`;
  }
  const chosen=state.events.find(v=>v.id===selectedEvent);
  for(const r of (chosen?.ranges||[])){
    const pts=[];for(let k=0;k<=72;k++){const a=k/72*2*Math.PI,x=clamp(r.x+r.distance*Math.cos(a),0,g.w),y=clamp(r.y+r.distance*Math.sin(a),0,g.d);pts.push(p(x,y,.02));}
    const lp=p(clamp(r.x+r.distance*.71,0,g.w),clamp(r.y+r.distance*.71,0,g.d),.02);
    html+=`<polyline points="${points(pts)}" fill="rgba(255,190,90,.07)" stroke="#ffbe5a" stroke-width="1.4" stroke-dasharray="5 4"/><line x1="${p(r.x,r.y,.02)[0]}" y1="${p(r.x,r.y,.02)[1]}" x2="${lp[0]}" y2="${lp[1]}" stroke="#ffbe5a" stroke-width="1" stroke-dasharray="2 3"/><text x="${lp[0]+6}" y="${lp[1]-6}" fill="#ffd28f" font-size="11">~${r.distance.toFixed(1)} m</text>`;
  }
  const latest=state.events.slice(-4).reverse();
  for(const event of latest){ if(!event.location)continue; const ep=p(event.location.x,event.location.y,.03); html+=`<g data-kind="event" data-id="${escapeHtml(event.id)}" style="cursor:pointer"><circle class="event-pulse" cx="${ep[0]}" cy="${ep[1]}" r="${event.id===selectedEvent?25:17}"/><circle class="event-pin" cx="${ep[0]}" cy="${ep[1]}" r="5"/><text class="event-map-label" x="${ep[0]+12}" y="${ep[1]-11}">${escapeHtml(event.label)}</text></g>`; }
  for(const sensor of state.sensors){const sp=p(sensor.x,sensor.y,.06);html+=`<g class="room-sensor" data-kind="sensor" data-id="${escapeHtml(sensor.id)}"><circle class="sensor-halo" cx="${sp[0]}" cy="${sp[1]}" r="19"/><circle class="sensor-node" cx="${sp[0]}" cy="${sp[1]}" r="8"/><circle class="sensor-center" cx="${sp[0]}" cy="${sp[1]}" r="2.3"/><text class="sensor-label" x="${sp[0]+13}" y="${sp[1]+4}">${escapeHtml(sensor.name)}</text></g>`;}
  if(!state.sensors.length) html+=`<text x="500" y="565" fill="#6ca9b8" text-anchor="middle" font-size="13">Add sensors to begin mapping sound</text>`;
  $('#room-map').innerHTML=html;
}
function renderSensors(){
  const root=$('#sensor-list');
  root.innerHTML=state.sensors.length ? state.sensors.map(s=>{const kind=s.nodeType==='esp32_inmp441'?'ESP32 + INMP441':'Mobile device';return `<button class="sensor-item ${s.id===selectedSensor?'selected':''}" data-id="${escapeHtml(s.id)}"><span class="sensor-item-icon">${s.nodeType==='esp32_inmp441'?'▣':'◎'}</span><span class="sensor-item-main"><strong>${escapeHtml(s.name)}</strong><small>${escapeHtml(kind)} · ${s.x.toFixed(1)}, ${s.y.toFixed(1)} m · ${s.thresholdDbfs} dBFS</small></span><span class="sensor-item-status ${isOnline(s)?'online':''}" title="${isOnline(s)?'Online':'Offline'}"></span></button>`;}).join('') : '<div class="empty-list">No sensors yet.<br>Add a mobile device or ESP32 node.</div>';
}
function renderSensorEditor(){
  const s=state.sensors.find(v=>v.id===selectedSensor),root=$('#sensor-editor');
  if(!s){root.innerHTML='';return;}
  const isEsp=s.nodeType==='esp32_inmp441';
  const nodeLabel=isEsp?'ESP32 + INMP441':'Mobile device';
  root.innerHTML=`<div class="editor-title">CONFIGURE / ${escapeHtml(s.name.toUpperCase())}</div><div class="node-type-badge">${escapeHtml(nodeLabel)}</div><label>SENSOR NAME<input id="sensor-edit-name" maxlength="40" value="${escapeHtml(s.name)}"></label><label>TRIGGER THRESHOLD<div class="range-row"><input id="sensor-edit-threshold" type="range" min="-60" max="-8" value="${s.thresholdDbfs}"><output id="threshold-output">${s.thresholdDbfs} dBFS</output></div></label><p class="help-text">Lower values catch quieter sounds. dBFS is relative to this sensor microphone.</p><label>LEVEL AT 1 M (DISTANCE CALIBRATION)<div class="range-row"><input id="sensor-edit-ref" type="range" min="-70" max="0" value="${s.refDbfsAt1m??-20}"><output id="ref-output">${s.refDbfsAt1m??-20} dBFS</output></div></label><p class="help-text">Make a typical sound 1 m from this sensor and set this to the peak level it reports. Distances are rough estimates.</p><label>SOUNDS TO WATCH<input id="sensor-edit-watch" maxlength="100" placeholder="e.g. clapping, glass, speech" value="${escapeHtml(s.watchFor)}"></label><p class="help-text">Comma-separated labels are highlighted after classification. All threshold crossings are still recorded.</p>${isEsp?'<p class="help-text">Copy the credentials into the ESP32 firmware configuration. The gateway CA certificate must also be installed there.</p>':''}<div class="sensor-link"><button class="copy-button" id="copy-link">${isEsp?'COPY ESP32 CREDENTIALS ↗':'COPY PAIRING LINK ↗'}</button><button class="danger-button" id="delete-sensor" title="Remove sensor">REMOVE</button></div>`;
  $('#sensor-edit-name').addEventListener('change',()=>patchSensor({name:$('#sensor-edit-name').value}));
  $('#sensor-edit-threshold').addEventListener('input',e=>$('#threshold-output').textContent=`${e.target.value} dBFS`);
  $('#sensor-edit-threshold').addEventListener('change',e=>patchSensor({thresholdDbfs:Number(e.target.value)}));
  $('#sensor-edit-ref').addEventListener('input',e=>$('#ref-output').textContent=`${e.target.value} dBFS`);
  $('#sensor-edit-ref').addEventListener('change',e=>patchSensor({refDbfsAt1m:Number(e.target.value)}));
  $('#sensor-edit-watch').addEventListener('change',()=>patchSensor({watchFor:$('#sensor-edit-watch').value}));
  $('#copy-link').addEventListener('click',async()=>{
    let copyText;
    try {
      const latest = await api('/api/state');
      const currentSensor=latest.sensors.find(sensor => sensor.id === s.id);
      if (!currentSensor) throw new Error('This sensor is no longer available');
      copyText=isEsp
        ? `static const char GATEWAY_BASE_URL[] = "${latest.baseUrl}";\nstatic const char SENSOR_ID[] = "${currentSensor.id}";\nstatic const char SENSOR_TOKEN[] = "${currentSensor.token}";\n// Install the CA certificate from ${latest.baseUrl}/ca.crt in SOUNDDETECT_CA.`
        : currentSensor.pairingUrl;
    } catch (error) {
      toast(`Could not refresh sensor details: ${error.message}`);
      return;
    }
    try { await navigator.clipboard.writeText(copyText); toast(isEsp?'ESP32 credentials copied':'Pairing link copied'); }
    catch { prompt(isEsp?'Copy ESP32 credentials:':'Copy pairing link:', copyText); }
  });
  $('#delete-sensor').addEventListener('click',async()=>{if(!confirm(`Remove ${s.name}? Existing recordings remain.`))return;try{await api(`/api/sensors/${s.id}`,{method:'DELETE'});selectedSensor=null;await refresh({editor:true});toast('Sensor removed');}catch(e){toast(e.message);}});
}
async function patchSensor(payload){if(!selectedSensor)return;try{await api(`/api/sensors/${selectedSensor}`,{method:'PATCH',body:JSON.stringify(payload)});await refresh({editor:true});toast('Sensor updated');}catch(e){toast(e.message);}}
function renderFurnitureEditor(){const item=state.furniture.find(v=>v.id===selectedFurniture),root=$('#furniture-editor');root.innerHTML=item?`Selected: ${escapeHtml(item.type)} at ${item.x.toFixed(1)}, ${item.y.toFixed(1)} m<br><button id="delete-furniture" class="danger-button">REMOVE FURNITURE</button>`:'';if(item)$('#delete-furniture').addEventListener('click',async()=>{try{await api(`/api/furniture/${item.id}`,{method:'DELETE'});selectedFurniture=null;await refresh({editor:true});}catch(e){toast(e.message);}});}
function watchMatch(event){return event.observations.some(o=>{const s=state.sensors.find(v=>v.id===o.sensorId);return (s?.watchFor||'').split(',').map(v=>v.trim().toLowerCase()).filter(Boolean).some(tag=>event.label.toLowerCase().includes(tag)||event.candidates.some(c=>c.label.toLowerCase().includes(tag)||c.rawLabel.toLowerCase().includes(tag)));});}
function observationAudioMeta(o){const values=[];if(o.sampleRate)values.push(`${Math.round(o.sampleRate/1000)} kHz`);if(o.firmware)values.push(o.firmware);return values.length?` · ${values.map(escapeHtml).join(' · ')}`:'';}
function renderEvents(){
  const events=[...state.events].reverse(),list=$('#events-list');
  list.innerHTML=events.length?events.map(e=>`<button class="event-row ${e.id===selectedEvent?'active':''}" data-id="${escapeHtml(e.id)}"><span class="event-icon">${watchMatch(e)?'✳':'≋'}</span><span class="event-info"><strong>${escapeHtml(e.label)}</strong><small>${e.observations.length} sensor${e.observations.length===1?'':'s'} · ${Math.round(e.confidence*100)}% confidence${watchMatch(e)?' · WATCHLIST':''}</small></span><span class="event-time">${fmtTime(e.capturedAt)}</span></button>`).join(''):'<div class="empty-list">No events yet.<br>Start a sensor and make a sound above its threshold.</div>';
  const e=state.events.find(v=>v.id===selectedEvent),detail=$('#event-detail');
  if(!e){detail.dataset.rendered='';detail.innerHTML='<div class="detail-empty"><span class="empty-glyph">⌁</span><strong>Select an event</strong><p>Classification, sensor readings, and recordings will appear here.</p></div>';return;}
  const peak=Math.max(...e.observations.map(o=>o.peakDbfs));
  const detailHtml=`<div class="detail-kicker">EVENT / ${escapeHtml(e.id.toUpperCase())}</div><h3 class="detail-title">${escapeHtml(e.label)}</h3><div class="detail-confidence">${Math.round(e.confidence*100)}% classifier confidence ${watchMatch(e)?' · WATCHLIST MATCH':''}</div><div class="detail-facts"><div><span>DETECTED AT</span><strong>${fmtTime(e.capturedAt)}</strong></div><div><span>PEAK LEVEL</span><strong>${peak.toFixed(1)} dBFS</strong></div><div><span>SENSOR COUNT</span><strong>${e.observations.length}</strong></div><div><span>MAP POSITION</span><strong>${e.location?`${e.location.x.toFixed(1)}, ${e.location.y.toFixed(1)} m`:'—'}</strong></div></div><div class="detail-location">${escapeHtml(e.locationMethod||'Location pending')}</div>${e.candidates?.length?`<div class="candidate-list">${e.candidates.map(c=>`<span>${escapeHtml(c.label)} ${Math.round(c.confidence*100)}%</span>`).join('')}</div>`:''}${e.observations.map(o=>`<div class="clip-row">${escapeHtml(o.sensorName)} · ${o.peakDbfs.toFixed(1)} dBFS · ${o.duration.toFixed(1)}s${o.estimatedDistance!=null?` · ~${o.estimatedDistance.toFixed(1)} m away`:''}${observationAudioMeta(o)}<audio controls preload="none" src="/api/audio?event=${encodeURIComponent(e.id)}&observation=${encodeURIComponent(o.id)}&key=${encodeURIComponent(key)}"></audio></div>`).join('')}`;
  // Rebuild only when the content changed, so a playing <audio> is not replaced every refresh.
  if(detail.dataset.rendered!==detailHtml){detail.innerHTML=detailHtml;detail.dataset.rendered=detailHtml;}
}
function renderEventRules(){
  const root=$('#event-rule-list');
  if(!state.eventRules?.length){root.innerHTML='<div class="rule-empty">No sequence rules yet. Create one above.</div>';return;}
  root.innerHTML=state.eventRules.map(rule=>`<article class="rule-card"><div class="rule-card-head"><div><strong>${escapeHtml(rule.name)}</strong><small>${rule.scope==='same_sensor'?'Same sensor':'Any sensors'} · within ${rule.windowSeconds}s</small></div><span class="rule-state ${rule.enabled?'enabled':''}">${rule.enabled?'ACTIVE':'PAUSED'}</span></div><div class="rule-sequence"><span>${escapeHtml(rule.firstMatch)}</span><b>→</b><span>${escapeHtml(rule.secondMatch)}</span></div><div class="rule-actions"><button type="button" data-rule-action="edit" data-rule-id="${escapeHtml(rule.id)}">EDIT</button><button type="button" data-rule-action="toggle" data-rule-id="${escapeHtml(rule.id)}">${rule.enabled?'PAUSE':'ENABLE'}</button><button type="button" data-rule-action="delete" data-rule-id="${escapeHtml(rule.id)}">REMOVE</button></div></article>`).join('');
}
function renderIncidents(){
  const root=$('#incident-list');
  if(!state.incidents?.length){root.innerHTML='<div class="incident-empty"><span>⌁</span><strong>No compound events yet</strong><p>Create an event rule, then wait for its sound sequence to be classified.</p></div>';return;}
  root.innerHTML=[...state.incidents].reverse().map(incident=>`<article class="incident-card"><div class="incident-heading"><div><span class="incident-kicker">RULE TRIGGERED</span><h3>${escapeHtml(incident.ruleName)}</h3></div><time>${fmtTime(incident.triggeredAt)}</time></div><div class="incident-sequence">${(incident.sequence||[]).map((step,index)=>`<button type="button" class="incident-step" data-sound-event-id="${escapeHtml(step.eventId)}"><span class="incident-step-index">0${index+1}</span><span class="incident-step-body"><strong>${escapeHtml(step.label)}</strong><small>${fmtTime(step.capturedAt)} · ${escapeHtml((step.sensorNames||[]).join(', ')||'Sensor')} · ${Math.round((step.confidence||0)*100)}% confidence</small></span></button>`).join('<div class="sequence-arrow">↓</div>')}</div><div class="incident-foot">Second sound arrived within ${incident.windowSeconds}s · ${fmtTime(incident.startedAt)} → ${fmtTime(incident.triggeredAt)}</div></article>`).join('');
}
function renderPhoto(){const root=$('#photo-preview');if(state.room.photo){root.classList.add('visible');root.innerHTML=`<img alt="Uploaded room reference" src="/api/photo?key=${encodeURIComponent(key)}&v=${encodeURIComponent(state.room.photo)}"><small>Photo reference · arrange furniture on the map</small>`;}else{root.classList.remove('visible');root.innerHTML='';}}

$('#room-form').addEventListener('submit',async(e)=>{e.preventDefault();try{await api('/api/room',{method:'POST',body:JSON.stringify({name:$('#room-name').value,width:Number($('#room-width').value),depth:Number($('#room-depth').value)})});await refresh({editor:true});toast('Room dimensions updated');}catch(err){toast(err.message);}});
$('#add-sensor').addEventListener('click',()=>{$('#node-type-menu').hidden=!$('#node-type-menu').hidden;});
$('#node-type-menu').addEventListener('click',async(e)=>{const nodeType=e.target.closest('[data-node-type]')?.dataset.nodeType;if(!nodeType)return;$('#node-type-menu').hidden=true;try{const sensor=await api('/api/sensors',{method:'POST',body:JSON.stringify({nodeType})});selectedSensor=sensor.id;await refresh({editor:true});toast(nodeType==='esp32_inmp441'?'ESP32 node added; configure its firmware credentials':'Mobile sensor added; copy its pairing link');}catch(error){toast(error.message);}});
$('#sensor-list').addEventListener('click',e=>{const id=e.target.closest('[data-id]')?.dataset.id;if(id){selectedSensor=id;renderSensors();renderSensorEditor();}});
$('#events-list').addEventListener('click',e=>{const id=e.target.closest('[data-id]')?.dataset.id;if(id){selectedEvent=id;renderEvents();renderMap();}});
$('#incident-list').addEventListener('click',e=>{const id=e.target.closest('[data-sound-event-id]')?.dataset.soundEventId;if(id){selectedEvent=id;renderEvents();renderMap();$('#events').scrollIntoView({behavior:'smooth',block:'start'});}});
function resetEventRuleForm(){const form=$('#event-rule-form');form.reset();$('#event-rule-id').value='';$('#event-rule-window').value=45;$('#event-rule-submit').innerHTML='Create event rule <span>↗</span>';$('#event-rule-cancel').hidden=true;}
$('#event-rule-form').addEventListener('submit',async(e)=>{e.preventDefault();const id=$('#event-rule-id').value;const payload={name:$('#event-rule-name').value,firstMatch:$('#event-rule-first').value,secondMatch:$('#event-rule-second').value,windowSeconds:Number($('#event-rule-window').value),scope:$('#event-rule-scope').value};try{await api(id?'/api/event-rules/'+id:'/api/event-rules',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});resetEventRuleForm();await refresh({editor:true});toast(id?'Event rule updated':'Event rule created');}catch(error){toast(error.message);}});
$('#event-rule-cancel').addEventListener('click',resetEventRuleForm);
$('#event-rule-list').addEventListener('click',async(e)=>{const button=e.target.closest('[data-rule-action]');if(!button)return;const rule=state.eventRules.find(item=>item.id===button.dataset.ruleId);if(!rule)return;const action=button.dataset.ruleAction;if(action==='edit'){$('#event-rule-id').value=rule.id;$('#event-rule-name').value=rule.name;$('#event-rule-first').value=rule.firstMatch;$('#event-rule-second').value=rule.secondMatch;$('#event-rule-window').value=rule.windowSeconds;$('#event-rule-scope').value=rule.scope;$('#event-rule-submit').innerHTML='Save event rule <span>↗</span>';$('#event-rule-cancel').hidden=false;$('#event-rule-form').scrollIntoView({behavior:'smooth',block:'center'});return;}if(action==='delete'&&!confirm('Remove the "'+rule.name+'" rule? Existing triggered events remain.'))return;try{if(action==='delete')await api('/api/event-rules/'+rule.id,{method:'DELETE'});else await api('/api/event-rules/'+rule.id,{method:'PATCH',body:JSON.stringify({enabled:!rule.enabled})});if($('#event-rule-id').value===rule.id)resetEventRuleForm();await refresh({editor:true});toast(action==='delete'?'Event rule removed':rule.enabled?'Event rule paused':'Event rule enabled');}catch(error){toast(error.message);}});
$('#furniture-palette').addEventListener('click',async(e)=>{const type=e.target.closest('[data-type]')?.dataset.type;if(!type||!state)return;const sizes={sofa:[2,.8],table:[1.2,.8],bed:[2,1.5],desk:[1.4,.7],chair:[.55,.55],cabinet:[1,.5],plant:[.5,.5]};const [w,d]=sizes[type];try{const item=await api('/api/furniture',{method:'POST',body:JSON.stringify({type,x:Math.max(.2,state.room.width/2-w/2),y:Math.max(.2,state.room.depth/2-d/2),w,d})});selectedFurniture=item.id;await refresh({editor:true});toast(`${type} added; drag to place it`);}catch(err){toast(err.message);}});
$('#photo-input').addEventListener('change',async(e)=>{const file=e.target.files?.[0];if(!file)return;if(file.size>6_000_000){toast('Choose a photo under 6 MB');return;}try{const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));await api('/api/photo',{method:'POST',body:JSON.stringify({mime:file.type,base64:btoa(binary)})});await refresh({editor:true});toast('Photo reference added');}catch(err){toast(err.message);}});
$('#fit-map').addEventListener('click',()=>{renderMap();toast('Map view reset');});
const map=$('#room-map');
function svgPosition(event){const rect=map.getBoundingClientRect();return [(event.clientX-rect.left)*1000/rect.width,(event.clientY-rect.top)*650/rect.height];}
map.addEventListener('pointerdown',e=>{const group=e.target.closest('[data-kind]');if(!group||!state)return;const kind=group.dataset.kind,id=group.dataset.id;if(kind==='event'){selectedEvent=id;renderEvents();renderMap();return;}const item=(kind==='sensor'?state.sensors:state.furniture).find(v=>v.id===id);if(!item)return;if(kind==='sensor'){selectedSensor=id;renderSensors();renderSensorEditor();}else{selectedFurniture=id;renderFurnitureEditor();}const [sx,sy]=svgPosition(e),[x,y]=inverse(sx,sy);dragging={kind,id,dx:item.x-x,dy:item.y-y,moved:false};map.setPointerCapture(e.pointerId);renderMap();});
map.addEventListener('pointermove',e=>{if(!dragging||!state)return;const item=(dragging.kind==='sensor'?state.sensors:state.furniture).find(v=>v.id===dragging.id);if(!item)return;const [sx,sy]=svgPosition(e),[x,y]=inverse(sx,sy);item.x=clamp(x+dragging.dx,0,state.room.width-(item.w||0));item.y=clamp(y+dragging.dy,0,state.room.depth-(item.d||0));dragging.moved=true;renderMap();});
map.addEventListener('pointerup',async()=>{if(!dragging)return;const {kind,id,moved}=dragging;dragging=null;if(!moved)return;const item=(kind==='sensor'?state.sensors:state.furniture).find(v=>v.id===id);try{await api(`/api/${kind==='sensor'?'sensors':'furniture'}/${id}`,{method:'PATCH',body:JSON.stringify({x:item.x,y:item.y})});await refresh({editor:true});}catch(e){toast(e.message);await refresh({editor:true});}});
setInterval(()=>$('#server-clock').textContent=new Date().toLocaleString([], {month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'}).toUpperCase(),1000);
refresh({editor:true});setInterval(()=>refresh(),2500);
