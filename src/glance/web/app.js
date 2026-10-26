/* Glance dashboard: dependency-free. Works against the live API or an embedded snapshot (static report / site).
 *
 * Layout: a collapsible sidebar that jumps between sections, and one section at a time in the main column:
 *   Overview · A/B test · Floor plan · Traffic · Audience · Recommendations · Data & privacy.
 * Sections are routed with the URL hash (#/overview ...) and rendered lazily when first shown, because a canvas / SVG
 * can only be measured while it is visible. Pages lead with numbers and charts; the explanations live in tooltips
 * (the ⓘ marks) and in the definitions on Data & privacy. */
(function () {
  'use strict';
  const SNAP = window.__GLANCE_SNAPSHOT__ || null;
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
      if (SNAP.__empty__) throw new Error('No data has arrived yet. It appears here after the store computer pushes its first snapshot.');
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

  // ---- light / dark. Both palettes are token sets in style.css, keyed off data-theme on <html>; with nothing stored
  // we follow the operating system, which is what the dashboard did before the toggle existed. Applied here, at script
  // time rather than from init(), so the page is never painted in the wrong theme first.
  const mqDark = window.matchMedia('(prefers-color-scheme: dark)');
  const theme = {
    stored: () => { const v = prefs.get('glance.theme'); return v === 'light' || v === 'dark' ? v : null; },
    now: () => theme.stored() || (mqDark.matches ? 'dark' : 'light'),
    apply() {
      const v = theme.stored();
      if (v) document.documentElement.setAttribute('data-theme', v); else document.documentElement.removeAttribute('data-theme');
    },
  };
  theme.apply();

  // ---- formatting
  const ok = (x) => x !== null && x !== undefined && isFinite(x);
  const pct = (x, d = 0) => (ok(x) ? (x * 100).toFixed(d) + '%' : '–');
  const num = (x, d = 0) => (ok(x) ? x.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }) : '–');
  const n0 = (x) => num(x, ok(x) && Math.abs(x) < 10 ? 1 : 0);
  const secs = (x) => (!ok(x) ? '–' : x >= 60 ? Math.floor(x / 60) + 'm ' + Math.round(x % 60) + 's' : Math.round(x) + 's');
  const sign = (v, d = 0) => (ok(v) ? (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(d) : '–');
  const signPct = (v) => (ok(v) ? sign(v) + '%' : '–');
  const xfmt = (v) => (ok(v) ? v.toFixed(1) + '×' : '–');
  const rng = (r, f) => { const a = f(r[0]), b = f(r[1]); return a === b ? a : a + '–' + b; };
  const median = (xs) => { const v = xs.filter(ok).sort((a, b) => a - b); if (!v.length) return null; const m = v.length >> 1; return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; };
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const pretty = (l) => (l ? String(l).replace(/[_-]+/g, ' ').replace(/^./, (c) => c.toUpperCase()) : '');
  const day = (iso) => MONTHS[+iso.slice(5, 7) - 1] + ' ' + (+iso.slice(8, 10));
  const info = (text) => h('i', { class: 'qi', title: text, tabindex: '0', role: 'img', 'aria-label': text }, 'i');
  const empty = (msg) => h('div', { class: 'empty' }, msg);
  const KIND_COLOR = () => ({ fixture: css('--accent'), area: css('--teal'), staff: css('--violet') });

  const TABS = ['overview', 'insights', 'floor', 'activity', 'audience', 'recommend', 'data'];
  const state = { cfg: null, runs: [], sel: 'all', mode: 'traffic', zone: null, d: {}, tab: 'overview', dirty: new Set(TABS), seriesErr: null, assume: null, ab: null, prevName: null };
  const views = [];  // floor-plan canvases (just the one on the Floor plan section)
  const TITLES = { overview: 'Overview', insights: 'A/B test', floor: 'Floor plan', activity: 'Traffic', audience: 'Audience', recommend: 'Recommendations', data: 'Data & privacy' };
  const ui = { closeDrawer: () => {} };

  const suffix = () => (state.sel === 'all' ? '' : '?runs=' + state.sel);
  const sep = () => (state.sel === 'all' ? '?' : '&');
  const tsPath = () => '/api/timeseries' + suffix() + (state.zone ? sep() + 'zone=' + state.zone : '');
  const runLabel = (r) => (pretty(r.label) || 'Run ' + r.run_id) + ' · ' + day(r.start_iso) + ', ' + r.start_iso.slice(11, 16);
  function groupSessions(runs) {
    const m = new Map();
    runs.forEach((r) => { const k = (r.label || '') + '@' + Math.round(r.t_start); if (!m.has(k)) m.set(k, { run: r, ids: [] }); m.get(k).ids.push(r.run_id); });
    return [...m.values()];
  }
  const sessLabel = (x) => runLabel(x.run);
  const sessName = (x) => pretty(x.run.label) || day(x.run.start_iso);
  const sessKey = (x) => x.ids.join(',');

  // ------------------------------------------------------------------------------------ sidebar
  const mqMobile = window.matchMedia('(max-width: 860px)');
  function setupSidebar() {
    const app = $('#app'), side = $('#side'), tog = $('#sideToggle'), menu = $('#menuBtn'), scrim = $('#scrim');
    const saved = prefs.get('glance.sidebar');
    app.classList.toggle('collapsed', saved ? saved === 'collapsed' : window.innerWidth < 1100);
    requestAnimationFrame(() => requestAnimationFrame(() => app.classList.remove('boot')));  // no slide-in animation on load
    const sync = () => {
      const mobile = mqMobile.matches, open = app.classList.contains('drawer-open');
      const expanded = mobile ? open : !app.classList.contains('collapsed');
      const label = mobile ? 'Close menu' : expanded ? 'Collapse sidebar' : 'Expand sidebar';
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
      prefs.set('glance.sidebar', collapsed ? 'collapsed' : 'open');
      sync();
    });
    menu.addEventListener('click', () => { app.classList.add('drawer-open'); sync(); tog.focus(); });
    scrim.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && app.classList.contains('drawer-open')) { closeDrawer(); menu.focus(); } });
    mqMobile.addEventListener('change', () => { app.classList.remove('drawer-open'); sync(); });
    sync();
  }

  // ------------------------------------------------------------------------------------ theme toggle
  function setupTheme() {
    const btn = $('#themeBtn');
    const sync = () => {
      const dark = theme.now() === 'dark';
      const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
      btn.classList.toggle('is-dark', dark);          // which icon shows: the mode the button switches to
      btn.setAttribute('aria-label', label); btn.title = label;
    };
    // charts and the floor-plan canvas read the colour tokens when they draw, so a switch means redrawing them
    const restyle = () => { sync(); markDirty(); paint(state.tab); };
    btn.addEventListener('click', () => { prefs.set('glance.theme', theme.now() === 'dark' ? 'light' : 'dark'); theme.apply(); restyle(); });
    mqDark.addEventListener('change', () => { theme.apply(); restyle(); });  // only visible while nothing is stored
    sync();
  }

  // ------------------------------------------------------------------------------------ sections / routing
  const tabFromHash = () => { const m = /^#\/([a-z]+)/.exec(location.hash); return m && TABS.includes(m[1]) ? m[1] : 'overview'; };
  const R = { overview: renderOverview, insights: renderInsightsTab, floor: renderFloorTab, activity: renderActivity, audience: renderAudience, recommend: renderRecommend, data: renderData };
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
    document.title = TITLES[t] + ' · ' + (state.cfg ? state.cfg.name + ' · ' : '') + 'Glance';
    ui.closeDrawer();
    if (changed) window.scrollTo(0, 0);
    paint(t);
  }
  function setupTabs() {
    window.addEventListener('hashchange', () => showTab(tabFromHash()));
    showTab(tabFromHash());
  }
  function redraw() {  // size changed: redraw what is visible, mark the rest stale
    const cur = state.tab; markDirty(); state.dirty.delete(cur);
    if (cur === 'floor') { views.forEach(drawFloor); renderCameras(); }
    else if (cur === 'activity') renderSeries();
    else if (cur === 'data') renderCameras();
    else state.dirty.add(cur);
  }

  // ------------------------------------------------------------------------------------ live view (simulated)
  async function initLive() {
    let info_;
    try { info_ = await api('/api/live'); } catch (e) { return; }
    if (!info_ || !info_.camera) return;
    const cam = $('#liveCam'), fp = $('#liveFp'), card = $('#liveCard');
    const play = $('#livePlay'), seek = $('#liveSeek'), time = $('#liveTime'), rate = $('#liveRate');
    cam.src = info_.camera; fp.src = info_.floorplan;
    card.hidden = false;
    $('#liveBadge').title = (info_.note || '') + ' ' + Math.round(info_.seconds) + ' s loop.';
    const fmt = (t) => Math.floor(t / 60) + ':' + String(Math.floor(t % 60)).padStart(2, '0');
    let scrubbing = false, userPaused = false;
    const sync = () => {  // the camera is the clock; the floor plan follows it
      if (card.offsetParent === null) return;  // on another tab: nothing to keep in step
      if (Math.abs(fp.currentTime - cam.currentTime) > 0.12) fp.currentTime = cam.currentTime;
      if (!scrubbing && cam.duration) seek.value = String(Math.round(cam.currentTime / cam.duration * 1000));
      time.textContent = fmt(cam.currentTime) + ' / ' + fmt(cam.duration || info_.seconds);
    };
    setInterval(sync, 150);
    const setPlaying = (on) => {
      if (on) { cam.play().catch(() => {}); fp.play().catch(() => {}); } else { cam.pause(); fp.pause(); }
      play.textContent = on ? 'Pause' : 'Play';
    };
    play.addEventListener('click', () => setPlaying(cam.paused));
    seek.addEventListener('input', () => { scrubbing = true; const t = seek.value / 1000 * (cam.duration || info_.seconds); cam.currentTime = t; fp.currentTime = t; });
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
    setupTheme();
    try {
      [state.cfg, state.runs, state.cams] = await Promise.all([api('/api/config'), api('/api/runs'), api('/api/cameras').catch(() => null)]);
    } catch (e) { $('#panes').prepend(h('div', { class: 'empty' }, 'Could not load data: ' + e.message)); return; }
    $('#storeName').textContent = state.cfg.name;
    const demo = window.__GLANCE_DEMO__;  // set by `glance site`: "simulated data" label + optional call-to-action link
    if (demo && demo.label) { const chip = $('#demoChip'); chip.textContent = demo.label; chip.hidden = false; }
    if (demo && /^(https?:|mailto:)/i.test(demo.ctaUrl || '')) { const a = $('#ctaLink'); a.href = demo.ctaUrl; a.textContent = demo.ctaText || 'Request a pilot'; a.hidden = false; }
    // One entry per recording session: with several cameras a session is several runs (one per camera) analysed together.
    const sess = groupSessions(state.runs);
    state.sessions = sess;
    const period = $('#period');
    period.append(h('option', { value: 'all' }, 'All periods (' + sess.length + ')'));
    sess.forEach((x) => period.append(h('option', { value: sessKey(x) }, sessLabel(x))));
    state.sel = sess.length ? sessKey(sess[sess.length - 1]) : 'all';
    period.value = state.sel;
    period.addEventListener('change', () => { state.sel = period.value; state.zone = null; load(); });

    const a = $('#cmpA'), b = $('#cmpB');
    sess.forEach((x) => { a.append(h('option', { value: sessKey(x) }, sessLabel(x))); b.append(h('option', { value: sessKey(x) }, sessLabel(x))); });
    if (sess.length >= 2) {
      a.value = sessKey(sess[0]); b.value = sessKey(sess[1]);
      try {  // the headline A/B result on the Overview: the first two periods, whatever period is selected above
        const cmp = await api(`/api/compare?a=${a.value}&b=${b.value}`);
        if (cmp && !cmp.error && cmp.zones && cmp.zones.length) state.ab = { cmp, nameA: sessName(sess[0]), nameB: sessName(sess[1]) };
      } catch (e) { state.ab = null; }
    }
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

    await load();
    initLive();
  }

  function prevSession() {  // the period before the selected one, for "vs last period" deltas
    const i = (state.sessions || []).findIndex((x) => sessKey(x) === state.sel);
    return i > 0 ? state.sessions[i - 1] : null;
  }
  async function load() {
    const sfx = suffix(), prev = prevSession();
    try {
      const soft = (p) => (p ? api(p).catch(() => null) : Promise.resolve(null));  // older snapshots lack some views: the tabs then say so
      const [summary, zones, heat, ts, tsAll, flows, insights, quality, audience, recs, prevSum] = await Promise.all([
        api('/api/summary' + sfx), api('/api/zones' + sfx), api('/api/heatmap' + sfx), api(tsPath()), api('/api/timeseries' + sfx),
        api('/api/flows' + sfx), api('/api/insights' + sfx), api('/api/quality' + sfx),
        soft('/api/audience' + sfx), soft('/api/recommendations' + sfx), soft(prev ? '/api/summary?runs=' + sessKey(prev) : null)]);
      state.d = { summary, zones, heat, ts, tsAll, flows, insights, quality, audience, recs, prev: prevSum }; state.seriesErr = null;
      state.prevName = prevSum ? sessName(prev) : null;
      state.assume = null;  // re-seeded from the new selection's assumptions
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
  async function openZone(id) { await setZone(id); if (location.hash !== '#/floor') location.hash = '#/floor'; else paint('floor'); }
  const keyClick = (fn) => (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); } };

  const fixturesRanked = () => state.d.zones.filter((z) => z.kind === 'fixture').sort((a, b) => (b.engaged_per_100 || 0) - (a.engaged_per_100 || 0));

  // ------------------------------------------------------------------------------------ shared small charts
  function spark(vals) {  // trend line for a stat tile: lightly smoothed, area wash under a 2px line
    if (vals.length < 3) return null;
    const v = vals.map((x, i) => (vals[Math.max(0, i - 1)] + x + vals[Math.min(vals.length - 1, i + 1)]) / 3);
    const max = Math.max(1e-9, ...v), W = 100, H = 30, X = (i) => (i / (v.length - 1) * W).toFixed(2), Y = (x) => (H - 2 - (H - 4) * x / max).toFixed(2);
    const line = v.map((x, i) => (i ? 'L' : 'M') + X(i) + ' ' + Y(x)).join(' ');
    return s('svg', { class: 'spark', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      s('path', { d: line + ` L${W} ${H} L0 ${H} Z`, fill: css('--accent'), 'fill-opacity': 0.1 }),
      s('path', { d: line, fill: 'none', stroke: css('--accent'), 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke', 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  }
  function delta(cur, prev, pts) {  // signed change vs the previous period; up is good for every metric shown
    if (!ok(cur) || !ok(prev) || !state.prevName) return null;
    let d, txt;
    if (pts) { d = (cur - prev) * 100; txt = sign(d, 1) + ' pts'; } else { if (!prev) return null; d = (cur - prev) / prev * 100; txt = sign(d) + '%'; }
    const cls = Math.abs(d) < 0.5 ? 'flat' : d > 0 ? 'up' : 'down';
    return h('span', { class: 'delta ' + cls }, cls === 'up' ? '▲ ' + txt : cls === 'down' ? '▼ ' + txt : 'No change', h('span', { class: 'vs' }, 'vs ' + state.prevName));
  }
  const kpiTile = (label, value, d, sub, trend) => h('div', { class: 'kpi' }, h('div', { class: 'l' }, label),
    h('div', { class: 'row' }, h('div', { class: 'v' }, value), d), sub ? h('div', { class: 's' }, sub) : null, trend);
  const smallTile = (label, value, sub) => h('div', { class: 'kpi' }, h('div', { class: 'l' }, label), h('div', { class: 'v' }, value), sub ? h('div', { class: 's' }, sub) : null);
  function hbars(rows, { med, onPick, fmt = (v) => num(v, 1) }) {  // horizontal bars, value at the tip, optional median tick
    const max = Math.max(1e-9, ...rows.map((r) => r.v || 0), med || 0) * 1.05;
    return h('div', { class: 'hbars' }, rows.map((r) => h('div', { class: 'hbar' + (onPick ? ' click' : ''), title: r.title || '',
      ...(onPick ? { tabindex: '0', role: 'button', onclick: () => onPick(r), onkeydown: keyClick(() => onPick(r)) } : {}) },
      h('span', { class: 'nm' }, r.name),
      h('div', { class: 'track' }, h('i', { class: 'fill', style: `width:${(100 * (r.v || 0) / max).toFixed(1)}%` + (r.color ? ';background:' + r.color : '') }),
        ok(med) ? h('i', { class: 'med', style: `left:${(100 * med / max).toFixed(1)}%` }) : null),
      h('span', { class: 'n' }, fmt(r.v)))));
  }
  function ageCols(rows, height) {  // one column per age band, share on the cap
    const max = Math.max(1e-9, ...rows.map((r) => r.share));
    return h('div', { class: 'cols', style: `height:${height}px` }, rows.map((r) => h('div', { class: 'c',
      title: `${r.label}: ${pct(r.share)} of visitors (95% interval ${pct(r.ci[0])}–${pct(r.ci[1])}) · ${num(r.n)} people` },
      h('div', { class: 'b', style: `height:${(100 * r.share / max).toFixed(1)}%` }, h('span', {}, pct(r.share))), h('div', { class: 'x' }, r.label))));
  }
  const GCOL = ['var(--accent)', 'var(--orange)'];
  const SEG_LABEL = { female: 'Female-presenting', male: 'Male-presenting' };
  const segName = (l) => SEG_LABEL[l] || l;
  const segShort = (l) => ({ female: 'Female', male: 'Male' }[l] || l);
  const groupName = (g) => SEG_LABEL[g] ? SEG_LABEL[g].toLowerCase() : /^<\d+$/.test(g) ? 'under ' + g.slice(1) + 's' : /^\d+\+$/.test(g) ? g.slice(0, -1) + '+ year-olds' : g + ' year-olds';
  function genderSplit(rows) {
    return h('div', {}, h('div', { class: 'split', role: 'img', 'aria-label': rows.map((r) => segName(r.label) + ' ' + pct(r.share)).join(', ') },
      rows.map((r, i) => h('div', { style: `flex:${r.share} 1 0px;background:${GCOL[i % 2]}`, title: `${segName(r.label)}: ${pct(r.share)} (95% interval ${pct(r.ci[0])}–${pct(r.ci[1])}) · ${num(r.n)} people` }))),
      h('div', { class: 'split-key' }, rows.map((r, i) => h('span', {}, h('i', { class: 'dot', style: `background:${GCOL[i % 2]};margin-right:6px` }), segName(r.label) + ' · ', h('b', {}, pct(r.share))))));
  }

  // ------------------------------------------------------------------------------------ Overview
  function renderOverview() { renderKPIs(); renderOvTest(); renderOvPrize(); renderOvDisplays(); renderOvAudience(); }

  function renderKPIs() {
    const m = state.d.summary, p = state.d.prev || {}, ts = (state.d.tsAll || []).filter((t) => !t.gap);
    $('#kpis').replaceChildren(
      kpiTile('Visitors', num(m.visitors), delta(m.visitors, p.visitors), num(m.footfall) + ' door entries', spark(ts.map((t) => t.entries))),
      kpiTile('Display impressions', num(m.impressions), delta(m.impressions, p.impressions), num(m.impressions / m.visitors, 1) + ' displays seen per visitor', spark(ts.map((t) => t.impressions))),
      kpiTile('Visitors who engaged', pct(m.engaged_visitor_rate), delta(m.engaged_visitor_rate, p.engaged_visitor_rate, true), num(m.engaged_visitors) + ' stopped at a display', spark(ts.map((t) => t.engaged))),
      kpiTile('Median engaged time', secs(m.median_engaged_s), delta(m.median_engaged_s, p.median_engaged_s), 'per stop at a display', null));
  }

  const VERD = { improved: ['Winner', 'good'], worse: ['Underperforming', 'bad'] };
  function headline(cmp) {  // the result worth leading with: a clear win/loss first, else the biggest move
    const rows = cmp.zones.map((r) => ({ r, m: r.adjusted || r }));
    const clear = rows.filter((x) => x.m.verdict === 'improved' || x.m.verdict === 'worse');
    return (clear.length ? clear : rows).slice().sort((a, b) => Math.abs(b.m.lift_pct || 0) - Math.abs(a.m.lift_pct || 0))[0];
  }
  function verdictPill(m, undecided = 'Too early to call') {
    const v = VERD[m.verdict];
    return h('span', { class: 'pill ' + (v ? v[1] : '') }, v ? v[0] : undecided);
  }
  function abBars(r, nameA, nameB) {
    const max = Math.max(1e-9, r.a.per_100 || 0, r.b.per_100 || 0) * 1.05;
    const row = (tag, v, name) => h('div', { class: 'ab-row', title: `${name}: ${num(v, 1)} engaged per 100 visitors` },
      h('span', { class: 'tag-' + tag.toLowerCase() }, tag),
      h('div', { class: 'track' }, h('i', { class: 'fill', style: `width:${(100 * (v || 0) / max).toFixed(1)}%;background:var(--series-${tag.toLowerCase()})` })),
      h('span', { class: 'n' }, num(v, 1)));
    return h('div', { class: 'ab-bars' }, row('A', r.a.per_100, nameA), row('B', r.b.per_100, nameB));
  }
  const ciLine = (cmp, m) => ok(m.ci_low_pct) ? `${cmp.ci_level_pct || 95}% confidence: ${signPct(m.ci_low_pct)} to ${signPct(m.ci_high_pct)}` : '';
  function renderOvTest() {
    const box = $('#ovTest'), ab = state.ab;
    if (!ab) { box.replaceChildren(h('div', { class: 'eyebrow' }, 'A/B test'), h('p', { class: 'hint' }, 'Record a second period after a layout change to test it.')); return; }
    const x = headline(ab.cmp), m = x.m, up = m.verdict === 'improved', down = m.verdict === 'worse';
    box.replaceChildren(
      h('div', { class: 'card-h', style: 'margin-bottom:8px' }, h('div', { class: 'eyebrow' }, 'A/B test'), h('a', { class: 'more', href: '#/insights' }, 'All displays →')),
      h('div', { class: 'ab-name' }, x.r.name, ' ', verdictPill(m)),
      h('div', { class: 'headline' }, h('span', { class: 'big' + (up ? ' up' : down ? ' down' : '') }, signPct(m.lift_pct)), h('span', { class: 'sub' }, 'engaged visitors')),
      abBars(x.r, ab.nameA, ab.nameB),
      h('div', { class: 'sub' }, h('span', { class: 'tag-a' }, 'A'), ' ' + ab.nameA + '   ', h('span', { class: 'tag-b' }, 'B'), ' ' + ab.nameB,
        info(`${ciLine(ab.cmp, m)}. Engaged visitors per 100 store visitors, adjusted for store-wide changes. ${ab.cmp.visitors.a} visitors in A, ${ab.cmp.visitors.b} in B.`)),
      h('div', { class: 'mini-h' }, 'Other displays'),
      h('ul', { class: 'ab-others' }, ab.cmp.zones.filter((r) => r.zone_id !== x.r.zone_id).map((r) => { const o = r.adjusted || r;
        return h('li', {}, h('span', {}, r.name), h('span', { class: 'lift' + (o.verdict === 'improved' ? ' up' : o.verdict === 'worse' ? ' down' : '') }, signPct(o.lift_pct)), verdictPill(o, 'No clear change')); })));
  }

  // ---- money and the editable assumptions behind every estimate
  const currency = () => (state.d.recs && state.d.recs.assumptions.currency) || 'USD';
  const money = (x) => { try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency(), maximumFractionDigits: 0 }).format(x); } catch (e) { return Math.round(x).toLocaleString(); } };
  const moneyK = (x) => { if (Math.abs(x) < 1000) return money(x);
    try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency(), notation: 'compact', maximumFractionDigits: 1 }).format(x); } catch (e) { return money(x); } };
  function ensureAssume() {
    const r = state.d.recs;
    if (!state.assume && r && r.assumptions && r.assumptions.visitors_per_day) {
      const a = r.assumptions; state.assume = { V: a.visitors_per_day, value: a.avg_item_value, prLo: a.purchase_rate[0], prHi: a.purchase_rate[1] };
    }
    return state.assume;
  }
  function calc(eff) {  // per-visitor deltas x the (editable) visitors per day and money assumptions
    const A = state.assume, eng = eff.engaged_per_visitor.map((x) => x * A.V);
    const rev = [eng[0] * A.prLo * A.value, eng[1] * A.prHi * A.value];
    return { eng, pass: eff.passers_per_visitor ? eff.passers_per_visitor.map((x) => x * A.V) : null, rev, month: rev.map((x) => x * 30) };
  }
  function layoutPicks() {  // one layout recommendation per display
    const r = state.d.recs; if (!r) return [];
    const seen = new Set(), out = [];
    r.recommendations.filter((x) => x.lever === 'layout' && x.zone_id).forEach((x) => { if (!seen.has(x.zone_id)) { seen.add(x.zone_id); out.push(x); } });
    return out;
  }
  const TYPE = { increase_reach: 'Get it seen', improve_conversion: 'Get people to stop' };
  function prizeTotals(picks) {
    const t = picks.map((x) => calc(x.effect)), sum = (k) => [0, 1].map((i) => t.reduce((a, e) => a + e[k][i], 0));
    return { t, eng: sum('eng'), rev: sum('rev'), month: sum('month') };
  }
  function renderOvPrize() {
    const box = $('#ovPrize'), picks = layoutPicks();
    if (!picks.length || !ensureAssume()) { box.replaceChildren(h('div', { class: 'eyebrow' }, 'Opportunity'), h('p', { class: 'hint' }, 'No display stands out from the rest yet.')); return; }
    const T = prizeTotals(picks);
    box.replaceChildren(
      h('div', { class: 'card-h', style: 'margin-bottom:8px' }, h('div', { class: 'eyebrow' }, 'Opportunity'), h('a', { class: 'more', href: '#/recommend' }, 'Recommendations →')),
      h('div', { class: 'headline' }, h('span', { class: 'big' }, rng(T.month, moneyK)), h('span', { class: 'sub' }, 'a month')),
      h('div', { class: 'sub' }, '+' + rng(T.eng, n0) + ' engaged shoppers a day', info('If each display below reached your store median. Uses the purchase-rate and basket-size assumptions on Recommendations; not a forecast.')),
      h('ul', { class: 'prize-list' }, picks.slice(0, 3).map((x, i) => h('li', {}, h('span', {}, x.zone), h('span', { class: 'what' }, TYPE[x.type] || 'Layout change'),
        h('span', { class: 'val' }, rng(T.t[i].month, moneyK))))));
  }

  function renderOvDisplays() {
    const fx = fixturesRanked(), box = $('#ovDisplays');
    if (!fx.length) { box.replaceChildren(empty('No displays measured yet.')); return; }
    const med = median(fx.map((z) => z.engaged_per_100));
    box.replaceChildren(
      hbars(fx.map((z) => ({ id: z.zone_id, name: z.name, v: z.engaged_per_100, title: `${z.name}: reach ${pct(z.reach)} · stop rate ${pct(z.engagement_rate)} · ${num(z.impressions)} impressions` })),
        { med, onPick: (r) => openZone(r.id) }),
      h('div', { class: 'key' }, h('span', {}, h('i', { style: 'background:var(--accent)' }), 'Engaged per 100 visitors'), h('span', {}, h('i', { class: 'dash' }), 'Store median')));
  }

  function renderOvAudience() {
    const a = state.d.audience, box = $('#ovAudience');
    if (!a || !a.enabled || !a.has_data) { box.replaceChildren(empty(a && !a.enabled ? 'Audience estimates are switched off for this store.' : 'No audience data in this period.')); return; }
    const st = a.store;
    box.replaceChildren(h('div', { class: 'mix' },
      h('div', {}, h('div', { class: 'mini-h', style: 'margin-top:0' }, 'Age'), st.by_age.length ? ageCols(st.by_age, 118) : empty('Too few people to split.')),
      st.by_gender ? h('div', {}, h('div', { class: 'mini-h', style: 'margin-top:0' }, 'Perceived gender'), genderSplit(st.by_gender)) : null));
  }

  // ------------------------------------------------------------------------------------ floor plan
  function renderFloorTab() { views.forEach(drawFloor); renderZoneDetail(); renderZones(); }
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
    if (state.mode === 'traffic') lg.append(h('span', {}, 'Less'), h('span', { class: 'ramp' }), h('span', {}, 'More time spent'),
      info(num(d.heat.total_s / 60, 0) + ' person-minutes tracked. Dashed outline = back of house, excluded from customer numbers.'));
    else lg.append(h('span', {}, 'Darker = more engaged visitors per 100'));
    lg.append(h('span', { style: 'margin-left:auto' }, 'Click a display to focus'));
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
        'Reach ' + pct(m.reach) + ' · ' + num(m.impressions) + ' impressions', h('br'),
        m.engaged === null ? 'Median time ' + secs(m.median_dwell_s) : 'Stop rate ' + pct(m.engagement_rate) + ' · ' + num(m.engaged) + ' engaged', h('br'),
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

  // ---- floor plan: detail panel (a funnel: all visitors -> came near -> stopped)
  const stat = (l, v, sub) => h('div', { class: 'stat' }, h('div', { class: 'l' }, l), h('div', { class: 'v' }, v), sub ? h('div', { class: 's' }, sub) : null);
  function funnel(steps) {
    return h('div', { class: 'funnel' }, steps.map(([l, f, t]) => h('div', { class: 'st', title: t || '' }, h('span', { class: 'sub' }, l),
      h('div', { class: 'track' }, h('i', { class: 'fill', style: `width:${(100 * Math.min(1, f || 0)).toFixed(1)}%` })), h('span', { class: 'n' }, pct(Math.min(1, f))))));
  }
  function renderZoneDetail() {
    const box = $('#zoneDetail'), m = state.d.summary, z = state.zone && zoneById(state.zone);
    if (!z) {
      box.replaceChildren(
        h('div', { class: 'zd-h' }, h('div', {}, h('h2', {}, 'Whole store'), h('div', { class: 'sub' }, 'All displays together'))),
        h('div', { class: 'mini-h' }, 'Visitor funnel'),
        funnel([['Visitors', 1, num(m.visitors) + ' visitors'], ['Stopped', m.engaged_visitor_rate, num(m.engaged_visitors) + ' stopped at a display']]),
        h('div', { class: 'stats' },
          stat('Impressions', num(m.impressions)),
          stat('Stop rate', pct(m.engagement_rate)),
          stat('Door entries', num(m.footfall)),
          stat('Median visit', secs(m.median_visit_s))),
        h('p', { class: 'hint' }, 'Click a display on the map or in the table.'));
      return;
    }
    const fx = fixturesRanked(), rank = fx.findIndex((x) => x.zone_id === z.zone_id) + 1;
    const isFx = z.kind === 'fixture';
    const steps = isFx
      ? [['Visitors', 1, num(m.visitors) + ' visitors'], ['Came near', z.reach, num(z.impressions) + ' impressions'], ['Stopped', z.engaged_per_100 / 100, num(z.engaged) + ' engaged']]
      : [['Visitors', 1, num(m.visitors) + ' visitors'], ['Passed by', z.reach, num(z.impressions) + ' visits']];
    const stats = isFx
      ? [stat('Stop rate', pct(z.engagement_rate), 'of people near it'), stat('Median stop', secs(z.median_engaged_s))]
      : [stat('Median time', secs(z.median_dwell_s)), stat('Visits', num(z.impressions))];
    const edges = state.d.flows.edges;
    const list = (title, es, key) => h('div', {}, h('div', { class: 'mini-h' }, title),
      es.length ? h('ul', { class: 'mini-list' }, es.map((e) => h('li', {}, h('span', {}, e[key]), h('span', { class: 'n' }, num(e.count))))) : h('div', { class: 'sub' }, '–'));
    box.replaceChildren(
      h('div', { class: 'zd-h' }, h('div', {}, h('h2', {}, z.name), h('div', { class: 'sub' }, isFx ? 'Display' + (rank ? ' · #' + rank + ' of ' + fx.length : '') : 'Area · traffic and time only')),
        h('button', { class: 'link-btn', type: 'button', onclick: () => pickZone(null, { toggle: false }) }, 'Clear')),
      h('div', { class: 'mini-h' }, 'Visitor funnel'), funnel(steps),
      h('div', { class: 'stats' }, stats),
      h('div', { class: 'moves' }, list('Arrive from', edges.filter((e) => e.to === z.zone_id).slice(0, 3), 'from_name'),
        list('Go next', edges.filter((e) => e.from === z.zone_id).slice(0, 3), 'to_name')),
      h('p', { class: 'hint' }, h('a', { class: 'more', href: '#/activity' }, 'Traffic over time →')));
  }

  // ---- floor plan: all displays
  function renderZones() {
    const zs = state.d.zones, col = KIND_COLOR(), fx = fixturesRanked();
    const areas = zs.filter((z) => z.kind === 'area');
    const mx = (k) => Math.max(1e-9, ...fx.map((z) => z[k] || 0));
    const M = { e: mx('engaged_per_100'), r: mx('reach'), s: mx('engagement_rate') };
    const cellBar = (v, max, txt, muted) => h('span', { class: 'cell-bar' + (muted ? ' muted-bar' : '') }, txt, h('span', { class: 'tb' }, h('i', { style: `width:${(100 * (v || 0) / max).toFixed(1)}%` })));
    const pick = (id) => ({ onclick: () => pickZone(id), tabindex: '0', onkeydown: keyClick(() => pickZone(id)) });
    const row = (z, i) => h('tr', { class: 'zrow' + (state.zone === z.zone_id ? ' sel' : ''), ...pick(z.zone_id) },
      h('td', {}, h('div', { class: 'zname' }, h('span', { class: 'rank' }, i + 1), h('span', { class: 'dot', style: 'background:' + col[z.kind] }), z.name)),
      h('td', {}, cellBar(z.engaged_per_100, M.e, num(z.engaged_per_100, 1))),
      h('td', {}, cellBar(z.reach, M.r, pct(z.reach), true)),
      h('td', {}, cellBar(z.engagement_rate, M.s, pct(z.engagement_rate), true)),
      h('td', {}, secs(z.median_engaged_s)), h('td', { class: 'muted' }, num(z.impressions)));
    const th = (t, tip) => h('th', tip ? { title: tip } : {}, t);
    const t = h('table', {}, h('thead', {}, h('tr', {}, th('Display'), th('Engaged / 100', 'Engaged visitors per 100 store visitors'),
      th('Reach', 'Share of visitors who came near it'), th('Stop rate', 'Engaged ÷ impressions'), th('Median stop'), th('Impressions'))),
      h('tbody', {}, fx.map(row)));
    const wrap = h('div', {}, t);
    if (areas.length) wrap.append(h('div', { class: 'mini-h' }, 'Areas'),
      h('table', {}, h('tbody', {}, areas.map((z) => h('tr', { class: 'zrow' + (state.zone === z.zone_id ? ' sel' : ''), ...pick(z.zone_id) },
        h('td', {}, h('div', { class: 'zname' }, h('span', { class: 'dot', style: 'background:' + col.area }), z.name)),
        h('td', {}, 'reach ' + pct(z.reach)), h('td', {}, 'median ' + secs(z.median_dwell_s)), h('td', { class: 'muted' }, num(z.impressions) + ' visits'))))));
    if (!fx.length) wrap.replaceChildren(empty('No data yet.'));
    $('#zoneTable').replaceChildren(wrap);
  }

  // ------------------------------------------------------------------------------------ cameras & coverage (Data & privacy)
  function renderCameras() {
    const c = state.cams, card = $('#camCard');
    card.hidden = !c;
    if (!c || !card.offsetWidth) return;
    const cov = c.coverage, cfg = state.cfg, cols = [css('--accent'), css('--teal'), css('--violet'), css('--warn')];
    $('#camSub').textContent = `${c.cameras.length} camera${c.cameras.length > 1 ? 's' : ''} · ${pct(cov.covered_fraction)} of the floor covered` + (c.multi_camera ? ` · ${pct(cov.overlap_fraction)} overlap` : '');
    const W = Math.max(260, Math.min(520, $('#camMap').clientWidth || 440)), H = Math.round(W * cfg.floor.height_m / cfg.floor.width_m), sx = W / cov.nx, sy = H / cov.ny;
    const svg = s('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Which camera owns which part of the floor' });
    svg.append(s('rect', { x: 0, y: 0, width: W, height: H, fill: css('--floor') }));
    const tint = s('g', { opacity: 0.22 });
    cov.owner_grid.forEach((row, j) => { let i = 0; while (i < row.length) { let k = i; while (k < row.length && row[k] === row[i]) k++;
      (row[i] < 0 ? svg : tint).append(s('rect', { x: i * sx, y: j * sy, width: (k - i) * sx + 0.5, height: sy + 0.5, fill: row[i] < 0 ? css('--bad-soft') : cols[row[i] % cols.length] })); i = k; } });
    svg.append(tint);
    const fx = W / cfg.floor.width_m, fy = H / cfg.floor.height_m;
    cfg.zones.forEach((z) => {
      const d = z.polygon.map(([x, y], i) => (i ? 'L' : 'M') + (x * fx).toFixed(1) + ' ' + (y * fy).toFixed(1)).join(' ') + ' Z';
      const cx = z.polygon.reduce((a, p) => a + p[0], 0) / z.polygon.length * fx, cy = z.polygon.reduce((a, p) => a + p[1], 0) / z.polygon.length * fy;
      svg.append(s('path', { d, fill: 'none', stroke: css('--ink'), 'stroke-opacity': 0.5, 'stroke-width': 1.2 }), s('text', { x: cx, y: cy + 4, 'text-anchor': 'middle', style: 'font-size:10.5px' }, z.name + (c.multi_camera && cov.zone_owner[z.id] ? ' [' + cov.zone_owner[z.id] + ']' : '')));
    });
    c.cameras.forEach((cam, i) => { if (!cam.pose) return;
      svg.append(s('circle', { cx: cam.pose.x * fx, cy: cam.pose.y * fy, r: 6, fill: cols[i % cols.length], stroke: css('--card'), 'stroke-width': 2 }), s('text', { x: cam.pose.x * fx + 9, y: cam.pose.y * fy - 7, style: 'font-size:11px;font-weight:650' }, cam.name)); });
    $('#camMap').replaceChildren(svg);
    const cal = (k) => ({ good: ['good', 'good'], fair: ['fair', 'warn'], poor: ['poor', 'bad'] }[k] || [k, '']);
    $('#camList').replaceChildren(h('table', { class: 'runs' }, h('thead', {}, h('tr', {}, ['Camera', 'Role', 'Calibration', 'Error'].map((x) => h('th', {}, x)))),
      h('tbody', {}, c.cameras.map((cam, i) => { const q = cam.calibration, st = cal(q.status);
        return h('tr', {}, h('td', {}, h('span', { class: 'dot', style: 'background:' + cols[i % cols.length] }), ' ' + cam.name), h('td', {}, cam.role),
          h('td', {}, h('span', { class: 'pill conf ' + st[1] }, st[0])),
          h('td', { title: q.loo_rmse_cm === null ? 'Only 4 points: the error is exact by construction' : 'Reprojection error / leave-one-out error' }, q.rmse_cm.toFixed(1) + ' cm' + (q.loo_rmse_cm !== null ? ' / ' + q.loo_rmse_cm.toFixed(1) : ''))); }))),
      h('p', { class: 'hint' }, 'Tint = the camera that counts people there, so overlaps never double count. ', info('Pink = not seen by any camera. Error = calibration accuracy on the floor plan; above 10 cm, zone numbers start to drift.')));
    const warns = [].concat(cov.warnings || [], ...c.cameras.map((cam) => (cam.calibration.warnings || []).map((w) => cam.name + ': ' + w)));
    $('#camWarn').replaceChildren(...warns.map((w) => h('li', {}, w)));
  }

  // ------------------------------------------------------------------------------------ Traffic
  function renderActivity() { $('#seriesZone').value = state.zone || ''; renderActStats(); renderSeries(); renderFlows(); }
  function renderActStats() {
    const ts = (state.d.tsAll || []).filter((t) => !t.gap), m = state.d.summary;
    const peak = ts.reduce((b, t) => (!b || t.entries > b.entries ? t : b), null);
    const slotMin = ts.length > 1 ? Math.max(1, Math.round((ts[1].t_epoch - ts[0].t_epoch) / 60)) : 5;
    const multiDay = ts.length && ts[0].t.slice(0, 10) !== ts[ts.length - 1].t.slice(0, 10);
    const busiest = state.d.zones.filter((z) => z.kind === 'fixture').sort((a, b) => b.impressions - a.impressions)[0];
    const e = state.d.flows.edges, path = e.find((x) => x.from !== 'ENTRANCE' && x.to !== 'EXIT') || e[0];
    $('#actStats').replaceChildren(
      smallTile('Peak time', peak ? peak.t.slice(11, 16) : '–', peak ? (multiDay ? day(peak.t) + ' · ' : '') + num(peak.entries) + ' entries in ' + slotMin + ' min' : ''),
      smallTile('Busiest display', busiest ? busiest.name : '–', busiest ? num(busiest.impressions) + ' impressions' : ''),
      smallTile('Top path', path ? path.from_name + ' → ' + path.to_name : '–', path ? num(path.count) + ' visitors' : ''),
      smallTile('Median visit', secs(m.median_visit_s), num(m.footfall) + ' door entries'));
  }
  const niceStep = (x) => { const p = Math.pow(10, Math.floor(Math.log10(Math.max(x, 1e-9)))), f = x / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; };
  const colPath = (x, y, w, hh, r) => { r = Math.min(r, w / 2, hh); return `M${x} ${y + hh}V${y + r}Q${x} ${y} ${x + r} ${y}H${x + w - r}Q${x + w} ${y} ${x + w} ${y + r}V${y + hh}Z`; };
  function renderSeries() {
    const ts = (state.d && state.d.ts) || [], box = $('#series');
    if (state.seriesErr) { box.replaceChildren(empty(state.seriesErr)); return; }
    if (!ts.length) { box.replaceChildren(empty('No activity in this period.')); return; }
    const W = box.clientWidth || 560, H = 300, m = { l: 34, r: 6, t: 10, b: 38 };
    const gapW = 16, n = ts.length, gaps = ts.filter((t) => t.gap).length;
    const slot = Math.max(6, (W - m.l - m.r - gaps * gapW) / n), bw = Math.min(22, slot * 0.72);
    const ymax = Math.max(1, ...ts.map((t) => Math.max(t.impressions, t.entries))), step = niceStep(ymax / 4), ytop = Math.ceil(ymax / step) * step;
    const y = (v) => m.t + (H - m.t - m.b) * (1 - v / ytop);
    const svg = s('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Impressions, engaged visits and door entries per time slot' });
    for (let v = 0; v <= ytop + 1e-9; v += step) svg.append(s('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: css(v === 0 ? '--line' : '--grid') }), s('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, Math.round(v)));
    const soft = css('--accent'), acc = css('--accent'), ink = css('--ink-2');
    let x = m.l, pts = [], lastDay = '';
    const every = Math.ceil(n / Math.max(1, Math.floor((W - m.l) / 56)));
    ts.forEach((t, i) => {
      if (t.gap) { svg.append(s('line', { x1: x + gapW / 2, x2: x + gapW / 2, y1: m.t, y2: H - m.b, stroke: css('--muted'), 'stroke-dasharray': '3 3' })); x += gapW; }
      const bx = x + (slot - bw) / 2;
      if (t.impressions > 0) svg.append(s('path', { d: colPath(bx, y(t.impressions), bw, y(0) - y(t.impressions), 4), fill: soft, 'fill-opacity': 0.28 }));
      if (t.engaged > 0) svg.append(s('path', { d: colPath(bx + bw * 0.22, y(t.engaged), bw * 0.56, y(0) - y(t.engaged), 3), fill: acc }));
      pts.push([bx + bw / 2, y(t.entries)]);
      svg.append(s('rect', { x, y: m.t, width: slot, height: H - m.t - m.b, fill: 'transparent' },
        s('title', {}, `${day(t.t)} ${t.t.slice(11, 16)} · ${t.impressions} impressions · ${t.engaged} engaged · ${t.entries} entries`)));
      const d = t.t.slice(0, 10);
      if (i % every === 0 || t.gap) {
        svg.append(s('text', { x: bx + bw / 2, y: H - m.b + 15, 'text-anchor': 'middle' }, t.t.slice(11, 16)));
        if (d !== lastDay) { svg.append(s('text', { x: bx + bw / 2, y: H - m.b + 29, 'text-anchor': 'middle', style: 'font-weight:600' }, day(t.t))); lastDay = d; }
      }
      x += slot;
    });
    svg.insertBefore(s('polyline', { points: pts.map((p) => p.map((q) => q.toFixed(1)).join(',')).join(' '), fill: 'none', stroke: ink, 'stroke-width': 2, 'stroke-linejoin': 'round', 'pointer-events': 'none' }), null);
    const lg = h('div', { class: 'key' }, h('span', {}, h('i', { style: 'background:var(--accent);opacity:.3' }), 'Impressions'), h('span', {}, h('i', { style: 'background:var(--accent)' }), 'Engaged'),
      h('span', {}, h('i', { class: 'line', style: 'border-color:var(--ink-2)' }), 'Door entries'));
    box.replaceChildren(svg, lg);
  }
  function renderFlows() {
    const f = state.d.flows, box = $('#flows');
    if (!f.edges.length) { box.replaceChildren(empty('No movement data yet.')); return; }
    const max = f.edges[0].count;
    box.replaceChildren(...f.edges.slice(0, 10).map((e) => h('div', { class: 'flow' },
      h('div', { class: 't', title: `${e.from_name} → ${e.to_name}: ${e.count} visitors` }, e.from_name, h('i', {}, '→'), e.to_name), h('div', { class: 'c' }, num(e.count)),
      h('div', { class: 'bar', style: 'width:' + Math.max(1, Math.round(100 * e.count / max)) + '%' }))),
      h('div', { class: 'sub', style: 'margin-top:10px' }, 'From ' + num(f.n_paths) + ' visits with a path'));
  }

  // ------------------------------------------------------------------------------------ A/B test
  function renderInsightsTab() { renderCompare(); renderInsights(); }
  const CONF = { high: 'good', medium: 'warn', low: 'bad' };
  const KIND = { result: 'Result', opportunity: 'Opportunity', warning: 'Watch', info: 'Pattern' };
  function insightCard(c) {
    return h('div', { class: 'ins ' + c.kind }, h('div', { class: 'ins-h' }, h('span', { class: 'kind' }, KIND[c.kind] || 'Insight'), h('span', { class: 'pill conf ' + (CONF[c.confidence] || '') }, c.confidence + ' confidence')),
      h('h3', {}, c.title), h('p', {}, c.detail));
  }
  function renderInsights() {
    const ins = state.d.insights, box = $('#insights');
    box.replaceChildren(...(ins.length ? ins.map(insightCard) : [empty('Cards appear once a display has 20+ impressions.')]));
  }
  async function renderCompare() {
    const a = $('#cmpA').value, b = $('#cmpB').value, box = $('#compare');
    if ((state.sessions || state.runs).length < 2) { box.replaceChildren(empty('Record a second period (after a layout change) to run an A/B test.')); return; }
    if (a === b) { box.replaceChildren(empty('Pick two different periods.')); return; }
    let cmp;
    try { cmp = await api(`/api/compare?a=${a}&b=${b}`); } catch (e) { box.replaceChildren(empty(e.message)); return; }
    if (cmp.error) { box.replaceChildren(empty(cmp.error)); return; }
    const nameOf = (ids) => { const x = state.sessions.find((q) => sessKey(q) === ids); return x ? sessName(x) : ids; };
    const nA = nameOf(a), nB = nameOf(b), top = headline(cmp), tm = top.m, up = tm.verdict === 'improved', down = tm.verdict === 'worse';
    const lvNote = cmp.layout_versions ? h('div', { class: 'lv-note' }, h('b', {}, 'The floor plan changed between these periods. '),
      'A change can come from where a display now stands; untouched displays are the yardstick.') : null;
    const hero = h('div', { class: 'ab-hero' },
      h('div', {}, h('div', { class: 'eyebrow' }, up ? 'Clear winner' : down ? 'Clear loser' : 'Biggest change'),
        h('div', { class: 'ab-name', style: 'margin-top:4px' }, top.r.name, ' ', verdictPill(tm)),
        h('div', { class: 'headline' }, h('span', { class: 'big' + (up ? ' up' : down ? ' down' : '') }, signPct(tm.lift_pct)), h('span', { class: 'sub' }, 'engaged visitors per 100')),
        h('div', { class: 'sub' }, ciLine(cmp, tm))),
      h('div', {}, abBars(top.r, nA, nB),
        h('div', { class: 'key' }, h('span', {}, h('span', { class: 'tag-a' }, 'A'), ' ' + nA + ' · ' + num(cmp.visitors.a) + ' visitors'),
          h('span', {}, h('span', { class: 'tag-b' }, 'B'), ' ' + nB + ' · ' + num(cmp.visitors.b) + ' visitors'))));

    const rows = cmp.zones.map((r) => ({ r, m: r.adjusted || r }));
    const lo = Math.min(-50, ...rows.map((x) => (ok(x.m.ci_low_pct) ? x.m.ci_low_pct : 0)));
    const hi = Math.min(300, Math.max(50, ...rows.map((x) => (ok(x.m.ci_high_pct) ? x.m.ci_high_pct : 0))));
    const X = (v) => 100 * (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo);
    const vmax = Math.max(1e-9, ...rows.map((x) => Math.max(x.r.a.per_100 || 0, x.r.b.per_100 || 0))) * 1.08;
    const P = (v) => 100 * (v || 0) / vmax;
    const level = cmp.ci_level_pct || 95;
    const grid = h('div', { class: 'ab-grid' }, h('div', { class: 'hd' }, 'Display'), h('div', { class: 'hd dumb' }, 'Engaged per 100 visitors, A → B'),
      h('div', { class: 'hd' }, 'Change', info(`Dot = measured change, bar = ${level}% confidence interval, dashed line = no change. Adjusted for store-wide shifts using the displays that were not changed. ${cmp.note || ''}`)),
      h('div', { class: 'hd vd' }, 'Result'));
    rows.forEach((x, i) => {
      const c = (i === rows.length - 1 ? ' last' : '') + (x.r.zone_id === top.r.zone_id ? ' row-hl' : '');
      const col = x.m.verdict === 'improved' ? 'var(--good)' : x.m.verdict === 'worse' ? 'var(--bad)' : 'var(--muted)';
      const pa = P(x.r.a.per_100), pb = P(x.r.b.per_100), has = ok(x.m.lift_pct);
      grid.append(
        h('div', { class: 'nm' + c }, x.r.name),
        h('div', { class: 'dumb' + c, title: `A ${num(x.r.a.per_100, 1)} → B ${num(x.r.b.per_100, 1)} engaged per 100 visitors` },
          h('div', { class: 'db' }, h('div', { class: 'db-track' },
            h('i', { class: 'db-line', style: `left:${Math.min(pa, pb).toFixed(1)}%;width:${Math.abs(pb - pa).toFixed(1)}%` }),
            h('i', { class: 'db-dot a', style: `left:${pa.toFixed(1)}%` }), h('i', { class: 'db-dot b', style: `left:${pb.toFixed(1)}%` })),
          h('span', { class: 'db-n' }, num(x.r.a.per_100, 1) + ' → ' + num(x.r.b.per_100, 1)))),
        h('div', { class: 'fp-cell' + c, title: has ? `${signPct(x.m.lift_pct)} (${level}% CI ${signPct(x.m.ci_low_pct)} to ${signPct(x.m.ci_high_pct)})` : '' },
          h('span', { class: 'lift' }, has ? signPct(x.m.lift_pct) : '–'),
          h('div', { class: 'fp' }, h('i', { class: 'fp-zero', style: `left:${X(0).toFixed(1)}%` }),
            has && ok(x.m.ci_low_pct) ? h('i', { class: 'fp-ci', style: `left:${X(x.m.ci_low_pct).toFixed(1)}%;width:${(X(x.m.ci_high_pct) - X(x.m.ci_low_pct)).toFixed(1)}%;background:${col}` }) : null,
            has ? h('i', { class: 'fp-pt', style: `left:${X(x.m.lift_pct).toFixed(1)}%;background:${col}` }) : null)),
        h('div', { class: 'vd' + c }, verdictPill(x.m, 'No clear change')));
    });
    const decided = rows.some((x) => x.m.verdict === 'improved' || x.m.verdict === 'worse');
    box.replaceChildren(...[lvNote, hero, grid,
      h('div', { class: 'ab-foot' }, h('span', {}, h('span', { class: 'tag-a' }, 'A'), ' before'), h('span', {}, h('span', { class: 'tag-b' }, 'B'), ' after'),
        h('span', {}, decided ? `Results are ${level}% confident, adjusted for store-wide shifts.` : 'No display has changed clearly yet: small stores need a few hundred visitors per period.'))].filter(Boolean));
  }

  // ------------------------------------------------------------------------------------ Audience
  function renderAudience() {
    const a = state.d.audience, off = $('#audOff'), body = $('#audBody');
    const say = (msg) => { off.hidden = false; body.hidden = true; off.replaceChildren(empty(msg)); };
    if (!a) return say('This view is not in the data file. Rebuild the site with a current version of Glance.');
    if (!a.enabled) return say('Audience estimates are switched off for this store. They are opt-in and need a legal review first (see Data & privacy).');
    if (!a.has_data) return say('No audience counts in this period yet.');
    off.hidden = true; body.hidden = false;
    const c = a.coverage, st = a.store;
    const hl = a.highlights.filter((x) => x.verdict === 'over' || x.verdict === 'under')
      .sort((p, q) => Math.abs(Math.log(q.index || 1)) - Math.abs(Math.log(p.index || 1)));
    const topAge = st.by_age.slice().sort((p, q) => q.share - p.share)[0];
    const best = hl.filter((x) => x.verdict === 'over').sort((p, q) => q.index - p.index)[0];
    $('#audStats').replaceChildren(
      smallTile('Visitors counted', num(c.visitors_counted), 'age known for ' + pct(c.age_known_share)),
      smallTile('Largest age group', topAge ? topAge.label : '–', topAge ? pct(topAge.share) + ' of visitors' : ''),
      smallTile('Gender split', st.by_gender ? st.by_gender.map((g) => pct(g.share)).join(' / ') : '–', st.by_gender ? st.by_gender.map((g) => segShort(g.label)).join(' / ') + '-presenting' : 'hidden: a group is too small'),
      smallTile('Strongest pull', best ? xfmt(best.index) : '–', best ? best.zone + ' · ' + groupName(best.group) : 'no group stands out yet'));
    $('#audCoverage').replaceChildren('n = ' + num(st.n), info(`Share of visitors per age band with its 95% interval (hover a column). Groups under ${a.min_cell} people are merged or hidden.`));
    $('#audAge').replaceChildren(st.by_age.length ? ageCols(st.by_age, 180) : empty('Too few people with a known age to show a split.'));
    $('#audGender').replaceChildren(st.by_gender ? genderSplit(st.by_gender) : empty('One group is below the minimum size, so no split is shown.'));
    const mx = st.matrix;
    if (mx) {
      const top = Math.max(1e-9, ...mx.share.flat());
      $('#audMatrix').replaceChildren(h('div', { class: 'mini-h' }, 'Age × gender'), h('table', { class: 'heat-t' }, h('thead', {}, h('tr', {}, h('th', {}, ''), mx.cols.map((x) => h('th', {}, segShort(x))))),
        h('tbody', {}, mx.rows.map((row, i) => h('tr', {}, h('td', {}, row), mx.share[i].map((v, j) => h('td', {
          style: `background:color-mix(in srgb, var(--accent) ${Math.round(8 + 40 * v / top)}%, transparent)`, title: `${row}, ${segName(mx.cols[j])}: ${pct(v)} of visitors · ${num(mx.n[i][j])} people` }, pct(v))))))));
    } else $('#audMatrix').replaceChildren(h('p', { class: 'hint' }, 'Age × gender hidden: a cell is under ' + a.min_cell + ' people.'));

    const cell = (g) => {
      const has = ok(g.index) && g.verdict !== 'n/a';
      let bg = 'var(--soft)', strong = false;
      if (has) {
        const t = g.index >= 1 ? Math.min(1, (g.index - 1) / 0.8) : Math.min(1, (1 - g.index) / 0.5), p = Math.round(6 + 80 * t);
        bg = `color-mix(in srgb, ${g.index >= 1 ? 'var(--div-pos)' : 'var(--div-neg)'} ${p}%, var(--div-mid))`; strong = p >= 50;
      }
      return h('div', { class: 'aff-cell' + (strong ? ' strong' : ''), style: 'background:' + bg,
        title: `${segName(g.label)}: ${pct(g.share)} of the people engaging here vs ${pct(g.visitor_share)} of visitors` + (g.ci ? ` · index ${xfmt(g.index)} (95% interval ${xfmt(g.ci[0])}–${xfmt(g.ci[1])})` : '') + ` · ${num(g.n)} people` },
        h('b', {}, has ? xfmt(g.index) : '–'), segShort(g.label));
    };
    $('#audZones').replaceChildren(h('div', { class: 'aff' }, a.zones.map((z) => h('div', { class: 'aff-row' },
      h('div', { class: 'nm' }, z.name, h('small', {}, num(z.n) + ' ' + (z.base === 'engaged' ? 'engaged' : 'visits'))),
      z.age || z.gender ? h('div', { class: 'aff-cells' }, (z.age || []).map(cell), z.age && z.gender ? h('i', { class: 'aff-sep' }) : null, (z.gender || []).map(cell))
        : h('div', { class: 'sub' }, 'Too few people to split')))));

    $('#audHighlights').replaceChildren(...(hl.length ? hl.slice(0, 5).map((x) => {
      const z = a.zones.find((q) => q.zone_id === x.zone_id), g = z && (z[x.dimension] || []).find((q) => q.label === x.group);
      return h('div', { class: 'standout', title: `index ${xfmt(x.index)}, 95% interval ${xfmt(x.ci[0])}–${xfmt(x.ci[1])}, ${x.n} people` },
        h('span', { class: 'x ' + x.verdict }, xfmt(x.index)), h('span', { class: 't' }, x.zone + ' · ' + groupName(x.group)),
        h('span', { class: 'd' }, g ? `${pct(g.share)} of its engagers vs ${pct(g.visitor_share)} of visitors` : num(x.n) + ' people'));
    }) : [empty('No group stands out clearly yet.')]));
    $('#audNotes').replaceChildren(...a.notes.map((n) => h('li', {}, n)));
  }

  // ------------------------------------------------------------------------------------ Recommendations
  function renderAssumptions() {
    const A = state.assume, box = $('#assumeForm'), as = state.d.recs.assumptions;
    const isRate = (k) => k === 'prLo' || k === 'prHi';
    const field = (label, key, step, unit, tip) => h('label', { class: 'af', title: tip }, h('span', {}, label),
      h('span', { class: 'af-in' }, h('input', { type: 'number', min: '0', step, value: String(isRate(key) ? +(A[key] * 100).toFixed(1) : +A[key].toFixed(2)),
        'aria-label': label, onchange: (e) => { const v = parseFloat(e.target.value); if (isFinite(v) && v >= 0) { A[key] = isRate(key) ? v / 100 : v; if (A.prLo > A.prHi) { if (key === 'prLo') A.prHi = A.prLo; else A.prLo = A.prHi; } state.dirty.add('overview'); renderRecommendBody(); renderAssumptions(); } } }), unit));
    box.replaceChildren(h('div', { class: 'ah' }, h('b', { style: 'font-size:13px' }, 'Your numbers', info(as.note || 'Inputs, not measurements.')),
      h('button', { class: 'btn sm', type: 'button', onclick: () => { state.assume = null; state.dirty.add('overview'); renderRecommend(); } }, 'Reset')),
      field('Visitors / day', 'V', '10', '', 'Source: ' + as.visitors_per_day_source),
      field('Avg. spend', 'value', '1', as.currency, 'Average spend of one engaged shopper who buys'),
      field('Buy rate, low', 'prLo', '0.5', '%', 'Cautious share of engaged shoppers who buy'),
      field('Buy rate, high', 'prHi', '0.5', '%', 'Optimistic share of engaged shoppers who buy'));
  }
  function floorMap(moves) {
    const cfg = state.cfg, W = Math.max(280, $('#recMap').clientWidth || 520), H = Math.round(W * cfg.floor.height_m / cfg.floor.width_m);
    const sx = W / cfg.floor.width_m, sy = H / cfg.floor.height_m, kc = KIND_COLOR();
    const svg = s('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Floor plan with suggested moves' });
    svg.append(s('rect', { x: 0.5, y: 0.5, width: W - 1, height: H - 1, rx: 8, fill: css('--floor'), stroke: css('--line') }));
    const path = (poly) => poly.map(([x, y], i) => (i ? 'L' : 'M') + (x * sx).toFixed(1) + ' ' + (y * sy).toFixed(1)).join(' ') + ' Z';
    const cen = (poly) => [poly.reduce((a, p) => a + p[0], 0) / poly.length * sx, poly.reduce((a, p) => a + p[1], 0) / poly.length * sy];
    const moved = new Set(moves.map((m) => m.zone_id));
    cfg.zones.forEach((z) => {
      const c = cen(z.polygon);
      svg.append(s('path', { d: path(z.polygon), fill: 'none', stroke: kc[z.kind], 'stroke-width': moved.has(z.id) ? 1 : 1.4, 'stroke-dasharray': z.kind === 'staff' || moved.has(z.id) ? '4 3' : '0', opacity: moved.has(z.id) ? 0.6 : 1 }),
        s('text', { x: c[0], y: c[1] + 4, 'text-anchor': 'middle', style: 'font-size:11px' }, z.name));
    });
    const e = cfg.entrance;
    svg.append(s('line', { x1: e.p1[0] * sx, x2: e.p2[0] * sx, y1: e.p1[1] * sy, y2: e.p2[1] * sy, stroke: css('--bad'), 'stroke-width': 4 }));
    svg.append(s('defs', {}, s('marker', { id: 'arr', viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0 0L10 5L0 10z', fill: css('--accent') }))));
    moves.forEach((m) => {
      const a = cen(m.from), b = cen(m.to), name = (cfg.zones.find((z) => z.id === m.zone_id) || {}).name || m.zone_id;
      svg.append(s('path', { d: path(m.to), fill: css('--accent'), 'fill-opacity': 0.16, stroke: css('--accent'), 'stroke-width': 2 }),
        s('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: css('--accent'), 'stroke-width': 2, 'marker-end': 'url(#arr)' }),
        s('text', { x: b[0], y: b[1] - 4, 'text-anchor': 'middle', style: 'font-size:11px;font-weight:650;fill:' + css('--ink') }, name + ' →'));
    });
    return svg;
  }
  function recCard(r) {
    const e = calc(r.effect), ev = r.evidence || {};
    const scale = Math.max(0.6, ev.reach || 0, ev.engagement_rate || 0, ev.median_reach || 0, ev.median_engagement_rate || 0) * 1.1;
    const gb = (l, v, med, low) => h('div', { class: 'gb', title: `${l}: ${pct(v)} (store median ${pct(med)})` }, h('span', { class: 'l' }, l),
      h('div', { class: 'track' }, h('i', { class: 'fill' + (low ? ' low' : ''), style: `width:${(100 * (v || 0) / scale).toFixed(1)}%` }), ok(med) ? h('i', { class: 'med', style: `left:${(100 * med / scale).toFixed(1)}%` }) : null),
      h('span', { class: 'n' }, pct(v)));
    return h('article', { class: 'rec' },
      h('div', { class: 'rec-h' }, h('div', {}, h('div', { class: 'type' }, TYPE[r.type] || 'Layout change'), h('h3', {}, r.zone || r.title, info(r.why + (r.scenario ? ' Scenario: ' + r.scenario + '.' : '')))),
        h('span', { class: 'pill conf ' + (CONF[r.confidence] || '') }, r.confidence + ' confidence')),
      ok(ev.reach) ? h('div', { class: 'gap-bars' }, gb('Reach', ev.reach, ev.median_reach, r.type === 'increase_reach'), gb('Stop rate', ev.engagement_rate, ev.median_engagement_rate, r.type === 'improve_conversion')) : h('p', { class: 'sub', style: 'margin:0' }, r.why),
      h('div', { class: 'eff' }, h('div', { class: 'es' }, h('div', { class: 'v' }, '+' + rng(e.eng, n0)), h('div', { class: 'l' }, 'engaged shoppers / day')),
        h('div', { class: 'es money' }, h('div', { class: 'v' }, rng(e.month, moneyK)), h('div', { class: 'l' }, 'est. sales / month'))),
      h('ul', { class: 'acts' }, r.actions.map((x) => h('li', {}, x.replace(/with Compare/, 'with an A/B test')))));
  }
  function mktCard(m) {
    const A = state.assume, base = state.d.recs.assumptions, f = A.V / base.visitors_per_day;
    const sc = m.scenarios;
    return h('article', { class: 'rec' },
      h('div', { class: 'rec-h' }, h('div', {}, h('div', { class: 'type' }, 'Audience'), h('h3', {}, m.zone + ' draws ' + groupName(m.group), info(m.why + ' ' + (m.note || '')))),
        h('span', { class: 'pill conf ' + (CONF[m.confidence] || '') }, m.confidence + ' confidence')),
      h('div', { class: 'headline', style: 'margin:0' }, h('span', { class: 'big', style: 'font-size:28px' }, xfmt(m.index)), h('span', { class: 'sub' }, 'over-represented among its engagers')),
      h('table', { class: 'runs sens' }, h('thead', {}, h('tr', {}, h('th', {}, 'If their visits rise'), sc.map((x) => h('th', {}, '+' + x.uplift_pct + '%')))),
        h('tbody', {}, h('tr', {}, h('td', {}, 'Engaged / day'), sc.map((x) => h('td', {}, '+' + n0(x.extra_engaged_all_per_day * f)))),
          h('tr', {}, h('td', {}, 'Sales / day'), sc.map((x) => { const eng = x.extra_engaged_all_per_day * f; return h('td', {}, rng([eng * A.prLo * A.value, eng * A.prHi * A.value], money)); })))),
      h('ul', { class: 'acts' }, m.actions.slice(0, 2).map((x) => h('li', {}, x))));
  }
  function renderRecommendBody() {
    const r = state.d.recs, picks = layoutPicks(), hero = $('#recHero');
    if (picks.length) {
      const T = prizeTotals(picks);
      hero.replaceChildren(h('div', { class: 'eyebrow' }, 'Size of the prize', info('Sum of one scenario per display (each brought up to your store median), assuming they do not affect each other. Not a forecast.')),
        h('div', { class: 'headline' }, h('span', { class: 'big' }, rng(T.month, moneyK)), h('span', { class: 'sub' }, 'a month')),
        h('div', { class: 'sub2' }, '+' + rng(T.eng, n0) + ' engaged shoppers a day · ' + rng(T.rev, moneyK) + ' a day'),
        h('p', { class: 'hint' }, picks.length + ' layout change' + (picks.length > 1 ? 's' : '') + ', each display brought up to your store median.'));
    } else hero.replaceChildren(h('div', { class: 'eyebrow' }, 'Size of the prize'), h('p', { class: 'hint' }, r.warnings[0] || 'No display stands out from the others yet.'));
    const moves = r.layout_plan.moves;
    $('#recMapWrap').hidden = !moves.length;
    if (moves.length) {
      $('#recMapSub').textContent = 'dashed = now · filled = a busier spot';
      $('#recMap').replaceChildren(floorMap(moves));
      $('#recPlan').replaceChildren(...r.recommendations.filter((x) => x.zone_id).map((x) => h('li', {}, h('b', {}, x.zone + ': '), x.title.split(': ').slice(1).join(': ') || x.title, h('span', { class: 'sub' }, ' · ' + rng(calc(x.effect).month, moneyK) + ' / month'))));
    }
    $('#recList').replaceChildren(...(r.recommendations.length ? r.recommendations.map(recCard) : [empty(r.warnings[0] || 'No display stands out from the others yet.')]));
    $('#recMkt').replaceChildren(...(r.marketing.length ? r.marketing.map(mktCard) : [empty(state.d.audience && state.d.audience.enabled ? 'No group is clearly over-represented at a display yet.' : 'Needs audience estimates (switched off for this store).')]));
  }
  function renderRecommend() {
    const r = state.d.recs;
    if (!r) { $('#recList').replaceChildren(empty('This view is not in the data file. Rebuild the site with a current version of Glance.')); return; }
    ensureAssume();
    $('#recAssume').hidden = !state.assume;
    if (!state.assume) { $('#recMapWrap').hidden = true; $('#recList').replaceChildren(empty(r.warnings[0] || 'Not enough data.')); $('#recMkt').replaceChildren(); return; }
    renderAssumptions();
    renderRecommendBody();
    $('#recCaveats').replaceChildren(...(r.caveats || []).concat(r.warnings || []).map((x) => h('li', {}, x)));
  }

  // ------------------------------------------------------------------------------------ Data & privacy
  function renderData() { renderQuality(); renderCameras(); }
  function renderQuality() {
    const q = state.d.quality, p = q.privacy, box = $('#quality');
    const tile = (b, t) => h('li', {}, h('b', {}, b), h('span', {}, t));
    const runs = h('table', { class: 'runs' }, h('thead', {}, h('tr', {}, ['Period', 'Frames', 'Detector', 'Speed', 'Customers', 'Staff', 'Calibration'].map((x) => h('th', {}, x)))),
      h('tbody', {}, q.runs.map((r) => { const m = r.meta || {}; return h('tr', {}, h('td', {}, pretty(r.label) || r.run_id), h('td', {}, num(m.frames_processed)), h('td', {}, m.detector || '–'),
        h('td', {}, m.processing_fps ? num(m.processing_fps) + ' fps' : '–'), h('td', {}, num(m.customers)), h('td', {}, num(m.staff)), h('td', {}, ok(m.calibration_rmse_cm) ? m.calibration_rmse_cm + ' cm' : '–')); })));
    box.replaceChildren(
      h('ul', { class: 'priv q-list' },
        tile(num(p.faces_stored), 'face images stored'),
        tile(num(p.raw_frames_stored), 'raw video frames stored'),
        tile(p.trajectories_stored ? 'Stored' : 'None', 'movement paths stored'),
        tile(p.appearance_reid ? 'On' : 'None', 'appearance re-identification'),
        tile(ok(p.id_rotation_hours) ? p.id_rotation_hours + ' h' : '–', 'anonymous ID rotation'),
        tile(p.blur_mode === 'person' ? 'Whole body' : pretty(p.blur_mode) || '–', 'blurred before any preview'),
        tile(p.demographics === 'aggregate_counts_only' ? 'Counts only' : 'Off', 'age & gender estimates')),
      q.warnings.length ? h('div', { class: 'q-sec' }, h('div', { class: 'mini-h' }, 'Heads-up'), h('ul', { class: 'q-list q-warn' }, q.warnings.map((x) => h('li', {}, x)))) : null,
      h('div', { class: 'q-sec' }, h('div', { class: 'mini-h' }, 'Processing runs'), h('div', { style: 'overflow-x:auto' }, runs)));
  }

  init();
})();
