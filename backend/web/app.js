const API = "";                       // same origin as this page
const $ = (s, r=document) => r.querySelector(s);
const state = {};                     // per-device UI state

function toast(msg){
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(()=>t.classList.remove("show"), 1600);
}
async function api(path, opts){
  const r = await fetch(API + path, opts);
  if(!r.ok){
    const t = await r.text();
    let msg = t;
    try{ const j = JSON.parse(t); if(j && typeof j.detail === "string") msg = j.detail; }catch(_){}
    throw new Error((msg || String(r.status)).slice(0, 240));
  }
  return r.status === 204 ? null : r.json();
}
// "45 s" under a minute, "2:30" above
function fmtLen(s){ s = Math.round(+s || 0); return s < 60 ? s + " s" : Math.floor(s/60) + ":" + String(s%60).padStart(2,"0"); }
function hexToRgb(h){
  const n = parseInt(h.slice(1),16);
  return [(n>>16)&255,(n>>8)&255,n&255];
}
function rgbToHex([r,g,b]){
  return "#" + [r,g,b].map(v=>v.toString(16).padStart(2,"0")).join("");
}
// Poller wrapper: skip a tick if the previous request is still in flight (no
// pile-up, no out-of-order responses) or the tab is in the background.
function guarded(fn){
  let busy = false;
  return async ()=>{ if(busy || document.hidden) return; busy = true;
    try{ await fn(); } finally { busy = false; } };
}
function debounce(fn, ms){
  let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); };
}

// ---- health polling ----
async function poll(){
  try{
    const h = await api("/api/health");
    const f = h.fpp || {};
    $("#hdot").className = "dot " + (f.ok ? "ok" : "bad");
    $("#hlabel").textContent = f.ok ? "online" : "FPP offline";
    $("#s-mode").textContent = f.ok ? (f.mode || "—") : "offline";
    $("#s-out").textContent = f.outputs_enabled ? "ready" : "off";
    $("#s-dev").textContent = h.devices ?? "—";
    const w = (f.warnings||[]).filter(x=>!/send data to myself/i.test(x));
    $("#s-warn").textContent = w.length ? "⚠ " + w.join(" · ") : "";
  }catch(e){
    $("#hdot").className = "dot bad";
    $("#hlabel").textContent = "unreachable";
    $("#s-mode").textContent = "—";
  }
}

// ---- send helpers ----
// Per-fixture single-flight: at most one request in flight per fixture and the
// latest value always wins. (Was one debounce shared by all fixtures: touching
// fixture B within 110 ms silently dropped fixture A's update.)
const _fxq = {};
function sendFixture(id){
  const q = _fxq[id] || (_fxq[id] = {busy:false, dirty:false, t:null});
  q.dirty = true;
  if(q.busy || q.t) return;
  q.t = setTimeout(()=>{ q.t = null; _pumpFixture(id); }, 60);   // coalesce a drag
}
async function _pumpFixture(id){
  const q = _fxq[id]; if(q.busy) return;
  q.busy = true;
  try{
    while(q.dirty){
      q.dirty = false;
      const s = state[id];
      try{
        await api(`/api/devices/${id}/set`, {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ rgb:s.rgb, brightness:s.brightness, strobe_hz:s.strobe||null })
        });
        s.on = true; renderOnState(id); setManual(id, true);
      }catch(e){ toast("Error: "+e.message); break; }
    }
  } finally { q.busy = false; }
}
async function releaseFixture(id){
  try{ await api(`/api/devices/${id}/release`, {method:"POST"});
       if(state[id]){ state[id].on=false; renderOnState(id); }
       setManual(id, false); toast("Back to the scene"); }
  catch(e){ toast("Error: "+e.message); }
}
// A Live-card setting overrides the running scene for that fixture until you
// press Release. The server tells us which fixtures are overridden (play/state).
function setManual(id, on){ const c = $("#c_"+id); if(c) c.classList.toggle("manual", !!on); }
function renderOverrides(list){
  const set = new Set(list||[]);
  for(const id in state) setManual(id, set.has(id));
}

async function fixtureOff(id){
  try{ await api(`/api/devices/${id}/off`, {method:"POST"});
       if(state[id]){ state[id].on=false; renderOnState(id); setManual(id, true); }
       toast(state[id] ? "Off (overriding the scene until Release)" : "Fog burst cancelled"); }
  catch(e){ toast("Error: "+e.message); }
}

const PRESETS = [
  ["Blood", "#c8003c"], ["Ember", "#ff5a1e"], ["Toxic", "#39ff14"],
  ["Phantom", "#8b2fff"], ["Ice", "#2ea6ff"], ["Bone", "#fff4d6"],
];

function lightCard(dev){
  state[dev.id] = { rgb: hexToRgb("#8b2fff"), brightness: 200, strobe: 0, on:false };
  const el = document.createElement("div"); el.className = "card"; el.id = "c_"+dev.id;
  el.innerHTML = `
    <div class="row">
      <div class="swatch" id="sw_${dev.id}"></div>
      <div class="grow"><div class="name">${dev.name}<span class="mbadge" title="Overriding the running scene">Manual</span></div>
        <div class="sub">DMX ${dev.dmx_address} · ${dev.profile_id}</div></div>
      <input type="color" id="col_${dev.id}" value="#8b2fff">
    </div>
    <label>Brightness <span class="muted" id="bl_${dev.id}">78%</span></label>
    <input type="range" id="br_${dev.id}" min="0" max="255" value="200">
    <label>Strobe <span class="muted" id="stl_${dev.id}">off</span></label>
    <input type="range" id="st_${dev.id}" min="0" max="20" step="1" value="0">
    <div class="chips">${PRESETS.map(([n,h])=>
      `<button class="chip ghost" style="border-color:${h}66;color:${h}" onclick="preset('${dev.id}','${h}')">${n}</button>`).join("")}</div>
    <div class="btns">
      <button class="primary" onclick="sendFixture('${dev.id}')">Apply</button>
      <button class="ghost" onclick="fixtureOff('${dev.id}')">Off</button>
      <button class="ghost relbtn" onclick="releaseFixture('${dev.id}')" title="Hand this light back to the scene">Release</button>
    </div>`;
  $("#lights").appendChild(el);
  const swatch = ()=>{ $("#sw_"+dev.id).style.background = rgbToHex(state[dev.id].rgb); };
  swatch();
  $("#col_"+dev.id).addEventListener("input", e=>{
    state[dev.id].rgb = hexToRgb(e.target.value); swatch(); sendFixture(dev.id); });
  $("#br_"+dev.id).addEventListener("input", e=>{
    state[dev.id].brightness = +e.target.value;
    $("#bl_"+dev.id).textContent = Math.round(e.target.value/255*100)+"%"; sendFixture(dev.id); });
  $("#st_"+dev.id).addEventListener("input", e=>{
    state[dev.id].strobe = +e.target.value;
    $("#stl_"+dev.id).textContent = e.target.value>0 ? e.target.value+" Hz" : "off";
    sendFixture(dev.id); });
}
function preset(id, hex){
  state[id].rgb = hexToRgb(hex); $("#col_"+id).value = hex;
  $("#sw_"+id).style.background = hex; sendFixture(id);
}
function renderOnState(id){
  const c = $("#c_"+id); if(c) c.style.borderColor = state[id].on ? rgbToHex(state[id].rgb) : "var(--line)";
}

function fogCard(dev){
  const el = document.createElement("div"); el.className="card";
  el.innerHTML = `
    <div class="row">
      <div class="grow"><div class="name">${dev.name}</div>
        <div class="sub">DMX ${dev.dmx_address} · bursts auto-stop at the safety cap</div></div>
    </div>
    <label>Burst length <span class="muted" id="fgl_${dev.id}">3.0 s</span></label>
    <input type="range" id="fg_${dev.id}" min="0.5" max="8" step="0.5" value="3">
    <div class="btns">
      <button class="primary" onclick="fogBurst('${dev.id}')">💨 Fog Burst</button>
      <button class="ghost" onclick="fixtureOff('${dev.id}')">Off</button>
    </div>`;
  $("#fog").appendChild(el);
  $("#fg_"+dev.id).addEventListener("input", e=>{
    $("#fgl_"+dev.id).textContent = (+e.target.value).toFixed(1)+" s"; });
}
async function fogBurst(id){
  const secs = +$("#fg_"+id).value;
  try{ const r = await api(`/api/devices/${id}/fog`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({seconds:secs})});
    toast(`Fog on ${r.seconds}s`); }
  catch(e){ toast("Error: "+e.message); }
}
async function stopAll(){
  if(window.simStop) simStop();
  stopAudio();
  try{ await api("/api/stop",{method:"POST"});
    for(const id in state){ state[id].on=false; renderOnState(id); setManual(id, false); }
    toast("STOPPED — all off"); }
  catch(e){ toast("STOP failed: "+e.message); }
}

