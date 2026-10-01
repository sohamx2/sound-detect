const $ = (selector) => document.querySelector(selector);
const key = new URLSearchParams(location.search).get('key') || sessionStorage.getItem('sounddetect-admin-key') || '';
if (key) sessionStorage.setItem('sounddetect-admin-key', key);
let state = null;
let selectedSensor = null;
let selectedFurniture = null;
let selectedEvent = null;
let dragging = null;
let lastEventCount = 0;
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
    if (state.events.length > lastEventCount && lastEventCount) toast('New sound event received');
    lastEventCount = state.events.length;
  } catch (error) {
    toast(error.message);
    if (!key) $('#room-map').innerHTML = '<text x="500" y="300" text-anchor="middle" fill="#9ccbd7">Open the dashboard URL printed by the server.</text>';
  }
}
function renderAll(editor=false) {
  const room = state.room;
  $('#room-title').textContent = room.name;
  $('#metric-sensors').textContent = String(state.sensors.filter(isOnline).length).padStart(2,'0');
  $('#metric-events').textContent = String(state.events.length).padStart(2,'0');
  $('#metric-area').textContent = (room.width * room.depth).toFixed(1);
  $('#classifier-status').textContent = state.classifier;
  $('#sensor-count').textContent = String(state.sensors.length).padStart(2,'0');
  $('#map-scale').textContent = `${room.width} × ${room.depth} m`;
  if (editor || document.activeElement?.closest('#room-form') == null) {
    $('#room-name').value = room.name;
    $('#room-width').value = room.width;
    $('#room-depth').value = room.depth;
  }
  renderMap(); renderSensors(); renderEvents(); renderPhoto();
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
  const latest=state.events.slice(-4).reverse();
  for(const event of latest){ if(!event.location)continue; const ep=p(event.location.x,event.location.y,.03); html+=`<g data-kind="event" data-id="${escapeHtml(event.id)}" style="cursor:pointer"><circle class="event-pulse" cx="${ep[0]}" cy="${ep[1]}" r="${event.id===selectedEvent?25:17}"/><circle class="event-pin" cx="${ep[0]}" cy="${ep[1]}" r="5"/><text class="event-map-label" x="${ep[0]+12}" y="${ep[1]-11}">${escapeHtml(event.label)}</text></g>`; }
  for(const sensor of state.sensors){const sp=p(sensor.x,sensor.y,.06);html+=`<g class="room-sensor" data-kind="sensor" data-id="${escapeHtml(sensor.id)}"><circle class="sensor-halo" cx="${sp[0]}" cy="${sp[1]}" r="19"/><circle class="sensor-node" cx="${sp[0]}" cy="${sp[1]}" r="8"/><circle class="sensor-center" cx="${sp[0]}" cy="${sp[1]}" r="2.3"/><text class="sensor-label" x="${sp[0]+13}" y="${sp[1]+4}">${escapeHtml(sensor.name)}</text></g>`;}
  if(!state.sensors.length) html+=`<text x="500" y="565" fill="#6ca9b8" text-anchor="middle" font-size="13">Add phone sensors to begin mapping sound</text>`;
  $('#room-map').innerHTML=html;
}
function renderSensors(){
  const root=$('#sensor-list');
  root.innerHTML=state.sensors.length ? state.sensors.map(s=>`<button class="sensor-item ${s.id===selectedSensor?'selected':''}" data-id="${escapeHtml(s.id)}"><span class="sensor-item-icon">◎</span><span class="sensor-item-main"><strong>${escapeHtml(s.name)}</strong><small>${s.x.toFixed(1)}, ${s.y.toFixed(1)} m · ${s.thresholdDbfs} dBFS</small></span><span class="sensor-item-status ${isOnline(s)?'online':''}" title="${isOnline(s)?'Online':'Offline'}"></span></button>`).join('') : '<div class="empty-list">No sensors yet.<br>Add a phone to start listening.</div>';
}
function renderSensorEditor(){
  const s=state.sensors.find(v=>v.id===selectedSensor),root=$('#sensor-editor');
  if(!s){root.innerHTML='';return;}
  root.innerHTML=`<div class="editor-title">CONFIGURE / ${escapeHtml(s.name.toUpperCase())}</div><label>SENSOR NAME<input id="sensor-edit-name" maxlength="40" value="${escapeHtml(s.name)}"></label><label>TRIGGER THRESHOLD<div class="range-row"><input id="sensor-edit-threshold" type="range" min="-60" max="-8" value="${s.thresholdDbfs}"><output id="threshold-output">${s.thresholdDbfs} dBFS</output></div></label><p class="help-text">Lower values catch quieter sounds. dBFS is relative to this phone's microphone.</p><label>SOUNDS TO WATCH<input id="sensor-edit-watch" maxlength="100" placeholder="e.g. clapping, glass, speech" value="${escapeHtml(s.watchFor)}"></label><p class="help-text">Comma-separated labels are highlighted after Mac classification. All threshold crossings are still recorded.</p><div class="sensor-link"><button class="copy-button" id="copy-link">COPY PHONE LINK ↗</button><button class="danger-button" id="delete-sensor" title="Remove sensor">REMOVE</button></div>`;
  $('#sensor-edit-name').addEventListener('change',()=>patchSensor({name:$('#sensor-edit-name').value}));
  $('#sensor-edit-threshold').addEventListener('input',e=>$('#threshold-output').textContent=`${e.target.value} dBFS`);
  $('#sensor-edit-threshold').addEventListener('change',e=>patchSensor({thresholdDbfs:Number(e.target.value)}));
  $('#sensor-edit-watch').addEventListener('change',()=>patchSensor({watchFor:$('#sensor-edit-watch').value}));
  $('#copy-link').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(s.pairingUrl);toast('Phone link copied');}catch{prompt('Copy phone link:',s.pairingUrl);}});
  $('#delete-sensor').addEventListener('click',async()=>{if(!confirm(`Remove ${s.name}? Existing recordings remain.`))return;try{await api(`/api/sensors/${s.id}`,{method:'DELETE'});selectedSensor=null;await refresh({editor:true});toast('Sensor removed');}catch(e){toast(e.message);}});
}
async function patchSensor(payload){if(!selectedSensor)return;try{await api(`/api/sensors/${selectedSensor}`,{method:'PATCH',body:JSON.stringify(payload)});await refresh({editor:true});toast('Sensor updated');}catch(e){toast(e.message);}}
function renderFurnitureEditor(){const item=state.furniture.find(v=>v.id===selectedFurniture),root=$('#furniture-editor');root.innerHTML=item?`Selected: ${escapeHtml(item.type)} at ${item.x.toFixed(1)}, ${item.y.toFixed(1)} m<br><button id="delete-furniture" class="danger-button">REMOVE FURNITURE</button>`:'';if(item)$('#delete-furniture').addEventListener('click',async()=>{try{await api(`/api/furniture/${item.id}`,{method:'DELETE'});selectedFurniture=null;await refresh({editor:true});}catch(e){toast(e.message);}});}
function watchMatch(event){return event.observations.some(o=>{const s=state.sensors.find(v=>v.id===o.sensorId);return (s?.watchFor||'').split(',').map(v=>v.trim().toLowerCase()).filter(Boolean).some(tag=>event.label.toLowerCase().includes(tag)||event.candidates.some(c=>c.label.toLowerCase().includes(tag)||c.rawLabel.toLowerCase().includes(tag)));});}
function renderEvents(){
  const events=[...state.events].reverse(),list=$('#events-list');
  list.innerHTML=events.length?events.map(e=>`<button class="event-row ${e.id===selectedEvent?'active':''}" data-id="${escapeHtml(e.id)}"><span class="event-icon">${watchMatch(e)?'✳':'≋'}</span><span class="event-info"><strong>${escapeHtml(e.label)}</strong><small>${e.observations.length} sensor${e.observations.length===1?'':'s'} · ${Math.round(e.confidence*100)}% confidence${watchMatch(e)?' · WATCHLIST':''}</small></span><span class="event-time">${fmtTime(e.capturedAt)}</span></button>`).join(''):'<div class="empty-list">No events yet.<br>Start a phone sensor and make a sound above its threshold.</div>';
  const e=state.events.find(v=>v.id===selectedEvent),detail=$('#event-detail');
  if(!e){detail.innerHTML='<div class="detail-empty"><span class="empty-glyph">⌁</span><strong>Select an event</strong><p>Classification, sensor readings, and recordings will appear here.</p></div>';return;}
  const peak=Math.max(...e.observations.map(o=>o.peakDbfs));
  detail.innerHTML=`<div class="detail-kicker">EVENT / ${escapeHtml(e.id.toUpperCase())}</div><h3 class="detail-title">${escapeHtml(e.label)}</h3><div class="detail-confidence">${Math.round(e.confidence*100)}% classifier confidence ${watchMatch(e)?' · WATCHLIST MATCH':''}</div><div class="detail-facts"><div><span>DETECTED AT</span><strong>${fmtTime(e.capturedAt)}</strong></div><div><span>PEAK LEVEL</span><strong>${peak.toFixed(1)} dBFS</strong></div><div><span>SENSOR COUNT</span><strong>${e.observations.length}</strong></div><div><span>MAP POSITION</span><strong>${e.location?`${e.location.x.toFixed(1)}, ${e.location.y.toFixed(1)} m`:'—'}</strong></div></div><div class="detail-location">${escapeHtml(e.locationMethod||'Location pending')}</div>${e.candidates?.length?`<div class="candidate-list">${e.candidates.map(c=>`<span>${escapeHtml(c.label)} ${Math.round(c.confidence*100)}%</span>`).join('')}</div>`:''}${e.observations.map(o=>`<div class="clip-row">${escapeHtml(o.sensorName)} · ${o.peakDbfs.toFixed(1)} dBFS · ${o.duration.toFixed(1)}s<audio controls preload="none" src="/api/audio?event=${encodeURIComponent(e.id)}&observation=${encodeURIComponent(o.id)}&key=${encodeURIComponent(key)}"></audio></div>`).join('')}`;
}
function renderPhoto(){const root=$('#photo-preview');if(state.room.photo){root.classList.add('visible');root.innerHTML=`<img alt="Uploaded room reference" src="/api/photo?key=${encodeURIComponent(key)}&v=${encodeURIComponent(state.room.photo)}"><small>Photo reference · arrange furniture on the map</small>`;}else{root.classList.remove('visible');root.innerHTML='';}}

$('#room-form').addEventListener('submit',async(e)=>{e.preventDefault();try{await api('/api/room',{method:'POST',body:JSON.stringify({name:$('#room-name').value,width:Number($('#room-width').value),depth:Number($('#room-depth').value)})});await refresh({editor:true});toast('Room dimensions updated');}catch(err){toast(err.message);}});
$('#add-sensor').addEventListener('click',async()=>{try{const s=await api('/api/sensors',{method:'POST',body:'{}'});selectedSensor=s.id;await refresh({editor:true});toast('Sensor added; copy its phone link');}catch(e){toast(e.message);}});
$('#sensor-list').addEventListener('click',e=>{const id=e.target.closest('[data-id]')?.dataset.id;if(id){selectedSensor=id;renderSensors();renderSensorEditor();}});
$('#events-list').addEventListener('click',e=>{const id=e.target.closest('[data-id]')?.dataset.id;if(id){selectedEvent=id;renderEvents();renderMap();}});
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
