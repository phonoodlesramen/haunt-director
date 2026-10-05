/* Timeline / keyframe editor. Enhances the Scenes tab into a studio: pick a
   scene, scrub a live stage preview, edit per-fixture event blocks. Changes
   autosave (debounced) with Undo + Revert; each track can be muted for
   isolating what's playing. Shares global scope with app.js (toast, go,
   AUDIOLIST, playAudio, stage.js's window.stageUpdate). */
(function(){
  const $=(s,r=document)=>r.querySelector(s);
  const jget=(p,o)=>fetch(p,o).then(r=>{ if(!r.ok) throw new Error(r.status); return r.status===204?null:r.json(); });
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
  const uid=()=>'e'+Math.random().toString(36).slice(2,8);
  const LBL=112, PPS=12;             // label column px, px per second

  let SC=null, DEVS=[], sel=null, scrubT=0, stageHome=null, previewTimer=null;
  let MUTE=new Set();                // muted lane keys: device ids, 'music', 'voiceover', cue ids
  let BASELINE=null, LAST=null, UNDO=[], saveTimer=null;
  window.__mute=MUTE;

  const EV_TYPES=[['fade_color','Fade to color'],['set_color','Set color'],
    ['fade_brightness','Fade brightness'],['set_brightness','Set brightness'],
    ['pulse','Pulse'],['strobe','Strobe'],['fog','Fog'],['blackout','Blackout']];
  const COLOR_T=new Set(['fade_color','set_color','pulse','strobe']);
  const BRIGHT_T=new Set(['fade_color','set_color','fade_brightness','set_brightness','pulse','strobe']);
  const DUR_T=new Set(['fade_color','fade_brightness','pulse','strobe','fog']);
  const RATE_T=new Set(['pulse','strobe']);
  const CURVE_T=new Set(['fade_color','fade_brightness']);
  const FOG_T=new Set(['fog']);

  function blockColor(ev){
    if(ev.type==='fog') return '#9fb3c8';
    if(ev.type==='blackout') return '#2b2f3c';
    return (ev.params&&ev.params.color)||'#8a8fa0';
  }
  function audioLists(){ return (typeof AUDIOLIST!=='undefined'&&AUDIOLIST)?AUDIOLIST:{music:[],voice:[],sfx:[]}; }
  function durOf(f){ const D=(audioLists().dur)||{}; return +D[f]||0; }   // clip length in seconds (0 = unknown)
  function isVoiceFile(f){ return (audioLists().voice||[]).some(it=>(it.file||it)===f); }   // library-classified spoken line
  const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const AUDIO_CATS={music:'Music', sfx:'Ambience & SFX', voice:'Voice'};
  // build categorical <optgroup> options (titled) from the library, for the given category keys
  function audioOptsHTML(keys, cur){
    const AL=audioLists(); let html='', found=false;
    keys.forEach(k=>{
      const items=AL[k]||[]; if(!items.length) return;
      html+='<optgroup label="'+AUDIO_CATS[k]+'">';
      items.forEach(it=>{ const f=it.file||it, t=it.title||f; if(f===cur) found=true;
        html+='<option value="'+esc(f)+'"'+(f===cur?' selected':'')+'>'+esc(t)+'</option>'; });
      html+='</optgroup>';
    });
    if(cur && !found) html+='<option value="'+esc(cur)+'" selected>'+esc(cur.replace(/\.[^.]+$/,''))+'</option>';
    return html;
  }
  function fillAudioSelect(el, keys, cur, noneLabel){
    if(!el) return;
    el.innerHTML=(noneLabel!=null?'<option value="">'+noneLabel+'</option>':'')+audioOptsHTML(keys, cur||'');
  }

  window.enterEditor=async function(id){
    try{ SC=await jget('/api/scenes/'+id); }catch(e){ if(typeof toast==='function') toast('Could not open editor'); return; }
    try{ DEVS=await jget('/api/devices'); }catch(e){ DEVS=[]; }
    SC.tracks=SC.tracks||{}; sel=null; scrubT=0; window.__editing=true; MUTE.clear();
    const stage=$('#stage'), host=$('#stageHost');
    if(stage&&host&&stage.parentNode!==host){ stageHome=stage.parentNode; host.appendChild(stage); }
    $('#edtName').textContent=SC.name||'Scene';
    fillSettings(); buildTimeline(); renderEventEditor(); setScrub(0);
    try{ localStorage.setItem('hd_scene', SC.id); }catch(_){}
    BASELINE=snap(); LAST=BASELINE; UNDO.length=0; setSaveStatus('saved'); updateUndoBtn();
    if(typeof go==='function') go('editor');
  };
  window.exitEditor=function(){
    clearTimeout(saveTimer); if(SC) commit();          // flush any pending change
    window.__editing=false; restoreStage();
    if(typeof go==='function') go('scenes');
    if(typeof loadScenes==='function') loadScenes();
  };
  function restoreStage(){
    const stage=$('#stage');
    if(stage&&stageHome&&stage.parentNode!==stageHome) stageHome.appendChild(stage);
  }
  window.__restoreStage=restoreStage;
  window.__edtSync=function(t){ if(window.__scrubbing) return; if(window.__editing&&SC){ scrubT=clamp(t,0,SC.duration_s||90); $('#edtScrub').value=Math.round(scrubT/(SC.duration_s||90)*1000); $('#edtTime').textContent=scrubT.toFixed(1)+' / '+(SC.duration_s||90).toFixed(0)+'s'; movePlayhead(); } };

  // ---- scrub / seek: dragging the playhead moves the marker; on release we seek
  // the running scene (server + browser audio) instead of snapping back to the clock.
  function beginScrub(){ window.__scrubbing=true; }
  function endScrub(doSeek){
    window.__scrubbing=false;
    // only seek if the scene being edited is the one actually playing (a seek used
    // to jump whatever scene was live, e.g. the armed show)
    if(doSeek && window.__playing && SC && window.__playScene===SC.id){
      fetch('/api/play/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({t:+scrubT.toFixed(2)})}).catch(()=>{});
      if(typeof playAudio==='function') playAudio(SC,false,[...MUTE],scrubT);
    }
  }

  // ---- autosave + undo + revert ----
  function snap(){ if(!SC) return ''; return JSON.stringify({name:SC.name,blurb:SC.blurb,duration_s:SC.duration_s,enabled:SC.enabled,tag:SC.tag||'',audio:SC.audio||null,audio_fade_out_s:(SC.audio_fade_out_s==null?null:SC.audio_fade_out_s),tracks:SC.tracks||{}}); }
  function normalizeAudio(){
    const A=SC.audio; if(!A) return;
    A.layers=A.layers||[]; if(A.gain==null) A.gain=1;
    if(!A.voiceover) A.voiceover_at_s=A.voiceover_at_s||0;
    if(!(A.music||A.voiceover||A.layers.length)) SC.audio=null;
  }
  async function putScene(){
    normalizeAudio();
    await jget('/api/scenes/'+SC.id,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:SC.name,blurb:SC.blurb,duration_s:SC.duration_s,enabled:SC.enabled,tag:SC.tag||'',audio:SC.audio,audio_fade_out_s:(SC.audio_fade_out_s==null?null:SC.audio_fade_out_s),tracks:SC.tracks})});
    const en=document.getElementById('edtName'); if(en) en.textContent=SC.name;
  }
  function setSaveStatus(k){ const el=document.getElementById('edtStat'); if(!el) return;
    el.dataset.k=k; el.textContent=k==='saving'?'Saving…':k==='error'?'Save failed':'Saved'; }
  function touch(){ if(!SC||!window.__editing) return; setSaveStatus('saving'); clearTimeout(saveTimer); saveTimer=setTimeout(commit,700); }
  async function commit(){
    if(!SC) return; const cur=snap();
    if(cur===LAST){ setSaveStatus('saved'); return; }
    if(LAST!=null){ UNDO.push(LAST); if(UNDO.length>80) UNDO.shift(); updateUndoBtn(); }
    LAST=cur;
    try{ await putScene(); setSaveStatus('saved'); }catch(e){ setSaveStatus('error'); }
  }
  function updateUndoBtn(){ const b=document.getElementById('edtUndo'); if(b) b.disabled=!UNDO.length; }
  function applySnapshot(s){ const o=JSON.parse(s);
    SC.name=o.name; SC.blurb=o.blurb; SC.duration_s=o.duration_s; SC.enabled=o.enabled; SC.tag=o.tag||''; SC.audio=o.audio; SC.audio_fade_out_s=(o.audio_fade_out_s==null?null:o.audio_fade_out_s); SC.tracks=o.tracks||{};
    sel=null; fillSettings(); buildTimeline(); renderEventEditor(); setScrub(Math.min(scrubT,SC.duration_s||90)); }
  window.undoEdit=function(){ if(!UNDO.length) return; const prev=UNDO.pop(); updateUndoBtn();
    applySnapshot(prev); LAST=snap(); setSaveStatus('saving'); putScene().then(()=>setSaveStatus('saved')).catch(()=>setSaveStatus('error')); };
  window.revertEdits=function(){ if(BASELINE==null||!SC) return;
    if(snap()===BASELINE){ setSaveStatus('saved'); if(typeof toast==='function') toast('No changes to revert'); return; }
    if(LAST!=null){ UNDO.push(LAST); updateUndoBtn(); }
    applySnapshot(BASELINE); LAST=BASELINE; setSaveStatus('saving');
    putScene().then(()=>{ setSaveStatus('saved'); if(typeof toast==='function') toast('Reverted to opened state'); }).catch(()=>setSaveStatus('error')); };
  window.saveTimeline=async function(){ clearTimeout(saveTimer); await commit(); if(typeof toast==='function') toast('Saved'); };

  // ---- mute ----
  function _applyMute(){
    buildTimeline(); previewAt(scrubT);
    const arr=[...MUTE];
    // apply live to a scene that's already playing: server (lights + Pi audio) + browser audio
    if(SC && window.__playScene===SC.id)   // never mute lanes on some other live scene
      fetch('/api/play/mute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mute:arr})}).catch(()=>{});
    if(typeof setAudioMute==='function') setAudioMute(arr);
  }
  function toggleMute(key){
    if(MUTE.has(key)) MUTE.delete(key); else MUTE.add(key);
    _applyMute();
  }
  function toggleMuteGroup(keys){
    // one mute button standing in for every SFX hit sharing a lane
    const allMuted = keys.length>0 && keys.every(k=>MUTE.has(k));
    if(allMuted) keys.forEach(k=>MUTE.delete(k)); else keys.forEach(k=>MUTE.add(k));
    _applyMute();
  }
  function stripMuted(st){ if(!st||!st.devices||!MUTE.size) return st; const d={}; for(const k in st.devices) if(!MUTE.has(k)) d[k]=st.devices[k]; return Object.assign({},st,{devices:d}); }
  window.__stripMuted=stripMuted;
  function mkLbl(text, muteKey){
    const lbl=document.createElement('div'); lbl.className='rowlbl';
    lbl.innerHTML='<span class="kd"></span><span class="rl-name">'+text+'</span>'+
      '<button class="mutebtn'+(MUTE.has(muteKey)?' on':'')+'" data-m="'+muteKey+'" title="'+(MUTE.has(muteKey)?'Un-mute':'Mute')+' this track">'+(MUTE.has(muteKey)?'🔇':'🔊')+'</button>';
    const mb=lbl.querySelector('.mutebtn');
    mb.addEventListener('pointerdown',ev=>{ ev.preventDefault(); ev.stopPropagation(); });
    mb.addEventListener('click',ev=>{ ev.preventDefault(); ev.stopPropagation(); toggleMute(muteKey); });
    return lbl;
  }
  function mkLblGroup(text, keys){
    // label + a single mute button that toggles every hit on the lane at once
    const allMuted = keys.length>0 && keys.every(k=>MUTE.has(k));
    const lbl=document.createElement('div'); lbl.className='rowlbl';
    lbl.innerHTML='<span class="kd"></span><span class="rl-name">'+text+'</span>'+
      '<button class="mutebtn'+(allMuted?' on':'')+'" title="'+(allMuted?'Un-mute':'Mute')+' this track">'+(allMuted?'🔇':'🔊')+'</button>';
    const mb=lbl.querySelector('.mutebtn');
    mb.addEventListener('pointerdown',ev=>{ ev.preventDefault(); ev.stopPropagation(); });
    mb.addEventListener('click',ev=>{ ev.preventDefault(); ev.stopPropagation(); toggleMuteGroup(keys); });
    return lbl;
  }

  // ---- scene settings panel ----
  function fillSettings(){
    const AL=audioLists(), a=SC.audio||{};
    $('#setName').value=SC.name||''; $('#setBlurb').value=SC.blurb||'';
    $('#setDur').value=SC.duration_s||90; $('#setDurVal').textContent=lenText(SC.duration_s||90);
    fillExtend();
    fillAudioSelect($('#setMusic'),['music','sfx'],a.music||'','None');
    fillAudioSelect($('#setVo'),['voice'],a.voiceover||'','None');
    const at=$('#setVoAt'); at.max=SC.duration_s||90; at.value=a.voiceover_at_s||0;
    $('#setVoAtVal').textContent=(a.voiceover_at_s||0).toFixed(1)+' s';
    $('#setEnabled').checked=SC.enabled!==false;
    { const rl=$('#setRole'); if(rl) rl.value=SC.tag||''; }
    fillFadeOverride();
    setVoVis();
  }
  function fillFadeOverride(){
    const on=$('#setFadeOn'), wrap=$('#setFadeWrap'), rng=$('#setFade'),
          val=$('#setFadeVal'), hint=$('#setFadeHint');
    if(!on) return;
    const v=(SC && SC.audio_fade_out_s!=null) ? +SC.audio_fade_out_s : null;
    on.checked = v!=null;
    if(wrap) wrap.style.display = v!=null ? '' : 'none';
    if(rng) rng.value = v!=null ? v : 2;
    if(val) val.textContent = ((v!=null?v:2)).toFixed(1)+' s';
    if(hint) hint.textContent = v!=null ? (v.toFixed(1)+' s') : 'auto';
  }
  function fillSel(el,files,cur,none){
    el.innerHTML='<option value="">'+none+'</option>'+files.map(f=>'<option'+(f===cur?' selected':'')+' value="'+f+'">'+f.replace(/\.[^.]+$/,'')+'</option>').join('');
    if(cur&&files.indexOf(cur)<0){ const o=document.createElement('option'); o.value=cur; o.textContent=cur.replace(/\.[^.]+$/,''); o.selected=true; el.appendChild(o); }
  }
  function setVoVis(){ $('#setVoWrap').style.display=$('#setVo').value?'':'none'; }

  // ---- timeline render ----
  function devOrder(){
    const k={dmx_fixture:0,dmx_fog:1};
    return DEVS.filter(d=>d.kind==='dmx_fixture'||d.kind==='dmx_fog').sort((a,b)=>k[a.kind]-k[b.kind]);
  }
  function buildTimeline(){
    const dur=SC.duration_s||90, W=Math.max(320,dur*PPS), panel=$('#tlpanel');
    document.documentElement.style.setProperty('--tl-lbl',LBL+'px');
    panel.innerHTML='';
    const inner=document.createElement('div'); inner.className='tlinner'; inner.style.width=(LBL+W)+'px';
    const ruler=document.createElement('div'); ruler.className='tlruler'; ruler.style.marginLeft=LBL+'px'; ruler.style.width=W+'px';
    const step=dur<=20?2:(dur<=60?5:10);
    for(let s=0;s<=dur+0.001;s+=step){ const t=document.createElement('div'); t.className='tick'; t.style.left=(s*PPS)+'px'; t.textContent=s+'s'; ruler.appendChild(t); }
    ruler.addEventListener('pointerdown', rulerScrub);
    inner.appendChild(ruler);
    for(const d of devOrder()){
      const row=document.createElement('div'); row.className='tlrow'+(d.kind==='dmx_fog'?' fog':'')+(MUTE.has(d.id)?' muted':'');
      row.appendChild(mkLbl(d.name, d.id));
      const track=document.createElement('div'); track.className='tltrack'; track.style.width=W+'px';
      track.addEventListener('pointerdown', rulerScrub);   // click empty track to scrub
      track.addEventListener('dblclick',(function(dev){ return function(e){ if(e.target!==this) return; e.preventDefault(); e.stopPropagation(); addEventAt(dev, timeFromX(e.clientX)); }; })(d.id));   // double-click empty track: add an event right there
      const evs=(SC.tracks[d.id]||[]).slice().sort((a,b)=>a.t-b.t);
      for(const ev of evs){
        const b=document.createElement('div');
        b.className='tlblock'+(sel&&sel.dev===d.id&&sel.id===ev.id?' sel':'');
        const dr=DUR_T.has(ev.type);
        const w=Math.max(8,(dr?(ev.duration_s||0.4):0.4)*PPS);
        b.style.left=(ev.t*PPS)+'px'; b.style.width=w+'px'; b.style.background=blockColor(ev);
        b.innerHTML='<div class="bt">'+ev.type.replace('_',' ')+'</div>'+(dr?'<div class="tlresize"></div>':'');
        b.dataset.dev=d.id; b.dataset.id=ev.id;
        b.addEventListener('pointerdown', blockDown);
        track.appendChild(b);
      }
      const add=document.createElement('div'); add.className='tladd'; add.textContent='+'; add.title='Add event at the playhead';
      add.addEventListener('pointerdown', e=>e.stopPropagation());
      add.onclick=(function(dev){ return function(){ addEvent(dev); }; })(d.id);
      track.appendChild(add);
      row.appendChild(track); inner.appendChild(row);
    }
    // ---- audio lanes: music bed + voice-over + layered beds/sfx ----
    const A = SC.audio || (SC.audio = {});
    const mkAudioRow=(labelText,muteKey,first)=>{
      const row=document.createElement('div'); row.className='tlrow audio'+(first?' first':'')+(MUTE.has(muteKey)?' muted':'');
      row.appendChild(mkLbl(labelText,muteKey));
      const track=document.createElement('div'); track.className='tltrack'; track.style.width=W+'px';
      track.addEventListener('pointerdown', rulerScrub);
      row.appendChild(track); inner.appendChild(row); return track;
    };
    const mkAudioRowGroup=(labelText,keys,first)=>{
      const allMuted = keys.length>0 && keys.every(k=>MUTE.has(k));
      const row=document.createElement('div'); row.className='tlrow audio'+(first?' first':'')+(allMuted?' muted':'');
      row.appendChild(mkLblGroup(labelText,keys));
      const track=document.createElement('div'); track.className='tltrack'; track.style.width=W+'px';
      track.addEventListener('pointerdown', rulerScrub);
      row.appendChild(track); inner.appendChild(row); return track;
    };
    const mkAudioBlock=(track,kind,id,at,wsec,color,tag)=>{
      const b=document.createElement('div');
      b.className='tlblock audioblk'+(sel&&sel.kind===kind&&(kind!=='layer'||sel.id===id)?' sel':'');
      b.style.left=((at||0)*PPS)+'px'; b.style.width=Math.max(10,wsec*PPS)+'px'; b.style.background=color;
      b.innerHTML='<div class="bt">'+tag+'</div>';
      b.dataset.kind=kind; if(id) b.dataset.id=id;
      b.addEventListener('pointerdown', blockDown);
      track.appendChild(b);
    };
    let firstAudio=true;
    if(A.music){
      const t=mkAudioRow('MUSIC · '+A.music.replace(/\.[^.]+$/,''),'music',firstAudio); firstAudio=false;
      const b=document.createElement('div'); b.className='tlblock audioblk music';
      b.style.left='0px'; b.style.width=Math.max(10,dur*PPS)+'px'; b.style.background='#7d6a46';
      b.innerHTML='<div class="bt">MUSIC BED</div>'; t.appendChild(b);
    }
    if(A.voiceover){
      const t=mkAudioRow('VO · '+A.voiceover.replace(/\.[^.]+$/,''),'voiceover',firstAudio); firstAudio=false;
      mkAudioBlock(t,'vo',null,A.voiceover_at_s||0,durOf(A.voiceover)||3.0,'#b57bff','VOICE');
    }
    const _layers=A.layers||[];
    // looping beds fill the scene and stay one lane each
    _layers.filter(c=>c.loop).forEach(cue=>{
      const t=mkAudioRow('BED · '+cue.file.replace(/\.[^.]+$/,''),cue.id,firstAudio); firstAudio=false;
      mkAudioBlock(t,'layer',cue.id,cue.at_s||0,Math.max(1,dur-(cue.at_s||0)),'#6a8fb5','BED');
    });
    // one-shot clips of the same file share ONE lane, so the same sound can be
    // stamped at several points without a new track per hit. Voice lines (spoken
    // narration) render on their own purple lanes; ambience/SFX stay blue. Any
    // number of voice lines can be stacked in one scene.
    const _grpOrder=[]; const _grp={};
    _layers.filter(c=>!c.loop).forEach(c=>{
      if(!(c.file in _grp)){ _grp[c.file]=[]; _grpOrder.push(c.file); }
      _grp[c.file].push(c);
    });
    const _mkGroup=(file,color,tag,label)=>{
      const cues=_grp[file]; const keys=cues.map(c=>c.id);
      const t=mkAudioRowGroup(label+' · '+file.replace(/\.[^.]+$/,''),keys,firstAudio); firstAudio=false;
      cues.forEach(cue=>{ mkAudioBlock(t,'layer',cue.id,cue.at_s||0,durOf(cue.file)||3.0,color,tag); });
    };
    _grpOrder.filter(isVoiceFile).forEach(file=>_mkGroup(file,'#b57bff','VOICE','VO'));
    _grpOrder.filter(f=>!isVoiceFile(f)).forEach(file=>_mkGroup(file,'#9fb3c8','SFX','SFX'));
    const ph=document.createElement('div'); ph.className='playhead'; ph.id='edtPlayhead'; ph.style.left=(LBL+scrubT*PPS)+'px';
    ph.innerHTML='<div class="phandle" title="Drag to scrub"></div>';
    ph.querySelector('.phandle').addEventListener('pointerdown', e=>{ e.stopPropagation(); rulerScrub(e); });
    inner.appendChild(ph);
    panel.appendChild(inner);
  }
  function movePlayhead(){ const ph=$('#edtPlayhead'); if(ph) ph.style.left=(LBL+scrubT*PPS)+'px'; }

  function setScrub(t){
    const dur=SC.duration_s||90; scrubT=clamp(t,0,dur);
    $('#edtScrub').value=Math.round(scrubT/dur*1000);
    $('#edtTime').textContent=scrubT.toFixed(1)+' / '+dur.toFixed(0)+'s';
    movePlayhead(); previewAt(scrubT);
  }
  function previewAt(t){
    clearTimeout(previewTimer);
    previewTimer=setTimeout(async()=>{
      try{ const st=await jget('/api/scenes/'+SC.id+'/state?t='+(+t).toFixed(2)); st.playing=true; if(window.stageUpdate) window.stageUpdate(stripMuted(st),SC); }catch(e){}
    },45);
  }

  // ---- event editor ----
  function evByRef(r){ return (SC.tracks[r.dev]||[]).find(e=>e.id===r.id); }
  function selectEvent(dev,id){ sel={dev,id}; buildTimeline(); renderEventEditor(); const e=evByRef(sel); if(e) setScrub(e.t); }
  window.deleteEvent=function(){
    if(!sel) return;
    const A=SC.audio||{};
    if(sel.kind==='vo'){ A.voiceover=null; A.voiceover_at_s=0; fillSettings(); sel=null; buildTimeline(); renderEventEditor(); touch(); return; }
    if(sel.kind==='layer'){ A.layers=(A.layers||[]).filter(c=>c.id!==sel.id); sel=null; buildTimeline(); renderEventEditor(); touch(); return; }
    const a=SC.tracks[sel.dev]||[]; const i=a.findIndex(e=>e.id===sel.id); if(i>=0) a.splice(i,1); sel=null; buildTimeline(); renderEventEditor(); previewAt(scrubT); touch();
  };
  window.addLayer=function(){
    if(!SC) return;
    const A=SC.audio || (SC.audio={});
    A.layers=A.layers||[];
    const AL=audioLists(); const choices=(AL.sfx||[]).concat(AL.music||[]);
    if(!choices.length){ if(typeof toast==='function') toast('No sound files available'); return; }
    const first=choices[0].file||choices[0];
    const cue={id:'c'+Math.random().toString(36).slice(2,8), file:first, at_s:+scrubT.toFixed(2), gain:0.8, loop:true};
    A.layers.push(cue); sel={kind:'layer', id:cue.id}; buildTimeline(); renderEventEditor(); touch();
    if(typeof toast==='function') toast('Sound layer added');
  };
  // Add a spoken voice-over line at the playhead. Unlike the single "primary"
  // voice-over in Scene settings, these stack: add as many as you want, each
  // placed and trimmed on its own VO lane.
  window.addVoice=function(){
    if(!SC) return;
    const A=SC.audio || (SC.audio={});
    A.layers=A.layers||[];
    const voices=(audioLists().voice||[]);
    if(!voices.length){ if(typeof toast==='function') toast('No voice-over clips in the library yet'); return; }
    const first=voices[0].file||voices[0];
    const cue={id:'c'+Math.random().toString(36).slice(2,8), file:first, at_s:+scrubT.toFixed(2), gain:1.0, loop:false};
    A.layers.push(cue); sel={kind:'layer', id:cue.id}; buildTimeline(); renderEventEditor(); touch();
    if(typeof toast==='function') toast('Voice line added — choose the clip on the right');
  };
  // Build a fresh event of a given type with sensible default params.
  function mkEvent(type,t){
    const ev={id:uid(),t:+(+t).toFixed(2),type};
    const p={};
    if(COLOR_T.has(type)) p.color='#E8A33D';
    if(BRIGHT_T.has(type)) p.brightness=0.8;
    if(RATE_T.has(type)) p.rate_hz=1.5;
    if(FOG_T.has(type)) p.intensity=0.6;
    if(DUR_T.has(type)) ev.duration_s=(type==='fog')?3.0:2.0;
    ev.params=p; return ev;
  }
  // Fixture-aware default: fog device -> fog; light device -> last-used light type, else fade_color.
  function trackDefaultType(dev){
    const d=DEVS.find(x=>x.id===dev);
    if(d && d.kind==='dmx_fog') return 'fog';
    const evs=SC.tracks[dev]||[];
    for(let i=evs.length-1;i>=0;i--){ if(evs[i].type!=='fog') return evs[i].type; }
    return 'fade_color';
  }
  function addEventAt(dev,t){
    const ev=mkEvent(trackDefaultType(dev), clamp(+t,0,SC.duration_s||90));
    (SC.tracks[dev]=SC.tracks[dev]||[]).push(ev); sel={dev,id:ev.id}; scrubT=ev.t;
    buildTimeline(); renderEventEditor(); setScrubUiLight(); previewAt(scrubT); touch();
  }
  function addEvent(dev){ addEventAt(dev, scrubT); }

  function fieldLabel(inputEl,text){ const f=inputEl.closest('.field'); if(f){ const m=f.querySelector('label .muted'); if(m) m.textContent=text; } }

  function renderAudioEditor(host, del){
    const A=SC.audio||{}, dur=SC.duration_s||90, AL=audioLists();
    del.hidden=false;
    if(sel.kind==='vo'){
      del.textContent='Remove';
      let h='<div class="field"><label>Voice-over clip</label><select id="aoFile">'+audioOptsHTML(['voice'], A.voiceover)+'</select></div>';
      h+='<div class="field"><label>Start <span class="muted">'+(A.voiceover_at_s||0).toFixed(1)+'s</span></label><input type="range" id="aoAt" min="0" max="'+dur+'" step="0.1" value="'+(A.voiceover_at_s||0)+'"></div>';
      h+='<div class="field"><label>Level <span class="muted">'+Math.round((A.gain!=null?A.gain:1)*100)+'%</span></label><input type="range" id="aoGain" min="0" max="1" step="0.05" value="'+(A.gain!=null?A.gain:1)+'"></div>';
      h+='<div class="trimrow" style="margin-top:2px"><button class="mini" style="color:var(--text)" onclick="previewClip(\''+(A.voiceover||'')+'\')">▶ Preview</button><button class="mini" id="aoTrim" style="color:var(--text)">✂ Trim</button></div>';
      host.innerHTML=h;
      $('#aoFile').onchange=e=>{ A.voiceover=e.target.value||null; fillSettings(); buildTimeline(); renderEventEditor(); touch(); };
      $('#aoAt').oninput=e=>{ A.voiceover_at_s=clamp(+e.target.value,0,dur); fieldLabel(e.target,A.voiceover_at_s.toFixed(1)+'s'); syncVoSlider(); scrubT=A.voiceover_at_s; setScrubUiLight(); buildTimeline(); touch(); };
      $('#aoGain').oninput=e=>{ A.gain=+e.target.value; fieldLabel(e.target,Math.round(A.gain*100)+'%'); touch(); };
      { const tb=$('#aoTrim'); if(tb) tb.onclick=()=>openTrim(A.voiceover, nf=>{ A.voiceover=nf; fillSettings(); buildTimeline(); renderEventEditor(); touch(); }); }
      return;
    }
    const cue=(A.layers||[]).find(c=>c.id===sel.id);
    if(!cue){ sel=null; return renderEventEditor(); }
    del.textContent='Delete';
    const _voice=isVoiceFile(cue.file);
    let h='<div class="field"><label>'+(_voice?'Voice-over clip':'Sound')+'</label><select id="aoFile">'+audioOptsHTML(['voice','sfx','music'], cue.file)+'</select></div>';
    h+='<div class="field"><label>Start <span class="muted">'+(cue.at_s||0).toFixed(1)+'s</span></label><input type="range" id="aoAt" min="0" max="'+dur+'" step="0.1" value="'+(cue.at_s||0)+'"></div>';
    h+='<div class="field"><label>Level <span class="muted">'+Math.round((cue.gain!=null?cue.gain:1)*100)+'%</span></label><input type="range" id="aoGain" min="0" max="1" step="0.05" value="'+(cue.gain!=null?cue.gain:1)+'"></div>';
    if(!_voice) h+='<div class="field toggle"><label style="margin:0">Loop as a bed</label><label class="switch"><input type="checkbox" id="aoLoop"'+(cue.loop?' checked':'')+'><span class="track"></span></label></div>';
    h+='<div class="trimrow" style="margin-top:2px"><button class="mini" style="color:var(--text)" onclick="previewClip(\''+cue.file+'\')">▶ Preview</button><button class="mini" id="aoDup" style="color:var(--text)">⧉ Duplicate</button><button class="mini" id="aoTrim" style="color:var(--text)">✂ Trim</button></div>';
    h+='<div class="muted" style="font-size:12px;margin-top:6px">'+(_voice?'This is a spoken line — stack as many as you like. Duplicate drops another copy on the same lane at the playhead.':'Duplicate drops another hit of this sound on the same lane at the playhead. Move the playhead, then Duplicate (or Ctrl+D) to stamp it again.')+'</div>';
    host.innerHTML=h;
    $('#aoFile').onchange=e=>{ cue.file=e.target.value; if(isVoiceFile(cue.file)) cue.loop=false; buildTimeline(); renderEventEditor(); touch(); };
    $('#aoAt').oninput=e=>{ cue.at_s=clamp(+e.target.value,0,dur); fieldLabel(e.target,cue.at_s.toFixed(1)+'s'); scrubT=cue.at_s; setScrubUiLight(); buildTimeline(); touch(); };
    $('#aoGain').oninput=e=>{ cue.gain=+e.target.value; fieldLabel(e.target,Math.round(cue.gain*100)+'%'); touch(); };
    { const lp=$('#aoLoop'); if(lp) lp.onchange=e=>{ cue.loop=e.target.checked; buildTimeline(); touch(); }; }
    { const tb=$('#aoTrim'); if(tb) tb.onclick=()=>openTrim(cue.file, nf=>{ cue.file=nf; buildTimeline(); renderEventEditor(); touch(); }); }
    { const db=$('#aoDup'); if(db) db.onclick=()=>dupLayerCue(); }
  }

  function renderEventEditor(){
    const host=$('#edtEvent'), del=$('#edtDel');
    if(sel && sel.kind){ return renderAudioEditor(host, del); }
    if(!sel){ del.hidden=true; del.textContent='Delete'; host.innerHTML='<div class="muted" style="font-size:13px">Double-click a track to drop an event right where you click, or tap an existing event/sound to edit it. ＋ Sound adds an audio layer; 🔊 mutes a track.<br><span style="opacity:.8">Shortcuts: Del remove · Ctrl+D duplicate · Ctrl+C/V copy-paste · ←/→ nudge (Shift = 1s) · Space play/stop</span></div>'; return; }
    const ev=evByRef(sel); if(!ev){ del.hidden=true; sel=null; return renderEventEditor(); }
    del.hidden=false;
    const dur=SC.duration_s||90, p=ev.params=ev.params||{};
    let h='<div class="field"><label>Type</label><select id="evType">'+EV_TYPES.map(x=>'<option value="'+x[0]+'"'+(x[0]===ev.type?' selected':'')+'>'+x[1]+'</option>').join('')+'</select></div>';
    const NIN='style="width:78px;margin-top:6px;background:#161922;color:var(--text,#e8eaf0);border:1px solid #2b3040;border-radius:6px;padding:3px 7px;font-size:13px"';
    h+='<div class="evrow'+(DUR_T.has(ev.type)?'':' one')+'"><div class="field"><label>Start <span class="muted">'+ev.t.toFixed(1)+'s</span></label><input type="range" id="evT" min="0" max="'+dur+'" step="0.1" value="'+ev.t+'"><input type="number" id="evTn" min="0" max="'+dur+'" step="0.1" value="'+ev.t.toFixed(2)+'" '+NIN+'></div>';
    if(DUR_T.has(ev.type)) h+='<div class="field"><label>Length <span class="muted">'+(ev.duration_s||0).toFixed(1)+'s</span></label><input type="range" id="evDur" min="0.1" max="'+dur+'" step="0.1" value="'+(ev.duration_s||1)+'"><input type="number" id="evDurn" min="0.1" max="'+dur+'" step="0.1" value="'+(ev.duration_s||1).toFixed(2)+'" '+NIN+'></div>';
    h+='</div>';
    if(COLOR_T.has(ev.type)) h+='<div class="field"><label>Color</label><div class="swatchrow"><input type="color" id="evColor" value="'+(p.color||'#E8A33D')+'"><input type="text" id="evColorT" value="'+(p.color||'#E8A33D')+'"></div></div>';
    if(BRIGHT_T.has(ev.type)) h+='<div class="field"><label>Brightness <span class="muted">'+Math.round((p.brightness!=null?p.brightness:1)*100)+'%</span></label><input type="range" id="evBri" min="0" max="1" step="0.02" value="'+(p.brightness!=null?p.brightness:1)+'"></div>';
    if(RATE_T.has(ev.type)) h+='<div class="field"><label>Rate <span class="muted">'+(p.rate_hz!=null?p.rate_hz:1)+' Hz</span></label><input type="range" id="evRate" min="0.2" max="12" step="0.1" value="'+(p.rate_hz!=null?p.rate_hz:1)+'"></div>';
    if(FOG_T.has(ev.type)) h+='<div class="field"><label>Intensity <span class="muted">'+Math.round((p.intensity!=null?p.intensity:0.6)*100)+'%</span></label><input type="range" id="evInt" min="0" max="1" step="0.02" value="'+(p.intensity!=null?p.intensity:0.6)+'"></div>';
    if(CURVE_T.has(ev.type)) h+='<div class="field"><label>Curve</label><select id="evCurve">'+['linear','ease_in','ease_out','ease_in_out'].map(c=>'<option'+((p.curve||'linear')===c?' selected':'')+'>'+c+'</option>').join('')+'</select></div>';
    host.innerHTML=h;

    const softRerender=()=>{ buildTimeline(); previewAt(scrubT); touch(); };
    $('#evType').onchange=e=>{ ev.type=e.target.value;
      if(COLOR_T.has(ev.type)&&!p.color) p.color='#E8A33D';
      if(BRIGHT_T.has(ev.type)&&p.brightness==null) p.brightness=0.8;
      if(RATE_T.has(ev.type)&&p.rate_hz==null) p.rate_hz=1.5;
      if(FOG_T.has(ev.type)&&p.intensity==null) p.intensity=0.6;
      if(DUR_T.has(ev.type)&&!ev.duration_s) ev.duration_s=2.0;
      renderEventEditor(); softRerender(); };
    $('#evT').oninput=e=>{ ev.t=clamp(+e.target.value,0,dur); fieldLabel(e.target,ev.t.toFixed(1)+'s'); const n=$('#evTn'); if(n) n.value=ev.t.toFixed(2); scrubT=ev.t; setScrubUi(); softRerender(); };
    { const tn=$('#evTn'); if(tn) tn.onchange=e=>{ ev.t=+clamp(+e.target.value||0,0,dur).toFixed(2); const r=$('#evT'); if(r) r.value=ev.t; fieldLabel(e.target,ev.t.toFixed(1)+'s'); scrubT=ev.t; setScrubUi(); softRerender(); }; }
    const d1=$('#evDur'); if(d1) d1.oninput=e=>{ ev.duration_s=+e.target.value; fieldLabel(e.target,ev.duration_s.toFixed(1)+'s'); const n=$('#evDurn'); if(n) n.value=(+e.target.value).toFixed(2); softRerender(); };
    { const dn=$('#evDurn'); if(dn) dn.onchange=e=>{ ev.duration_s=+clamp(+e.target.value||0.1,0.1,dur).toFixed(2); const r=$('#evDur'); if(r) r.value=ev.duration_s; fieldLabel(e.target,ev.duration_s.toFixed(1)+'s'); softRerender(); }; }
    const c1=$('#evColor'); if(c1) c1.oninput=e=>{ p.color=e.target.value; $('#evColorT').value=e.target.value; softRerender(); };
    const c2=$('#evColorT'); if(c2) c2.onchange=e=>{ if(/^#[0-9a-f]{6}$/i.test(e.target.value)){ p.color=e.target.value; $('#evColor').value=e.target.value; softRerender(); } };
    const b1=$('#evBri'); if(b1) b1.oninput=e=>{ p.brightness=+e.target.value; fieldLabel(e.target,Math.round(p.brightness*100)+'%'); softRerender(); };
    const r1=$('#evRate'); if(r1) r1.oninput=e=>{ p.rate_hz=+e.target.value; fieldLabel(e.target,p.rate_hz+' Hz'); previewAt(scrubT); touch(); };
    const i1=$('#evInt'); if(i1) i1.oninput=e=>{ p.intensity=+e.target.value; fieldLabel(e.target,Math.round(p.intensity*100)+'%'); softRerender(); };
    const cv=$('#evCurve'); if(cv) cv.onchange=e=>{ p.curve=e.target.value; previewAt(scrubT); touch(); };
  }
  function setScrubUi(){ const dur=SC.duration_s||90; $('#edtScrub').value=Math.round(scrubT/dur*1000); $('#edtTime').textContent=scrubT.toFixed(1)+' / '+dur.toFixed(0)+'s'; movePlayhead(); }

  // ---- transport ----
  window.edtPlay=async function(){ if(window.edtPlay._busy) return; window.edtPlay._busy=true; try{ await jget('/api/scenes/'+SC.id+'/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({loop:false,mute:[...MUTE]})}); if(typeof playAudio==='function') playAudio(SC,false,[...MUTE]); if(typeof toast==='function') toast('Playing'); }catch(e){ if(typeof toast==='function') toast('Error: '+e.message); } finally { window.edtPlay._busy=false; } };
  window.edtStop=async function(){ try{ await jget('/api/play/stop',{method:'POST'}); }catch(e){} if(typeof stopAudio==='function') stopAudio(); previewAt(scrubT); };

  // settings inputs -> live SC + autosave (handles both input + change so selects/checkbox fire)
  function onSettings(e){
    if(!SC||!window.__editing) return;
    const id=e.target.id;
    if(id==='edtScrub'){ if(e.type==='change'){ endScrub(true); } else { beginScrub(); setScrub(e.target.value/1000*(SC.duration_s||90)); } return; }
    let changed=true;
    if(id==='setName'){ SC.name=e.target.value; const en=document.getElementById('edtName'); if(en) en.textContent=SC.name||'Scene'; }
    else if(id==='setBlurb'){ SC.blurb=e.target.value; }
    else if(id==='setEnabled'){ SC.enabled=e.target.checked; }
    else if(id==='setRole'){ SC.tag=e.target.value||''; }
    else if(id==='setFadeOn'){
      if(e.target.checked){ const v=+($('#setFade')?$('#setFade').value:2)||0; SC.audio_fade_out_s=v; }
      else { SC.audio_fade_out_s=null; }
      fillFadeOverride();
    }
    else if(id==='setFade'){ const v=Math.max(0,+e.target.value||0); SC.audio_fade_out_s=v;
      const val=$('#setFadeVal'); if(val) val.textContent=v.toFixed(1)+' s';
      const hint=$('#setFadeHint'); if(hint) hint.textContent=v.toFixed(1)+' s'; }
    else if(id==='setDur'){ const v=+e.target.value||0; document.getElementById('setDurVal').textContent=lenText(v); setTimeout(fillExtend,0); SC.duration_s=Math.max(3,v); const va=document.getElementById('setVoAt'); if(va) va.max=SC.duration_s; buildTimeline(); }
    else if(id==='setVoAt'){ SC.audio=SC.audio||{}; SC.audio.voiceover_at_s=+e.target.value; document.getElementById('setVoAtVal').textContent=(+e.target.value).toFixed(1)+' s'; buildTimeline(); }
    else if(id==='setVo'){ setVoVis(); SC.audio=SC.audio||{}; SC.audio.voiceover=e.target.value||null; if(SC.audio.voiceover_at_s==null) SC.audio.voiceover_at_s=0; buildTimeline(); }
    else if(id==='setMusic'){ SC.audio=SC.audio||{}; SC.audio.music=e.target.value||null; buildTimeline(); }
    else changed=false;
    if(changed) touch();
  }
  document.addEventListener('input',onSettings);
  document.addEventListener('change',onSettings);
  // ---- clipboard / duplicate / keyboard ----
  let CLIP=null;
  function copySel(){ if(!sel||sel.kind) return; const ev=evByRef(sel); if(!ev) return; CLIP={dev:sel.dev, ev:JSON.parse(JSON.stringify(ev))}; if(typeof toast==='function') toast('Copied event'); }
  function pasteClip(){ if(!CLIP||!SC) return; const dev=CLIP.dev; const ev=JSON.parse(JSON.stringify(CLIP.ev)); ev.id=uid(); ev.t=+clamp(+scrubT,0,SC.duration_s||90).toFixed(2); (SC.tracks[dev]=SC.tracks[dev]||[]).push(ev); sel={dev,id:ev.id}; buildTimeline(); renderEventEditor(); previewAt(scrubT); touch(); if(typeof toast==='function') toast('Pasted'); }
  function dupLayerCue(){
    // stamp another copy of the selected audio layer at the playhead; it lands on
    // the same lane (grouped by file). SFX stay one-shots so they don't loop.
    if(!SC||!sel||sel.kind!=='layer') return;
    const A=SC.audio||{}; const cue=(A.layers||[]).find(c=>c.id===sel.id); if(!cue) return;
    const copy={id:'c'+Math.random().toString(36).slice(2,8), file:cue.file,
                at_s:+clamp(+scrubT,0,SC.duration_s||90).toFixed(2),
                gain:(cue.gain!=null?cue.gain:0.8), loop:!!cue.loop};
    A.layers=A.layers||[]; A.layers.push(copy); sel={kind:'layer',id:copy.id};
    buildTimeline(); renderEventEditor(); touch();
    if(typeof toast==='function') toast(cue.loop?'Bed duplicated at playhead':(isVoiceFile(cue.file)?'Voice line duplicated at playhead':'SFX duplicated at playhead'));
  }
  window.dupLayerCue=dupLayerCue;
  function dupSel(){
    if(sel&&sel.kind==='layer') return dupLayerCue();
    if(!sel||sel.kind) return; const ev=evByRef(sel); if(!ev) return; const copy=JSON.parse(JSON.stringify(ev)); copy.id=uid(); const off=DUR_T.has(ev.type)?(ev.duration_s||1):0.5; copy.t=+clamp(ev.t+off,0,SC.duration_s||90).toFixed(2); (SC.tracks[sel.dev]=SC.tracks[sel.dev]||[]).push(copy); sel={dev:sel.dev,id:copy.id}; scrubT=copy.t; buildTimeline(); renderEventEditor(); setScrubUiLight(); previewAt(scrubT); touch(); if(typeof toast==='function') toast('Duplicated'); }
  function nudgeSel(dir,big){ if(!sel||sel.kind) return; const ev=evByRef(sel); if(!ev) return; ev.t=+clamp(ev.t+dir*(big?1:0.1),0,SC.duration_s||90).toFixed(2); scrubT=ev.t; buildTimeline(); renderEventEditor(); setScrubUiLight(); previewAt(scrubT); touch(); }
  document.addEventListener('keydown',e=>{
    if(!window.__editing) return;
    const t=e.target, typing = !!(t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    const mod = e.ctrlKey||e.metaKey;
    if(mod && !e.shiftKey && (e.key==='z'||e.key==='Z')){ if(typing) return; e.preventDefault(); window.undoEdit(); return; }
    if(mod && (e.key==='c'||e.key==='C')){ if(typing) return; copySel(); return; }
    if(mod && (e.key==='v'||e.key==='V')){ if(typing) return; e.preventDefault(); pasteClip(); return; }
    if(mod && (e.key==='d'||e.key==='D')){ if(typing) return; e.preventDefault(); dupSel(); return; }
    if(typing) return;
    if(e.key==='Delete'||e.key==='Backspace'){ if(sel){ e.preventDefault(); window.deleteEvent(); } return; }
    if(e.key==='ArrowLeft'){ if(sel&&!sel.kind){ e.preventDefault(); nudgeSel(-1,e.shiftKey); } return; }
    if(e.key==='ArrowRight'){ if(sel&&!sel.kind){ e.preventDefault(); nudgeSel(1,e.shiftKey); } return; }
    if(e.key===' '||e.code==='Space'){ if(e.repeat){ e.preventDefault(); return; } const ae=document.activeElement; if(ae&&/^(BUTTON|INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return; e.preventDefault(); if(window.__playing) window.edtStop(); else window.edtPlay(); return; }
  });
  window.previewVo2=function(){ const vo=$('#setVo').value; if(!vo){ if(typeof toast==='function') toast('No voice-over selected'); return; } try{ const a=new Audio('/audio/'+vo); a.play().catch(()=>{}); }catch(e){} };

  // ---- import an audio track from a YouTube link ----
  window.toggleYt=function(){ const r=$('#ytRow'); if(r) r.style.display = (r.style.display==='none'?'':'none'); };
  window.importYouTube=async function(){
    if(!SC) return;
    const url=($('#ytUrl').value||'').trim(), kind=$('#ytKind').value||'music';
    const stat=$('#ytStat'), btn=$('#ytGo');
    if(!/^https?:\/\//i.test(url)){ stat.textContent='Enter a valid URL'; return; }
    btn.disabled=true; stat.textContent='Importing… this can take up to a minute';
    try{
      const resp=await fetch('/api/audio/youtube',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,kind})});
      if(!resp.ok){ let d=''; try{ d=(await resp.json()).detail; }catch(_){} throw new Error(d||('HTTP '+resp.status)); }
      const r=await resp.json();
      if(typeof loadAudioLists==='function') await loadAudioLists();
      SC.audio=SC.audio||{};
      if(kind==='music'){ SC.audio.music=r.file; }
      else if(kind==='voice'){ SC.audio.voiceover=r.file; if(SC.audio.voiceover_at_s==null) SC.audio.voiceover_at_s=0; }
      else { SC.audio.layers=SC.audio.layers||[]; SC.audio.layers.push({id:'c'+Math.random().toString(36).slice(2,8),file:r.file,at_s:+(scrubT||0).toFixed(2),gain:0.8,loop:true}); }
      fillSettings(); buildTimeline(); renderEventEditor();
      clearTimeout(saveTimer); await commit();     // persist now so a quick Play uses the new track
      stat.textContent='Added: '+(r.title||r.file);
      $('#ytUrl').value='';
    }catch(e){ stat.textContent='Import failed: '+e.message; }
    btn.disabled=false;
  };

  // ---- upload an audio file into the library ----
  window.toggleUp=function(){ const r=$('#upRow'); if(r) r.style.display=(r.style.display==='none'?'':'none'); };
  window.uploadAudio=async function(){
    if(!SC) return;
    const inp=$('#upFile'), cat=($('#upKind')&&$('#upKind').value)||'music', stat=$('#upStat'), btn=$('#upGo');
    const f=inp&&inp.files&&inp.files[0];
    if(!f){ if(stat) stat.textContent='Choose a file first.'; return; }
    if(btn) btn.disabled=true; if(stat) stat.textContent='Uploading…';
    try{
      const title=(($('#upTitle')&&$('#upTitle').value)||'').trim();
      const qs='filename='+encodeURIComponent(f.name)+'&category='+encodeURIComponent(cat)+(title?'&title='+encodeURIComponent(title):'');
      const resp=await fetch('/api/audio/upload?'+qs,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});
      if(!resp.ok){ let d=''; try{ d=(await resp.json()).detail; }catch(_){} throw new Error(d||('HTTP '+resp.status)); }
      const r=await resp.json();
      if(typeof loadAudioLists==='function') await loadAudioLists();
      SC.audio=SC.audio||{};
      if(cat==='voice'){ SC.audio.voiceover=r.file; if(SC.audio.voiceover_at_s==null) SC.audio.voiceover_at_s=0; }
      else if(cat==='music'){ SC.audio.music=r.file; }
      else { SC.audio.layers=SC.audio.layers||[]; SC.audio.layers.push({id:'c'+Math.random().toString(36).slice(2,8),file:r.file,at_s:+(scrubT||0).toFixed(2),gain:0.8,loop:true}); }
      fillSettings(); buildTimeline(); renderEventEditor();
      clearTimeout(saveTimer); await commit();
      if(stat) stat.textContent='Added: '+(r.title||r.file);
      if(inp) inp.value='';
    }catch(e){ if(stat) stat.textContent='Upload failed: '+e.message; }
    if(btn) btn.disabled=false;
  };

  // ---- direct manipulation: scrub by ruler/track, drag/resize blocks ----
  function trackOriginX(){ const t=document.querySelector('.tltrack'); return t?t.getBoundingClientRect().left:0; }
  function timeFromX(x){ const dur=SC.duration_s||90; return clamp((x-trackOriginX())/PPS,0,dur); }
  function fieldLabelById(id,text){ const el=document.getElementById(id); if(el) fieldLabel(el,text); }
  function setScrubUiLight(){ const dur=SC.duration_s||90; const s=document.getElementById('edtScrub'); if(s) s.value=Math.round(scrubT/dur*1000); const tt=document.getElementById('edtTime'); if(tt) tt.textContent=scrubT.toFixed(1)+' / '+dur.toFixed(0)+'s'; movePlayhead(); }

  function rulerScrub(e){
    if(!SC) return;
    e.preventDefault(); beginScrub();
    const mv=me=>setScrub(timeFromX(me.clientX));
    setScrub(timeFromX(e.clientX));
    const up=()=>{ window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up); endScrub(true); };
    window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
  }

  function audioBlockDown(e){
    e.preventDefault(); e.stopPropagation();
    const el=e.currentTarget, kind=el.dataset.kind, id=el.dataset.id;
    sel = (kind==='layer') ? {kind:'layer',id} : {kind:'vo'};
    document.querySelectorAll('.tlblock.sel').forEach(x=>x.classList.remove('sel')); el.classList.add('sel');
    renderEventEditor();
    const dur=SC.duration_s||90;
    const cue = kind==='layer' ? (SC.audio.layers||[]).find(c=>c.id===id) : null;
    const x0=e.clientX, t0 = kind==='vo' ? (SC.audio.voiceover_at_s||0) : (cue?cue.at_s||0:0);
    let moved=false;
    try{ el.setPointerCapture(e.pointerId); }catch(_){}
    const mv=me=>{ const nt=clamp(t0+(me.clientX-x0)/PPS,0,dur); if(Math.abs(me.clientX-x0)>2) moved=true;
      if(kind==='vo'){ SC.audio.voiceover_at_s=nt; syncVoSlider(); }
      else if(cue){ cue.at_s=nt; if(cue.loop) el.style.width=Math.max(10,(dur-nt)*PPS)+'px'; }
      el.style.left=(nt*PPS)+'px'; scrubT=nt; setScrubUiLight(); };
    const up=()=>{ window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up);
      try{ el.releasePointerCapture(e.pointerId); }catch(_){}
      buildTimeline(); renderEventEditor(); if(moved) touch(); };
    window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
  }
  function syncVoSlider(){ const A=SC.audio||{}; const s=$('#setVoAt'); if(s) s.value=A.voiceover_at_s||0; const l=$('#setVoAtVal'); if(l) l.textContent=(A.voiceover_at_s||0).toFixed(1)+' s'; }
  function optsFor(list,cur){ return list.map(f=>'<option value="'+f+'"'+(f===cur?' selected':'')+'>'+f.replace(/\.[^.]+$/,'')+'</option>').join(''); }
  window.previewClip=function(f){ if(!f) return; try{ new Audio('/audio/'+f).play().catch(()=>{}); }catch(e){} };

  // ---- clip trimming: listen, mark In/Out, cut to a new clip ----
  let TRIM=null;   // {file, inS, outS, apply}
  window.openTrim=function(file, applyCb){
    if(!file){ if(typeof toast==='function') toast('No clip to trim'); return; }
    document.querySelectorAll('.trimbox').forEach(x=>x.remove());
    TRIM={file, inS:0, outS:durOf(file)||0, apply:applyCb};
    const host=$('#edtEvent'); if(!host) return;
    const box=document.createElement('div'); box.className='trimbox';
    box.innerHTML=
      '<div class="trimhd">✂ Trim · '+file.replace(/\.[^.]+$/,'')+'</div>'+
      '<audio id="trimAud" controls preload="metadata" src="/audio/'+file+'"></audio>'+
      '<div class="trimrow"><button class="mini" onclick="trimSetIn()">Set In</button>'+
        '<span class="muted" id="trimInL">in 0.0s</span>'+
        '<button class="mini" onclick="trimSetOut()">Set Out</button>'+
        '<span class="muted" id="trimOutL">out '+(TRIM.outS?TRIM.outS.toFixed(1):'—')+'s</span></div>'+
      '<div class="trimrow"><button class="mini" style="flex:1" onclick="trimPreview()">▶ Preview In→Out</button>'+
        '<button class="mini primary" id="trimApplyBtn" style="flex:1;color:var(--accent-ink)" onclick="trimApply()">✂ Trim &amp; use</button></div>'+
      '<div class="muted" id="trimStat" style="font-size:12px;margin-top:6px">Play the clip, mark In and Out, then trim.</div>';
    host.appendChild(box);
    const a=$('#trimAud');
    a.addEventListener('loadedmetadata',()=>{ if((!TRIM.outS||TRIM.outS<=0)&&isFinite(a.duration)){ TRIM.outS=+a.duration.toFixed(2); const l=$('#trimOutL'); if(l) l.textContent='out '+a.duration.toFixed(1)+'s'; } });
  };
  window.trimSetIn=function(){ const a=$('#trimAud'); if(!a||!TRIM) return; TRIM.inS=+a.currentTime.toFixed(2); const l=$('#trimInL'); if(l) l.textContent='in '+TRIM.inS.toFixed(1)+'s'; };
  window.trimSetOut=function(){ const a=$('#trimAud'); if(!a||!TRIM) return; TRIM.outS=+a.currentTime.toFixed(2); const l=$('#trimOutL'); if(l) l.textContent='out '+TRIM.outS.toFixed(1)+'s'; };
  window.trimPreview=function(){ const a=$('#trimAud'); if(!a||!TRIM) return; try{ a.currentTime=TRIM.inS||0; }catch(e){} a.play().catch(()=>{});
    const stop=()=>{ if(a.currentTime>=(TRIM.outS||a.duration||0)){ a.pause(); a.removeEventListener('timeupdate',stop); } };
    a.addEventListener('timeupdate',stop); };
  window.trimApply=async function(){
    if(!TRIM) return; const file=TRIM.file, inS=TRIM.inS, outS=TRIM.outS, apply=TRIM.apply;
    const stat=$('#trimStat'), btn=$('#trimApplyBtn');
    if(!(outS>inS) || (outS-inS)<0.2){ if(stat) stat.textContent='Set Out later than In (min 0.2s)'; return; }
    if(btn) btn.disabled=true; if(stat) stat.textContent='Trimming…';
    try{
      const resp=await fetch('/api/audio/trim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file,start:inS,end:outS})});
      if(!resp.ok){ let d=''; try{ d=(await resp.json()).detail; }catch(_){} throw new Error(d||('HTTP '+resp.status)); }
      const r=await resp.json();
      if(typeof loadAudioLists==='function') await loadAudioLists();
      if(typeof apply==='function') apply(r.file);
      clearTimeout(saveTimer); await commit();     // persist the swap to the trimmed clip immediately
      document.querySelectorAll('.trimbox').forEach(x=>x.remove());
      if(typeof toast==='function') toast('Trimmed to '+(r.dur||'?')+'s');
    }catch(e){ if(stat) stat.textContent='Trim failed: '+e.message; if(btn) btn.disabled=false; }
  };
  window.trimMusic=function(){ const m=SC&&SC.audio&&SC.audio.music; if(!m){ if(typeof toast==='function') toast('No music set'); return; }
    openTrim(m, nf=>{ SC.audio.music=nf; fillSettings(); buildTimeline(); touch(); }); };

  function blockDown(e){
    if(!SC) return;
    if(e.currentTarget.dataset.kind==='vo' || e.currentTarget.dataset.kind==='layer') return audioBlockDown(e);
    if(e.currentTarget.classList.contains('music')) return;   // music bed block is non-interactive
    e.preventDefault(); e.stopPropagation();
    const el=e.currentTarget, dev=el.dataset.dev, id=el.dataset.id;
    sel={dev,id};
    document.querySelectorAll('.tlblock.sel').forEach(x=>x.classList.remove('sel'));
    el.classList.add('sel'); renderEventEditor();
    const ev=evByRef(sel); if(!ev) return;
    const dur=SC.duration_s||90;
    const resizing=e.target.classList.contains('tlresize') && DUR_T.has(ev.type);
    const x0=e.clientX, t0=ev.t, d0=ev.duration_s||1;
    let moved=false;
    try{ el.setPointerCapture(e.pointerId); }catch(_){}
    const mv=me=>{
      const dx=me.clientX-x0; if(Math.abs(dx)>2) moved=true;
      if(resizing){ ev.duration_s=clamp(d0+dx/PPS,0.1,dur-ev.t); el.style.width=Math.max(8,ev.duration_s*PPS)+'px'; fieldLabelById('evDur',ev.duration_s.toFixed(1)+'s'); const n=document.getElementById('evDurn'); if(n) n.value=ev.duration_s.toFixed(2); }
      else { ev.t=clamp(t0+dx/PPS,0,dur); el.style.left=(ev.t*PPS)+'px'; scrubT=ev.t; setScrubUiLight(); fieldLabelById('evT',ev.t.toFixed(1)+'s'); const n=document.getElementById('evTn'); if(n) n.value=ev.t.toFixed(2); }
      previewAt(scrubT);
    };
    const up=()=>{ window.removeEventListener('pointermove',mv); window.removeEventListener('pointerup',up);
      try{ el.releasePointerCapture(e.pointerId); }catch(_){}
      buildTimeline(); if(moved){ renderEventEditor(); touch(); } previewAt(scrubT); };
    window.addEventListener('pointermove',mv); window.addEventListener('pointerup',up);
  }

  // ---- Extend: make this scene longer (new lights continue it; YouTube music gets longer) ----
  const MAX_SCENE_S=300;
  function lenText(s){ return (typeof fmtLen==='function') ? fmtLen(s) : (Math.round(s)+' s'); }
  function fillExtend(){
    const go=$('#extGo'), st=$('#extStat'); if(!go||!SC) return;
    const room=MAX_SCENE_S-(+SC.duration_s||0);      // the server adds at most what's left
    go.disabled = room<5;
    if(st && room<5) st.textContent='This scene is at the 5-minute limit.';
  }
  function busy(on, title, sub){
    const v=$('#busyVeil'); if(!v) return;
    v.hidden=!on; if(on){ $('#busyTitle').textContent=title||'Working…'; $('#busySub').textContent=sub||''; }
  }
  async function postJSON(url, body){
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const t=await r.text(); let j=null; try{ j=JSON.parse(t); }catch(_){}
    if(!r.ok) throw new Error((j&&typeof j.detail==='string')?j.detail:(t||String(r.status)).slice(0,200));
    return j;
  }
  let EXTENDING=false;
  async function adoptServerCopy(before){
    // take the server's (longer) scene into the editor; Undo goes back to `before`
    const fresh=await jget('/api/scenes/'+SC.id);
    if(before!=null){ UNDO.push(before); if(UNDO.length>80) UNDO.shift(); updateUndoBtn(); }
    SC=fresh; SC.tracks=SC.tracks||{}; sel=null;
    if(typeof loadAudioLists==='function'){ try{ await loadAudioLists(); }catch(_){} }
    fillSettings(); buildTimeline(); renderEventEditor(); setScrub(Math.min(scrubT,SC.duration_s));
    LAST=snap(); setSaveStatus('saved');
  }
  window.extendScene=async function(){
    if(!SC||EXTENDING) return;
    const go=$('#extGo'); EXTENDING=true; if(go){ go.disabled=true; go.blur(); }
    const secs=+($('#extSecs').value||60), note=($('#extNote').value||'').trim(), st=$('#extStat');
    clearTimeout(saveTimer); await commit();                       // your edits land first
    const before=snap(), id=SC.id, fromS=+SC.duration_s;
    busy(true,'Extending “'+(SC.name||'scene')+'” by '+lenText(secs)+'…','Writing the new part'+(SC.audio&&SC.audio.music?' and fetching more of the song':'')+'. This can take a minute.');
    try{
      const r=await postJSON('/api/scenes/'+id+'/extend',{seconds:secs,note:note||null,from_s:fromS});
      if(!SC||SC.id!==id) return;                                   // you left for another scene
      await adoptServerCopy(before);
      let msg='Added '+lenText(r.added_s)+' ('+r.events+' events'+(r.beat?', on the beat':'')+'). Now '+lenText(r.duration_s)+'.';
      if(r.music&&r.music.extended&&!r.music.loops) msg+=' The song plays through.';
      else if(r.music&&r.music.extended&&r.music.loops) msg+=' The song ends at '+lenText(r.music.dur)+', then loops.';
      else if(r.music&&r.music.loops) msg+=' The music loops to fill it.';
      if(st) st.textContent=msg; $('#extNote').value='';
      if(typeof toast==='function') toast('Extended to '+lenText(r.duration_s));
    }catch(e){
      // the reply may have been lost (phone slept, Wi-Fi blip) after the server
      // saved: if the scene did get longer, take that version rather than
      // letting this stale copy autosave over it
      let adopted=false;
      try{ const chk=await jget('/api/scenes/'+id);
        if(SC&&SC.id===id&&chk&&+chk.duration_s>fromS+0.01){ await adoptServerCopy(before); adopted=true; } }catch(_){}
      if(st) st.textContent = adopted ? 'Extended to '+lenText(SC.duration_s)+' (the reply got lost, but it saved).' : 'Extend failed: '+e.message;
      if(typeof toast==='function') toast(adopted?'Extended':'Extend failed');
    }finally{ EXTENDING=false; busy(false); fillExtend(); }
  };

  // ---- scene management ----
  window.newScene=async function(){
    try{ const r=await jget('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'New Scene',duration_s:30})});
      if(typeof toast==='function') toast('New scene'); window.enterEditor(r.id);
    }catch(e){ if(typeof toast==='function') toast('Create failed: '+e.message); }
  };
  window.duplicateScene=async function(){
    if(!SC) return;
    try{ const r=await jget('/api/scenes/'+SC.id+'/duplicate',{method:'POST'}); if(typeof toast==='function') toast('Duplicated'); window.enterEditor(r.id);
    }catch(e){ if(typeof toast==='function') toast('Duplicate failed: '+e.message); }
  };
  let _delArm=0;
  window.deleteScene=async function(){
    if(!SC) return;
    const btn=document.getElementById('btnDelScene');
    if(Date.now()-_delArm>3000){ _delArm=Date.now(); if(btn) btn.textContent='🗑 Tap again to delete'; setTimeout(()=>{ if(btn&&Date.now()-_delArm>=3000) btn.textContent='🗑 Delete'; },3100); return; }
    _delArm=0; if(btn) btn.textContent='🗑 Delete';
    try{ await jget('/api/scenes/'+SC.id,{method:'DELETE'}); if(typeof toast==='function') toast('Deleted'); window.__editing=false; restoreStage(); if(typeof go==='function') go('scenes'); if(typeof loadScenes==='function') loadScenes();
    }catch(e){ if(typeof toast==='function') toast('Delete failed: '+e.message); }
  };
})();
