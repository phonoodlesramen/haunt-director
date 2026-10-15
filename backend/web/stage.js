/* Visual stage — faithful port of the studio's render (beam cones, landing
   pools, light-tinted fog) over the house photo, plus placement + aim controls.
   Driven by /api/devices (positions) and /api/play/state (live colours). */
(function(){
  const stage = document.getElementById("stage");
  if(!stage) return;
  const lc = document.getElementById("fxLights"), fc = document.getElementById("fxFog");
  const lctx = lc.getContext("2d"), fctx = fc.getContext("2d");
  const svg = document.getElementById("aimline"), hint = document.getElementById("aimhint");
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  let CW = 0, CH = 0;

  const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
  const hx = h => { const n = parseInt((h||"#000000").slice(1),16); return [(n>>16)&255,(n>>8)&255,n&255]; };
  function hsv(h,s,v){ const i=Math.floor(h*6), f=h*6-i, p=v*(1-s), q=v*(1-f*s), t=v*(1-(1-f)*s);
    let r,g,b; switch(((i%6)+6)%6){case 0:r=v,g=t,b=p;break;case 1:r=q,g=v,b=p;break;case 2:r=p,g=v,b=t;break;
      case 3:r=p,g=q,b=v;break;case 4:r=t,g=p,b=v;break;default:r=v,g=p,b=q;}
    return [Math.round(r*255),Math.round(g*255),Math.round(b*255)]; }

  let DEVS = [];                 // device list with positions
  const fixEls = {}, aimEls = {}, shown = {};
  let STATE = {devices:{}, playing:false, t:0};
  let sel = null, aiming = null, editMode = false;

  function resize(){
    const w = stage.clientWidth, h = stage.clientHeight;
    for(const c of [lc, fc]){
      if(c.width !== Math.round(w*dpr) || c.height !== Math.round(h*dpr)){
        c.width = Math.round(w*dpr); c.height = Math.round(h*dpr);
      }
    }
    CW = lc.width; CH = lc.height;
  }

  // ---- fixture + aim markers (placement / aim controls) ----
  async function buildMarkers(){
    try{ DEVS = await (await fetch("/api/devices")).json(); }catch(e){ return; }
    Object.values(fixEls).forEach(e=>e.remove()); Object.values(aimEls).forEach(e=>e.remove());
    for(const k in fixEls) delete fixEls[k]; for(const k in aimEls) delete aimEls[k];
    // a device added without a stage position (say, by editing devices.json)
    // still shows up, in a row along the ground, ready to drag into place
    let unplaced = 0;
    for(const d of DEVS){
      if(!d.sim_position && (d.kind === "dmx_fixture" || d.kind === "dmx_fog" || d.kind === "static_rgb")){
        const x = Math.min(0.94, 0.1 + 0.12 * unplaced++);
        d.sim_position = {x: x, y: d.kind === "dmx_fog" ? 0.6 : 0.64};
        if(d.kind !== "dmx_fog" && !d.aim) d.aim = {x: x, y: 0.3};
      }
    }
    for(const d of DEVS){
      if(!d.sim_position) continue;
      const f = document.createElement("div");
      f.className = "fixture " + d.kind;
      f.innerHTML = '<div class="pin"></div><div class="lbl"></div>';
      f.querySelector(".lbl").textContent = d.name;
      stage.appendChild(f); fixEls[d.id] = f;
      makeDraggable(f, d, "sim_position");
      if(d.aim){
        const a = document.createElement("div");
        a.className = "aimdot"; stage.appendChild(a); aimEls[d.id] = a;
        makeDraggable(a, d, "aim");
      }
    }
    positionMarkers();
  }
  function positionMarkers(){
    let lines = "";
    for(const d of DEVS){
      const f = fixEls[d.id]; if(!f || !d.sim_position) continue;
      f.style.left = (d.sim_position.x*100)+"%"; f.style.top = (d.sim_position.y*100)+"%";
      f.classList.toggle("sel", d.id===sel);
      const a = aimEls[d.id];
      if(a && d.aim){
        a.style.left = (d.aim.x*100)+"%"; a.style.top = (d.aim.y*100)+"%";
        a.classList.toggle("show", d.id===sel);
        if(d.id===sel) lines += `<line x1="${d.sim_position.x*100}%" y1="${d.sim_position.y*100}%" x2="${d.aim.x*100}%" y2="${d.aim.y*100}%"/>`;
      }
    }
    svg.innerHTML = lines;
  }
  function savePlacement(d){
    fetch(`/api/devices/${d.id}/placement`, {method:"PUT", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({sim_position:d.sim_position, aim:d.aim, spread:d.spread})}).catch(()=>{});
  }
  function makeDraggable(el, d, key){
    let drag = false;
    el.addEventListener("pointerdown", ev=>{
      ev.preventDefault(); drag = true; el.setPointerCapture(ev.pointerId);
      sel = d.id; if(d.aim) aiming = d.id;
      if(hint && d.aim){ hint.textContent = d.name + " — drag to aim"; hint.hidden = false; }
      positionMarkers();
    });
    el.addEventListener("pointermove", ev=>{
      if(!drag) return;
      const r = stage.getBoundingClientRect();
      d[key].x = clamp((ev.clientX-r.left)/r.width, .02, .98);
      d[key].y = clamp((ev.clientY-r.top)/r.height, .03, .97);
      positionMarkers();
    });
    const end = ev=>{ if(drag){ drag=false; try{el.releasePointerCapture(ev.pointerId)}catch(e){}
      aiming = null; if(hint) hint.hidden = true; savePlacement(d); } };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  }
  window.toggleLayout = function(){
    editMode = !editMode;
    stage.classList.toggle("stage-edit", editMode);
    const b = document.getElementById("editLayout");
    if(b){ b.textContent = editMode ? "Done" : "Edit layout"; b.style.color = editMode ? "var(--accent)" : ""; }
  };

  // ---- light pass (ported) ----
  function drawLights(lights, fogD){
    lctx.clearRect(0,0,CW,CH); lctx.globalCompositeOperation = "lighter";
    const vol = 0.30 + 0.70*clamp(fogD,0,1);
    for(const L of lights){
      if(L.b <= 0.004) continue;
      const sx=L.x*CW, sy=L.y*CH, ax=L.ax*CW, ay=L.ay*CH;
      let dx=ax-sx, dy=ay-sy; const len=Math.hypot(dx,dy)||1; dx/=len; dy/=len;
      const px=-dy, py=dx, spread=L.spread||1;
      const w0=CW*0.010*spread, ex=sx+dx*len*1.28, ey=sy+dy*len*1.28, w2=CW*0.105*spread, w1=CW*0.075*spread;
      const col=`${L.r|0},${L.g|0},${L.b8|0}`;
      const g=lctx.createLinearGradient(sx,sy,ex,ey);
      g.addColorStop(0,`rgba(${col},${(0.52*L.b*vol).toFixed(3)})`);
      g.addColorStop(0.30,`rgba(${col},${(0.30*L.b*vol).toFixed(3)})`);
      g.addColorStop(0.72,`rgba(${col},${(0.13*L.b*vol).toFixed(3)})`);
      g.addColorStop(1,`rgba(${col},0)`);
      lctx.fillStyle=g; lctx.beginPath();
      lctx.moveTo(sx+px*w0,sy+py*w0); lctx.lineTo(sx-px*w0,sy-py*w0);
      lctx.lineTo(ex-px*w2,ey-py*w2); lctx.lineTo(ex+px*w2,ey+py*w2); lctx.closePath(); lctx.fill();
      const hr=w1*2.3, hg=lctx.createRadialGradient(ax,ay,0,ax,ay,hr);
      hg.addColorStop(0,`rgba(${col},${(0.80*L.b).toFixed(3)})`);
      hg.addColorStop(0.42,`rgba(${col},${(0.30*L.b).toFixed(3)})`); hg.addColorStop(1,`rgba(${col},0)`);
      lctx.fillStyle=hg; lctx.beginPath(); lctx.ellipse(ax,ay,hr,hr*0.86,0,0,6.2832); lctx.fill();
      const pr=w0*4.2, pg=lctx.createRadialGradient(sx,sy,0,sx,sy,pr);
      pg.addColorStop(0,`rgba(${col},${(0.95*L.b).toFixed(3)})`); pg.addColorStop(1,`rgba(${col},0)`);
      lctx.fillStyle=pg; lctx.beginPath(); lctx.ellipse(sx,sy,pr,pr*0.55,0,0,6.2832); lctx.fill();
    }
    lctx.globalCompositeOperation = "source-over";
  }
  function distToSeg(px,py,ax,ay,bx,by){
    const vx=bx-ax, vy=by-ay, L2=vx*vx+vy*vy;
    let t = L2 ? ((px-ax)*vx+(py-ay)*vy)/L2 : 0; t=clamp(t,0,1);
    return Math.hypot(px-(ax+vx*t), py-(ay+vy*t));
  }

  // ---- fog (ported) ----
  const FOG = {p:[], carry:0, wind:0}; const FOG_MAX = 200;
  function stepFog(dt, emit, lights, now){
    FOG.wind = 0.012 + 0.016*Math.sin(now*0.00021) + 0.006*Math.sin(now*0.00077);
    if(emit && emit.rate > 0.01){
      FOG.carry += emit.rate*46*dt;
      while(FOG.carry>=1 && FOG.p.length<FOG_MAX){
        FOG.carry-=1; const a=(Math.random()-0.5)*1.7, sp=(0.024+Math.random()*0.042)*(0.5+emit.rate);
        FOG.p.push({x0:emit.x, x:emit.x+(Math.random()-0.5)*0.010, y:emit.y+(Math.random()-0.5)*0.006,
          vx:Math.sin(a)*sp, vy:-Math.abs(Math.cos(a))*sp*0.55-0.012, r:0.010+Math.random()*0.011,
          age:0, life:4.0+Math.random()*3.0, wob:Math.random()*6.283, wobr:0.4+Math.random()*0.7,
          gy:0.010+Math.random()*0.052, tr:.35, tg:.38, tb:.45});
      }
    }
    const gb = emit ? emit.y : 0.86;
    for(let i=FOG.p.length-1;i>=0;i--){ const q=FOG.p[i]; q.age+=dt;
      if(q.age>=q.life){ FOG.p.splice(i,1); continue; }
      q.vy += 0.040*dt; const gy=gb+q.gy;
      if(q.y>gy){ q.y=gy+(q.y-gy)*0.70; q.vy*=-0.10; }
      q.wob+=dt*q.wobr; q.vx+=Math.sin(q.wob)*0.010*dt; q.vx+=FOG.wind*0.35*dt;
      q.vx*=(1-1.15*dt); q.vy*=(1-0.55*dt); q.x+=q.vx*dt; q.y+=q.vy*dt; q.r+=0.011*dt;
      if(Math.abs(q.x-q.x0)>0.26 || q.y<-0.12 || q.x<-0.1 || q.x>1.1){ FOG.p.splice(i,1); continue; }
      let tr=.26, tg=.29, tb=.37;
      for(const L of lights){ if(L.b<=0.01) continue;
        const dB=distToSeg(q.x,q.y,L.x,L.y,L.ax,L.ay), dS=Math.hypot(q.x-L.x,q.y-L.y);
        const w=L.b*(0.95/(1+Math.pow(dB/0.085,2))+0.55/(1+Math.pow(dS/0.11,2)));
        tr+=(L.r/255)*w; tg+=(L.g/255)*w; tb+=(L.b8/255)*w; }
      const mx=Math.max(1,tr,tg,tb); q.tr=tr/mx; q.tg=tg/mx; q.tb=tb/mx;
    }
  }
  function drawFog(){
    fctx.clearRect(0,0,CW,CH); fctx.globalCompositeOperation="lighter";
    for(const q of FOG.p){
      const fi=clamp(q.age/0.55,0,1), fo=clamp((q.life-q.age)/(q.life*0.55),0,1), a=0.082*fi*fo;
      if(a<0.004) continue;
      const x=q.x*CW, y=q.y*CH, r=q.r*CW, c=`${Math.round(q.tr*255)},${Math.round(q.tg*255)},${Math.round(q.tb*255)}`;
      const g=fctx.createRadialGradient(x,y,0,x,y,r);
      g.addColorStop(0,`rgba(${c},${a.toFixed(3)})`); g.addColorStop(0.55,`rgba(${c},${(a*0.45).toFixed(3)})`); g.addColorStop(1,`rgba(${c},0)`);
      fctx.fillStyle=g; fctx.beginPath(); fctx.ellipse(x,y,r,r*0.62,0,0,6.2832); fctx.fill();
    }
    fctx.globalCompositeOperation="source-over";
  }
  function fogDensity(){ let d=0; for(const q of FOG.p){ const fi=clamp(q.age/0.55,0,1), fo=clamp((q.life-q.age)/(q.life*0.55),0,1); d+=0.008*fi*fo; } return clamp(d,0,1); }

  // ---- map live play-state -> studio light model ----
  function lightsNow(now){
    const out = [];
    for(const d of DEVS){
      if(d.kind !== "dmx_fixture" || !d.aim || !d.sim_position) continue;
      const t = STATE.devices[d.id];
      const tr = t ? hx(t.hex) : [0,0,0], tb = t ? (t.brightness||0)/255 : 0;
      if(!shown[d.id]) shown[d.id] = {rgb:[0,0,0], b:0};
      const s = shown[d.id];
      // A state worked out here every frame (the simulator, the full-screen view)
      // is exact: show it as it is, so a hit lands on its beat at full strength.
      // Live status only arrives a few times a second: ease between those.
      const k = STATE.exact ? 1 : 0.20;
      s.rgb = s.rgb.map((c,i)=> c + (tr[i]-c)*k);
      s.b += (tb - s.b)*k;
      let b = s.b, c = s.rgb.slice();
      if(t && t.strobe){ const shz=Math.min(Math.max(+t.strobe_hz||3,0.5),8); b = ((now*shz/1000)%1) < 0.5 ? Math.max(b,0.9) : 0; }
      if(aiming === d.id){ b = Math.max(b,0.85); if(c[0]+c[1]+c[2] < 60) c = [232,226,208]; }
      out.push({x:d.sim_position.x, y:d.sim_position.y, ax:d.aim.x, ay:d.aim.y,
                spread:d.spread||1, r:c[0], g:c[1], b8:c[2], b:clamp(b,0,1)});
    }
    // static solar spotlights: always on at their saved colour (auto mode cycles hue),
    // independent of any scene playing.
    for(const d of DEVS){
      if(d.kind !== "static_rgb" || !d.aim || !d.sim_position) continue;
      if(d.static_on === false) continue;
      const c = (d.static_mode === "auto") ? hsv((now/12000)%1, 1, 1) : hx(d.static_color || "#ff3030");
      let b = clamp(d.static_brightness==null ? 1 : d.static_brightness, 0, 1);
      if(aiming === d.id) b = Math.max(b, 0.85);
      out.push({x:d.sim_position.x, y:d.sim_position.y, ax:d.aim.x, ay:d.aim.y,
                spread:d.spread||1, r:c[0], g:c[1], b8:c[2], b:b});
    }
    return out;
  }
  function emitter(){
    for(const d of DEVS){
      if(d.kind !== "dmx_fog" || !d.sim_position) continue;
      const t = STATE.devices[d.id];
      return {x:d.sim_position.x, y:d.sim_position.y, rate: t ? (t.fog||0)/255 : 0};
    }
    return null;
  }

  let last = performance.now();
  function frame(now){
    const dt = Math.min(0.05, (now-last)/1000); last = now;
    resize();
    if(CW){
      const lights = lightsNow(now), emit = emitter();
      stepFog(dt, emit, lights, now);
      drawLights(lights, fogDensity());
      drawFog();
    }
    const tc = document.getElementById("tcode");
    if(tc) tc.textContent = (STATE.sim ? "SIM  " : "") + (STATE.t||0).toFixed(2) + " s";
    requestAnimationFrame(frame);
  }

  window.stageUpdate = function(st, scene){
    STATE = (st && st.devices) ? st : {devices:{}, playing:false, t:0};
    const wrap = document.getElementById("stage");
    if(wrap) wrap.classList.toggle("sim", !!(st && st.sim));
    const lbl = document.getElementById("stagelabel");
    if(lbl) lbl.textContent = (st && st.playing)
      ? ((st.sim ? "Simulator only  ·  " : "") + (scene?scene.name:"Playing") + "  ·  " + (st.t||0).toFixed(1) + "s")
      : (editMode ? "Drag a fixture to place it, drag its dot to aim" : "Idle — press Play on a scene below");
  };

  window.stageReloadDevices = buildMarkers;   // call after a yard-light change so the stage updates
  window.__stageLights = (now)=>lightsNow(now||performance.now());   // test hook
  window.__stageDevs = ()=>DEVS;
  buildMarkers();
  requestAnimationFrame(frame);
})();