// ---- yard lights (static solar RGB spotlights: record what they're set to) ----
// The physical light has 8 fixed colours + 1 auto-cycle mode; we mirror that.
const YARD_PRESETS = [
  ["Red","#ff0000"], ["Green","#00e01e"], ["Blue","#0a3cff"], ["Yellow","#ffd400"],
  ["Cyan","#00e5ff"], ["Magenta","#ff20c8"], ["Purple","#9a2bff"], ["White","#fff4e0"],
];
const yardState = {};
function yardCard(dev){
  const cfg = yardState[dev.id] = {
    on: dev.static_on !== false,
    color: (dev.static_color || "#ff3030").toLowerCase(),
    mode: dev.static_mode === "auto" ? "auto" : "fixed",
    brightness: dev.static_brightness == null ? 1 : +dev.static_brightness,
  };
  const el = document.createElement("div"); el.className = "card"; el.id = "y_"+dev.id;
  el.innerHTML = `
    <div class="row">
      <div class="swatch" id="ysw_${dev.id}"></div>
      <div class="grow"><div class="name">${dev.name}</div>
        <div class="sub">Solar RGB spotlight — set this to match the light in the yard</div></div>
      <label class="switch" title="On / off"><input type="checkbox" id="yon_${dev.id}"><span class="track"></span></label>
    </div>
    <label>Color mode</label>
    <div class="chips" id="ychips_${dev.id}">${YARD_PRESETS.map(([n,h])=>
      `<button class="chip ghost ypre" data-h="${h}" title="${n}" style="border-color:${h}" onclick="yardPick('${dev.id}','${h}')">${n}</button>`).join("")}
      <button class="chip ghost yauto" id="yauto_${dev.id}" onclick="yardAuto('${dev.id}')">🌈 Auto cycle</button></div>
    <label>Brightness <span class="muted" id="ybl_${dev.id}">${Math.round(cfg.brightness*100)}%</span></label>
    <input type="range" id="ybr_${dev.id}" min="0" max="100" value="${Math.round(cfg.brightness*100)}">`;
  $("#yardlights").appendChild(el);
  $("#yon_"+dev.id).checked = cfg.on;
  $("#yon_"+dev.id).addEventListener("change", e=>{ cfg.on = e.target.checked; renderYard(dev.id); saveStatic(dev.id); });
  $("#ybr_"+dev.id).addEventListener("input", e=>{
    cfg.brightness = (+e.target.value)/100; $("#ybl_"+dev.id).textContent = e.target.value+"%"; saveStatic(dev.id); });
  renderYard(dev.id);
}
function renderYard(id){
  const cfg = yardState[id]; if(!cfg) return;
  const sw = $("#ysw_"+id);
  if(sw){ sw.style.background = cfg.mode==="auto"
    ? "conic-gradient(from 0deg,#ff0000,#ffd400,#00e01e,#00e5ff,#0a3cff,#9a2bff,#ff0000)"
    : cfg.color; sw.style.opacity = cfg.on ? "1" : "0.3"; }
  const card = $("#y_"+id); if(card) card.style.borderColor = cfg.on ? (cfg.mode==="auto"?"var(--accent)":cfg.color) : "var(--line)";
  document.querySelectorAll("#ychips_"+id+" .ypre").forEach(b=>{
    const on = cfg.mode==="fixed" && b.dataset.h.toLowerCase()===cfg.color;
    b.style.background = on ? b.dataset.h : "transparent";
    b.style.color = on ? "#0b0b0f" : b.dataset.h;
    b.classList.toggle("sel", on); });
  const au = $("#yauto_"+id); if(au){ au.classList.toggle("sel", cfg.mode==="auto");
    au.style.background = cfg.mode==="auto" ? "var(--accent)" : "transparent";
    au.style.color = cfg.mode==="auto" ? "var(--accent-ink)" : "var(--text)"; }
}
function yardPick(id, hex){ const cfg=yardState[id]; if(!cfg) return; cfg.mode="fixed"; cfg.color=hex.toLowerCase(); renderYard(id); saveStatic(id); }
function yardAuto(id){ const cfg=yardState[id]; if(!cfg) return; cfg.mode="auto"; renderYard(id); saveStatic(id); }
const saveStatic = debounce(async (id)=>{
  const cfg = yardState[id]; if(!cfg) return;
  try{
    await api(`/api/devices/${id}/static`, {method:"PUT", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({static_color:cfg.color, static_mode:cfg.mode, static_brightness:cfg.brightness, static_on:cfg.on})});
    if(typeof window.stageReloadDevices === "function") window.stageReloadDevices();
  }catch(e){ toast("Yard light save failed: "+e.message); }
}, 160);

// ---- boot ----
// ---- global output guards + top signal strip ----
let OUTPUT = {fog_enabled:true, audio_muted:false, volume:100, duck_db:9, light_delay_ms:0};
let SIGDEVS = [];
const SIGEL = {};   // device id -> {pill, sw, fog}
let PRESEL = null;  // presence-sensor pill refs {pill, sw, lab}

function shortLabel(name){ return ((name||"").trim().split(/\s+/)[0]||"?").toUpperCase().slice(0,5); }

function buildSignalStrip(){
  const strip = $("#sigstrip"); if(!strip) return;
  strip.innerHTML = ""; for(const k in SIGEL) delete SIGEL[k];
  // presence sensor pill (LD2410) — always first, live from /api/play/state
  {
    const pill = document.createElement("div");
    pill.className = "sigpill presence nosensor";
    pill.title = "Presence sensor (LD2410)";
    const sw = document.createElement("span"); sw.className = "sigsw";
    const lab = document.createElement("span"); lab.textContent = "SENSOR";
    pill.append(sw, lab); strip.appendChild(pill);
    PRESEL = {pill, sw, lab};
  }
  SIGDEVS.forEach(d=>{
    const pill = document.createElement("div");
    pill.className = "sigpill" + (d.kind==="dmx_fog" ? " fog" : "");
    const sw = document.createElement("span"); sw.className = "sigsw";
    const lab = document.createElement("span"); lab.textContent = shortLabel(d.name);
    pill.append(sw, lab); strip.appendChild(pill);
    SIGEL[d.id] = {pill, sw, fog: d.kind==="dmx_fog"};
  });
}

function renderSignal(st){
  const devs = (st && st.devices) || {};
  SIGDEVS.forEach(d=>{
    const el = SIGEL[d.id]; if(!el) return;
    const s = devs[d.id];
    if(el.fog){
      const firing = !!(s && (s.fog||0) > 0);
      el.pill.classList.toggle("firing", firing);
      el.pill.classList.toggle("suppressed", firing && !OUTPUT.fog_enabled);
      el.pill.classList.toggle("on", firing);
    } else {
      const bri = s ? (s.brightness||0) : 0, on = bri > 6, hex = s ? s.hex : "#000000";
      el.sw.style.background = on ? hex : "#090b10";
      el.sw.style.boxShadow = (on && bri > 40) ? ("0 0 7px " + hex) : "none";
      el.pill.classList.toggle("on", on);
      el.pill.classList.toggle("strobe", !!(s && s.strobe));
    }
  });
  if(PRESEL){
    const pr = (st && st.presence) || null;
    const connected = !!(pr && pr.connected);
    const active = !!(pr && pr.active);
    const armed = !!(pr && pr.armed);
    const off = !!(pr && pr.enabled === false);      // reading, but not allowed to fire
    PRESEL.pill.classList.toggle("nosensor", !connected);
    PRESEL.pill.classList.toggle("clear", connected && !active);
    PRESEL.pill.classList.toggle("present", connected && active);
    PRESEL.pill.classList.toggle("armed", armed && !off);
    PRESEL.pill.classList.toggle("off", connected && off);
    PRESEL.lab.textContent = !connected ? "SENSOR" : off ? "OFF" : (active ? "PRESENT" : "CLEAR");
    PRESEL.pill.title = !connected ? "Presence sensor (LD2410)"
      : "Presence sensor: " + (active ? "PRESENT" : "CLEAR") + (off ? " · triggers off (only the button fires)" : "");
  }
}

