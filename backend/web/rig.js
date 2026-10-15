/* System -> Your rig: add, change and remove the lights, foggers and yard
   lights. The server checks every change (domain/rig.py) and refuses one that
   would leave a device broken; changes wait while the show is armed or a scene
   is playing. After a change, the Live cards, signal strip and stage reload. */
(function(){
  "use strict";
  var R = {data: null, edit: null, form: null, delArm: 0, saving: false};
  var KIND = {dmx_fixture: "light", dmx_fog: "fogger", static_rgb: "yard light"};

  function $(id){ return document.getElementById(id); }
  function esc(v){ return String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function say(m){ if(typeof toast === "function") toast(m); }
  async function call(path, opts){
    var r = await fetch(path, opts), t = await r.text(), j = null;
    try{ j = t ? JSON.parse(t) : null; }catch(e){}
    if(!r.ok) throw new Error((j && typeof j.detail === "string") ? j.detail : (t || String(r.status)).slice(0, 240));
    return j;
  }
  function json(method, body){ return {method: method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})}; }

  function profById(id){ return ((R.data && R.data.profiles) || []).find(function(p){ return p.id === id; }); }
  function profLabel(p){ return p ? (p.vendor === "Generic" ? "" : p.vendor + " ") + p.model + " · " + p.mode_name : "no profile"; }
  function devs(){ return (R.data && R.data.devices) || []; }
  function isDmx(d){ return d.kind === "dmx_fixture" || d.kind === "dmx_fog"; }

  // ---------------- list ----------------
  function render(){
    var host = $("rigCard"); if(!host) return;
    var d = R.data;
    if(!d){ host.innerHTML = '<div class="muted">Loading…</div>'; return; }
    var list = devs().filter(function(x){ return isDmx(x) || x.kind === "static_rgb"; })
      .sort(function(a, b){ return (isDmx(b) - isDmx(a)) || ((a.dmx_address || 999) - (b.dmx_address || 999)) || a.name.localeCompare(b.name); });
    var nL = list.filter(function(x){ return x.kind === "dmx_fixture"; }).length;
    var nF = list.filter(function(x){ return x.kind === "dmx_fog"; }).length;
    var h = '<div class="rgsum"><span>' + nL + ' light' + (nL === 1 ? '' : 's') + ' · ' + nF + ' fogger' + (nF === 1 ? '' : 's') +
      (d.block.needed_end ? ' · DMX 1 to ' + d.block.needed_end : '') + '</span>' +
      '<span title="The output frame covers this much of the DMX universe. It grows on its own as you add devices.">Output: channels 1 to ' + d.block.end + '</span></div>';
    if(d.busy) h += '<div class="rgiss warn">' + esc(d.busy_reason) + '</div>';
    (d.issues || []).forEach(function(i){ h += '<div class="rgiss ' + i.level + '">' + esc(i.message) + '</div>'; });
    h += '<div class="rglist">';
    if(!list.length) h += '<div class="muted" style="padding:8px 0">Nothing here yet. Add your first light below.</div>';
    list.forEach(function(x){
      var p = profById(x.profile_id), bad = (d.issues || []).some(function(i){ return (i.devices || [i.device]).indexOf(x.id) >= 0 && i.level === "error"; });
      var lead = x.follows ? devs().find(function(y){ return y.id === x.follows; }) : null;
      var where = x.span ? (x.span[0] === x.span[1] ? x.span[0] : x.span[0] + ' to ' + x.span[1]) : (x.dmx_address || '?');
      var sub = isDmx(x) ? ('DMX ' + where + ' · ' + esc(profLabel(p)))
                         : 'Yard light (not DMX). Set its color on Live';
      if(lead) sub += ' · follows ' + esc(lead.name);
      if(x.enabled === false) sub += ' · not in use';
      h += '<div class="rgrow' + (bad ? ' bad' : '') + (x.enabled === false ? ' off' : '') + '">' +
           '<span class="rgicon ' + x.kind + '"></span>' +
           '<div class="grow"><div class="rgname">' + esc(x.name) + (p && !p.tested && isDmx(x) ? ' <span class="rgbadge">starter profile</span>' : '') + '</div>' +
           '<div class="sub">' + sub + '</div></div>' +
           '<button class="ghost rgbtn" data-edit="' + esc(x.id) + '"' + (R.edit ? ' disabled' : '') + '>Edit</button></div>';
      if(R.edit === x.id) h += formHtml();
    });
    h += '</div>';
    if(R.edit && R.edit.indexOf("new:") === 0) h += formHtml();
    if(!R.edit){
      h += '<div class="btns rgadd"><button class="primary" data-new="dmx_fixture">＋ Light</button>' +
           '<button class="ghost" data-new="dmx_fog">＋ Fogger</button>' +
           '<button class="ghost" data-new="static_rgb">＋ Yard light</button></div>';
    }
    h += '<div class="sub" style="margin-top:12px">New devices start along the ground on the stage picture. Drag them into place with Scenes → Edit layout. ' +
         'A light that follows another plays that light\'s part in any scene without a part of its own, so new lights join the scenes you already have.</div>';
    host.innerHTML = h;
    wire();
  }

  // ---------------- the form ----------------
  function formHtml(){
    var f = R.form, isNew = R.edit.indexOf("new:") === 0, dmx = f.kind !== "static_rgb";
    var h = '<div class="rgform">';
    h += '<div class="rghd">' + (isNew ? 'New ' + KIND[f.kind] : 'Edit ' + esc(f.name || KIND[f.kind])) + '</div>';
    h += '<div class="field"><label>Name</label><input type="text" id="rgName" maxlength="40" value="' + esc(f.name) + '" placeholder="e.g. Porch Light"></div>';
    if(dmx){
      var want = f.kind === "dmx_fog" ? "fog" : "light";
      var opts = (R.data.profiles || []).filter(function(p){ return p.kind === want; });
      h += '<div class="field"><label>Fixture profile</label><select id="rgProf">' +
        (f.profile_id ? '' : '<option value="">Choose one…</option>') +
        opts.map(function(p){ return '<option value="' + esc(p.id) + '"' + (p.id === f.profile_id ? ' selected' : '') + '>' +
          esc(profLabel(p)) + ' (' + p.channel_count + ' ch' + (p.tested ? ', tested' : ', starter') + (p.source === "yours" ? ', yours' : '') + ')</option>'; }).join("") +
        '</select><div class="sub" id="rgProfNote"></div>' +
        '<div class="sub">Not listed? Write a profile for it (see the fixture profiles guide) and restart the app.</div></div>';
      h += '<div class="field"><label>DMX address <span class="muted" id="rgSpan"></span></label><div class="rowline">' +
        '<input type="number" id="rgAddr" min="1" max="512" value="' + (f.dmx_address || '') + '" style="flex:1">' +
        '<button class="ghost rgbtn" id="rgNext" type="button">Next free</button></div>' +
        '<div class="sub" id="rgAddrNote">Set the same number on the ' + KIND[f.kind] + '\'s own display.</div></div>';
      var peers = devs().filter(function(x){ return x.kind === f.kind && x.id !== R.edit; });
      h += '<div class="field"><label>Follows</label><select id="rgFollow">' +
        '<option value="">Nobody: only its own parts</option>' +
        peers.map(function(x){ return '<option value="' + esc(x.id) + '"' + (x.id === f.follows ? ' selected' : '') + '>' + esc(x.name) + '</option>'; }).join("") +
        '</select><div class="sub">In any scene with no part written for this ' + KIND[f.kind] + ', it plays this one\'s part.</div></div>';
    }
    if(!isNew) h += '<div class="field rowline" style="justify-content:space-between"><span class="sub" style="margin:0">In use</span>' +
      '<label class="switch" style="margin:0"><input type="checkbox" id="rgOn"' + (f.enabled !== false ? ' checked' : '') + '><span class="track"></span></label></div>';
    h += '<div class="rgerr" id="rgErr" hidden></div>';
    h += '<div class="btns"><button class="primary" id="rgSave">' + (isNew ? 'Add it' : 'Save') + '</button>' +
         '<button class="ghost" id="rgCancel">Cancel</button>';
    if(!isNew && f.kind === "dmx_fixture") h += '<button class="ghost" id="rgFlash" title="Flash it white for 3 seconds">Flash it</button>';
    if(!isNew) h += '<button class="ghost danger" id="rgDel">Delete</button>';
    h += '</div></div>';
    return h;
  }

  function spanText(){
    var f = R.form, p = profById(f.profile_id), a = +f.dmx_address;
    if(!p || !a) return {text: "", clash: null};
    var b = a + p.channel_count - 1;
    var clash = devs().find(function(x){ return x.id !== R.edit && x.span && a <= x.span[1] && x.span[0] <= b; });
    return {text: "uses " + (a === b ? "channel " + a : "channels " + a + " to " + b), clash: clash, end: b};
  }
  function showSpan(){
    var el = $("rgSpan"), note = $("rgAddrNote"); if(!el) return;
    var s = spanText();
    el.textContent = s.text ? "· " + s.text : "";
    if(s.clash) note.innerHTML = '<span style="color:var(--danger)">Overlaps ' + esc(s.clash.name) + ' (' + s.clash.span[0] + ' to ' + s.clash.span[1] + ').</span>';
    else if(s.end > 512) note.innerHTML = '<span style="color:var(--danger)">That runs past channel 512.</span>';
    else note.textContent = "Set the same number on the " + KIND[R.form.kind] + "'s own display.";
    var pn = $("rgProfNote"), p = profById(R.form.profile_id);
    if(pn) pn.textContent = p ? (p.notes || "") : "";
  }
  async function nextFree(){
    var f = R.form; if(!f.profile_id) return;
    try{
      var r = await call("/api/rig/suggest-address?profile_id=" + encodeURIComponent(f.profile_id) +
                         (R.edit.indexOf("new:") === 0 ? "" : "&exclude=" + encodeURIComponent(R.edit)));
      if(r.address){ f.dmx_address = r.address; var el = $("rgAddr"); if(el) el.value = r.address; showSpan(); }
      else say("No room left in the DMX universe.");
    }catch(e){ say(e.message); }
  }
  function wire(){
    var host = $("rigCard");
    host.querySelectorAll("[data-edit]").forEach(function(b){ b.onclick = function(){ window.rigEdit(b.dataset.edit); }; });
    host.querySelectorAll("[data-new]").forEach(function(b){ b.onclick = function(){ window.rigNew(b.dataset.new); }; });
    if(!R.form) return;
    var f = R.form;
    var on = function(id, ev, fn){ var el = $(id); if(el) el.addEventListener(ev, fn); };
    on("rgName", "input", function(e){ f.name = e.target.value; });
    on("rgProf", "change", function(e){ f.profile_id = e.target.value; if(!f.dmx_address) nextFree(); showSpan(); });
    on("rgAddr", "input", function(e){ f.dmx_address = +e.target.value || null; showSpan(); });
    on("rgNext", "click", nextFree);
    on("rgFollow", "change", function(e){ f.follows = e.target.value; });
    on("rgOn", "change", function(e){ f.enabled = e.target.checked; });
    on("rgSave", "click", save);
    on("rgCancel", "click", function(){ R.edit = null; R.form = null; render(); });
    on("rgFlash", "click", flash);
    on("rgDel", "click", del);
    showSpan();
  }

  // ---------------- actions ----------------
  window.rigEdit = function(id){
    var x = devs().find(function(d){ return d.id === id; }); if(!x) return;
    R.edit = id; R.delArm = 0;
    R.form = {kind: x.kind, name: x.name, profile_id: x.profile_id || "", dmx_address: x.dmx_address || null,
              follows: x.follows || "", enabled: x.enabled !== false};
    render();
  };
  window.rigNew = function(kind){
    R.edit = "new:" + kind; R.delArm = 0;
    var first = kind === "static_rgb" ? null : (R.data.profiles || []).find(function(p){ return p.kind === (kind === "dmx_fog" ? "fog" : "light"); });
    var n = devs().filter(function(x){ return x.kind === kind; }).length + 1;
    R.form = {kind: kind, name: (kind === "dmx_fog" ? "Fogger " : kind === "static_rgb" ? "Yard Light " : "Light ") + n,
              profile_id: first ? first.id : "", dmx_address: null, follows: "", enabled: true};
    render();
    if(first) nextFree();
    var el = $("rgName"); if(el){ el.focus(); el.select(); }
  };
  async function after(r, msg){
    R.data = r.rig; R.edit = null; R.form = null;
    render(); say(msg);
    try{ if(window.loadDevices) await window.loadDevices(); }catch(e){}
    if(typeof loadSys === "function") loadSys(true);
  }
  async function save(){
    if(R.saving) return;
    var f = R.form, isNew = R.edit.indexOf("new:") === 0;
    if(!(f.name || "").trim()){ say("Give it a name."); return; }
    var body = {name: f.name.trim()};
    if(f.kind !== "static_rgb"){
      if(!f.profile_id){ say("Pick a fixture profile."); return; }
      body.profile_id = f.profile_id; body.dmx_address = f.dmx_address || null; body.follows = f.follows || "";
    }
    if(!isNew) body.enabled = f.enabled;
    R.saving = true;
    try{
      if(isNew){ body.kind = f.kind; if(!body.follows) delete body.follows;
                 var r = await call("/api/rig/devices", json("POST", body));
                 await after(r, "Added " + r.device.name + "."); }
      else { var r2 = await call("/api/rig/devices/" + encodeURIComponent(R.edit), json("PUT", body));
             await after(r2, "Saved."); }
    }catch(e){ showErr(e.message); }
    finally{ R.saving = false; }
  }
  function showErr(m){ var el = $("rgErr"); if(el){ el.textContent = m; el.hidden = false; } else say(m); }
  async function del(e){
    var btn = e.target;
    if(Date.now() - R.delArm > 3000){
      R.delArm = Date.now(); btn.textContent = "Tap again to delete";
      setTimeout(function(){ if(Date.now() - R.delArm >= 3000 && btn.isConnected) btn.textContent = "Delete"; }, 3100);
      return;
    }
    R.delArm = 0;
    try{
      var r = await call("/api/rig/devices/" + encodeURIComponent(R.edit), {method: "DELETE"});
      await after(r, "Deleted." + (r.unfollowed && r.unfollowed.length ? " " + r.unfollowed.join(", ") + " now play only their own parts." : ""));
    }catch(err){ showErr(err.message); }
  }
  async function flash(){
    try{ await call("/api/rig/devices/" + encodeURIComponent(R.edit) + "/identify", json("POST", {}));
         say("Watch the yard: it flashes white for 3 seconds."); }
    catch(e){ say(e.message); }
  }

  setInterval(function(){                       // keep the busy note and problems current
    var v = document.getElementById("view-system");
    if(v && v.classList.contains("active") && !document.hidden) window.rigLoad();
  }, 5000);

  window.rigLoad = async function(){
    if(R.edit) return;                       // don't yank a form out from under someone typing
    try{ R.data = await call("/api/rig"); }catch(e){ if(!R.data){ var h = $("rigCard"); if(h) h.innerHTML = '<div class="muted">Can\'t load the rig: ' + esc(e.message) + '</div>'; } return; }
    render();
  };
})();
