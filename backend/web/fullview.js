/* Full-screen play view: the house large, with the scene's lanes (each light,
   the fogger, the sound) sliding past a fixed playhead, so you can see each
   trigger coming and the moment it fires. A thin whole-scene bar on top shows
   the song's sections and lets you jump.

   It follows the Sim / Live switch. In Sim it plays in this browser (sim.js).
   In Live it plays on the yard and follows the player's clock; the house is
   drawn here from the same evaluation the rig uses, so it stays smooth on a
   phone. While the show is armed it's always Sim. */
(function(){
  "use strict";
  var SIZABLE = {fade_color: 1, fade_brightness: 1, strobe: 1, pulse: 1, fog: 1};
  var FS = {open: false, scene: null, raf: 0, t: 0, lastNow: 0, lanes: [], prev: null, next: null,
            wentFull: false, drag: false};
  var PAST = 0.3;                                  // the playhead sits 30% in: what's done, then what's coming
  function $(id){ return document.getElementById(id); }
  function clamp(v, a, b){ return Math.max(a, Math.min(b, v)); }
  function fmt(t){ t = Math.max(0, t || 0); var m = Math.floor(t / 60), s = t - 60 * m;
    return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1); }
  function mode(){ return window.playMode ? window.playMode() : "live"; }
  function capHz(){ try{ return (typeof OUTPUT !== "undefined" && OUTPUT && +OUTPUT.strobe_max_hz) || 0; }catch(e){ return 0; } }
  function rgba(hex, a){
    var h = String(hex || "#888888").replace(/^#+/, "");
    var r = parseInt(h.slice(0, 2), 16) || 0, g = parseInt(h.slice(2, 4), 16) || 0, b = parseInt(h.slice(4, 6), 16) || 0;
    return "rgba(" + r + "," + g + "," + b + "," + a + ")";
  }
  function title(file){
    try{
      var L = (typeof AUDIOLIST !== "undefined" && AUDIOLIST) || {};
      var all = [].concat(L.music || [], L.voice || [], L.sfx || []);
      var hit = all.find(function(x){ return x.file === file; });
      if(hit && hit.title) return hit.title;
    }catch(e){}
    return String(file || "").replace(/\.[a-z0-9]+$/i, "");
  }
  function clipLen(file, dflt){
    try{ var d = (typeof AUDIOLIST !== "undefined" && AUDIOLIST && AUDIOLIST.dur) || {}; return +d[file] || dflt; }
    catch(e){ return dflt; }
  }

  // ---------------- what's on screen ----------------
  function lanesFor(sc){
    var tracks = sc.tracks || {}, out = [];
    var devs = (typeof SIGDEVS !== "undefined" && SIGDEVS) || [];
    var ids = devs.map(function(d){ return d.id; }).filter(function(id){ return (tracks[id] || []).length; });
    Object.keys(tracks).forEach(function(id){ if(ids.indexOf(id) < 0 && (tracks[id] || []).length) ids.push(id); });
    ids.forEach(function(id){
      var d = devs.find(function(x){ return x.id === id; });
      var evs = tracks[id].slice().sort(function(a, b){ return a.t - b.t; });
      out.push({kind: d && d.kind === "dmx_fog" ? "fog" : "light", id: id, name: d ? d.name : id, evs: evs,
                maxDur: evs.reduce(function(m, e){ return Math.max(m, +e.duration_s || 0); }, 0)});
    });
    var au = sc.audio || {}, snd = [], d = +sc.duration_s || 0;
    if(au.music) snd.push({t: 0, dur: d, label: title(au.music), kind: "music"});
    if(au.voiceover) snd.push({t: +au.voiceover_at_s || 0, dur: clipLen(au.voiceover, 3), label: title(au.voiceover), kind: "voice"});
    (au.layers || []).forEach(function(c){
      var at = +c.at_s || 0;
      snd.push({t: at, dur: c.loop ? Math.max(0.5, d - at) : clipLen(c.file, 2), label: title(c.file), kind: c.loop ? "bed" : "sfx"});
    });
    if(snd.length) out.push({kind: "sound", id: "__sound", name: "Sound", items: snd.sort(function(a, b){ return a.t - b.t; }), maxDur: d});
    return out;
  }
  function firstFrom(evs, t){                     // first event with e.t >= t
    var lo = 0, hi = evs.length;
    while(lo < hi){ var mid = (lo + hi) >> 1; if(evs[mid].t < t) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // ---------------- the clock ----------------
  function liveOn(id){ var c = window.__liveClock; return !!(c && c.playing && c.scene === id); }
  function liveEstimate(now){ var c = window.__liveClock; return c.t + (now - c.at) / 1000; }
  function running(){
    var id = FS.scene.id;
    if(window.simActive && simActive(id)) return simPaused() ? "paused" : "sim";
    if(liveOn(id)) return "live";
    return "";
  }

  // ---------------- drawing ----------------
  function fit(cv){
    var dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if(cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)){ cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    var g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    return {g: g, w: w, h: h};
  }
  function drawLanes(t, states){
    var cv = $("fsLanes"); if(!cv) return;
    var c = fit(cv), g = c.g, W = c.w, H = c.h;
    g.clearRect(0, 0, W, H);
    var labW = clamp(Math.round(W * 0.2), 78, 132), axisH = 20, x0 = labW, aw = Math.max(40, W - labW - 6);
    var win = clamp(aw / 45, 6, 18), pps = aw / win, t0 = t - PAST * win, t1 = t0 + win, px = x0 + PAST * aw;
    var n = Math.max(1, FS.lanes.length), lh = clamp((H - axisH - 4) / n, 14, 84);   // every lane fits
    // time axis
    g.font = "11px ui-monospace,SFMono-Regular,monospace"; g.textBaseline = "middle";
    for(var s = Math.ceil(t0); s <= t1; s++){
      if(s < 0) continue;
      var x = x0 + (s - t0) * pps, major = s % 5 === 0;
      g.fillStyle = major ? "rgba(232,230,227,.28)" : "rgba(232,230,227,.10)";
      g.fillRect(x, axisH - (major ? 7 : 4), 1, major ? 7 : 4);
      g.fillRect(x, axisH, 1, n * lh);
      if(major && Math.abs(x - px) > 44){ g.fillStyle = "#8A8FA0"; g.fillText(fmt(s).replace(/\.0$/, ""), x + 3, 8); }   // NOW gets the spot by the playhead
    }
    // the part that has already played sits a little darker
    g.fillStyle = "rgba(0,0,0,.28)"; g.fillRect(x0, axisH, px - x0, n * lh);
    FS.lanes.forEach(function(L, i){
      var y = axisH + i * lh, mid = y + lh / 2, top = y + 4, bh = lh - 8;
      g.fillStyle = i % 2 ? "rgba(255,255,255,.02)" : "rgba(255,255,255,.045)";
      g.fillRect(0, y, W, lh);
      // label + the light's colour right now
      var st = states[L.id];
      if(L.kind !== "sound"){
        var on = st && (st.brightness > 3 || st.fog > 3);
        g.beginPath(); g.arc(12, mid, 6, 0, 6.2832);
        g.fillStyle = L.kind === "fog" ? (st && st.fog > 3 ? "rgba(159,179,200," + (0.3 + 0.7 * st.fog / 255) + ")" : "#272C3B")
                                       : (on ? rgba(st.hex, 0.35 + 0.65 * st.brightness / 255) : "#272C3B");
        g.fill();
        if(st && st.strobe){ g.strokeStyle = "#fff"; g.lineWidth = 2; g.stroke(); }
      } else {
        g.fillStyle = "#8A8FA0"; g.fillText("♪", 8, mid);
      }
      g.fillStyle = "#E8E6E3"; g.font = "600 12px 'IBM Plex Sans',system-ui,sans-serif";
      var nm = L.name, room = labW - 28;
      if(g.measureText(nm).width > room) nm = nm.replace(/\s+(light|lights|machine|fixture|par)$/i, "");   // "Skelly Light" -> "Skelly"
      g.save(); g.beginPath(); g.rect(22, y, labW - 26, lh); g.clip(); g.fillText(nm, 24, mid); g.restore();
      g.font = "11px ui-monospace,SFMono-Regular,monospace";
      g.save(); g.beginPath(); g.rect(x0, y, aw, lh); g.clip();
      if(L.kind === "sound"){
        L.items.forEach(function(it){
          if(it.t > t1 || it.t + it.dur < t0) return;
          var x = x0 + (it.t - t0) * pps, w = Math.max(4, it.dur * pps);
          var col = it.kind === "music" ? "rgba(107,174,122," : it.kind === "voice" ? "rgba(212,162,76," : "rgba(159,179,200,";
          var act = it.t <= t && t <= it.t + it.dur;
          g.fillStyle = col + (act ? ".55)" : ".28)"); g.fillRect(x, top, w, bh);
          g.fillStyle = col + ".95)"; g.fillRect(x, top, 2, bh);
          g.fillStyle = "#E8E6E3"; g.fillText(it.label, Math.max(x, x0) + 6, mid);
        });
      } else {
        var evs = L.evs, k = firstFrom(evs, t0 - L.maxDur - 0.01);
        for(; k < evs.length; k++){
          var e = evs[k];
          if(e.t > t1) break;
          var dur = SIZABLE[e.type] ? Math.max(0, +e.duration_s || 0) : 0;
          if(e.t + dur < t0) continue;
          var p = e.params || {}, x = x0 + (e.t - t0) * pps, w = dur ? Math.max(3, dur * pps) : 3;
          var done = e.t + dur < t, act = e.t <= t && t <= e.t + dur;
          var a = (done ? 0.42 : act ? 1 : 0.85) * (0.45 + 0.55 * (p.brightness == null ? 1 : clamp(+p.brightness, 0, 1)));
          var col = e.type === "fog" ? "#9FB3C8" : e.type === "blackout" ? "#000000"
                  : (e.type === "set_brightness" || e.type === "fade_brightness") ? "#c9c4bc" : (p.color || "#e8a33d");
          if(e.type === "fade_color" || e.type === "fade_brightness"){
            var gr = g.createLinearGradient(x, 0, x + w, 0);
            gr.addColorStop(0, rgba(col, a * 0.12)); gr.addColorStop(1, rgba(col, a * 0.8));
            g.fillStyle = gr; g.fillRect(x, top + bh * 0.25, w, bh * 0.75);
          } else if(e.type === "blackout"){
            g.fillStyle = "rgba(0,0,0,.9)"; g.fillRect(x, top, Math.max(5, w), bh);
            g.strokeStyle = "rgba(224,90,71,.8)"; g.lineWidth = 1; g.strokeRect(x + 0.5, top + 0.5, Math.max(5, w) - 1, bh - 1);
          } else {
            g.fillStyle = rgba(col, e.type === "pulse" ? a * 0.55 : a); g.fillRect(x, top, w, bh);
            var hz = +p.rate_hz || 0;
            if((e.type === "strobe" || e.type === "pulse") && hz > 0 && pps / hz >= 4){   // the flashes themselves
              g.fillStyle = e.type === "strobe" ? "rgba(255,255,255," + (0.75 * a) + ")" : rgba(col, Math.min(1, a + 0.2));
              for(var sx = x; sx < x + w; sx += pps / hz) g.fillRect(sx, top, e.type === "strobe" ? 1.5 : 2, bh);
            }
          }
          if(t >= e.t && t - e.t < 0.18){               // it just fired
            g.strokeStyle = "rgba(255,255,255,.95)"; g.lineWidth = 2;
            g.strokeRect(x - 1, top - 2, Math.max(5, w) + 2, bh + 4);
          }
        }
      }
      g.restore();
    });
    // the playhead
    g.fillStyle = "#E8A33D"; g.fillRect(px - 1, axisH - 4, 2, n * lh + 4);
    g.font = "700 10px 'IBM Plex Sans',system-ui,sans-serif"; g.fillText("NOW", px + 4, axisH - 9);
  }
  function drawOverview(t){
    var cv = $("fsOver"); if(!cv) return;
    var c = fit(cv), g = c.g, W = c.w, H = c.h, sc = FS.scene, d = +sc.duration_s || 1;
    g.clearRect(0, 0, W, H);
    g.fillStyle = "#1F2330"; g.fillRect(0, 0, W, H);
    var secs = (sc.gen && Array.isArray(sc.gen.sections)) ? sc.gen.sections : [];
    g.font = "600 10px 'IBM Plex Sans',system-ui,sans-serif"; g.textBaseline = "middle";
    if(secs.length){
      secs.forEach(function(s){
        var x = (+s.start || 0) / d * W, w = ((+s.end || d) - (+s.start || 0)) / d * W;
        var col = (Array.isArray(s.palette) && s.palette[0]) || s.accent || "#E8A33D";
        g.fillStyle = rgba(col, 0.42); g.fillRect(x, 0, Math.max(1, w - 1), H);
        if(w > 38){ g.fillStyle = "#E8E6E3"; g.fillText(String(s.kind || "").toUpperCase(), x + 5, H / 2); }
      });
    } else {
      if(!FS.density){                                // how busy each stretch is
        var B = 120, cnt = new Array(B).fill(0), mx = 1;
        FS.lanes.forEach(function(L){ (L.evs || []).forEach(function(e){ var b = Math.min(B - 1, Math.floor(e.t / d * B)); cnt[b]++; }); });
        cnt.forEach(function(v){ mx = Math.max(mx, v); }); FS.density = cnt.map(function(v){ return v / mx; });
      }
      var bw = W / FS.density.length;
      g.fillStyle = "rgba(232,163,61,.35)";
      FS.density.forEach(function(v, i){ var h = Math.max(1, v * (H - 6)); g.fillRect(i * bw, H - h - 3, Math.max(1, bw - 1), h); });
    }
    var px = clamp(t / d, 0, 1) * W;
    g.fillStyle = "rgba(0,0,0,.35)"; g.fillRect(px, 0, W - px, H);    // still to come, dimmed
    g.fillStyle = "#E8A33D"; g.fillRect(px - 1, 0, 3, H);
  }

  // ---------------- the loop ----------------
  function frame(now){
    if(!FS.open) return;
    var sc = FS.scene, d = +sc.duration_s || 0, run = running(), dt = FS.lastNow ? (now - FS.lastNow) / 1000 : 0;
    FS.lastNow = now;
    if(run === "sim" || run === "paused"){ FS.t = simTime(); }
    else if(run === "live"){
      var est = clamp(liveEstimate(now), 0, d), err = est - FS.t;
      FS.t = Math.abs(err) > 0.6 ? est : clamp(FS.t + dt + 0.12 * err, 0, d);   // smooth between status polls
    }
    var states = window.simEvaluate ? simEvaluate(sc, FS.t, capHz()) : {};
    if(run !== "sim" && run !== "paused" && window.stageUpdate){   // sim.js draws its own; otherwise we do
      var st = {t: FS.t, duration_s: d, playing: run === "live", scene: sc.id, sim: run !== "live", devices: states};
      if(window.__editing && window.__stripMuted && window.__editSceneId && __editSceneId() === sc.id) st = window.__stripMuted(st);
      window.stageUpdate(st, sc);
    }
    drawOverview(FS.t);
    drawLanes(FS.t, states);
    var tm = $("fsTime"); if(tm) tm.textContent = fmt(FS.t) + " / " + fmt(d);
    var pb = $("fsPlay");
    if(pb) pb.textContent = run === "sim" ? "❚❚" : run === "live" ? "■" : "▶";
    var mb = $("fsMode");
    if(mb){
      var m = run === "live" ? "live" : run ? "sim" : mode();
      mb.textContent = m === "live" ? "LIVE" : (window.__armed ? "SIM · show armed" : "SIM");
      mb.className = "fs-mode " + m;
    }
    FS.raf = requestAnimationFrame(frame);
  }

  // ---------------- controls ----------------
  function muteList(){ try{ return (window.__editing && window.__editMute) ? window.__editMute() : []; }catch(e){ return []; } }
  function toggle(){
    var sc = FS.scene, run = running(), d = +sc.duration_s || 0;
    var from = FS.t >= d - 0.05 ? 0 : FS.t;
    if(run === "sim"){ simPause(); return; }
    if(run === "paused"){ simResume(); return; }
    if(run === "live"){ if(typeof stopScene === "function") stopScene(); return; }
    if(mode() === "sim"){ simPlay(sc, {from: from, mute: muteList()}); return; }
    if(typeof playScene === "function"){                 // on the yard, then follow it
      playScene(sc.id, false).then(function(){
        if(from > 0.1){
          fetch("/api/play/seek", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({t: +from.toFixed(2)})}).catch(function(){});
          if(typeof playAudio === "function") playAudio(sc, false, [], from);
        }
      });
    }
  }
  function seekTo(t){
    var sc = FS.scene, d = +sc.duration_s || 0, run = running();
    t = clamp(t, 0, d); FS.t = t;
    if(run === "sim" || run === "paused") simSeek(t);
    else if(run === "live"){
      fetch("/api/play/seek", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({t: +t.toFixed(2)})}).catch(function(){});
      if(typeof playAudio === "function") playAudio(sc, false, [], t);
      if(window.__liveClock) window.__liveClock = {scene: sc.id, t: t, at: performance.now(), playing: true};
    }
  }
  function build(){
    if($("fsView")) return;
    var v = document.createElement("div");
    v.id = "fsView"; v.className = "fsview"; v.hidden = true;
    v.setAttribute("role", "dialog"); v.setAttribute("aria-label", "Full-screen play view");
    v.innerHTML =
      '<div class="fs-top">' +
        '<button class="fs-btn" id="fsClose" title="Close (Esc)" aria-label="Close">✕</button>' +
        '<div class="fs-title"><span id="fsName"></span><span class="fs-mode" id="fsMode"></span></div>' +
        '<span class="fs-time" id="fsTime"></span>' +
        '<button class="fs-stop" id="fsStopAll" title="Stop everything">■ STOP</button>' +
      '</div>' +
      '<div class="fs-stagehost" id="fsStageHost"></div>' +
      '<div class="fs-ctl">' +
        '<button class="fs-btn" id="fsBack" title="Back 5 s (←)" aria-label="Back 5 seconds">⏪</button>' +
        '<button class="fs-btn fs-play" id="fsPlay" title="Play or pause (Space)" aria-label="Play or pause">▶</button>' +
        '<button class="fs-btn" id="fsFwd" title="Forward 5 s (→)" aria-label="Forward 5 seconds">⏩</button>' +
        '<canvas id="fsOver" class="fs-over" title="The whole scene: tap to jump"></canvas>' +
      '</div>' +
      '<canvas id="fsLanes" class="fs-lanes"></canvas>';
    document.body.appendChild(v);
    $("fsClose").onclick = function(){ window.fsClose(); };
    $("fsStopAll").onclick = function(){ if(typeof stopAll === "function") stopAll(); };
    $("fsPlay").onclick = toggle;
    $("fsBack").onclick = function(){ seekTo(FS.t - 5); };
    $("fsFwd").onclick = function(){ seekTo(FS.t + 5); };
    var ov = $("fsOver");
    var at = function(ev){ var r = ov.getBoundingClientRect(); return clamp((ev.clientX - r.left) / r.width, 0, 1) * (+FS.scene.duration_s || 0); };
    ov.addEventListener("pointerdown", function(ev){ FS.drag = true; try{ ov.setPointerCapture(ev.pointerId); }catch(e){} FS.t = at(ev); });
    ov.addEventListener("pointermove", function(ev){ if(FS.drag && running() !== "sim" && running() !== "live") FS.t = at(ev); });
    ov.addEventListener("pointerup", function(ev){ if(!FS.drag) return; FS.drag = false; seekTo(at(ev)); });
    document.addEventListener("fullscreenchange", function(){
      if(!document.fullscreenElement && FS.open && FS.wentFull) window.fsClose();   // Esc out of full screen closes the view
    });
    window.addEventListener("keydown", function(ev){
      if(!FS.open) return;
      var k = ev.key;
      if(k === "Escape"){ window.fsClose(); }
      else if(k === " " || ev.code === "Space"){ toggle(); }
      else if(k === "ArrowLeft"){ seekTo(FS.t - (ev.shiftKey ? 1 : 5)); }
      else if(k === "ArrowRight"){ seekTo(FS.t + (ev.shiftKey ? 1 : 5)); }
      else return;
      ev.preventDefault(); ev.stopImmediatePropagation();
    }, true);
  }

  /* sceneOrId: a scene object (the editor's working copy) or an id.
     opts.autoplay: start it in the simulator (never starts the yard by itself). */
  window.fsOpen = async function(sceneOrId, opts){
    opts = opts || {};
    var sc = sceneOrId;
    if(typeof sc === "string"){
      var playing = window.simScene && simScene();
      if(playing && playing.id === sc) sc = playing;
      else { try{ sc = await api("/api/scenes/" + sc); }catch(e){ if(typeof toast === "function") toast("Couldn't open the scene: " + e.message); return; } }
    }
    if(!sc || !sc.id) return;
    build();
    if(FS.open) window.fsClose();
    FS.scene = sc; FS.lanes = lanesFor(sc); FS.density = null; FS.lastNow = 0;
    FS.t = (window.simActive && simActive(sc.id)) ? simTime() : (liveOn(sc.id) ? liveEstimate(performance.now()) : 0);
    $("fsName").textContent = sc.name || sc.id;
    var stage = $("stage");
    if(stage){ FS.prev = stage.parentNode; FS.next = stage.nextSibling; $("fsStageHost").appendChild(stage); }
    FS.open = true; window.__fsOpen = true;
    $("fsView").hidden = false; document.body.classList.add("fs-on");
    FS.wentFull = false;
    var el = $("fsView");
    if(el.requestFullscreen && !opts.noFullscreen){
      el.requestFullscreen().then(function(){ FS.wentFull = true; }).catch(function(){});
    }
    FS.raf = requestAnimationFrame(frame);
    if(opts.autoplay && mode() === "sim" && !(window.simActive && simActive(sc.id))) simPlay(sc, {mute: muteList()});
  };
  window.fsClose = function(){
    if(!FS.open) return;
    FS.open = false; window.__fsOpen = false;
    if(FS.raf) cancelAnimationFrame(FS.raf);
    FS.raf = 0;
    var stage = $("stage");
    if(stage && FS.prev){ if(FS.next && FS.next.parentNode === FS.prev) FS.prev.insertBefore(stage, FS.next); else FS.prev.appendChild(stage); }
    $("fsView").hidden = true; document.body.classList.remove("fs-on");
    if(document.fullscreenElement && document.exitFullscreen){ FS.wentFull = false; document.exitFullscreen().catch(function(){}); }
  };
  window.fsIsOpen = function(){ return FS.open; };
  /* the stage's ⛶ button: whatever this page is playing (Sim), or the yard's scene */
  window.fsOpenCurrent = function(){
    var s = window.simScene && simScene();
    if(s) return window.fsOpen(s);
    var c = window.__liveClock;
    if(c && c.playing && c.scene) return window.fsOpen(c.scene);
    if(typeof toast === "function") toast("Press ⛶ on a scene to watch it full screen");
  };
})();