function updateFogBanner(){ const b = $("#fogBanner"); if(b) b.hidden = !!OUTPUT.fog_enabled; }
function applyOutputUi(){
  const fe = $("#fogEnabled"), ae = $("#audioEnabled");
  if(fe) fe.checked = !!OUTPUT.fog_enabled;
  if(ae) ae.checked = !OUTPUT.audio_muted;
  const vv = $("#volume");
  if(vv && OUTPUT.volume != null && !_vol.dragging && !_vol.busy){ vv.value = OUTPUT.volume; _volLabel(OUTPUT.volume); }
  const dv = $("#duck");
  if(dv && OUTPUT.duck_db != null && !_duck.dragging && !_duck.busy){ dv.value = OUTPUT.duck_db; _duckLabel(OUTPUT.duck_db); }
  const lg = $("#lag");
  if(lg && OUTPUT.light_delay_ms != null && !_lag.dragging && !_lag.busy){ lg.value = OUTPUT.light_delay_ms; _lagLabel(OUTPUT.light_delay_ms); }
  updateFogBanner();
}
async function loadOutput(){ try{ OUTPUT = await api("/api/output"); applyOutputUi(); }catch(e){} }
async function setOutput(){
  const body = {fog_enabled: $("#fogEnabled").checked, audio_muted: !$("#audioEnabled").checked};
  try{
    OUTPUT = await api("/api/output",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    applyOutputUi();
    toast((OUTPUT.fog_enabled?"Fog output on":"Fog output off") + " · " + (OUTPUT.audio_muted?"sound off":"sound on"));
  }catch(e){ toast("Save failed: "+e.message); loadOutput(); }
}

// ---- master volume (everything the Pi plays) ----
// One request in flight at a time; the latest slider value always wins.
const _vol = {busy:false, dirty:false, val:null, dragging:false};
function _volLabel(v){ const l = $("#volVal"); if(l) l.textContent = v + "%"; }
async function _pumpVolume(){
  if(_vol.busy) return; _vol.busy = true;
  try{
    while(_vol.dirty){
      _vol.dirty = false;
      try{ OUTPUT = await api("/api/output", {method:"POST", headers:{"Content-Type":"application/json"},
                                               body: JSON.stringify({volume:_vol.val})}); }
      catch(e){ toast("Volume failed: "+e.message); break; }
    }
  } finally { _vol.busy = false; }
}
function onVolume(e){ _vol.val = +e.target.value; _vol.dirty = true; _volLabel(_vol.val); _pumpVolume(); }
// ---- voice ducking depth (same single-flight pattern as volume) ----
const _duck = {busy:false, dirty:false, val:null, dragging:false};
function _duckLabel(v){ const l = $("#duckVal"); if(l) l.textContent = (+v > 0) ? (v + " dB") : "Off"; }
async function _pumpDuck(){
  if(_duck.busy) return; _duck.busy = true;
  try{
    while(_duck.dirty){
      _duck.dirty = false;
      try{ OUTPUT = await api("/api/output", {method:"POST", headers:{"Content-Type":"application/json"},
                                               body: JSON.stringify({duck_db:_duck.val})}); }
      catch(e){ toast("Ducking failed: "+e.message); break; }
    }
  } finally { _duck.busy = false; }
}
function onDuck(e){ _duck.val = +e.target.value; _duck.dirty = true; _duckLabel(_duck.val); _pumpDuck(); }
(function(){
  const d = $("#duck"); if(!d) return;
  d.addEventListener("input", onDuck);
  d.addEventListener("pointerdown", ()=>{ _duck.dragging = true; });
  ["pointerup", "pointercancel", "change"].forEach(ev => d.addEventListener(ev, ()=>{ _duck.dragging = false; }));
})();
// ---- light timing (same single-flight pattern; applies live, even mid sync test) ----
const _lag = {busy:false, dirty:false, val:null, dragging:false};
function _lagLabel(v){ const l = $("#lagVal"); v = +v || 0;
  if(l) l.textContent = v === 0 ? "0 ms" : (v > 0 ? "+" + v + " ms (lights later)" : "\u2212" + (-v) + " ms (lights earlier)"); }
async function _pumpLag(){
  if(_lag.busy) return; _lag.busy = true;
  try{
    while(_lag.dirty){
      _lag.dirty = false;
      try{ OUTPUT = await api("/api/output", {method:"POST", headers:{"Content-Type":"application/json"},
                                               body: JSON.stringify({light_delay_ms:_lag.val})}); }
      catch(e){ toast("Light timing failed: "+e.message); break; }
    }
  } finally { _lag.busy = false; }
}
function onLag(e){ _lag.val = +e.target.value; _lag.dirty = true; _lagLabel(_lag.val); _pumpLag(); }
(function(){
  const d = $("#lag"); if(!d) return;
  d.addEventListener("input", onLag);
  d.addEventListener("pointerdown", ()=>{ _lag.dragging = true; });
  ["pointerup", "pointercancel", "change"].forEach(ev => d.addEventListener(ev, ()=>{ _lag.dragging = false; }));
})();
async function syncTest(){
  const b = $("#syncTest"); if(b && b.disabled) return;
  try{
    const r = await api("/api/output/sync-test", {method:"POST"});
    const d = SIGDEVS.find(x=>x.id===r.light);
    toast("Sync test: watch " + (d && d.name ? d.name : "the light") + ", listen for the click");
    if(b){ b.disabled = true; b.textContent = "⏱ Testing…"; setTimeout(()=>{ b.disabled = false; b.textContent = "⏱ Sync test"; }, (r.seconds||12) * 1000); }
  }catch(e){ toast("Sync test: " + e.message); }
}
// ---- fog test (Output panel) ----
// A short burst through the same capped, self-expiring path as the Live card's
// Fog Burst. The server refuses it while Fog output is off, so a stale toggle
// here can never fire the machine.
let FOG_IDS = [];
const FOG_TEST_S = 2;
let _fogTesting = false;
async function testFog(){
  if(_fogTesting) return;
  if(!FOG_IDS.length){ toast("No fog device set up"); return; }
  const b = $("#fogTest");
  _fogTesting = true; if(b){ b.disabled = true; b.textContent = "💨 Fogging…"; }
  try{
    const rs = await Promise.all(FOG_IDS.map(id => api(`/api/devices/${id}/fog`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({seconds:FOG_TEST_S})})));
    toast(`Test fog: ${rs[0].seconds}s burst`);
    await new Promise(r => setTimeout(r, FOG_TEST_S * 1000));   // button stays busy while it fogs
  }catch(e){
    const m = e.message || "";
    toast(/disabled/.test(m) ? "Fog output is off: turn it on first" : "Fog test failed: " + m);
  }finally{
    _fogTesting = false; if(b){ b.disabled = false; b.textContent = "💨 Test"; }
  }
}
async function testSound(){
  try{ const r = await api("/api/output/test-sound", {method:"POST"}); toast("Test sound at " + r.volume + "%"); }
  catch(e){
    const m = e.message || "";
    toast(/sound is off/.test(m) ? "Sound is off: turn it on first" :
          /not available/.test(m) ? "Pi audio isn't ready" : "Test failed: " + m);
  }
}
(function(){
  const v = $("#volume"); if(!v) return;
  v.addEventListener("input", onVolume);
  v.addEventListener("pointerdown", ()=>{ _vol.dragging = true; });
  ["pointerup", "pointercancel", "change"].forEach(ev => v.addEventListener(ev, ()=>{ _vol.dragging = false; }));
})();

async function boot(){
  try{
    const devs = await api("/api/devices");
    devs.filter(d=>d.kind==="dmx_fixture").forEach(lightCard);
    const fogs = devs.filter(d=>d.kind==="dmx_fog");
    FOG_IDS = fogs.filter(d=>d.enabled !== false).map(d=>d.id);
    if(fogs.length) fogs.forEach(fogCard); else $("#fog").innerHTML='<div class="card muted">No fog device.</div>';
    const yard = devs.filter(d=>d.kind==="static_rgb");
    if(yard.length) yard.forEach(yardCard);
    else $("#yardlights").innerHTML='<div class="card muted">No yard lights yet.</div>';
    SIGDEVS = devs.filter(d=>d.kind==="dmx_fixture"||d.kind==="dmx_fog")
                  .sort((a,b)=>(a.dmx_address||0)-(b.dmx_address||0));
    buildSignalStrip();
  }catch(e){ toast("Load failed: "+e.message); }
  loadOutput();
  poll(); setInterval(guarded(poll), 3000);
}
boot();

// ---- tabs ----
function go(tab){
  if(window.__editing && tab!=="editor"){ window.__editing=false; if(window.__restoreStage) window.__restoreStage(); }
  if(tab!=="library" && typeof libStopPreview === "function") libStopPreview();
  document.querySelectorAll(".view").forEach(v=>v.classList.toggle("active", v.id==="view-"+tab));
  document.querySelectorAll(".nav button").forEach(b=>b.setAttribute("aria-selected", String(b.dataset.tab===tab)));
  if(tab==="library" && typeof loadLibrary === "function") loadLibrary();
  if(tab==="system") loadSys(true);
  if(tab!=="editor"){ try{ localStorage.setItem("hd_tab", tab); }catch(e){} }
}

