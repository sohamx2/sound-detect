const query = new URLSearchParams(location.search);
const sensorId = query.get('sid') || '';
const token = query.get('token') || '';
const $ = (selector) => document.querySelector(selector);
let config = null;
let stream = null;
let context = null;
let source = null;
let processor = null;
let monitorGain = null;
let listening = false;
let recording = null;
let recentChunks = [];
let recentLength = 0;
let hotChunks = 0;
let cooldownUntil = 0;
let uploading = false;
const queue = [];

function message(text) { $('#sensor-message').textContent = text; }
function status(text, online) { $('#connection-pill').textContent = text; $('#connection-pill').style.borderColor = online ? '#5cae80' : '#2b6270'; }
async function fetchConfig() {
  try {
    const response = await fetch(`/api/sensor-config?sid=${encodeURIComponent(sensorId)}&token=${encodeURIComponent(token)}`, {cache:'no-store',headers:{'X-Sensor-Active':listening?'true':'false'}});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Pairing link rejected');
    config = value;
    $('#sensor-name').textContent = config.name;
    $('#sensor-threshold').textContent = `${config.thresholdDbfs} dBFS`;
    $('#sensor-watch').textContent = config.watchFor || 'All sounds';
    $('#threshold-marker').style.left = `${Math.max(0, Math.min(100, (config.thresholdDbfs + 70) / 70 * 100))}%`;
    $('#listen-button').disabled = false;
    status(listening ? 'LISTENING' : 'PAIRED', true);
    if (!listening) message('Ready. Tap the button to grant microphone access.');
  } catch (error) {
    status('DISCONNECTED', false);
    message(error.message);
  }
}
function updateLevel(db) {
  $('#level-value').textContent = `${db.toFixed(1)} dBFS`;
  $('#level-bar').style.width = `${Math.max(0, Math.min(100, (db + 70) / 70 * 100))}%`;
}
function handleSamples(samples) {
  if (!listening || !config) return;
  let power=0;
  for (let i=0;i<samples.length;i++) power += samples[i]*samples[i];
  const db = Math.max(-100, 20*Math.log10(Math.sqrt(power/samples.length) || 0.000001));
  updateLevel(db);
  if (recording) {
    recording.chunks.push(samples);
    recording.length += samples.length;
    recording.peak = Math.max(recording.peak, db);
    if (recording.length >= context.sampleRate * 3.2) finishRecording();
  } else {
    hotChunks = db >= config.thresholdDbfs ? hotChunks + 1 : 0;
    if (hotChunks >= 2 && Date.now() >= cooldownUntil) {
      recording = {chunks:recentChunks.map(c=>new Float32Array(c)),length:recentLength,peak:db,capturedAt:new Date().toISOString()};
      recording.chunks.push(samples); recording.length += samples.length;
      message('Sound detected — recording short clip…');
      $('#sensor-visual-state').textContent = 'CAPTURING';
      hotChunks = 0;
    }
  }
  recentChunks.push(samples);
  recentLength += samples.length;
  while (recentLength > context.sampleRate * .55 && recentChunks.length > 1) recentLength -= recentChunks.shift().length;
}
function wavBase64(chunks, length, sampleRate) {
  const buffer = new ArrayBuffer(44 + length*2);
  const view = new DataView(buffer);
  function ascii(offset, text) { for(let i=0;i<text.length;i++) view.setUint8(offset+i,text.charCodeAt(i)); }
  ascii(0,'RIFF'); view.setUint32(4,36+length*2,true); ascii(8,'WAVE'); ascii(12,'fmt ');
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,sampleRate,true); view.setUint32(28,sampleRate*2,true);
  view.setUint16(32,2,true); view.setUint16(34,16,true); ascii(36,'data'); view.setUint32(40,length*2,true);
  let offset=44;
  for(const chunk of chunks) for(let i=0;i<chunk.length;i++){const sample=Math.max(-1,Math.min(1,chunk[i]));view.setInt16(offset,sample<0?sample*32768:sample*32767,true);offset+=2;}
  const bytes=new Uint8Array(buffer);
  let binary='';
  for(let i=0;i<bytes.length;i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192));
  return btoa(binary);
}
function finishRecording() {
  const clip=recording;
  recording=null;
  cooldownUntil=Date.now()+2100;
  $('#sensor-visual-state').textContent = 'LISTENING';
  queue.push({sensorId, capturedAt:clip.capturedAt, peakDbfs:clip.peak, duration:clip.length/context.sampleRate,
              audioBase64:wavBase64(clip.chunks,clip.length,context.sampleRate)});
  if(queue.length>10){queue.shift();message('Upload queue full; oldest clip dropped.');}
  sendQueued();
}
async function sendQueued() {
  if(uploading || !queue.length) return;
  uploading=true;
  while(queue.length){
    try {
      const response=await fetch('/api/observations',{method:'POST',headers:{'Content-Type':'application/json','X-Sensor-Token':token},body:JSON.stringify(queue[0])});
      const data=await response.json();
      if(!response.ok) throw new Error(data.error || 'Upload failed');
      queue.shift();
      message('Recording sent to the Mac for analysis. Listening…');
      status('LISTENING',true);
    } catch(error) {
      message(`Connection interrupted: ${error.message}. Retrying…`);
      status('RETRYING',false);
      await new Promise(resolve=>setTimeout(resolve,4000));
    }
  }
  uploading=false;
}
async function start() {
  try {
    if(!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone unavailable. Use the HTTPS phone link and trust the local certificate.');
    stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});
    context=new (window.AudioContext||window.webkitAudioContext)({latencyHint:'interactive'});
    await context.resume();
    source=context.createMediaStreamSource(stream);
    monitorGain=context.createGain(); monitorGain.gain.value=0;
    if(context.audioWorklet){
      await context.audioWorklet.addModule('/pcm-processor.js');
      processor=new AudioWorkletNode(context,'pcm-processor');
      processor.port.onmessage=e=>handleSamples(new Float32Array(e.data));
    } else {
      processor=context.createScriptProcessor(4096,1,1);
      processor.onaudioprocess=e=>handleSamples(new Float32Array(e.inputBuffer.getChannelData(0)));
    }
    source.connect(processor); processor.connect(monitorGain); monitorGain.connect(context.destination);
    stream.getAudioTracks()[0].onended=stop;
    listening=true;
    document.body.classList.add('listening');
    $('#listen-button').innerHTML='STOP LISTENING <span>■</span>';
    $('#sensor-visual-state').textContent='LISTENING';
    message('Microphone active. Keep this page open and in the foreground.');
    status('LISTENING',true);
    fetchConfig();
  } catch(error) {
    message(`Could not start microphone: ${error.message}`);
    stop();
  }
}
function stop() {
  listening=false;
  if(recording){recording=null;}
  if(processor) processor.disconnect();
  if(source) source.disconnect();
  if(monitorGain) monitorGain.disconnect();
  if(stream) stream.getTracks().forEach(track=>track.stop());
  if(context) context.close();
  processor=null;source=null;monitorGain=null;stream=null;context=null;
  document.body.classList.remove('listening');
  $('#listen-button').innerHTML='START LISTENING <span>↗</span>';
  $('#sensor-visual-state').textContent='READY';
  status('PAIRED',true);
  message('Listening stopped. Tap to resume.');
  if(sensorId && token) fetchConfig();
}
$('#listen-button').addEventListener('click',()=>listening?stop():start());
if(!sensorId||!token){status('UNPAIRED',false);message('Open the pairing link copied from the Mac dashboard.');}
else{fetchConfig();setInterval(fetchConfig,5000);}
