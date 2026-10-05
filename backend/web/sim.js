/* Sim only: play a scene in this browser. The stage, the editor's playhead and
   this device's speaker follow it; the real fixtures, the fogger and the Pi's
   speaker are never touched, so you can rehearse a new scene while the show
   runs in the yard.

   simEvaluate() is a line-for-line port of domain/scenes._evaluate_track (the
   same evaluation the rig and the scrub preview use). tests/test_sim.py runs
   both on random scenes and checks they agree. Keep them in step. */
(function(){
  "use strict";
  var MIN_STROBE_S = 0.4;

  function hexRgb(h){
    h = String(h || "#000000").replace(/^#+/, "");
    var v = [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    return v.map(function(x){ return isFinite(x) ? x : 0; });
  }
  function lerp(a, b, f){ return a + (b - a) * Math.max(0.0, Math.min(1.0, f)); }
  function ease(f, curve){
    f = Math.max(0.0, Math.min(1.0, f));
    if(curve === "ease_in") return f * f;
    if(curve === "ease_out") return 1 - (1 - f) * (1 - f);
    if(curve === "ease_in_out") return 3 * f * f - 2 * f * f * f;
    return f;
  }
  function num(v, dflt){ return v === undefined ? dflt : Number(v); }

  function evalTrack(events, t){
    var rgb = [0, 0, 0], bri = 0.0, strobe = 0.0, fog = 0.0;
    var evs = (events || []).slice().sort(function(a, b){ return a.t - b.t; });   // stable, like sorted()
    for(var idx = 0; idx < evs.length; idx++){
      var e = evs[idx], p = e.params || {};
      if(e.t > t) break;                       // nothing from here on has started
      var dur = Math.max(e.duration_s || 0, 0);
      var active = e.t <= t && t <= e.t + dur;
      var done = t > e.t + dur;
      var fdur = Math.max(e.duration_s || 0, 0.0);
      if(idx + 1 < evs.length){ var gap = evs[idx + 1].t - e.t; if(gap > 0) fdur = Math.min(fdur, gap); }
      var f, tgt;
      if(e.type === "set_color"){ rgb = hexRgb(p.color); bri = num(p.brightness, 1.0); }
      else if(e.type === "set_brightness"){ bri = num(p.brightness, 1.0); }
      else if(e.type === "blackout"){ rgb = [0, 0, 0]; bri = 0.0; fog = 0.0; strobe = 0.0; }
      else if(e.type === "fade_color"){
        f = ease(fdur ? (t - e.t) / fdur : 1.0, p.curve || "linear");
        tgt = hexRgb(p.color);
        rgb = [Math.trunc(lerp(rgb[0], tgt[0], f)), Math.trunc(lerp(rgb[1], tgt[1], f)), Math.trunc(lerp(rgb[2], tgt[2], f))];
        bri = lerp(bri, num(p.brightness, 1.0), f);
      }
      else if(e.type === "fade_brightness"){
        f = ease(fdur ? (t - e.t) / fdur : 1.0, p.curve || "linear");
        bri = lerp(bri, num(p.brightness, 0.0), f);
      }
      else if(e.type === "pulse"){
        if(active){
          var hz = num(p.rate_hz, 1.0), base = num(p.brightness, 1.0);
          bri = base * (0.5 + 0.5 * Math.sin(2 * Math.PI * hz * (t - e.t)));
          if(p.color) rgb = hexRgb(p.color);
        }
      }
      else if(e.type === "strobe"){
        var sdur = Math.max(Math.max(e.duration_s || 0, 0.0), MIN_STROBE_S);
        if(idx + 1 < evs.length){ var sgap = evs[idx + 1].t - e.t; if(sgap > 0) sdur = Math.min(sdur, sgap); }
        var sActive = e.t <= t && t <= e.t + sdur, sDone = t > e.t + sdur;
        strobe = sActive ? num(p.rate_hz, 10) : (sDone ? 0.0 : strobe);
        if(sActive && p.color){ rgb = hexRgb(p.color); bri = num(p.brightness, 1.0); }
      }
      else if(e.type === "fog"){
        fog = active ? num(p.intensity, 0.6) : (done ? 0.0 : fog);
      }
    }
    return {rgb: rgb, brightness: Math.trunc(Math.max(0, Math.min(255, bri * 255))),
            strobe_hz: strobe, fog: Math.trunc(Math.max(0, Math.min(255, fog * 255)))};
  }

  function hex2(v){ return ("0" + v.toString(16)).slice(-2); }
  /* {dev: {hex, brightness, strobe, strobe_hz, fog}} -- the shape /api/scenes/{id}/state returns */
  window.simEvaluate = function(scene, t, capHz){
    var out = {}, tracks = (scene && scene.tracks) || {};
    Object.keys(tracks).forEach(function(dev){
      var s = evalTrack(tracks[dev], t);
      var hz = s.strobe_hz && capHz > 0 ? Math.min(s.strobe_hz, capHz) : s.strobe_hz;
      out[dev] = {hex: "#" + hex2(s.rgb[0]) + hex2(s.rgb[1]) + hex2(s.rgb[2]), brightness: s.brightness,
                  strobe: s.strobe_hz > 0, strobe_hz: Math.round(hz * 100) / 100, fog: s.fog};
    });
    return out;
  };
  window.__simTrack = evalTrack;               // test hook

  // ---------------- the sim player ----------------
  var SIM = {scene: null, start: 0, loop: false, mute: [], raf: 0, paused: null};   // paused: the held time
  function capHz(){ try{ return (typeof OUTPUT !== "undefined" && OUTPUT && +OUTPUT.strobe_max_hz) || 0; }catch(e){ return 0; } }
  function simTime(now){
    if(SIM.paused !== null) return SIM.paused;
    var d = +SIM.scene.duration_s || 0, raw = Math.max(0, (now - SIM.start) / 1000);
    return SIM.loop && d > 0 ? raw % d : Math.min(raw, d);
  }
  function render(t, playing){
    var sc = SIM.scene;
    var st = {t: t, duration_s: sc.duration_s, playing: playing, scene: sc.id, sim: true,
              devices: window.simEvaluate(sc, t, capHz())};
    if(window.__editing && window.__stripMuted) st = window.__stripMuted(st);
    if(window.stageUpdate && !window.__scrubbing) window.stageUpdate(st, sc);   // dragging the playhead previews instead
    if(window.__editing && window.__edtSync && window.__editSceneId && window.__editSceneId() === sc.id) window.__edtSync(t);
    var pg = document.getElementById("pg_" + sc.id);
    if(pg) pg.style.width = Math.min(100, t / (sc.duration_s || 1) * 100) + "%";
    var card = document.getElementById("sc_" + sc.id);
    if(card) card.classList.toggle("playing", playing);
  }
  function tick(){
    if(!SIM.scene || SIM.paused !== null) return;
    var d = +SIM.scene.duration_s || 0, raw = (performance.now() - SIM.start) / 1000;
    if(!SIM.loop && raw >= d){ render(d, false); window.simStop(); return; }
    render(simTime(performance.now()), true);
    SIM.raf = requestAnimationFrame(tick);
  }
  /* opts: {loop, mute: [lane keys], from: seconds} */
  window.simPlay = function(scene, opts){
    opts = opts || {};
    window.simStop();
    if(!scene) return;
    var from = Math.max(0, Math.min(+opts.from || 0, +scene.duration_s || 0));
    SIM.scene = scene; SIM.loop = !!opts.loop; SIM.mute = (opts.mute || []).slice(); SIM.paused = null;
    SIM.start = performance.now() - from * 1000;
    window.__simScene = scene.id;
    if(typeof playAudio === "function") playAudio(scene, SIM.loop, SIM.mute, from);   // this device's speaker
    SIM.raf = requestAnimationFrame(tick);
  };
  window.simStop = function(){
    var had = SIM.scene;
    if(SIM.raf) cancelAnimationFrame(SIM.raf);
    SIM.raf = 0;
    SIM.paused = null;
    if(had){
      SIM.scene = null; window.__simScene = null;
      if(typeof stopAudio === "function") stopAudio();
      var pg = document.getElementById("pg_" + had.id); if(pg) pg.style.width = "0";
      var card = document.getElementById("sc_" + had.id); if(card) card.classList.remove("playing");
    }
  };
  window.simSeek = function(t){
    if(!SIM.scene) return;
    var d = +SIM.scene.duration_s || 0; t = Math.max(0, Math.min(+t || 0, d));
    if(SIM.paused !== null){ SIM.paused = t; render(t, true); return; }   // stays paused, at the new spot
    SIM.start = performance.now() - t * 1000;
    if(typeof playAudio === "function") playAudio(SIM.scene, SIM.loop, SIM.mute, t);
  };
  /* hold the scene where it is (the sound stops); simResume() carries on from there */
  window.simPause = function(){
    if(!SIM.scene || SIM.paused !== null) return;
    SIM.paused = simTime(performance.now());
    if(SIM.raf) cancelAnimationFrame(SIM.raf);
    SIM.raf = 0;
    if(typeof stopAudio === "function") stopAudio();
    render(SIM.paused, true);
  };
  window.simResume = function(){
    if(!SIM.scene || SIM.paused === null) return;
    var t = SIM.paused;
    SIM.paused = null;
    SIM.start = performance.now() - t * 1000;
    if(typeof playAudio === "function") playAudio(SIM.scene, SIM.loop, SIM.mute, t);
    SIM.raf = requestAnimationFrame(tick);
  };
  window.simPaused = function(){ return !!SIM.scene && SIM.paused !== null; };
  window.simScene = function(){ return SIM.scene; };
  window.simSetMute = function(arr){ SIM.mute = (arr || []).slice(); };
  window.simActive = function(id){ return !!SIM.scene && (id === undefined || SIM.scene.id === id); };
  window.simTime = function(){ return SIM.scene ? simTime(performance.now()) : 0; };

  // ---------------- Sim / Live: one setting per phone or laptop ----------------
  var KEY = "hd.playMode";
  window.__armed = false;
  function stored(){ try{ return localStorage.getItem(KEY) === "sim" ? "sim" : "live"; }catch(e){ return "live"; } }
  /* What Play does right now: "sim" or "live". While the show is armed it's always sim. */
  window.playMode = function(){ return window.__armed ? "sim" : stored(); };
  window.setPlayMode = function(m){
    if(m === "live" && window.__armed){ if(typeof toast === "function") toast("The show is armed, so Play stays in Sim. Disarm to play on the yard."); return; }
    try{ localStorage.setItem(KEY, m === "sim" ? "sim" : "live"); }catch(e){}
    // a switch takes effect now: whatever was playing the other way stops
    if(m === "live" && window.simActive()) window.simStop();
    if(m === "sim" && window.__playing && !window.__armed){
      if(typeof stopAudio === "function") stopAudio();
      try{ fetch("/api/play/stop", {method: "POST"}); }catch(e){}
    }
    window.renderPlayMode();
    if(typeof toast === "function") toast(m === "sim" ? "Play runs in the simulator only" : "Play runs on the yard");
  };
  window.renderPlayMode = function(){
    var mode = window.playMode();
    document.querySelectorAll("[data-pm]").forEach(function(box){
      box.classList.toggle("armed", !!window.__armed);
      box.querySelectorAll("button[data-mode]").forEach(function(b){
        var on = b.getAttribute("data-mode") === mode;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
        if(b.getAttribute("data-mode") === "live"){
          b.disabled = !!window.__armed;
          b.title = window.__armed ? "The show is armed: Play can't touch the yard. Disarm to play live."
                                   : "Play drives the real lights, fog and the Pi's speaker";
        }
      });
      var note = box.querySelector(".pmnote");
      if(note) note.textContent = window.__armed ? "armed" : "";
    });
  };
  /* called by the status poll with the show's armed state */
  window.simSetArmed = function(armed){
    armed = !!armed;
    if(armed === window.__armed) return;
    window.__armed = armed;
    window.renderPlayMode();
  };
  document.addEventListener("DOMContentLoaded", function(){ window.renderPlayMode(); });
})();