// ---- scenes ----
let SCENES = [];
async function loadScenes(){
  try{ SCENES = await api("/api/scenes"); renderScenes(); }
  catch(e){ $("#scenes").innerHTML = '<div class="card muted">Could not load scenes.</div>'; }
}
function renderScenes(){
  const host = $("#scenes"); host.innerHTML = "";
  if(!SCENES.length){ host.innerHTML = '<div class="card muted">No scenes yet.</div>'; return; }
  SCENES.forEach((s,i)=>{
    const el = document.createElement("div"); el.className = "card scenecard" + (s.enabled===false?" disabled":""); el.id = "sc_"+s.id;
    el.innerHTML = `
      <div class="cardhead">
        <div class="grow"><div class="name">${s.name} ${s.audio&&s.audio.music?'<span title="has music">🔊</span>':''}${s.audio&&s.audio.voiceover?'<span title="has voice-over">🗣</span>':''}</div>
        ${s.blurb?`<div class="sub">${s.blurb}</div>`:''}
        <div class="sub">${s.duration_s}s · ${s.devices.length} fixtures</div></div>
        <button class="iconbtn" title="Move up" onclick="reorderScene('${s.id}',-1)" ${i===0?'disabled':''}>↑</button>
        <button class="iconbtn" title="Move down" onclick="reorderScene('${s.id}',1)" ${i===SCENES.length-1?'disabled':''}>↓</button>
        <button class="iconbtn" title="Watch full screen" aria-label="Watch full screen" onclick="fsOpen('${s.id}',{autoplay:true})">⛶</button>
        <button class="iconbtn" title="Edit scene + timeline" onclick="enterEditor('${s.id}')">✎</button>
      </div>
      <div class="sw">${s.devices.map(d=>`<i id="sw_${s.id}_${d}"></i>`).join("")}</div>
      <div class="progress"><i id="pg_${s.id}"></i></div>
      <div class="btns">
        <button class="primary" onclick="playScene('${s.id}',false)">▶ Play</button>
        <button class="ghost" onclick="playScene('${s.id}',true)">↻ Loop</button>
        <button class="ghost" onclick="stopScene()">■ Stop</button>
      </div>`;
    host.appendChild(el);
  });
}
async function playScene(id, loop){
  const s = SCENES.find(x=>x.id===id);
  if(window.playMode && playMode()==="sim"){        // this browser only: the yard isn't touched
    try{ const full = await api(`/api/scenes/${id}`);
         simPlay(full, {loop});
         toast((loop?"Looping ":"Playing ") + (full.name||id) + " in the simulator"); }
    catch(e){ toast("Error: "+e.message); }
    return;
  }
  if(window.simStop) simStop();
  try{ await api(`/api/scenes/${id}/play`, {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({loop})});
       if(s) playAudio(s, loop);      // browser plays the score in sync with the rig
       toast((loop?"Looping ":"Playing ") + (s?.name||id)); }
  catch(e){ toast("Error: "+e.message); }
}
async function stopScene(){
  if(window.playMode && playMode()==="sim"){ simStop(); toast("Stopped"); return; }   // never the yard's scene
  if(window.simStop) simStop();
  stopAudio();
  try{ await api("/api/play/stop", {method:"POST"}); toast("Stopped"); }
  catch(e){ toast("Error: "+e.message); }
}

// ---- scene audio (score + voice-over), played by the phone in sync with the rig ----
const AUDIO = {music:null, vo:null, voTimer:null, scene:null, confirmed:false, muteSet:[], layers:[]};
function stopAudio(){
  [AUDIO.music, AUDIO.vo].forEach(a=>{ try{ if(a){ a.pause(); a.src=""; } }catch(e){} });
  (AUDIO.layers||[]).forEach(L=>{ try{ clearTimeout(L.timer); if(L.el){ L.el.pause(); L.el.src=""; } }catch(e){} });
  clearTimeout(AUDIO.voTimer);
  AUDIO.music = AUDIO.vo = null; AUDIO.layers = []; AUDIO.scene = null; AUDIO.confirmed = false;
}
function playAudio(s, loop, mute, fromT){
  stopAudio();
  const a = s.audio; if(!a) return;
  fromT = +fromT || 0;                 // start position (seconds) for seek
  AUDIO.muteSet = mute ? (Array.isArray(mute) ? mute.slice() : [...mute]) : [];
  AUDIO.scene = s.id;
  if(a.music){
    AUDIO.music = new Audio("/audio/" + a.music);
    AUDIO.music.volume = a.gain ?? 1; AUDIO.music.loop = true;  // 30s score loops under a 90s scene
    AUDIO.music.muted = AUDIO.muteSet.indexOf("music") >= 0;
    if(fromT > 0){ AUDIO.music.addEventListener("loadedmetadata", ()=>{
      try{ if(isFinite(AUDIO.music.duration) && AUDIO.music.duration>0) AUDIO.music.currentTime = fromT % AUDIO.music.duration; }catch(e){} }, {once:true}); }
    AUDIO.music.play().catch(()=>toast("Tap Play again to allow sound"));
  }
  if(a.voiceover){
    const at = a.voiceover_at_s || 0;
    if(at >= fromT){                   // only schedule if it hasn't passed the seek point
      AUDIO.voTimer = setTimeout(()=>{
        AUDIO.vo = new Audio("/audio/" + a.voiceover);
        AUDIO.vo.muted = (AUDIO.muteSet||[]).indexOf("voiceover") >= 0;
        AUDIO.vo.play().catch(()=>{});
      }, (at - fromT) * 1000);
    }
  }
  // SFX / ambient beds: mirror the Pi so the desktop preview hears them too,
  // each independently mutable by its cue id
  (a.layers || []).forEach(cue=>{
    const at = cue.at_s || 0;
    if(at >= fromT){                   // future cue: schedule relative to the seek point
      const rec = {id:cue.id, el:null, timer:null};
      rec.timer = setTimeout(()=>{
        const el = new Audio("/audio/" + cue.file);
        el.loop = !!cue.loop; el.volume = (cue.gain!=null ? cue.gain : 1);
        el.muted = (AUDIO.muteSet||[]).indexOf(cue.id) >= 0;
        el.play().catch(()=>{});
        rec.el = el;
      }, (at - fromT) * 1000);
      AUDIO.layers.push(rec);
    } else if(cue.loop){               // a bed already active at this position: start it now
      const el = new Audio("/audio/" + cue.file);
      el.loop = true; el.volume = (cue.gain!=null ? cue.gain : 1);
      el.muted = (AUDIO.muteSet||[]).indexOf(cue.id) >= 0;
      el.play().catch(()=>{});
      AUDIO.layers.push({id:cue.id, el:el, timer:null});
    }
    // one-shot already passed the seek point: skip
  });
}
// live-toggle mute on browser-side audio (music/vo/layers) for the scene being previewed
function setAudioMute(mute){
  const M = mute ? (Array.isArray(mute) ? mute : [...mute]) : [];
  AUDIO.muteSet = M;
  try{ if(AUDIO.music) AUDIO.music.muted = M.indexOf("music") >= 0; }catch(e){}
  try{ if(AUDIO.vo) AUDIO.vo.muted = M.indexOf("voiceover") >= 0; }catch(e){}
  (AUDIO.layers||[]).forEach(L=>{ try{ if(L.el) L.el.muted = M.indexOf(L.id) >= 0; }catch(e){} });
}
function dim(hex, b){
  const [r,g,bl] = hexToRgb(hex); const f = Math.max(.05, b);
  return `rgb(${Math.round(r*f)},${Math.round(g*f)},${Math.round(bl*f)})`;
}
async function pollPlay(){
  try{
    const st = await api("/api/play/state");
    window.__playing = !!st.playing;
    window.__playScene = st.playing ? st.scene : null;
    if(window.simSetArmed) simSetArmed(!!(st.presence && st.presence.armed));
    window.__liveClock = {scene: st.scene, t: +st.t || 0, at: performance.now(), playing: !!st.playing};   // the full-screen view follows this
    const simOn = !!(window.simActive && simActive());   // the simulator owns the stage and this device's sound
    renderSignal(st);
    renderOverrides(st.overrides);
    // when the scene ends server-side, stop its audio too (guard the startup race)
    if(AUDIO.scene && !simOn){
      if(st.playing && st.scene===AUDIO.scene) AUDIO.confirmed = true;
      else if(AUDIO.confirmed) stopAudio();
    }
    // while the user is dragging the playhead, let the local scrub preview drive the stage
    if(window.stageUpdate && !simOn && !window.__fsOpen && !(window.__editing && !st.playing) && !window.__scrubbing){
      const s2 = (window.__editing && window.__stripMuted) ? window.__stripMuted(st) : st;
      window.stageUpdate(s2, SCENES.find(s=>s.id===st.scene));
    }
    if(window.__editing && st.playing && window.__edtSync && !simOn) window.__edtSync(st.t);
    $("#s-play").textContent = st.playing ? (SCENES.find(s=>s.id===st.scene)?.name || st.scene) : "Manual";
    SCENES.forEach(s=>{
      if(simOn && window.__simScene===s.id) return;    // the simulator draws its own progress
      const on = st.playing && st.scene===s.id;
      const card = $("#sc_"+s.id); if(card) card.classList.toggle("playing", on);
      const pg = $("#pg_"+s.id); if(pg) pg.style.width = on ? Math.min(100, st.t/s.duration_s*100)+"%" : "0";
      if(!on) s.devices.forEach(d=>{ const sw=$(`#sw_${s.id}_${d}`); if(sw) sw.style.background="var(--surface3)"; });
    });
    if(st.playing && st.devices){
      for(const [d,info] of Object.entries(st.devices)){
        const sw = $(`#sw_${st.scene}_${d}`);
        if(sw) sw.style.background = info.strobe ? "#fff" : (info.fog>40 ? "var(--fogc)" : dim(info.hex, info.brightness/255));
      }
    }
  }catch(e){}
}

