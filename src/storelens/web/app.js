/* StoreLens dashboard: dependency-free. Works against the live API or an embedded snapshot (static report / site).
 *
 * Layout: a collapsible sidebar that jumps between sections, and one section at a time in the main column:
 *   Overview (key numbers + live view) · Floor plan · Activity · Insights · Data & privacy.
 * Sections are routed with the URL hash (#/overview ...) and rendered lazily when first shown, because a canvas / SVG
 * can only be measured while it is visible. */
(function () {
  'use strict';
  const SNAP = window.__STORELENS_SNAPSHOT__ || null;
  const $ = (s, r = document) => r.querySelector(s);
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  // ---- tiny DOM helper (text is always set via textContent: config names can't inject markup)
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') e.className = v; else if (k === 'style') e.setAttribute('style', v);
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
      e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return e;
  }
  const svgNS = 'http://www.w3.org/2000/svg';
  function s(tag, attrs, ...kids) {
    const e = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return e;
  }

  async function api(path) {
    if (SNAP) {
      if (!(path in SNAP)) throw new Error('This static report only contains the default views (' + path + ').');
      return SNAP[path];
    }
    const r = await fetch(path);
    if (!r.ok) throw new Error(path + ' → ' + r.status);
    return r.json();
  }
  // localStorage can be missing or throw (private windows, file://): it is only ever a convenience
  const prefs = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };

  const pct = (x, d = 0) => (x === null || x === undefined ? '–' : (x * 100).toFixed(d) + '%');
  const num = (x, d = 0) => (x === null || x === undefined ? '–' : x.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
  const secs = (x) => (x === null || x === undefined ? '–' : x >= 60 ? Math.floor(x / 60) + 'm ' + Math.round(x % 60) + 's' : Math.round(x) + 's');
  const KIND_COLOR = () => ({ fixture: css('--accent'), area: css('--teal'), staff: css('--violet') });

  const TABS = ['overview', 'floor', 'activity', 'insights', 'data'];
  const state = { cfg: null, runs: [], sel: 'all', mode: 'traffic', zone: null, d: {}, tab: 'overview', dirty: new Set(TABS), seriesErr: null };
  const views = [];  // floor-plan canvases (just the one on the Floor plan section)
  const TITLES = { overview: 'Overview', floor: 'Floor plan', activity: 'Activity', insights: 'Insights', data: 'Data & privacy' };
  const ui = { closeDrawer: () => {} };

  const suffix = () => (state.sel === 'all' ? '' : '?runs=' + state.sel);
  const sep = () => (state.sel === 'all' ? '?' : '&');
  const tsPath = () => '/api/timeseries' + suffix() + (state.zone ? sep() + 'zone=' + state.zone : '');
  const runLabel = (r) => (r.label || 'run ' + r.run_id) + ' · ' + r.start_iso.slice(0, 16).replace('T', ' ');
  
  // ------------------------------------------------------------------------------------ sidebar
  const mqMobile = window.matchMedia('(max-width: 860px)');
  function setupSidebar() {
    const app = $('#app'), side = $('#side'), tog = $('#sideToggle'), menu = $('#menuBtn'), scrim = $('#scrim');
    const saved = prefs.get('storelens.sidebar');
    app.classList.toggle('collapsed', saved ? saved === 'collapsed' : window.innerWidth < 1100);
    requestAnimationFrame(() => requestAnimationFrame(() => app.classList.remove('boot')));  // no slide-in animation on load
    const sync = () => {
      const mobile = mqMobile.matches, open = app.classList.contains('drawer-open');
      const expanded = mobile ? open : !app.classList.contains('collapsed');
      const label = mobile ? 'Close key metrics' : expanded ? 'Collapse sidebar' : 'Expand sidebar';
      tog.setAttribute('aria-expanded', String(expanded)); tog.setAttribute('aria-label', label); tog.title = label;
      menu.setAttribute('aria-expanded', String(open));
      if ('inert' in side) side.inert = mobile && !open;  // keep the off-screen drawer out of the tab order
    };
    const closeDrawer = () => { app.classList.remove('drawer-open'); sync(); };
    ui.closeDrawer = closeDrawer;
    side.addEventListener('click', (e) => { if (e.target.closest('a') && mqMobile.matches) closeDrawer(); });  // pick a section, drawer closes
    $('#brand').addEventListener('click', () => window.scrollTo(0, 0));
    tog.addEventListener('click', () => {
      if (mqMobile.matches) { closeDrawer(); menu.focus(); return; }
      const collapsed = app.classList.toggle('collapsed');
      prefs.set('storelens.sidebar', collapsed ? 'collapsed' : 'open');
      sync();
    });
    menu.addEventListener('click', () => { app.classList.add('drawer-open'); sync(); tog.focus(); });
    scrim.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && app.classList.contains('drawer-open')) { closeDrawer(); menu.focus(); } });
    mqMobile.addEventListener('change', () => { app.classList.remove('drawer-open'); sync(); });
    sync();
  }

  // ------------------------------------------------------------------------------------ key numbers (Overview)
  function renderKPIs() {
    const m = state.d.summary;
    const kpi = (label, v, sub) => h('div', { class: 'kpi' }, h('div', { class: 'l' }, label), h('div', { class: 'v' }, v), h('div', { class: 's' }, sub));
    $('#kpis').replaceChildren(
      kpi('Visitors', num(m.visitors), num(m.footfall) + ' door entries'),
      kpi('Display impressions', num(m.impressions), 'near a display (opportunity to see)'),
      kpi('Visitors who engaged', pct(m.engaged_visitor_rate), num(m.engaged_visitors) + ' of ' + num(m.visitors) + ' visitors'),
      kpi('Median engaged time', secs(m.median_engaged_s), 'stopped at a display'));
  }

  // ------------------------------------------------------------------------------------ sections / routing
  const tabFromHash = () => { const m = /^#\/([a-z]+)/.exec(location.hash); return m && TABS.includes(m[1]) ? m[1] : 'overview'; };
  const R = { overview: renderKPIs, floor: renderFloorTab, activity: renderActivity, insights: renderInsightsTab, data: renderData };
  const markDirty = () => TABS.forEach((t) => state.dirty.add(t));
  function paint(t) {  // render a tab if its data changed since it was last drawn
    if (!state.d.summary || !state.dirty.has(t)) return;
    state.dirty.delete(t);
    try { R[t](); } catch (e) { console.error('render ' + t + ' failed', e); }
  }
  function showTab(t) {
    const changed = t !== state.tab;
    state.tab = t;
    document.querySelectorAll('#nav .nav-item').forEach((a) => {
      if (a.dataset.tab === t) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    TABS.forEach((x) => { $('#pane-' + x).hidden = x !== t; });
    $('#pageTitle').textContent = TITLES[t];
    document.title = TITLES[t] + ' · ' + (state.cfg ? state.cfg.name + ' · ' : '') + 'StoreLens';
    ui.closeDrawer();
    if (changed) window.scrollTo(0, 0);
    paint(t);
  }
  function goto(t) { if (state.tab === t && tabFromHash() === t) paint(t); else location.hash = '#/' + t; }
  function setupTabs() {
    window.addEventListener('hashchange', () => showTab(tabFromHash()));
    showTab(tabFromHash());
  }
  function redraw() {  // size changed: redraw what is visible, mark the rest stale
    const cur = state.tab; markDirty(); state.dirty.delete(cur);
    if (cur === 'floor') views.forEach(drawFloor);
    else if (cur === 'activity') renderSeries();
    else state.dirty.add(cur);
  }

  // ------------------------------------------------------------------------------------ live view (simulated)
  async function initLive() {
    let info;
    try { info = await api('/api/live'); } catch (e) { return; }
    if (!info || !info.camera) return;
    const cam = $('#liveCam'), fp = $('#liveFp'), card = $('#liveCard');
    const play = $('#livePlay'), seek = $('#liveSeek'), time = $('#liveTime'), rate = $('#liveRate');
    cam.src = info.camera; fp.src = info.floorplan;
    card.hidden = false;
    $('#liveSub').textContent = Math.round(info.seconds) + ' s loop · pipeline sees the overhead camera, you see both views';
    $('#liveNote').textContent = info.note || '';
    const fmt = (t) => Math.floor(t / 60) + ':' + String(Math.floor(t % 60)).padStart(2, '0');
    let scrubbing = false, userPaused = false;
    const sync = () => {  // the camera is the clock; the floor plan follows it
      if (card.offsetParent === null) return;  // on another tab: nothing to keep in step
      if (Math.abs(fp.currentTime - cam.currentTime) > 0.12) fp.currentTime = cam.currentTime;
      if (!scrubbing && cam.duration) seek.value = String(Math.round(cam.currentTime / cam.duration * 1000));
      time.textContent = fmt(cam.currentTime) + ' / ' + fmt(cam.duration || info.seconds);
    };
    setInterval(sync, 150);
    const setPlaying = (on) => {
      if (on) { cam.play().catch(() => {}); fp.play().catch(() => {}); } else { cam.pause(); fp.pause(); }
      play.textContent = on ? 'Pause' : 'Play';
    };
    play.addEventListener('click', () => setPlaying(cam.paused));
    seek.addEventListener('input', () => { scrubbing = true; const t = seek.value / 1000 * (cam.duration || info.seconds); cam.currentTime = t; fp.currentTime = t; });
    seek.addEventListener('change', () => { scrubbing = false; });
    rate.addEventListener('change', () => { cam.playbackRate = fp.playbackRate = Number(rate.value); });
    cam.addEventListener('playing', () => { play.textContent = 'Pause'; });
    cam.addEventListener('pause', () => { play.textContent = 'Play'; });
    // play only while on screen: leaving the Overview tab (or scrolling away) pauses the videos, coming back resumes them
    const io = 'IntersectionObserver' in window ? new IntersectionObserver((es) => es.forEach((e) => {
      if (e.isIntersecting && !userPaused) setPlaying(true); else if (!e.isIntersecting) { cam.pause(); fp.pause(); }
    }), { threshold: 0.25 }) : null;
    play.addEventListener('click', () => { userPaused = cam.paused; });
    if (io) io.observe(card); else setPlaying(true);
  }

  // ------------------------------------------------------------------------------------ boot
  async function init() {
    setupSidebar();
    try {
      [state.cfg, state.runs] = await Promise.all([api('/api/config'), api('/api/runs')]);
    } catch (e) { $('#panes').prepend(h('div', { class: 'empty' }, 'Could not load data: ' + e.message)); return; }
    $('#storeName').textContent = state.cfg.name;
    const demo = window.__STORELENS_DEMO__;  // set by `storelens site`: "simulated data" label + optional call-to-action link
    if (demo && demo.label) { const chip = $('#demoChip'); chip.textContent = demo.label; chip.hidden = false; }
    if (demo && /^(https?:|mailto:)/i.test(demo.ctaUrl || '')) { const a = $('#ctaLink'); a.href = demo.ctaUrl; a.textContent = demo.ctaText || 'Request a pilot'; a.hidden = false; }
    const period = $('#period');
    period.append(h('option', { value: 'all' }, 'All runs (' + state.runs.length + ')'));
    state.runs.forEach((r) => period.append(h('option', { value: String(r.run_id) }, runLabel(r))));
    state.sel = state.runs.length ? String(state.runs[state.runs.length - 1].run_id) : 'all';
    period.value = state.sel;
    period.addEventListener('change', () => { state.sel = period.value; state.zone = null; load(); });

    const a = $('#cmpA'), b = $('#cmpB');
    state.runs.forEach((r) => { a.append(h('option', { value: r.run_id }, runLabel(r))); b.append(h('option', { value: r.run_id }, runLabel(r))); });
    if (state.runs.length >= 2) { a.value = state.runs[0].run_id; b.value = state.runs[1].run_id; }
    a.addEventListener('change', renderCompare); b.addEventListener('change', renderCompare);

    const zs = $('#seriesZone');
    zs.append(h('option', { value: '' }, 'All displays'));
    state.cfg.zones.filter((z) => z.kind !== 'staff').forEach((z) => zs.append(h('option', { value: z.id }, z.name)));
    zs.addEventListener('change', () => pickZone(zs.value || null, { toggle: false }));

    document.querySelectorAll('[data-floor]').forEach((root) => views.push(makeFloor(root)));
    setupTabs();

    let lastW = 0, rt;  // the main column changes width when the sidebar opens or closes, not only with the window
    const onSize = (w) => { if (Math.abs(w - lastW) < 1) return; lastW = w; clearTimeout(rt); rt = setTimeout(redraw, 100); };
    if ('ResizeObserver' in window) new ResizeObserver((es) => onSize(es[0].contentRect.width)).observe($('#panes'));
    else window.addEventListener('resize', () => onSize(window.innerWidth));
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { markDirty(); paint(state.tab); });

    await load();
    initLive();
  }

  async function load() {
    const sfx = suffix();
    try {
      const [summary, zones, heat, ts, flows, insights, quality] = await Promise.all([
        api('/api/summary' + sfx), api('/api/zones' + sfx), api('/api/heatmap' + sfx), api(tsPath()),
        api('/api/flows' + sfx), api('/api/insights' + sfx), api('/api/quality' + sfx)]);
      state.d = { summary, zones, heat, ts, flows, insights, quality }; state.seriesErr = null;
    } catch (e) { showError(e.message); return; }
    showError(''); markDirty(); paint(state.tab);
  }
  function showError(msg) {
    let e = $('#loadErr');
    if (!msg) { if (e) e.remove(); return; }
    if (!e) { e = h('div', { class: 'empty', id: 'loadErr', role: 'alert' }); $('#panes').prepend(e); }
    e.textContent = msg;
  }
  async function loadSeries() {  // only the activity series depends on the selected display
    try { state.d.ts = await api(tsPath()); state.seriesErr = null; }
    catch (e) { state.d.ts = []; state.seriesErr = e.message; }
  }
  async function setZone(id) { state.zone = id; await loadSeries(); markDirty(); }
  async function pickZone(id, { toggle = true } = {}) { await setZone(toggle && state.zone === id ? null : id); paint(state.tab); }

  // ------------------------------------------------------------------------------------ tab renderers
  function renderFloorTab() { views.forEach(drawFloor); renderZoneDetail(); renderZones(); }
  function renderActivity() { $('#seriesZone').value = state.zone || ''; renderSeries(); renderFlows(); }
  function renderInsightsTab() { renderInsights(); renderCompare(); }
  function renderData() { renderQuality(); }

  const fixturesRanked = () => state.d.zones.filter((z) => z.kind === 'fixture').sort((a, b) => (b.engaged_per_100 || 0) - (a.engaged_per_100 || 0));

  // ------------------------------------------------------------------------------------ floor
  const STOPS = [[0, [255, 244, 170, 0.0]], [0.25, [253, 205, 100, 0.55]], [0.55, [244, 125, 60, 0.78]], [1, [190, 30, 45, 0.92]]];
  function ramp(t) {
    t = Math.sqrt(Math.max(0, Math.min(1, t)));
    for (let i = 1; i < STOPS.length; i++) if (t <= STOPS[i][0]) {
      const [t0, c0] = STOPS[i - 1], [t1, c1] = STOPS[i], f = (t - t0) / (t1 - t0 || 1);
      return c0.map((v, k) => v + (c1[k] - v) * f);
    }
    return STOPS[STOPS.length - 1][1];
  }
  const zoneById = (id) => state.d.zones.find((z) => z.zone_id === id);
  function inPoly(pt, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }
  function setMode(m) {
    state.mode = m;
    views.forEach((v) => v.root.querySelectorAll('.seg button').forEach((x) => x.classList.toggle('on', x.dataset.m === m)));
    markDirty(); paint(state.tab);
  }
  function drawFloor(v) {
    const cfg = state.cfg, d = state.d; if (!cfg || !d.heat || !v.cv.offsetWidth) return;  // hidden tab: drawn when shown
    const cv = v.cv, W = cv.clientWidth, H = Math.round(W * cfg.floor.height_m / cfg.floor.width_m), dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + 'px';
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const sx = W / cfg.floor.width_m, sy = H / cfg.floor.height_m;
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = css('--grid'); ctx.lineWidth = 1;
    for (let x = 1; x < cfg.floor.width_m; x++) { ctx.beginPath(); ctx.moveTo(x * sx, 0); ctx.lineTo(x * sx, H); ctx.stroke(); }
    for (let y = 1; y < cfg.floor.height_m; y++) { ctx.beginPath(); ctx.moveTo(0, y * sy); ctx.lineTo(W, y * sy); ctx.stroke(); }

    const kc = KIND_COLOR();
    if (state.mode === 'traffic' && d.heat.max > 0) {
      const off = document.createElement('canvas'); off.width = d.heat.nx; off.height = d.heat.ny;
      const octx = off.getContext('2d'), img = octx.createImageData(d.heat.nx, d.heat.ny);
      for (let y = 0; y < d.heat.ny; y++) for (let x = 0; x < d.heat.nx; x++) {
        const c = ramp(d.heat.grid[y][x] / d.heat.max), i = (y * d.heat.nx + x) * 4;
        img.data[i] = c[0]; img.data[i + 1] = c[1]; img.data[i + 2] = c[2]; img.data[i + 3] = c[3] * 255;
      }
      octx.putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(off, 0, 0, W, H);
    }
    const fixtures = d.zones.filter((z) => z.kind === 'fixture');
    const maxEng = Math.max(1e-9, ...fixtures.map((z) => z.engaged_per_100 || 0));
    for (const z of state.cfg.zones) {
      const zm = zoneById(z.id), col = kc[z.kind] || kc.fixture;
      ctx.beginPath(); z.polygon.forEach(([x, y], i) => (i ? ctx.lineTo(x * sx, y * sy) : ctx.moveTo(x * sx, y * sy))); ctx.closePath();
      if (state.mode === 'engaged' && z.kind === 'fixture' && zm) { ctx.globalAlpha = 0.12 + 0.6 * ((zm.engaged_per_100 || 0) / maxEng); ctx.fillStyle = col; ctx.fill(); ctx.globalAlpha = 1; }
      else if (z.id === state.zone || z.id === v.hover) { ctx.globalAlpha = 0.14; ctx.fillStyle = col; ctx.fill(); ctx.globalAlpha = 1; }
      ctx.lineWidth = z.id === state.zone ? 2.5 : 1.25; ctx.strokeStyle = col; ctx.setLineDash(z.kind === 'staff' ? [4, 3] : []); ctx.stroke(); ctx.setLineDash([]);
      const cx = z.polygon.reduce((a, p) => a + p[0], 0) / z.polygon.length * sx, cy = z.polygon.reduce((a, p) => a + p[1], 0) / z.polygon.length * sy;
      ctx.font = '600 11px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.lineJoin = 'round';
      const label = z.name + (state.mode === 'engaged' && zm && zm.engaged_per_100 !== null ? ' · ' + num(zm.engaged_per_100, 1) : '');
      ctx.strokeStyle = css('--card'); ctx.lineWidth = 3.5; ctx.strokeText(label, cx, cy); ctx.fillStyle = css('--ink'); ctx.fillText(label, cx, cy);
    }
    const e = cfg.entrance; ctx.strokeStyle = css('--bad'); ctx.lineWidth = 4; ctx.beginPath(); ctx.moveTo(e.p1[0] * sx, e.p1[1] * sy); ctx.lineTo(e.p2[0] * sx, e.p2[1] * sy); ctx.stroke();
    ctx.fillStyle = css('--muted'); ctx.font = '11px system-ui'; ctx.textAlign = 'center'; ctx.fillText('Entrance', (e.p1[0] + e.p2[0]) / 2 * sx, e.p1[1] * sy - 7);

    const lg = v.legend; lg.replaceChildren();
    if (state.mode === 'traffic') lg.append(h('span', {}, 'Time spent'), h('span', { class: 'ramp' }), h('span', {}, 'low → high'),
      h('span', {}, '· ' + num(d.heat.total_s / 60, 0) + ' person-minutes tracked'));
    else lg.append(h('span', {}, 'Zone shade = engaged visitors per 100 visitors'));
    lg.append(h('span', {}, '· click a display to focus it, click again to clear'),
      h('span', {}, '· dashed = back of house (excluded from customer metrics)'));
  }
  function makeFloor(root) {
    const v = { id: root.dataset.floor, root, cv: $('canvas.floor', root), tip: $('.tip', root), legend: $('.legend', root), hover: null };
    root.querySelectorAll('.seg button').forEach((btn) => btn.addEventListener('click', () => setMode(btn.dataset.m)));
    const cv = v.cv, tip = v.tip;
    const at = (ev) => { const r = cv.getBoundingClientRect(); return [(ev.clientX - r.left) / r.width * state.cfg.floor.width_m, (ev.clientY - r.top) / r.height * state.cfg.floor.height_m, ev.clientX - r.left, ev.clientY - r.top]; };
    const area = (poly) => { let a = 0; for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) a += poly[j][0] * poly[i][1] - poly[i][0] * poly[j][1]; return Math.abs(a / 2); };
    const find = (p) => { const hits = state.cfg.zones.filter((z) => inPoly([p[0], p[1]], z.polygon)); return hits.sort((a, b) => area(a.polygon) - area(b.polygon))[0]; };
    cv.addEventListener('mousemove', (ev) => {
      if (!state.cfg || !state.d.zones) return;
      const p = at(ev), z = find(p), id = z ? z.id : null;
      if (id !== v.hover) { v.hover = id; drawFloor(v); }
      if (!z) { tip.hidden = true; return; }
      const m = zoneById(z.id);
      tip.replaceChildren(h('b', {}, z.name), ...(z.kind === 'staff' ? ['Back of house (excluded)'] : m ? [
        num(m.impressions) + ' impressions · reach ' + pct(m.reach), h('br'),
        m.engaged === null ? 'median time ' + secs(m.median_dwell_s) : num(m.engaged) + ' engaged · rate ' + pct(m.engagement_rate), h('br'),
        m.engaged_per_100 !== null ? num(m.engaged_per_100, 1) + ' engaged / 100 visitors' : ''] : []));
      tip.hidden = false; tip.style.left = Math.min(p[2] + 14, cv.clientWidth - 240) + 'px'; tip.style.top = p[3] + 14 + 'px';
    });
    cv.addEventListener('mouseleave', () => { tip.hidden = true; if (v.hover) { v.hover = null; drawFloor(v); } });
    cv.addEventListener('click', (ev) => {
      const z = find(at(ev)); if (!z || z.kind === 'staff') return;
      pickZone(z.id);
    });
    return v;
  }

  // ------------------------------------------------------------------------------------ floor plan tab: detail panel
  const stat = (l, v, sub) => h('div', { class: 'stat' }, h('div', { class: 'l' }, l), h('div', { class: 'v' }, v), sub ? h('div', { class: 's' }, sub) : null);
  function renderZoneDetail() {
    const box = $('#zoneDetail'), m = state.d.summary, z = state.zone && zoneById(state.zone);
    if (!z) {
      box.replaceChildren(
        h('div', { class: 'zd-h' }, h('div', {}, h('h2', {}, 'Whole store'), h('div', { class: 'sub' }, 'All displays together'))),
        h('div', { class: 'stats' },
          stat('Impressions', num(m.impressions), 'near a display'),
          stat('Engaged impressions', num(m.engaged), pct(m.engagement_rate) + ' of impressions'),
          stat('Door entries', num(m.footfall), num(m.exits) + ' exits'),
          stat('Median visit', secs(m.median_visit_s), 'time in store')),
        h('p', { class: 'hint' }, 'Click a display on the map or in the table to see its numbers and where visitors come from and go next.'));
      return;
    }
    const fx = fixturesRanked(), rank = fx.findIndex((x) => x.zone_id === z.zone_id) + 1;
    const stats = z.kind === 'fixture' ? [
      stat('Engaged / 100', num(z.engaged_per_100, 1), rank ? '#' + rank + ' of ' + fx.length + ' displays' : ''),
      stat('Reach', pct(z.reach), 'of visitors came near'),
      stat('Engagement rate', pct(z.engagement_rate), num(z.engaged) + ' of ' + num(z.impressions) + ' impressions'),
      stat('Median engaged time', secs(z.median_engaged_s), 'when they stopped')] : [
      stat('Reach', pct(z.reach), 'of visitors passed through'),
      stat('Visits', num(z.impressions), 'in this period'),
      stat('Median time', secs(z.median_dwell_s), 'per visit')];
    const edges = state.d.flows.edges;
    const list = (title, es, key) => h('div', {}, h('div', { class: 'mini-h' }, title),
      es.length ? h('ul', { class: 'mini-list' }, es.map((e) => h('li', {}, h('span', {}, e[key]), h('span', { class: 'n' }, e.count)))) : h('div', { class: 'sub' }, 'None among the most common moves'));
    box.replaceChildren(
      h('div', { class: 'zd-h' }, h('div', {}, h('h2', {}, z.name), h('div', { class: 'sub' }, (z.kind === 'fixture' ? 'Display' : 'Area · traffic and time only'))),
        h('button', { class: 'link-btn', type: 'button', onclick: () => pickZone(null, { toggle: false }) }, 'Clear')),
      h('div', { class: 'stats' }, stats),
      list('People arrive from', edges.filter((e) => e.to === z.zone_id).slice(0, 3), 'from_name'),
      list('And go on to', edges.filter((e) => e.from === z.zone_id).slice(0, 3), 'to_name'),
      h('p', { class: 'hint' }, h('a', { class: 'more', href: '#/activity' }, 'See its activity over time →')));
  }

  // ------------------------------------------------------------------------------------ zones table
  function renderZones() {
    const zs = state.d.zones, col = KIND_COLOR();
    const fx = fixturesRanked();
    const areas = zs.filter((z) => z.kind !== 'fixture');
    const max = Math.max(1e-9, ...fx.map((z) => z.engaged_per_100 || 0));
    const pick = (id) => ({ onclick: () => pickZone(id), tabindex: '0', onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickZone(id); } } });
    const row = (z) => h('tr', { class: 'zrow' + (state.zone === z.zone_id ? ' sel' : ''), ...pick(z.zone_id) },
      h('td', {}, h('div', { class: 'zname' }, h('span', { class: 'dot', style: 'background:' + col[z.kind] }), z.name)),
      h('td', {}, h('span', { class: 'bar', style: 'width:' + Math.round(60 * (z.engaged_per_100 || 0) / max) + 'px' }), num(z.engaged_per_100, 1)),
      h('td', {}, pct(z.reach)), h('td', {}, pct(z.engagement_rate)), h('td', {}, secs(z.median_engaged_s)), h('td', { class: 'muted' }, num(z.impressions)));
    const t = h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Display'), h('th', { title: 'Engaged visitors per 100 store visitors' }, 'Engaged / 100'),
      h('th', { title: 'Share of visitors who came near it' }, 'Reach'), h('th', { title: 'Engaged ÷ impressions' }, 'Engage rate'), h('th', {}, 'Median time'), h('th', {}, 'Impr.'))),
      h('tbody', {}, fx.map(row)));
    const wrap = h('div', {}, t);
    if (areas.length) wrap.append(h('div', { class: 'sub', style: 'margin:12px 0 4px' }, 'Areas (traffic and time only)'),
      h('table', {}, h('tbody', {}, areas.filter((z) => z.kind === 'area').map((z) => h('tr', { class: 'zrow' + (state.zone === z.zone_id ? ' sel' : ''), ...pick(z.zone_id) },
        h('td', {}, h('div', { class: 'zname' }, h('span', { class: 'dot', style: 'background:' + col.area }), z.name)),
        h('td', {}, 'reach ' + pct(z.reach)), h('td', {}, 'median ' + secs(z.median_dwell_s)), h('td', { class: 'muted' }, num(z.impressions) + ' visits'))))));
    if (!fx.length) wrap.replaceChildren(h('div', { class: 'empty' }, 'No data yet.'));
    $('#zoneTable').replaceChildren(wrap);
  }

  // ------------------------------------------------------------------------------------ series
  function renderSeries() {
    const ts = (state.d && state.d.ts) || [], box = $('#series');
    const zname = state.zone ? state.cfg.zones.find((z) => z.id === state.zone).name : 'all displays';
    $('#seriesSub').textContent = 'Impressions and engaged visits at ' + zname + ', per time slot';
    if (state.seriesErr) { box.replaceChildren(h('div', { class: 'empty' }, state.seriesErr)); return; }
    if (!ts.length) { box.replaceChildren(h('div', { class: 'empty' }, 'No activity in this period.')); return; }
    const W = box.clientWidth || 520, H = 240, m = { l: 34, r: 8, t: 10, b: 34 };
    const gapW = 14, n = ts.length, gaps = ts.filter((t) => t.gap).length;
    const slot = Math.max(8, (W - m.l - m.r - gaps * gapW) / n), bw = Math.min(26, slot * 0.7);
    const ymax = Math.max(1, ...ts.map((t) => Math.max(t.impressions, t.entries))), ytop = Math.ceil(ymax / 4) * 4;
    const y = (v) => m.t + (H - m.t - m.b) * (1 - v / ytop);
    const svg = s('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Activity over time' });
    for (let i = 0; i <= 4; i++) { const v = ytop / 4 * i; svg.append(s('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: css('--line') }), s('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, Math.round(v))); }
    let x = m.l, pts = [], lastDay = '';
    ts.forEach((t, i) => {
      if (t.gap) { svg.append(s('line', { x1: x + gapW / 2, x2: x + gapW / 2, y1: m.t, y2: H - m.b, stroke: css('--muted'), 'stroke-dasharray': '3 3' })); x += gapW; }
      const bx = x + (slot - bw) / 2;
      svg.append(s('rect', { x: bx, y: y(t.impressions), width: bw, height: Math.max(0, y(0) - y(t.impressions)), rx: 2, fill: css('--accent-soft') }, s('title', {}, `${t.t.slice(11, 16)} · ${t.impressions} impressions, ${t.engaged} engaged, ${t.entries} entries`)));
      svg.append(s('rect', { x: bx + bw * 0.2, y: y(t.engaged), width: bw * 0.6, height: Math.max(0, y(0) - y(t.engaged)), rx: 2, fill: css('--accent') }));
      pts.push([bx + bw / 2, y(t.entries)]);
      const day = t.t.slice(0, 10);
      if (i % Math.ceil(n / Math.max(1, Math.floor((W - m.l) / 52))) === 0 || t.gap) {
        svg.append(s('text', { x: bx + bw / 2, y: H - m.b + 14, 'text-anchor': 'middle' }, t.t.slice(11, 16)));
        if (day !== lastDay) { svg.append(s('text', { x: bx + bw / 2, y: H - m.b + 27, 'text-anchor': 'middle' }, day.slice(5))); lastDay = day; }
      }
      x += slot;
    });
    svg.append(s('polyline', { points: pts.map((p) => p.join(',')).join(' '), fill: 'none', stroke: css('--muted'), 'stroke-width': 1.5, 'stroke-dasharray': '4 3' }));
    pts.forEach((p) => svg.append(s('circle', { cx: p[0], cy: p[1], r: 2.5, fill: css('--muted') })));
    const lg = h('div', { class: 'legend' }, h('span', { class: 'dot', style: 'background:' + css('--accent-soft') }), 'Impressions', h('span', { class: 'dot', style: 'background:' + css('--accent') }), 'Engaged', h('span', {}, '┄ Door entries'));
    box.replaceChildren(svg, lg);
  }

  // ------------------------------------------------------------------------------------ flows
  function renderFlows() {
    const f = state.d.flows, box = $('#flows');
    if (!f.edges.length) { box.replaceChildren(h('div', { class: 'empty' }, 'No movement data yet.')); return; }
    const max = f.edges[0].count;
    box.replaceChildren(...f.edges.map((e) => h('div', { class: 'flow' },
      h('div', { class: 't' }, e.from_name, h('i', {}, '→'), e.to_name, h('span', { class: 'bar', style: 'width:' + Math.round(100 * e.count / max) + '%' })),
      h('div', { class: 'c' }, e.count))), h('div', { class: 'sub', style: 'margin-top:8px' }, f.n_paths + ' visits with a path'));
  }

  // ------------------------------------------------------------------------------------ insights
  const CONF = { high: 'good', medium: 'warn', low: 'bad' };
  function insightCard(c) {
    return h('div', { class: 'ins ' + c.kind }, h('h3', {}, c.title), h('p', {}, c.detail), h('span', { class: 'pill ' + (CONF[c.confidence] || '') }, c.confidence + ' confidence'));
  }
  function renderInsights() {
    const ins = state.d.insights, box = $('#insights');
    box.replaceChildren(...(ins.length ? ins.map(insightCard) : [h('div', { class: 'empty' }, 'Not enough data for insights yet. Cards appear once a display has ≥ 20 impressions.')]));
  }

  // ------------------------------------------------------------------------------------ compare
  async function renderCompare() {
    const a = $('#cmpA').value, b = $('#cmpB').value, box = $('#compare');
    if (state.runs.length < 2) { box.replaceChildren(h('div', { class: 'empty' }, 'Run the pipeline on a second period (after a layout change) to compare before/after.')); return; }
    if (a === b) { box.replaceChildren(h('div', { class: 'empty' }, 'Pick two different runs.')); return; }
    let cmp, ins;
    try { [cmp, ins] = await Promise.all([api(`/api/compare?a=${a}&b=${b}`), api(`/api/insights?a=${a}&b=${b}`)]); }
    catch (e) { box.replaceChildren(h('div', { class: 'empty' }, e.message)); return; }
    if (cmp.error) { box.replaceChildren(h('div', { class: 'empty' }, cmp.error)); return; }
    const rows = cmp.zones.map((r) => ({ r, m: r.adjusted || r }));
    const lo = Math.min(-100, ...rows.map((x) => x.m.ci_low_pct ?? -100)), hi = Math.min(400, Math.max(100, ...rows.map((x) => x.m.ci_high_pct ?? 100)));
    const whisk = (x) => {
      const W = 200, Hh = 22, sx = (v) => 4 + (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo) * (W - 8);
      const col = x.m.verdict === 'improved' ? css('--good') : x.m.verdict === 'worse' ? css('--bad') : css('--muted');
      const svg = s('svg', { width: W, height: Hh, viewBox: `0 0 ${W} ${Hh}` });
      svg.append(s('line', { x1: sx(0), x2: sx(0), y1: 2, y2: Hh - 2, stroke: css('--muted'), 'stroke-dasharray': '2 2' }));
      if (x.m.lift_pct !== null && x.m.lift_pct !== undefined) {
        svg.append(s('line', { x1: sx(x.m.ci_low_pct), x2: sx(x.m.ci_high_pct), y1: Hh / 2, y2: Hh / 2, stroke: col, 'stroke-width': 2 }),
          s('circle', { cx: sx(x.m.lift_pct), cy: Hh / 2, r: 4, fill: col }));
      }
      return svg;
    };
    const chip = (v) => h('span', { class: 'pill ' + (v === 'improved' ? 'good' : v === 'worse' ? 'bad' : '') }, v || '–');
    const fmt = (v) => (v === null || v === undefined ? '–' : (v >= 0 ? '+' : '') + v.toFixed(0) + '%');
    const table = h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Display'), h('th', {}, 'Before / 100'), h('th', {}, 'After / 100'), h('th', { title: 'Without adjusting for other displays' }, 'Raw lift'),
      h('th', { title: 'Relative to the other displays, cancelling store-wide shifts' }, 'Adjusted lift'), h('th', { title: cmp.note || 'Bootstrap CI over visitors' }, (cmp.ci_level_pct || 95) + '% CI (' + Math.round(lo) + '% … ' + Math.round(hi) + '%)'), h('th', {}, 'Verdict'))),
      h('tbody', {}, rows.map((x) => h('tr', {}, h('td', {}, x.r.name), h('td', {}, num(x.r.a.per_100, 1)), h('td', {}, num(x.r.b.per_100, 1)),
        h('td', {}, fmt(x.r.lift_pct)), h('td', {}, x.r.adjusted ? fmt(x.r.adjusted.lift_pct) : '–'), h('td', {}, whisk(x)), h('td', {}, chip(x.m.verdict))))));
    const results = ins.filter((c) => c.kind === 'result');
    box.replaceChildren(
      h('div', { class: 'sub', style: 'margin-bottom:8px' }, `Engaged visitors per 100 store visitors · ${cmp.visitors.a} visitors before, ${cmp.visitors.b} after · verdict uses the adjusted lift when available · intervals widened for comparing ${cmp.n_compared || cmp.zones.length} displays`),
      table, results.length ? h('div', { class: 'insights', style: 'margin-top:14px' }, results.map(insightCard)) : h('div', { class: 'sub', style: 'margin-top:12px' }, 'No display shows a statistically clear change at 95% confidence. A small store needs several hundred visitors per period to detect modest changes.'));
  }

  // ------------------------------------------------------------------------------------ quality
  function renderQuality() {
    const q = state.d.quality, p = q.privacy, box = $('#quality');
    const priv = [`${p.raw_frames_stored} raw frames stored · ${p.faces_stored} faces stored`, 'No trajectories stored: only per-display events and aggregate heat grids',
      'No appearance re-identification: tracks cannot be linked across visits or days', `Visit IDs are salted hashes that rotate every ${p.id_rotation_hours} h (the salt is never written to disk)`,
      `Preview video is obscured (${p.blur_mode === 'person' ? 'whole person' : p.blur_mode} mosaic) before it is written`];
    const runs = h('table', { class: 'runs' }, h('thead', {}, h('tr', {}, ['Run', 'Frames', 'Detector', 'Speed', 'Customers', 'Staff', 'Stitched', 'Calib. error'].map((x) => h('th', {}, x)))),
      h('tbody', {}, q.runs.map((r) => { const m = r.meta || {}; return h('tr', {}, h('td', {}, r.label || r.run_id), h('td', {}, num(m.frames_processed)), h('td', {}, m.detector || '–'),
        h('td', {}, m.processing_fps ? num(m.processing_fps) + ' fps' : '–'), h('td', {}, num(m.customers)), h('td', {}, num(m.staff)), h('td', {}, num(m.stitched)), h('td', {}, m.calibration_rmse_cm !== undefined ? m.calibration_rmse_cm + ' cm' : '–')); })));
    box.replaceChildren(h('div', { class: 'q-grid' },
      h('div', {}, h('b', {}, 'Privacy by design'), h('ul', { class: 'q-list' }, priv.map((x) => h('li', {}, x)))),
      h('div', {}, h('b', {}, 'Heads-up'), h('ul', { class: 'q-list q-warn' }, q.warnings.map((x) => h('li', {}, x))))),
      h('div', { style: 'margin-top:14px;overflow-x:auto' }, runs));
  }

  init();
})();
