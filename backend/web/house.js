/* House lights card (Live tab): the yard lit for nights without a show.
   Lights only, never fog or sound. Off / Schedule / On, a brightness, how long
   each colour lasts, up to four colours per light (one holds steady), and a
   schedule of its own.

   houseDrift() mirrors haunt_director/house.py drift_rgb(); tests/test_house.py
   checks they agree. Keep them in step. */
(function(){
  "use strict";
  var GLIDE = 0.6, MAX_COLORS = 4;
  var DAYS = ["M", "T", "W", "T", "F", "S", "S"];          // Monday = 0, like Python's weekday()
  var DAYN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function hexRgb(h){
    h = String(h || "#000000").replace(/^#+/, "");
    var v = [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    return v.map(function(x){ return isFinite(x) ? x : 0; });
  }
  function hex2(v){ return ("0" + v.toString(16)).slice(-2); }
  function rgbHex(c){ return "#" + hex2(c[0]) + hex2(c[1]) + hex2(c[2]); }
  function phaseFor(i){ return (i * 0.618034) % 1; }
  function mod(a, n){ return ((a % n) + n) % n; }
  function driftRgb(colors, t, holdMin, phase){
    var cols = (colors || []).slice(0, MAX_COLORS).map(hexRgb);
    if(!cols.length) return [0, 0, 0];
    if(cols.length === 1) return cols[0];
    var n = cols.length, step = Math.max(60, holdMin * 60);
    var pos = mod(t / step + (phase || 0), n), k = Math.floor(pos), f = pos - k;
    var hold = 1 - GLIDE, g = f < hold ? 0 : (f - hold) / GLIDE;
    g = 0.5 - 0.5 * Math.cos(Math.PI * g);
    var a = cols[k % n], b = cols[(k + 1) % n];
    return [0, 1, 2].map(function(i){ return Math.round(a[i] + (b[i] - a[i]) * g); });
  }
  window.houseDrift = driftRgb;
  window.housePhase = phaseFor;

  // ---------------- the card ----------------
  var H = null, saveT = 0, saving = false, pending = false, editingUntil = 0, prev = null;
  function $(s){ return document.querySelector(s); }
  function esc(v){ return String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

  function statusText(st){
    var s = H.status || {}, mode = H.mode;
    if(mode === "off") return "Off. Choose Schedule or On to light the house.";
    if(s.reason === "show") return "The show has the yard. House lights come back when it's disarmed.";
    if(s.reason === "playing") return "A scene is playing. House lights come back when it ends.";
    if(s.reason === "stopped") return mode === "on" ? "Stopped with STOP EVERYTHING. Press On to light them again."
                                                    : "Stopped with STOP EVERYTHING. They're back at the next window, or press On.";
    if(mode === "on") return "On now, until you switch them off.";
    if(s.active && s.window) return "On until " + s.until + " (" + s.window + ").";
    if(s.next_on) return "Waiting. Next on " + s.next_on + (s.sunset_today ? " · dusk today " + s.sunset_today : "") + ".";
    return "No window coming up. Add or turn on a window below.";
  }
  function pill(){
    var s = H.status || {}, el = $("#hlPill"); if(!el) return;
    var txt = H.mode === "off" ? "off" : s.active ? "on" : s.reason === "show" ? "show" : s.reason === "stopped" ? "stopped" : "waiting";
    el.textContent = txt; el.className = "hl-pill " + txt;
  }

  function lightsHtml(){
    var order = H.light_order || Object.keys(H.lights || {});
    return order.map(function(id){
      var e = (H.lights || {})[id] || {on: true, colors: ["#ff7a1a"]}, name = (H.light_names || {})[id] || id;
      var cols = e.colors.map(function(c, j){
        return '<input type="color" value="' + esc(c) + '" aria-label="' + esc(name) + ' color ' + (j + 1) + '" onchange="houseColor(\'' + id + '\',' + j + ',this.value)">';
      }).join("");
      return '<div class="hlrow' + (e.on ? '' : ' off') + '">'
        + '<span class="hlnow" id="hlnow_' + esc(id) + '" title="What this light shows right now"></span>'
        + '<div class="grow"><div class="name">' + esc(name) + '</div>'
        + '<div class="sub">' + (e.colors.length === 1 ? 'steady' : e.colors.length + ' colors') + '</div></div>'
        + '<div class="hlcols">' + cols
        + (e.colors.length > 1 ? '<button class="mini" title="Remove the last color" onclick="houseDelColor(\'' + id + '\')">−</button>' : '')
        + (e.colors.length < MAX_COLORS ? '<button class="mini" title="Add a color" onclick="houseAddColor(\'' + id + '\')">＋</button>' : '')
        + '</div>'
        + '<label class="switch" title="Use this light for house lights"><input type="checkbox" ' + (e.on ? 'checked' : '')
        + ' onchange="houseLightOn(\'' + id + '\',this.checked)"><span class="track"></span></label></div>';
    }).join("");
  }
  function windowsHtml(){
    return (H.windows || []).map(function(w, i){
      var dusk = w.start_mode !== "clock";
      var days = DAYS.map(function(d, k){
        var on = (w.days || []).indexOf(k) >= 0;
        return '<button class="hlday' + (on ? ' on' : '') + '" title="' + DAYN[k] + '" aria-pressed="' + on + '" onclick="houseDay(' + i + ',' + k + ')">' + d + '</button>';
      }).join("");
      return '<div class="winrow">'
        + '<div class="rowline"><input type="text" value="' + esc(w.label) + '" oninput="houseWin(' + i + ',\'label\',this.value)" placeholder="Window name" style="flex:1">'
        + '<label class="switch" style="flex:none"><input type="checkbox" ' + (w.enabled ? 'checked' : '') + ' onchange="houseWin(' + i + ',\'enabled\',this.checked)"><span class="track"></span></label>'
        + '<button class="mini" style="flex:none" title="Remove this window" onclick="houseDelWindow(' + i + ')">✕</button></div>'
        + '<div class="rowline" style="margin-top:8px;gap:8px;flex-wrap:wrap">'
        + '<select onchange="houseWin(' + i + ',\'start_mode\',this.value,true)" style="flex:none;width:auto">'
        +   '<option value="dusk"' + (dusk ? ' selected' : '') + '>On at dusk</option>'
        +   '<option value="clock"' + (!dusk ? ' selected' : '') + '>On at a set time</option></select>'
        + (dusk
            ? '<input type="number" value="' + (w.dusk_offset_min || 0) + '" min="-180" max="180" oninput="houseWin(' + i + ',\'dusk_offset_min\',this.value)" style="width:64px"><span class="muted" style="font-size:12px;margin-right:6px">min vs sunset</span>'
            : '<input type="time" value="' + esc(w.time || "18:00") + '" oninput="houseWin(' + i + ',\'time\',this.value)" style="width:auto">')
        + '<span class="muted" style="font-size:12px">off at</span>'
        + '<input type="time" value="' + esc(w.end || "23:00") + '" oninput="houseWin(' + i + ',\'end\',this.value)" style="width:auto">'
        + '</div>'
        + '<div class="hldays">' + days + '</div>'
        + '</div>';
    }).join("") || '<div class="sub muted">No windows yet. Add one.</div>';
  }

  function render(full){
    if(!H || !$("#houseCard")) return;
    document.querySelectorAll("#houseCard [data-hm]").forEach(function(b){
      var on = b.getAttribute("data-hm") === H.mode;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    $("#hlStatus").textContent = statusText();
    pill();
    if(full){
      $("#hlLights").innerHTML = lightsHtml();
      $("#hlWindows").innerHTML = windowsHtml();
      $("#hlBri").value = Math.round((H.brightness || 0) * 100);
      $("#hlHold").value = Math.round(H.hold_min || 8);
    }
    $("#hlBriVal").textContent = $("#hlBri").value + "%";
    $("#hlHoldVal").textContent = $("#hlHold").value + " min";
    paintNow();
  }

  /* each light's dot shows what it's showing now; Preview runs an hour in 30 s */
  function paintNow(){
    if(!H) return;
    var order = H.light_order || [], t = Date.now() / 1000;
    if(prev){
      var el = (performance.now() - prev.start) / 1000;
      if(el > 30){ prev = null; var pb = $("#hlPrev"); if(pb) pb.textContent = "▶ Preview an hour"; }
      else t = prev.t0 + el * 120;
    }
    order.forEach(function(id, i){
      var e = (H.lights || {})[id], dot = document.getElementById("hlnow_" + id);
      if(!dot || !e) return;
      var c = e.on ? driftRgb(e.colors, t, H.hold_min || 8, phaseFor(i)) : [0, 0, 0];
      var b = e.on ? (H.brightness == null ? 0.8 : H.brightness) : 0;
      dot.style.background = rgbHex(c);
      dot.style.opacity = String(0.25 + 0.75 * b);
      dot.style.boxShadow = e.on && b > 0 ? "0 0 " + Math.round(4 + 10 * b) + "px " + rgbHex(c) : "none";
    });
  }
  setInterval(function(){ if(prev || document.querySelector("#view-live.active")) paintNow(); }, 120);

  // ---------------- saving (debounced: edits apply to the yard as you make them) ----------------
  function queue(full){
    editingUntil = Date.now() + 2500;
    if(full) render(true); else render(false);
    clearTimeout(saveT);
    saveT = setTimeout(save, 600);
  }
  async function save(){
    if(saving){ pending = true; return; }
    saving = true;
    try{
      var body = {brightness: H.brightness, hold_min: H.hold_min, lights: H.lights, windows: H.windows};
      var r = await api("/api/house", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      H.status = r.status;
      var s = $("#hlSaved"); if(s){ s.textContent = "Saved"; clearTimeout(save._t); save._t = setTimeout(function(){ s.textContent = ""; }, 1500); }
      render(false);
    }catch(e){ if(typeof toast === "function") toast("House lights: " + e.message); }
    saving = false;
    if(pending){ pending = false; save(); }
  }

  window.houseLoad = async function(force){
    if(!$("#houseCard")) return;
    if(!force && (Date.now() < editingUntil || saving)) return;      // don't trample an edit in progress
    try{
      var r = await api("/api/house");
      var full = !H || force || JSON.stringify([H.lights, H.windows, H.brightness, H.hold_min, H.light_order]) !==
                 JSON.stringify([r.lights, r.windows, r.brightness, r.hold_min, r.light_order]);
      H = r; render(full);
    }catch(e){ var st = $("#hlStatus"); if(st) st.textContent = "House lights aren't available: " + e.message; }
  };
  window.houseMode = async function(mode){
    try{
      H = await api("/api/house/mode", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode: mode})});
      render(true);
      if(typeof toast === "function") toast(mode === "on" ? "House lights on" : mode === "schedule" ? "House lights on their schedule" : "House lights off");
    }catch(e){ if(typeof toast === "function") toast("House lights: " + e.message); }
  };
  window.houseColor = function(id, j, v){ H.lights[id].colors[j] = v; queue(false); };
  window.houseAddColor = function(id){
    var c = H.lights[id].colors; if(c.length >= MAX_COLORS) return;
    c.push(c.length ? c[c.length - 1] : "#ff7a1a"); queue(true);
  };
  window.houseDelColor = function(id){ var c = H.lights[id].colors; if(c.length > 1){ c.pop(); queue(true); } };
  window.houseLightOn = function(id, on){ H.lights[id].on = !!on; queue(true); };
  window.houseWin = function(i, k, v, rerender){
    var w = H.windows[i]; if(!w) return;
    w[k] = (k === "dusk_offset_min") ? (parseInt(v, 10) || 0) : v;
    queue(!!rerender);
  };
  window.houseDay = function(i, k){
    var w = H.windows[i]; if(!w) return;
    var d = w.days || [], at = d.indexOf(k);
    if(at >= 0){ if(d.length === 1){ if(typeof toast === "function") toast("Keep at least one day"); return; } d.splice(at, 1); }
    else d.push(k);
    w.days = d.sort(function(a, b){ return a - b; }); queue(true);
  };
  window.houseAddWindow = function(){
    H.windows = H.windows || [];
    H.windows.push({label: "Evenings", enabled: true, start_mode: "dusk", dusk_offset_min: 0, time: "18:00", end: "23:00", days: [0, 1, 2, 3, 4, 5, 6]});
    queue(true);
  };
  window.houseDelWindow = function(i){ H.windows.splice(i, 1); queue(true); };
  window.housePreview = function(){
    prev = prev ? null : {start: performance.now(), t0: Date.now() / 1000};
    var pb = $("#hlPrev"); if(pb) pb.textContent = prev ? "■ Stop preview" : "▶ Preview an hour";
  };
  document.addEventListener("input", function(e){
    if(!H || !e.target) return;
    if(e.target.id === "hlBri"){ H.brightness = (+e.target.value) / 100; queue(false); }
    if(e.target.id === "hlHold"){ H.hold_min = +e.target.value; queue(false); }
  });
  document.addEventListener("DOMContentLoaded", function(){ window.houseLoad(true); });
  setInterval(function(){ if(document.querySelector("#view-live.active")) window.houseLoad(false); }, 4000);
})();