// ---- audio lists (shared with the timeline editor) + scene reorder ----
let AUDIOLIST = {music:[], voice:[]};
async function loadAudioLists(){
  try{ AUDIOLIST = await api("/api/audio"); }catch(e){ AUDIOLIST = {music:[], voice:[]}; }
}
async function reorderScene(id, dir){
  const i = SCENES.findIndex(s=>s.id===id); const j = i + dir;
  if(i<0 || j<0 || j>=SCENES.length) return;
  const a = SCENES[i], b = SCENES[j];
  try{
    await api(`/api/scenes/${a.id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body: JSON.stringify({order:j})});
    await api(`/api/scenes/${b.id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body: JSON.stringify({order:i})});
    await loadScenes();
  }catch(e){ toast("Reorder failed: " + e.message); }
}

// ---- generate a scene from a description (LLM authoring) ----
function toggleGen(){
  const p = $("#genPanel"); if(!p) return;
  const show = p.style.display === "none";
  p.style.display = show ? "" : "none";
  if(show){ const d = $("#genDesc"); if(d) d.focus(); }
}
function genDurLabel(){ const r = $("#genDur"), v = $("#genDurVal"); if(r && v) v.textContent = fmtLen(r.value); }
function songToggle(){
  const on = !!($("#genBeat") && $("#genBeat").checked);
  ["#songHint", "#songRow"].forEach(id => { const el = $(id); if(el) el.style.display = on ? "" : "none"; });
  const r = $("#genDur");
  if(on && r && +r.value <= 30){ r.value = 300; genDurLabel(); }        // default to the whole song
}
async function generateScene(){
  const desc = ($("#genDesc").value || "").trim();
  const dur = +($("#genDur").value || 30);
  const flow = $("#genFlow").value || "flat";
  const yt = ($("#genYt") ? $("#genYt").value : "").trim();
  const beat = !!($("#genBeat") && $("#genBeat").checked);
  const stat = $("#genStat"), btn = $("#genGo");
  if(!desc && !beat){ stat.textContent = "Describe the scene first."; $("#genDesc").focus(); return; }
  if(beat && !yt){ stat.textContent = "A song show needs a YouTube link to the song."; $("#genYt").focus(); return; }
  btn.disabled = true;
  const parts = Math.ceil(dur / 60);
  stat.textContent = beat ? "Downloading the song, finding the beat and the band, reading the lyrics… (about a minute)."
                    : parts > 1 ? "Writing a " + fmtLen(dur) + " scene in " + parts + " parts" + (yt ? " while the song downloads" : "") + "… (about a minute)."
                    : yt  ? "Generating the scene and clipping the audio… (this can take up to a minute)."
                          : "Generating… the model is composing your scene (a few seconds).";
  try{
    const body = {description:desc, duration_s:dur, intensity_flow:flow};
    if(yt) body.youtube_url = yt;
    if(beat){ body.song_show = true; const sg = ($("#genSong") ? $("#genSong").value : "").trim(); if(sg) body.song = sg; }
    const r = await api("/api/generate", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    let msg;
    if(r.song){ msg = "Built “" + (r.name||"song") + "”: ~" + Math.round(r.tempo||0) + " BPM, " + r.song.sections + " sections, "
        + (r.song.lyrics ? "colors from the lyrics" : "colors from the music (no timed lyrics found)")
        + (r.song.whole_song ? "" : "; cut at " + fmtLen((r.audio&&r.audio.dur)||dur)) + "."; }
    else if(r.tempo){ msg = "Locked “" + (r.name||"scene") + "” to ~" + Math.round(r.tempo) + " BPM — " + (r.events||0) + " beat-synced events."; }
    else {
      msg = "Created “" + (r.name||"scene") + "” — " + (r.events||0) + " events across " + ((r.devices||[]).length) + " fixtures.";
      if(r.audio && r.audio.file) msg += " Audio clipped (" + (r.audio.via==="captions" ? "matched to captions" : "best passage")
        + (r.audio.dur && r.audio.dur < dur - 1 ? "; the song is only " + fmtLen(r.audio.dur) + ", so it loops" : "") + ").";
      else if(r.audio && r.audio.error) msg += " (Audio import failed: " + r.audio.error + ".)";
    }
    stat.textContent = msg + " Opening…";
    await loadScenes();
    toast("Generated: " + (r.name||"scene"));
    if(typeof enterEditor === "function") enterEditor(r.id);
  }catch(e){ stat.textContent = "Generation failed: " + e.message; }
  btn.disabled = false;
}

// ---- sound library manager ----
const LIB_CATS = {music:"Music", sfx:"Ambience & SFX", voice:"Voice"};
let LIB = {music:[], sfx:[], voice:[], dur:{}};
let _libAudio = null;
let _libReq = 0;                 // guards against out-of-order library loads

function libDur(s){
  s = Math.round(s||0); if(!s) return "";
  const m = Math.floor(s/60), ss = s%60;
  return m + ":" + String(ss).padStart(2,"0");
}
async function loadLibrary(){
  const host = $("#libList"); if(!host) return;
  const my = ++_libReq;
  let data;
  try{ data = await api("/api/audio"); }
  catch(e){ if(my===_libReq) host.innerHTML = '<div class="card muted">Could not load the library.</div>'; return; }
  if(my !== _libReq) return;          // a newer load already superseded this one
  LIB = data; renderLibrary();
}
function renderLibrary(){
  const host = $("#libList"); if(!host) return;
  host.innerHTML = "";
  let total = 0;
  ["music","sfx","voice"].forEach(cat=>{
    const items = LIB[cat] || [];
    if(!items.length) return;
    total += items.length;
    const h = document.createElement("div"); h.className = "libcat";
    h.textContent = LIB_CATS[cat] + " · " + items.length;
    host.appendChild(h);
    items.forEach(it=>host.appendChild(libRow(it, cat)));
  });
  if(!total){
    const d = document.createElement("div"); d.className = "card muted";
    d.textContent = "Library is empty — add a YouTube clip or upload a file above.";
    host.appendChild(d);
  }
}
function libRow(it, cat){
  const row = document.createElement("div"); row.className = "card librow"; row.dataset.file = it.file;
  const play = document.createElement("button");
  play.className = "iconbtn libplay"; play.title = "Preview"; play.textContent = "▶";
  play.onclick = ()=>libPreview(it.file, play, row);
  const title = document.createElement("input");
  title.type = "text"; title.className = "libtitle"; title.value = it.title || it.file;
  title.setAttribute("aria-label", "Clip name");
  title.onchange = ()=>libRename(it.file, title.value.trim(), cat, title);
  const meta = document.createElement("div"); meta.className = "sub muted libmeta";
  const dur = libDur((LIB.dur||{})[it.file]);
  meta.textContent = [ (it.source||"file"), dur, it.file ].filter(Boolean).join("  ·  ");
  const tw = document.createElement("div"); tw.className = "libtw";
  tw.appendChild(title); tw.appendChild(meta);
  const left = document.createElement("div"); left.className = "libleft";
  left.appendChild(play); left.appendChild(tw);
  const sel = document.createElement("select"); sel.className = "libcatsel"; sel.title = "Category";
  [["music","Music"],["sfx","Ambience & SFX"],["voice","Voice"]].forEach(([v,l])=>{
    const o = document.createElement("option"); o.value = v; o.textContent = l;
    if(v===cat) o.selected = true; sel.appendChild(o);
  });
  sel.onchange = ()=>libRecat(it.file, sel.value, title.value.trim());
  const del = document.createElement("button");
  del.className = "iconbtn libdel"; del.title = "Delete"; del.textContent = "🗑";
  del.style.color = "var(--danger)"; del.style.borderColor = "#4a2b2b";
  del.onclick = ()=>libDelete(it.file, it.title || it.file);
  const right = document.createElement("div"); right.className = "libright";
  right.appendChild(sel); right.appendChild(del);
  row.appendChild(left); row.appendChild(right);
  return row;
}
function libStopPreview(){
  if(_libAudio){ try{ _libAudio.pause(); }catch(e){} }
  document.querySelectorAll("#libList .libplay").forEach(b=>b.textContent="▶");
  document.querySelectorAll("#libList .librow.playing").forEach(r=>r.classList.remove("playing"));
}
function libPreview(file, btn, row){
  const playing = _libAudio && _libAudio._file===file && !_libAudio.paused;
  libStopPreview();
  if(playing){ return; }        // was playing this one → toggle off
  try{
    _libAudio = new Audio("/audio/" + encodeURIComponent(file));
    _libAudio._file = file;
    _libAudio.onended = ()=>{ btn.textContent="▶"; if(row) row.classList.remove("playing"); };
    _libAudio.onerror = ()=>{ btn.textContent="▶"; if(row) row.classList.remove("playing"); toast("Can't preview that file."); };
    _libAudio.play().then(()=>{ btn.textContent="⏸"; if(row) row.classList.add("playing"); })
                    .catch(()=>{ btn.textContent="▶"; toast("Preview blocked by the browser."); });
  }catch(e){ toast("Can't preview: " + e.message); }
}
async function libImportYt(){
  const url = ($("#libYt").value || "").trim();
  const cat = $("#libYtCat").value || "sfx";
  const stat = $("#libStat"), btn = $("#libYtGo");
  if(!/^https?:\/\//i.test(url)){ stat.textContent = "Paste a valid YouTube (or other) link first."; $("#libYt").focus(); return; }
  btn.disabled = true; stat.textContent = "Downloading the audio… (up to a minute).";
  try{
    const r = await api("/api/audio/youtube", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({url, kind:cat})});
    $("#libYt").value = "";
    stat.textContent = "Added “" + (r.title || r.file) + "” to " + LIB_CATS[cat] + ".";
    toast("Added to library");
    await loadLibrary();
    if(typeof loadAudioLists === "function") loadAudioLists();   // refresh the scene pickers too
  }catch(e){ stat.textContent = "Import failed: " + e.message; }
  btn.disabled = false;
}
async function libUpload(){
  const inp = $("#libFile"); const cat = $("#libUpCat").value || "sfx";
  const stat = $("#libStat"), btn = $("#libUpGo");
  const f = inp && inp.files && inp.files[0];
  if(!f){ stat.textContent = "Choose an audio file to upload first."; return; }
  if(f.size > 60*1024*1024){ stat.textContent = "That file is over the 60 MB limit."; return; }
  btn.disabled = true; stat.textContent = "Uploading “" + f.name + "”…";
  try{
    const qs = "?filename=" + encodeURIComponent(f.name) + "&category=" + encodeURIComponent(cat);
    const r = await api("/api/audio/upload" + qs, {method:"POST",
      headers:{"Content-Type":"application/octet-stream"}, body: f});
    inp.value = "";
    stat.textContent = "Uploaded “" + (r.title || r.file) + "” to " + LIB_CATS[cat] + ".";
    toast("Uploaded to library");
    await loadLibrary();
    if(typeof loadAudioLists === "function") loadAudioLists();
  }catch(e){ stat.textContent = "Upload failed: " + e.message; }
  btn.disabled = false;
}
async function libRename(file, title, cat, inputEl){
  if(!title){ if(inputEl) inputEl.value = file; return; }
  try{
    await api("/api/audio/register", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({file, title, category:cat, source:_libSource(file)})});
    toast("Renamed");
    if(typeof loadAudioLists === "function") loadAudioLists();
  }catch(e){ toast("Rename failed: " + e.message); await loadLibrary(); }
}
async function libRecat(file, cat, title){
  try{
    await api("/api/audio/register", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({file, title:(title||undefined), category:cat, source:_libSource(file)})});
    toast("Moved to " + LIB_CATS[cat]);
    await loadLibrary();
    if(typeof loadAudioLists === "function") loadAudioLists();
  }catch(e){ toast("Move failed: " + e.message); await loadLibrary(); }
}
function _libSource(file){
  for(const c of ["music","sfx","voice"]){
    const hit = (LIB[c]||[]).find(x=>x.file===file);
    if(hit) return hit.source || "file";
  }
  return "file";
}
async function libDelete(file, label){
  if(!confirm("Delete “" + (label||file) + "” from the library? This removes the file for good.")) return;
  libStopPreview();
  try{
    let r = await api("/api/audio/delete", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({file})});
    if(r && r.need_force){
      const names = (r.used_by||[]).join(", ");
      if(!confirm("“" + (label||file) + "” is used by: " + names + ".\nDelete it anyway? Those scenes will lose this audio.")) return;
      r = await api("/api/audio/delete", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({file, force:true})});
    }
    $("#libStat").textContent = "Deleted “" + (label||file) + "”." +
      ((r.used_by && r.used_by.length) ? " It was removed from: " + r.used_by.join(", ") + "." : "");
    toast("Deleted");
    await loadLibrary();
    if(typeof loadAudioLists === "function") loadAudioLists();
  }catch(e){ toast("Delete failed: " + e.message); }
}

// ---- show mode ----
let SHOW = {armed:false, state:"disarmed"};
let SHOW_CDDRAG = false;
let SHOW_XFDRAG = false;
let SHOW_HOLDDRAG = false;
function holdText(v){ v = +v || 0; return v <= 0 ? "off" : (v.toFixed(2).replace(/0$/,"").replace(/\.0$/,"") + " s"); }
async function loadShow(){
  try{ SHOW = await api("/api/show"); renderShow(); }catch(e){}
}
function nameOf(id){ const s = SCENES.find(x=>x.id===id); return s ? s.name : id; }
function renderShow(){
  const dot=$("#showDot"), stEl=$("#showState"), sub=$("#showSub"), armBtn=$("#armBtn");
  if(!dot) return;
  const st = SHOW.state||"disarmed";
  dot.className = "ss-dot" + (st==="scare"?" scare":st==="cooldown"?" cooldown":(SHOW.armed?" armed":""));
  const ambOn = SHOW.ambient_enabled !== false, sensOn = SHOW.sensor_enabled !== false;
  const hasSensor = SHOW.gpio_pin != null, noScares = !(SHOW.scares||[]).length;
  const label = !SHOW.armed ? "Disarmed"
    : st==="scare" ? "SCARE — "+nameOf(SHOW.current)
    : st==="cooldown" ? "Cooldown"
    : st==="ambient" ? "Ambient — "+nameOf(SHOW.current)
    : st==="idle" ? (ambOn ? "Armed — no ambient scenes yet" : "Armed · waiting in the dark")
    : "Armed";
  stEl.textContent = label;
  const after = ambOn ? "then returns to ambient." : "then the yard goes dark again.";
  const base = ambOn ? "Arm the show to rotate ambient scenes" : "Arm the show to wait in the dark";
  sub.textContent = !SHOW.armed
    ? ((sensOn && hasSensor) ? base + " and let the sensor fire scares." : base + ". Scares fire from the button only.")
    : noScares ? "No scenes are tagged Scare yet, so nothing will fire. Set a scene's role in its editor."
    : SHOW.in_cooldown ? "In cooldown. A new trigger waits until it ends."
    : SHOW.hold_pending ? "Someone's there. A scare fires if they stay " + holdText(SHOW.presence_hold_s) + "."
    : !sensOn ? "Running. The sensor is switched off, so only the button fires a scare, " + after
    : !hasSensor ? "Running. No sensor pin is set, so only the button fires a scare, " + after
    : "Running. A presence trigger fires a random scare, " + after;
  const se=$("#sensorEnabled"); if(se) se.checked = sensOn;
  const ae=$("#ambientEnabled"); if(ae) ae.checked = ambOn;
  if(!SHOW_HOLDDRAG && $("#holdRange") && SHOW.presence_hold_s!=null){ $("#holdRange").value = SHOW.presence_hold_s; $("#holdVal").textContent = holdText(SHOW.presence_hold_s); }
  if(armBtn){ armBtn.textContent = SHOW.armed ? "■ Disarm show" : "▶ Arm show"; armBtn.classList.toggle("on", SHOW.armed); }
  if(!SHOW_CDDRAG && SHOW.cooldown_s!=null){ $("#cdRange").value = SHOW.cooldown_s; $("#cdVal").textContent = SHOW.cooldown_s + " s"; }
  if(!SHOW_XFDRAG && SHOW.crossfade_s!=null && $("#xfRange")){ $("#xfRange").value = SHOW.crossfade_s; $("#xfVal").textContent = (+SHOW.crossfade_s).toFixed(1) + " s"; }
  const gs=$("#gpioStat"); if(gs) gs.textContent = SHOW.gpio_pin==null ? "not set"
    : ("pin "+SHOW.gpio_pin+" · "+(!SHOW.gpio_ok ? "not connected" : (SHOW.presence_active ? "PRESENT" : "clear")) + (sensOn ? "" : " · triggers off"));
  if(SHOW.gpio_pin!=null && $("#gpioPin") && document.activeElement!==$("#gpioPin")) $("#gpioPin").value = SHOW.gpio_pin;
  const amb=(SHOW.ambient||[]).map(nameOf), scare=(SHOW.scares||[]).map(nameOf);
  $("#poolAmb").innerHTML = "<b>Ambient ("+amb.length+"):</b> " + (amb.join(", ")||"none tagged yet");
  $("#poolScare").innerHTML = "<b>Scares ("+scare.length+"):</b> " + (scare.join(", ")||"none tagged yet");
  const ss=$("#schedSunset"); if(ss) ss.textContent = SHOW.sunset_today || "—";
  if(SCHED===null && SHOW.windows){
    SCHED = {enabled: !!SHOW.schedule_enabled, lat: SHOW.lat, lng: SHOW.lng, windows: JSON.parse(JSON.stringify(SHOW.windows))};
    if($("#schedEnabled")) $("#schedEnabled").checked = SCHED.enabled;
    if($("#schedLat")) $("#schedLat").value = SCHED.lat!=null?SCHED.lat:"";
    if($("#schedLng")) $("#schedLng").value = SCHED.lng!=null?SCHED.lng:"";
    buildWindows();
  }
}
// ---- schedule editor (kept in a local model so the 1.5s poll doesn't clobber typing) ----
let SCHED = null;
function winSet(i, field, val){
  if(!SCHED || !SCHED.windows[i]) return;
  if(field==="duration_min"||field==="dusk_offset_min") val = (val===""? 0 : +val);
  SCHED.windows[i][field] = val;
}
function buildWindows(){
  const host=$("#schedWindows"); if(!host||!SCHED) return;
  host.innerHTML = SCHED.windows.map((w,i)=>{
    const dusk = (w.start_mode||"dusk")==="dusk";
    return '<div class="winrow">'
      + '<div class="rowline"><input type="text" value="'+(w.label||"").replace(/"/g,"&quot;")+'" oninput="winSet('+i+",'label',this.value)\" placeholder=\"Window name\" style=\"flex:1\">"
      + '<label class="switch" style="flex:none"><input type="checkbox" '+(w.enabled?"checked":"")+' onchange="winSet('+i+",'enabled',this.checked)\"><span class=\"track\"></span></label>"
      + '<button class="mini" style="flex:none" onclick="removeWindow('+i+')">✕</button></div>'
      + '<div class="rowline" style="margin-top:8px;gap:8px;flex-wrap:wrap">'
      + '<select onchange="winSet('+i+",'start_mode',this.value); buildWindows()\" style=\"flex:none;width:auto\">"
      +   '<option value="dusk"'+(dusk?" selected":"")+'>At dusk</option>'
      +   '<option value="clock"'+(!dusk?" selected":"")+'>At a set time</option></select>'
      + (dusk
          ? '<input type="number" value="'+(w.dusk_offset_min||0)+'" oninput="winSet('+i+",'dusk_offset_min',this.value)\" style=\"width:64px\"><span class=\"muted\" style=\"font-size:12px;margin-right:6px\">min vs sunset</span>"
          : '<input type="time" value="'+(w.time||"18:00")+'" oninput="winSet('+i+",'time',this.value)\" style=\"width:auto\">")
      + '<span class="muted" style="font-size:12px">for</span>'
      + '<input type="number" value="'+(w.duration_min||30)+'" oninput="winSet('+i+",'duration_min',this.value)\" style=\"width:70px\"><span class=\"muted\" style=\"font-size:12px\">min</span>"
      + '</div>'
      + '<div class="rowline" style="margin-top:6px"><span class="muted" style="font-size:12px">Date</span>'
      + '<input type="text" value="'+(w.date||"")+'" oninput="winSet('+i+",'date',this.value)\" placeholder=\"blank = every night · YYYY-MM-DD\" style=\"flex:1\"></div>"
      + '</div>';
  }).join("") || '<div class="sub muted">No windows yet. Add one.</div>';
}
function addWindow(){
  if(!SCHED) SCHED={enabled:false,lat:null,lng:null,windows:[]};
  SCHED.windows.push({label:"New window", enabled:true, date:"", start_mode:"dusk", time:"18:00", dusk_offset_min:0, duration_min:30});
  buildWindows();
}
function removeWindow(i){ if(SCHED){ SCHED.windows.splice(i,1); buildWindows(); } }
async function saveSchedule(){
  if(!SCHED) return;
  SCHED.enabled = $("#schedEnabled").checked;
  const la=parseFloat($("#schedLat").value), ln=parseFloat($("#schedLng").value);
  const body={enabled:SCHED.enabled, windows:SCHED.windows};
  if(!isNaN(la)) body.lat=la;
  if(!isNaN(ln)) body.lng=ln;
  try{ SHOW = await api("/api/show/schedule",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    SCHED=null; renderShow(); toast(body.enabled?"Schedule saved & on":"Schedule saved (off)"); }
  catch(e){ toast("Error: "+e.message); }
}
async function toggleArm(){
  try{ SHOW = await api("/api/show/arm", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({armed: !SHOW.armed})}); renderShow();
    toast(SHOW.armed ? "Show armed" : "Show disarmed"); }
  catch(e){ toast("Error: "+e.message); }
}
async function showTrigger(){
  try{ await api("/api/show/trigger", {method:"POST"}); toast(SHOW.armed ? "Scare triggered" : "Arm the show first"); loadShow(); }
  catch(e){ toast("Error: "+e.message); }
}
async function showConfig(body){
  try{ SHOW = await api("/api/show/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)}); renderShow(); return true; }
  catch(e){ toast("Error: "+e.message); renderShow(); return false; }
}
async function saveGpio(){
  const pin = parseInt($("#gpioPin").value, 10);
  if(isNaN(pin)){ toast("Enter a GPIO pin number"); return; }
  await showConfig({gpio_pin: pin, gpio_active_high: $("#gpioAH").checked});
  toast("Sensor saved");
}
async function clearGpio(){ await showConfig({clear_gpio:true}); toast("Sensor pin removed"); }
document.addEventListener("input", e=>{
  if(e.target && e.target.id==="cdRange"){ SHOW_CDDRAG=true; $("#cdVal").textContent = e.target.value + " s"; }
  if(e.target && e.target.id==="xfRange"){ SHOW_XFDRAG=true; $("#xfVal").textContent = (+e.target.value).toFixed(1) + " s"; }
  if(e.target && e.target.id==="holdRange"){ SHOW_HOLDDRAG=true; $("#holdVal").textContent = holdText(e.target.value); }
});
document.addEventListener("change", e=>{
  if(e.target && e.target.id==="cdRange"){ SHOW_CDDRAG=false; showConfig({cooldown_s: +e.target.value}); }
  if(e.target && e.target.id==="xfRange"){ SHOW_XFDRAG=false; showConfig({crossfade_s: +e.target.value}); }
  if(e.target && e.target.id==="holdRange"){ SHOW_HOLDDRAG=false; showConfig({presence_hold_s: +e.target.value}); }
  if(e.target && e.target.id==="sensorEnabled"){ const on=e.target.checked;
    showConfig({sensor_enabled: on}).then(ok=>ok && toast(on ? "Sensor will fire scares" : "Sensor off: only the button fires a scare")); }
  if(e.target && e.target.id==="ambientEnabled"){ const on=e.target.checked;
    showConfig({ambient_enabled: on}).then(ok=>ok && toast(on ? "Ambient scenes between scares" : "Sensor-only show: dark and quiet between scares")); }
});

// ---- scenes init (boot() above already set up Live + health) ----
loadAudioLists();
loadScenes();
loadShow();
setInterval(guarded(async ()=>{ if(document.querySelector("#view-show.active") || SHOW.armed) await loadShow(); }), 1500);
setInterval(guarded(pollPlay), 350);
// ---- System tab: FPP health, setup check, maintenance ----
const SYS = {lastChecks:0, reconnecting:false, arm:{}, fixing:false};
const _e = v => String(v==null ? "" : v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const _dotCls = st => st==="ok" ? "ok" : st==="warn" ? "warn" : st==="fail" ? "bad" : "";
function _dur(s){
  if(s==null) return "—";
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function _tile(k, st, v, d){
  return `<div class="systile"><div class="k">${_e(k)}</div><div class="v"><span class="dot ${_dotCls(st)}"></span><span class="vt">${_e(v)}</span></div><div class="d">${_e(d||"")}</div></div>`;
}
function renderSysHealth(h){
  const f = h.fpp || {}, v = h.vitals || {}, o = h.output || {}, a = h.audio || {}, se = h.sensor || {};
  const th = v.throttled, now = th ? th.flags.filter(x=>/now$/.test(x)) : [], past = th ? th.flags.filter(x=>/boot$/.test(x)) : [];
  const wifi = v.wifi, dbm = wifi ? wifi.signal_dbm : null;
  const wq = dbm==null ? null : dbm >= -60 ? "good" : dbm >= -70 ? "fair" : "weak";
  const tiles = [
    _tile("FPP", f.daemon_ok ? "ok" : "fail", f.daemon_ok ? `FPP ${f.version||""}` : "Not running",
          f.daemon_ok ? `${f.status||"idle"} · up ${_dur(f.uptime_s)}` : "not answering"),
    _tile("FPP web UI", f.web_ok && f.php_ok ? "ok" : (f.daemon_ok ? "fail" : ""), f.web_ok && f.php_ok ? "Answering" : "Not answering",
          f.web_ok && f.php_ok ? "Apache + PHP" : "lights unaffected"),
    _tile("Lights out", o.ready && (o.fps||0) >= 0.8*(o.target_fps||25) ? "ok" : "fail",
          o.ready ? `${String(o.transport||"").toUpperCase()} ${o.fps} fps` : "Not sending", `${o.send_errors||0} send errors`),
    _tile("Sound", a.ok ? "ok" : "fail", a.ok ? String(a.device||"default").split(",")[0] : "Not ready",
          a.ok ? `volume ${OUTPUT.volume}%` : "see the setup check"),
    _tile("Sensor", se.pin==null ? "warn" : se.ok ? "ok" : "fail",
          se.pin==null ? "Not set" : se.ok ? (se.present ? "PRESENT" : "CLEAR") : "Not reading", se.pin==null ? "scares by hand only" : `GPIO ${se.pin}`),
    _tile("Pi", now.length ? "fail" : past.length ? "warn" : (v.temp_c!=null ? "ok" : ""), v.temp_c!=null ? `${v.temp_c} °C` : "—",
          now.length ? now.join(", ") : past.length ? past.join(", ") : (v.load_1m!=null ? `load ${v.load_1m} · ${v.mem_used_pct}% RAM` : "")),
    _tile("Network", wq==="weak" ? "warn" : (v.ips && v.ips.length ? "ok" : ""), (v.ips||[])[0] || "—",
          wifi ? `Wi-Fi ${dbm} dBm (${wq})` : (v.ips ? "wired" : "")),
    _tile("Uptime", v.uptime_s!=null ? "ok" : "", _dur(v.uptime_s), v.disk_free_gb!=null ? `${v.disk_free_gb} GB free` : ""),
  ];
  $("#sysTiles").innerHTML = tiles.join("");
  const sh = h.show || {};
  $("#sysUpdated").textContent = "Updated " + new Date().toLocaleTimeString() +
    (sh.armed ? " · show armed" : "") + (h.busy ? " · a fix or restart is running…" : "");
}
function renderSysChecks(r){
  const cs = r.checks || [], n = r.counts || {};
  const bad = (n.fail||0) + (n.warn||0);
  $("#sysSummary").textContent = !cs.length ? "" : bad === 0 ? `All ${n.ok} checks pass. Your rig is ready to haunt. 🎃`
    : `${bad} of ${cs.length} need attention.`;
  $("#sysChecks").innerHTML = cs.map(c => {
    const fix = c.fix && c.status !== "ok"
      ? `<button class="${c.fix.disruptive ? "ghost" : "primary"}" data-fix="${_e(c.id)}" data-label="${_e(c.fix.label)}" data-dis="${c.fix.disruptive?1:0}" onclick="sysFix(this)">${_e(c.fix.label)}</button>` : "";
    return `<div class="chk"><span class="dot ${_dotCls(c.status)}"></span><div class="body"><div class="t">${_e(c.title)}</div>`+
      `<div class="d">${_e(c.detail)}</div>${c.help ? `<div class="h">${_e(c.help)}</div>` : ""}</div>${fix}</div>`;
  }).join("") || '<div class="muted">No checks available.</div>';
}
async function loadSys(withChecks){
  if(SYS.reconnecting) return;
  const fl = $("#fppLink"); if(fl) fl.href = `http://${location.hostname}/`;
  try{ renderSysHealth(await api("/api/system/health")); }catch(e){ $("#sysUpdated").textContent = "Can't reach Haunt Director: " + e.message; }
  if(withChecks || Date.now() - SYS.lastChecks > 10000){
    SYS.lastChecks = Date.now();
    try{ renderSysChecks(await api("/api/system/checks")); }catch(e){}
  }
}
function _twoTap(btn, key, label){
  // first tap arms for 3 s, second tap confirms (same pattern as Delete)
  if(Date.now() - (SYS.arm[key]||0) > 3000){
    SYS.arm[key] = Date.now(); const old = btn.dataset.orig || btn.textContent; btn.dataset.orig = old;
    btn.textContent = "Tap again: " + label;
    setTimeout(()=>{ if(Date.now() - (SYS.arm[key]||0) >= 3000) btn.textContent = btn.dataset.orig; }, 3100);
    return false;
  }
  SYS.arm[key] = 0; btn.textContent = btn.dataset.orig || btn.textContent; return true;
}
async function sysFix(btn){
  const id = btn.dataset.fix, label = btn.dataset.label;
  if(btn.dataset.dis === "1" && !_twoTap(btn, "fix:"+id, label)) return;
  if(SYS.fixing) return; SYS.fixing = true; btn.disabled = true; const t = btn.textContent; btn.textContent = "Working…";
  try{
    const r = await api(`/api/system/checks/${id}/fix`, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({confirm:true})});
    toast(r.detail || (r.ok ? "Done" : "That didn't work"));
    if(id === "audio" && r.ok) sysReconnect("Haunt Director is restarting with the new sound output.");
  }catch(e){ toast(e.message.replace(/^\{"detail":"|"\}$/g, "")); }
  finally{ SYS.fixing = false; btn.disabled = false; btn.textContent = t; if(!SYS.reconnecting) loadSys(true); }
}
async function sysAction(btn){
  const act = btn.dataset.act, label = (btn.dataset.orig || btn.textContent).replace(/^[^A-Za-z]+/, "");
  if(!_twoTap(btn, "act:"+act, label)) return;
  btn.disabled = true;
  try{
    const r = await api("/api/system/action", {method:"POST", headers:{"Content-Type":"application/json"},
                                               body: JSON.stringify({action:act, confirm:true})});
    toast(r.detail);
    if(r.ok && act === "reboot") sysReconnect("The Pi is rebooting. This usually takes 1 to 3 minutes.", 240);
    else if(r.ok && act === "restart_app") sysReconnect("Haunt Director is restarting.");
  }catch(e){ toast("Failed: " + e.message); }
  finally{ btn.disabled = false; if(!SYS.reconnecting) loadSys(true); }
}
async function sysReconnect(msg, maxS){
  SYS.reconnecting = true; maxS = maxS || 90;
  const box = $("#sysReconnect"); box.hidden = false; $("#sysReconnectSub").textContent = msg;
  const t0 = Date.now(); let wentDown = false;
  while((Date.now() - t0) / 1000 < maxS){
    await new Promise(r => setTimeout(r, 2000));
    let up = false;
    try{ const r = await fetch("/api/health", {cache:"no-store"}); up = r.ok; }catch(e){ up = false; }
    if(!up) wentDown = true;
    if(up && (wentDown || Date.now() - t0 > 12000)){ location.reload(); return; }
  }
  $("#sysReconnectSub").textContent = "Still not back. Check the Pi's power and network, then reload this page.";
  SYS.reconnecting = false;
}
setInterval(guarded(async ()=>{ if(document.querySelector("#view-system.active")) await loadSys(false); }), 3000);

// restore last view across reloads (per-browser); never auto-open the editor
let __startTab = "live";
try{ __startTab = localStorage.getItem("hd_tab") || "live"; }catch(e){}
if(location.hash === "#scenes") __startTab = "scenes";
if(__startTab === "editor") __startTab = "scenes";
if(__startTab !== "live") go(__startTab);
