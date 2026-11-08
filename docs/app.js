/* Stock Finder India - client app (no build step, no dependencies).
 * Reads JSON produced by the engine (GitHub Actions) from ./data/ and does all
 * personal calculations (position size, P&L) on the device. Nothing personal
 * ever leaves the phone/laptop: settings and "my positions" live in localStorage. */
"use strict";

const DATA = "data/";
const LS = { settings: "sf.settings", my: "sf.my", seen: "sf.seenIdeas", alerted: "sf.alerted", tab: "sf.tab" };
const DEFAULTS = { tradeMoney: 10000, protect: false, capital: 500000, ltPct: 70, swingPct: 30, riskPct: 1, maxPosPct: 20, notify: false };
const S = { latest: null, live: null, news: null, bt: null, track: null, swingFilter: "buy", refreshingLive: false };

// ------------------------------------------------------------------ storage (safe)
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const settings = () => {
  const st = { ...DEFAULTS, ...store.get(LS.settings, {}) };
  if (!st.protectChosen) st.protect = false;          // protection only if explicitly chosen in Settings
  return st;
};
const myPositions = () => store.get(LS.my, []);

// ------------------------------------------------------------------ format helpers
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? esc(u) : "#");
const inr = (x, d = 0) => (x == null || isNaN(x) ? "–" : "₹" + Number(x).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
// prices: whole rupees above ₹1,000, otherwise up to 2 decimals (no trailing ".0")
const px = (x) => (x == null || isNaN(x) ? "–" : "₹" + Number(x).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(x) < 1000 ? 2 : 0 }));
const shares = (n) => `${num(n, 0)} share${n === 1 ? "" : "s"}`;
const num = (x, d = 1) => (x == null || isNaN(x) ? "–" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: d }));
const pct = (x, d = 1, sign = true) => (x == null || isNaN(x) ? "–" : (sign && x > 0 ? "+" : "") + Number(x).toFixed(d) + "%");
const tone = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
const ago = (iso) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 36 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
};
const fmtDate = (d) => (d ? new Date(d).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "–");
const fmtClock = (d) => (d ? new Date(d).toLocaleTimeString("en-IN", {
  hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata", timeZoneName: "short",
}) : "–");
const indiaDateOf = (value) => new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
}).format(new Date(value));
const indiaDate = () => indiaDateOf(new Date());

function liveQuote(sym) {
  const q = S.live && S.live.quotes && S.live.quotes[sym];
  return q ? q : null;
}

function livePrice(sym) {
  const q = liveQuote(sym);
  return quoteFresh(q) ? q : null;
}

function quoteChange(q) {
  if (!q) return null;
  if (q.change_pct != null) return Number(q.change_pct);
  return q.prev_close ? (q.price / q.prev_close - 1) * 100 : null;
}

function quoteFresh(q) {
  if (!q || q.stale || q.is_fresh === false) return false;
  if (q.date && q.date !== indiaDate()) return false;
  const stamp = q.asof || S.live?.updated;
  if (!stamp) return q.is_fresh === true;
  const age = Date.now() - new Date(stamp).getTime();
  return Number.isFinite(age) && age >= -2 * 60 * 1000 && age <= 30 * 60 * 1000;
}

function feedView() {
  const f = S.live || {};
  const count = Object.keys(f.quotes || {}).length;
  const age = f.updated ? Date.now() - new Date(f.updated).getTime() : Infinity;
  let key = f.feed_status;
  if (!key) {
    if (!S.live) key = "unavailable";
    else if (f.market_open === false) key = "closed";
    else if (f.market_open !== true) key = "unavailable";
    else if (!count) key = "unavailable";
    else key = age <= 30 * 60 * 1000 ? "near_live" : "delayed";
  }
  if (f.market_open && age > 30 * 60 * 1000 && (key === "near_live" || key === "partial")) key = "delayed";
  const map = {
    near_live: { cls: "good", label: "Near-live feed", icon: "●" },
    partial: { cls: "warn", label: "Partial feed", icon: "●" },
    delayed: { cls: "warn", label: "Prices delayed", icon: "●" },
    unavailable: { cls: "bad", label: "Feed unavailable", icon: "●" },
    closed: { cls: "info", label: "Market closed", icon: "○" },
  };
  return { key, ...(map[key] || map.delayed), count, coverage: f.coverage_pct, updated: f.updated };
}

// ------------------------------------------------------------------ charts (inline SVG)
function spark(arr, h = 46) {
  const a = (arr || []).filter((v) => v != null);
  if (a.length < 2) return "";
  const w = 300, min = Math.min(...a), max = Math.max(...a), r = max - min || 1;
  const pts = a.map((v, i) => `${((i / (a.length - 1)) * w).toFixed(1)},${(h - 3 - ((v - min) / r) * (h - 6)).toFixed(1)}`).join(" ");
  const col = a[a.length - 1] >= a[0] ? "var(--up)" : "var(--down)";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <polyline fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke" points="${pts}"/></svg>`;
}

function lineChart(curve) {
  if (!curve || curve.length < 2) return "";
  const w = 600, h = 220, pad = 34;
  const vals = curve.flatMap((c) => [c.p, c.b]);
  const min = Math.min(...vals), max = Math.max(...vals), r = max - min || 1;
  const x = (i) => pad + (i / (curve.length - 1)) * (w - pad - 8);
  const y = (v) => h - 22 - ((v - min) / r) * (h - 40);
  const path = (k) => curve.map((c, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(c[k]).toFixed(1)}`).join("");
  const ticks = [min, (min + max) / 2, max].map((v) => `<text x="2" y="${y(v) + 4}" font-size="11" fill="currentColor" opacity=".6">${Math.round(v)}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Model portfolio vs Nifty 50, growth of 100">
    ${ticks}
    <path d="${path("b")}" fill="none" stroke="var(--muted)" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <path d="${path("p")}" fill="none" stroke="var(--brand)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
    <text x="${pad}" y="${h - 4}" font-size="11" fill="currentColor" opacity=".6">${esc(curve[0].d)}</text>
    <text x="${w - 8}" y="${h - 4}" font-size="11" fill="currentColor" opacity=".6" text-anchor="end">${esc(curve[curve.length - 1].d)}</text>
  </svg>
  <div class="small"><span style="color:var(--brand)">━ Model portfolio</span> &nbsp; <span class="muted">━ Nifty 50</span> (growth of ₹100)</div>`;
}

// ------------------------------------------------------------------ position sizing
function swingSize(pk) {
  const st = settings();
  const regime = S.latest?.market?.regime ?? 2;
  const swingCap = (st.capital * st.swingPct) / 100;
  let riskAmt = (swingCap * st.riskPct) / 100;
  if (regime === 1) riskAmt /= 2; // cautious market: half size
  const entry = pk.price;
  const risk = pk.risk_per_share;
  if (!entry || !risk) return { qty: 0 };
  let qty = Math.floor(riskAmt / risk);
  const cap = Math.floor((swingCap * st.maxPosPct) / 100 / entry);
  qty = Math.max(0, Math.min(qty, cap));
  // costly stocks: allow a single share if its risk is only a little above budget
  if (qty === 0 && cap >= 1 && risk <= riskAmt * 1.5) qty = 1;
  const gainPerShare = pk.target ? pk.target - entry : (entry * (pk.reward_pct || 0)) / 100;
  return { qty, amount: qty * entry, maxLoss: qty * risk, gain: qty * gainPerShare, halved: regime === 1 };
}

// Equal amount per stock; stocks priced above the per-stock budget are skipped
// and their share is spread over the rest (so expensive stocks never show "0").
function ltBudget() {
  const st = settings();
  const ltCap = (st.capital * st.ltPct) / 100;
  let picks = (S.latest?.long_term?.picks || []).slice();
  let per = ltCap / (picks.length || 1);
  for (let i = 0; i < 5; i++) {
    const ok = picks.filter((p) => p.price <= per);
    if (ok.length === picks.length || !ok.length) break;
    picks = ok;
    per = ltCap / ok.length;
  }
  return { per, affordable: new Set(picks.filter((p) => p.price <= per).map((p) => p.symbol)) };
}

function ltSize(pick) {
  const b = ltBudget();
  if (!b.affordable.has(pick.symbol)) return { per: b.per, qty: 0, amount: 0, skip: true };
  const qty = Math.floor(b.per / pick.price);
  return { per: b.per, qty, amount: qty * pick.price };
}

// ------------------------------------------------------------------ data loading
async function getJSON(name) {
  const r = await fetch(DATA + name + "?t=" + Date.now(), { cache: "no-store" });
  if (!r.ok) throw new Error(name + " " + r.status);
  return r.json();
}

// remember the highest price seen since buying (the long-term safety price follows it)
function updateHighs() {
  const my = myPositions();
  let changed = false;
  for (const p of my) {
    const now = livePrice(p.symbol)?.price ?? latestPrice(p.symbol);
    const high = p.high ?? p.price;
    if (now != null && now > high) { p.high = now; changed = true; }
    else if (p.high == null) { p.high = p.price; changed = true; }
  }
  if (changed) store.set(LS.my, my);
}

async function loadAll() {
  try {
    const [latest, live, news] = await Promise.allSettled([getJSON("latest.json"), getJSON("live.json"), getJSON("market_news.json")]);
    if (latest.status === "fulfilled") S.latest = latest.value;
    if (live.status === "fulfilled") S.live = live.value;
    if (news.status === "fulfilled") S.news = news.value;
  } catch (e) { console.warn(e); }
  updateHighs();
  renderAll();
  checkAlerts();
}

async function refreshLive() {
  if (S.refreshingLive) return;
  S.refreshingLive = true;
  renderLiveDashboard();
  try { S.live = await getJSON("live.json"); updateHighs(); renderAll(); checkAlerts(); } catch { /* keep the last good snapshot */ }
  finally { S.refreshingLive = false; renderLiveDashboard(); }
}

async function loadResults() {
  if (!S.bt) { try { S.bt = await getJSON("backtest.json"); } catch { S.bt = {}; } }
  if (!S.track) { try { S.track = await getJSON("track_record.json"); } catch { S.track = { trades: [] }; } }
  renderRecord();
}

// ------------------------------------------------------------------ status bar
function renderStatus() {
  const el = $("#status");
  const L = S.latest;
  if (!L) { el.textContent = "No data yet"; return; }
  const f = feedView();
  const active = f.key === "near_live" || f.key === "partial";
  const upd = S.live?.updated || L.generated;
  el.innerHTML = `<span class="dot ${active ? "live" : ""}"></span>${esc(f.label)} · ${esc(ago(upd))}`;
}

// ------------------------------------------------------------------ MARKET DETAILS (advanced view, under "More")
function renderMarket() {
  const el = $("#tab-market");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const m = L.market;
  const nq = livePrice("NIFTY");
  const nifty = nq ? nq.price : m.nifty;
  const nchg = nq && nq.prev_close ? (nq.price / nq.prev_close - 1) * 100 : m.nifty_change_pct;
  const buys = (L.swing || []).filter((x) => x.action === "BUY");
  const lt = L.long_term || {};
  let html = "";
  if (L.demo) html += `<div class="callout bad"><b>Demo data.</b> These numbers are synthetic, for testing only. Run the GitHub workflow to load real market data.</div>`;
  const stale = (Date.now() - new Date(L.generated).getTime()) / 36e5 > 80;
  if (stale) html += `<div class="callout warn">Data is more than 3 days old (last update ${esc(fmtDate(L.generated))}). Check that the GitHub Action is running.</div>`;

  html += `<div class="card regime r${m.regime}">
    <div class="row"><div><div class="muted small">Nifty 50</div>
      <div class="big">${num(nifty, 0)} <span class="${tone(nchg)}" style="font-size:18px">${pct(nchg, 2)}</span></div></div>
      <span class="badge ${m.regime === 2 ? "b-high" : m.regime === 1 ? "b-medium" : "b-red"}">Market: ${esc(m.regime_label)}</span></div>
    ${spark(m.nifty_spark)}
    <p style="margin:8px 0 0">${esc(m.regime_advice)}</p>
    ${m.ai_summary ? `<div class="callout info small">🤖 ${esc(m.ai_summary)}</div>` : ""}
    <div class="kv">
      <div><span>Stocks above 200-day avg</span><b>${pct(m.breadth_above_200dma, 0, false)}</b></div>
      <div><span>Advances / Declines</span><b>${m.advances} / ${m.declines}</b></div>
      <div><span>Nifty 1-year return</span><b class="${tone(m.nifty_ret_1y)}">${pct(m.nifty_ret_1y)}</b></div>
      <div><span>Nifty P/E</span><b>${m.nifty_pe ?? "–"}</b></div>
    </div></div>`;

  html += `<div class="card small">This page shows the market only. For what to buy, see <b>Suggestions</b>.
    <div class="btns"><button class="btn primary" data-tab="today">See today's suggestions</button></div></div>`;
  el.innerHTML = html;
}

// ------------------------------------------------------------------ LIVE INTELLIGENCE
// This screen adds intraday context to an existing plan. It never upgrades a
// WATCH idea to BUY: only the daily, backtested engine is allowed to do that.
function liveAsOf() {
  const stamps = Object.values(S.live?.quotes || {}).map((q) => q.asof).filter(Boolean)
    .sort((a, b) => new Date(b) - new Date(a));
  return stamps[0] || S.live?.updated || null;
}

function intradayFacts(q) {
  if (!q) return "";
  const vwapGap = q.vwap ? (q.price / q.vwap - 1) * 100 : null;
  return `<div class="live-metrics">
    <div><span>vs VWAP</span><b class="${tone(vwapGap)}">${pct(vwapGap, 2)}</b></div>
    <div><span>15-min move</span><b class="${tone(q.return_15m)}">${pct(q.return_15m, 2)}</b></div>
    <div><span>Session range</span><b>${q.range_position == null ? "–" : num(q.range_position, 0) + "%"}</b></div>
  </div>`;
}

function systemStrategyName(key) {
  return (S.latest?.strategies || []).find((s) => s.key === key)?.name || null;
}

function sameSwingStrategy(position, item) {
  if (position.kind !== "swing" || !item) return false;
  if (position.strategy && item.strategy) return position.strategy === item.strategy;
  const itemName = item.strategy_name || systemStrategyName(item.strategy);
  return !!(position.strategy_name && itemName && position.strategy_name === itemName);
}

function matchingSystemItem(position, items, peers = myPositions()) {
  if (position.kind !== "swing") return null;
  const candidates = items.filter((x) => x.symbol === position.symbol);
  if (position.planId) {
    const exactId = candidates.find((x) => x.id === position.planId);
    if (exactId) return exactId;
  }
  const exact = candidates.filter((x) => sameSwingStrategy(position, x));
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) {
    let boughtDay = null;
    try { if (position.date) boughtDay = indiaDateOf(position.date); } catch { /* invalid legacy date */ }
    const newPending = exact.find((x) => x.status === "pending" && boughtDay
      && x.signal_date && boughtDay > x.signal_date);
    return newPending || exact.find((x) => x.status === "selling")
      || exact.find((x) => x.status === "open") || exact[0];
  }
  const positionIdentified = !!(position.planId || position.strategy || position.strategy_name);
  const candidateIdentified = candidates.some((x) => x.strategy || x.strategy_name);
  // Never attach an identified system strategy to an unidentified legacy lot,
  // or one explicitly saved under a different strategy.
  if (candidateIdentified || positionIdentified) return null;
  const peerCount = peers.filter((p) => p.kind === "swing" && p.symbol === position.symbol).length;
  return candidates.length === 1 && peerCount === 1 ? candidates[0] : null;
}

function positionSystemContext(position, peers = myPositions()) {
  const plan = matchingSystemItem(position, S.latest?.open_trades || [], peers);
  const rawExit = matchingSystemItem(position, S.latest?.sell_today || [], peers);
  const exitDay = rawExit?.exit_signal_date || S.latest?.data_date;
  let boughtDay = null;
  try { if (position.date) boughtDay = indiaDateOf(position.date); } catch { /* invalid legacy date */ }
  const replacementLot = !!(rawExit && plan?.status === "pending"
    && ((position.planId && position.planId === plan.id)
      || (boughtDay && exitDay && boughtDay > exitDay)));
  return { plan, exit: replacementLot ? null : rawExit };
}

function tighterStop(localStop, systemStop) {
  const stops = [localStop, systemStop].map(Number).filter((x) => Number.isFinite(x) && x > 0);
  return stops.length ? Math.max(...stops) : null;
}

function liveMonitorItems() {
  const plans = S.latest?.open_trades || [];
  const exits = S.latest?.sell_today || [];
  const mine = myPositions();
  const usedPlans = new Set();
  const usedExits = new Set();
  const out = mine.map((p) => {
    const { plan: candidatePlan, exit } = positionSystemContext(p, mine);
    // A newly pending plan can coexist with yesterday's completed exit. Keep it as its own card.
    const t = exit && candidatePlan?.status !== "selling" ? null : candidatePlan;
    if (t) usedPlans.add(t);
    if (exit) usedExits.add(exit);
    const authoritativeExit = !!exit || t?.status === "selling";
    const systemStop = t?.status === "open" ? t.stop : null;
    return {
      key: p.id, symbol: p.symbol, name: p.name || t?.name || p.symbol,
      source: t || exit ? "My Stocks · System plan" : "My Stocks",
      status: authoritativeExit ? "selling" : t?.status || "open", authoritativeExit,
      entry: p.price, stop: tighterStop(p.stop, systemStop ?? t?.stop),
      target: t?.target ?? p.target, qty: p.qty, local: p,
      maxEntry: t?.max_entry, signalClose: t?.signal_close, initialStop: t?.initial_stop ?? t?.stop,
      signalDate: t?.signal_date, strategy: t?.strategy || exit?.strategy || p.strategy,
      sellReason: exit?.reason || t?.sell_reason,
    };
  });
  for (const t of plans) if (!usedPlans.has(t)) out.push({
    key: t.id, symbol: t.symbol, name: t.name || t.symbol, source: "System plan", status: t.status,
    entry: t.entry || t.signal_close, stop: t.stop, target: t.target, maxEntry: t.max_entry,
    signalClose: t.signal_close, initialStop: t.initial_stop ?? t.stop, signalDate: t.signal_date,
    strategy: t.strategy, authoritativeExit: t.status === "selling", sellReason: t.sell_reason,
  });
  for (const exit of exits) {
    if (usedExits.has(exit)) continue;
    const matchingSelling = plans.filter((t) => t.symbol === exit.symbol && t.status === "selling");
    const duplicate = matchingSelling.some((t) => exit.strategy && t.strategy === exit.strategy)
      || (!exit.strategy && matchingSelling.length === 1
        && exits.filter((x) => x.symbol === exit.symbol).length === 1);
    if (duplicate) continue;
    out.push({
      key: `exit-${exit.id || exit.symbol}-${exit.strategy || exit.reason || "plan"}`,
      symbol: exit.symbol, name: exit.name || exit.symbol, source: "System exit",
      status: "selling", authoritativeExit: true, entry: exit.entry, lastPrice: exit.price,
      strategy: exit.strategy, sellReason: exit.reason,
    });
  }
  return out;
}

function liveMonitorCard(x) {
  const q = liveQuote(x.symbol);
  const now = q?.price ?? x.lastPrice ?? x.entry;
  const chg = quoteChange(q);
  const fresh = quoteFresh(q);
  let displayStop = x.stop;
  let displayTarget = x.target;
  let state = { cls: "ok", label: x.status === "pending" ? "WAIT FOR PLAN" : "HOLD" };
  if (x.status === "selling") {
    const session = sessionInfo(S.latest?.data_date || "");
    const label = session.actedToday
      ? (session.phase === "during" ? "SELL NOW" : "SELL IF NOT DONE")
      : "SELL AT OPEN";
    state = { cls: "bad", label }; // authoritative daily instruction beats feed health
  } else if (x.status === "pending") {
    if ((q && !fresh) || (S.live?.market_open && !q)) state = { cls: "warn", label: "QUOTE DELAYED" };
    else if (q && q.open != null && (!x.signalDate || !q.date || q.date > x.signalDate)) {
      const invalidOpen = ((x.maxEntry != null && q.open > x.maxEntry)
        || (x.initialStop != null && q.open <= x.initialStop));
      if (invalidOpen) state = { cls: "warn", label: "SKIP TODAY" };
      else {
        const originalRisk = x.signalClose != null && x.initialStop != null ? x.signalClose - x.initialStop : null;
        if (originalRisk != null && originalRisk > 0) {
          displayStop = Math.round((q.open - originalRisk) * 100) / 100;
          if (x.target != null && x.signalClose != null)
            displayTarget = Math.round((q.open + x.target - x.signalClose) * 100) / 100;
        }
        if (displayStop != null && q.day_low != null && q.day_low <= displayStop)
          state = { cls: "bad", label: "STOP TOUCHED" };
        else state = { cls: "info", label: "WITHIN PLANNED LIMIT" };
      }
    }
  } else if ((q && !fresh) || (S.live?.market_open && !q)) {
    state = { cls: "warn", label: "QUOTE DELAYED" };
  } else if (q && displayStop && ((q.day_low != null && q.day_low <= displayStop) || q.price <= displayStop)) {
    state = { cls: "bad", label: "SELL / CHECK ORDER" };
  } else if (q && displayTarget && (q.day_high >= displayTarget || q.price >= displayTarget)) {
    state = { cls: "good", label: "TARGET TOUCHED" };
  }
  const stopGap = displayStop && now ? (now / displayStop - 1) * 100 : null;
  const targetGap = displayTarget && now ? (displayTarget / now - 1) * 100 : null;
  const pl = x.entry && now ? (now / x.entry - 1) * 100 : null;
  const plLabel = x.status === "pending" && !x.local ? "Vs signal close" : "Since entry";
  return `<article class="card live-quote ${state.cls}">
    <div class="row"><div><h3>${esc(x.name)}</h3><div class="muted small">${esc(x.symbol)} · ${esc(x.source)}</div></div>
      <span class="badge b-${state.cls === "bad" ? "red" : state.cls === "warn" ? "medium" : state.cls === "good" ? "high" : "watch"}">${esc(state.label)}</span></div>
    <div class="quote-price">${px(now)} <span class="${tone(chg)}">${pct(chg, 2)}</span></div>
    <div class="kv live-kv">
      <div><span>${plLabel}</span><b class="${tone(pl)}">${pct(pl, 2)}</b></div>
      <div><span>Safety buffer</span><b class="${stopGap != null && stopGap < 2 ? "down" : ""}">${stopGap == null ? "–" : pct(stopGap, 1, false)}</b></div>
      <div><span>Safety price</span><b>${px(displayStop)}</b></div>
      <div><span>To target</span><b>${targetGap == null ? "–" : pct(targetGap, 1, false)}</b></div>
    </div>${intradayFacts(q)}
    <div class="quote-asof">${x.sellReason ? `Exit reason: ${esc(x.sellReason)} · ` : ""}${q?.asof ? `Quote ${esc(fmtClock(q.asof))}` : "No intraday quote"}${q && !quoteFresh(q) ? " · delayed" : ""}</div>
  </article>`;
}

function factorChip(label, ok, detail = "") {
  const cls = ok === true ? "pass" : ok === false ? "fail" : "neutral";
  return `<span class="factor ${cls}">${ok === true ? "✓" : ok === false ? "!" : "·"} ${esc(label)}${detail ? ` ${esc(detail)}` : ""}</span>`;
}

function radarCard(pk) {
  const q = liveQuote(pk.symbol);
  const now = q?.price ?? pk.price;
  const fresh = quoteFresh(q);
  let state = { cls: "b-low", label: "WATCH ONLY" };
  if (pk.action === "BUY") {
    if ((q && !fresh) || (S.live?.market_open && !q)) state = { cls: "b-medium", label: "CHECK DELAYED QUOTE" };
    else if (q && q.open != null && q.date > (pk.signal_date || S.latest?.data_date || "")) {
      if (q.open > pk.max_entry || q.open <= pk.stop) state = { cls: "b-medium", label: "SKIP TODAY" };
      else {
        const dayStop = Math.round((q.open - (pk.risk_per_share ?? (pk.price - pk.stop))) * 100) / 100;
        const sessionLow = q.day_low ?? q.price;
        state = sessionLow <= dayStop
          ? { cls: "b-red", label: "STOP TOUCHED" }
          : { cls: "b-high", label: "OPEN WAS VALID" };
      }
    } else if (S.live?.market_open && q && (q.open == null || !q.date)) state = { cls: "b-medium", label: "CHECK QUOTE" };
    else state = { cls: "b-high", label: "DAILY PLAN VALID" };
  }
  const newsOk = !(pk.red_flags || []).length && (pk.news_sentiment ?? 0) > -0.3;
  const edgeOk = pk.strategy_enabled !== false && (pk.hist_avg_return ?? 0) > 0;
  const trendOk = pk.price == null || pk.sma200 == null ? null : pk.price > pk.sma200;
  const rsOk = pk.rs_percentile == null ? null : pk.rs_percentile >= 70;
  const volumeOk = pk.vol_ratio == null ? null : pk.vol_ratio >= 1.5;
  return `<article class="card radar-card">
    <div class="row"><div><h3>${esc(pk.name)}</h3><div class="muted small">${esc(pk.symbol)} · ${esc(pk.strategy_name)}</div></div>
      <span class="badge ${state.cls}">${esc(state.label)}</span></div>
    <div class="quote-price">${px(now)} <span class="${tone(quoteChange(q))}">${pct(quoteChange(q), 2)}</span></div>
    ${intradayFacts(q)}
    <div class="factors">
      ${factorChip("Backtested edge", edgeOk)}
      ${factorChip("Long trend", trendOk)}
      ${factorChip("Relative strength", rsOk, pk.rs_percentile == null ? "" : num(pk.rs_percentile, 0) + "%")}
      ${factorChip("Volume", volumeOk, pk.vol_ratio == null ? "" : num(pk.vol_ratio, 1) + "×")}
      ${factorChip("News", newsOk)}
    </div>
    <div class="quote-asof">Score ${num(pk.score, 0)}/100 · ${num(pk.hist_trades, 0)} historical trades · ${pct(pk.hist_win_rate, 0, false)} winners</div>
  </article>`;
}

function strategyHealthCard(s) {
  const all = s.stats || {};
  const recent = s.last_12m || {};
  const enough = (recent.trades || 0) >= 30;
  const healthy = enough && (recent.avg_return || 0) > 0 && (recent.profit_factor || 0) >= 1.1;
  const label = !enough ? "BUILDING EVIDENCE" : healthy ? "HEALTHY RECENTLY" : "WEAK RECENTLY";
  return `<article class="card strategy-health ${healthy ? "good" : enough ? "warn" : "info"}">
    <div class="row"><h3>${esc(s.name)}</h3><span class="badge ${healthy ? "b-high" : enough ? "b-medium" : "b-watch"}">${label}</span></div>
    <div class="kv live-kv"><div><span>5-year trades</span><b>${num(all.trades, 0)}</b></div>
      <div><span>5-year profit factor</span><b>${all.profit_factor ?? "–"}</b></div>
      <div><span>Recent win rate</span><b>${pct(recent.win_rate, 0, false)}</b></div>
      <div><span>Recent avg trade</span><b class="${tone(recent.avg_return)}">${pct(recent.avg_return, 2)}</b></div></div>
  </article>`;
}

function renderLiveDashboard() {
  const el = $("#tab-live");
  if (!el) return;
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const f = feedView();
  const live = S.live || {};
  const requested = live.requested ?? f.count;
  const received = live.received ?? f.count;
  const monitor = liveMonitorItems();
  const nq = liveQuote("NIFTY");
  const nprice = nq?.price ?? L.market?.nifty;
  const nchg = quoteChange(nq) ?? L.market?.nifty_change_pct;
  const radar = (L.swing || []).slice().sort((a, b) =>
    Number(b.action === "BUY") - Number(a.action === "BUY") || (b.score || 0) - (a.score || 0)).slice(0, 10);
  const health = (L.strategies || []).map(strategyHealthCard).join("");
  const quoteSummary = !S.live ? "Live feed has not loaded"
    : requested ? `${received}/${requested} requested quotes${f.coverage != null ? ` · ${num(f.coverage, 0)}% coverage` : ""}`
      : f.key === "closed" ? "Live quotes resume during market hours" : "No live quotes received";
  el.innerHTML = `<h2>Live Intelligence</h2>
    <div class="card feed-card ${f.cls}">
      <div class="row"><div><div class="feed-title"><span class="feed-led">${f.icon}</span> ${esc(f.label)}</div>
        <div class="muted small">As of ${esc(fmtClock(liveAsOf()))} · ${quoteSummary}</div></div>
        <button class="btn small" data-refresh-live ${S.refreshingLive ? "disabled" : ""}>${S.refreshingLive ? "Refreshing…" : "Refresh"}</button></div>
      <p class="small">${esc(live.data_delay_note || "Free market data is near-live and may be delayed. Confirm every order and price in your broker app.")}</p>
    </div>
    <div class="card regime r${L.market?.regime ?? 1}"><div class="row"><div><div class="muted small">Nifty 50</div>
      <div class="big">${num(nprice, 0)} <span class="${tone(nchg)} live-change">${pct(nchg, 2)}</span></div></div>
      <span class="badge ${(L.market?.regime ?? 1) === 2 ? "b-high" : (L.market?.regime ?? 1) === 1 ? "b-medium" : "b-red"}">${esc(L.market?.regime_label || "Unknown")}</span></div>
      ${intradayFacts(nq)}
      <div class="kv live-kv"><div><span>Above 200-day average</span><b>${pct(L.market?.breadth_above_200dma, 0, false)}</b></div>
        <div><span>Advances / declines</span><b>${L.market?.advances ?? "–"} / ${L.market?.declines ?? "–"}</b></div></div></div>
    <div class="callout info small"><b>How to use this:</b> live data monitors an existing plan, entry limit, stop and target. It does not turn a watch-only stock into a buy. "No trade" is a valid outcome.</div>
    <h2>Live risk monitor</h2>
    ${monitor.length ? `<div class="grid live-grid">${monitor.map(liveMonitorCard).join("")}</div>` : `<div class="card empty">No open or recorded positions yet. Add a purchase from Suggestions or My Stocks to monitor it here.</div>`}
    <h2>Signal radar</h2><p class="muted small">Daily backtested ideas with intraday confirmation. Only green plan cards were selected as BUY by the daily engine. A valid opening does not mean you should chase a late entry.</p>
    <div class="grid live-grid">${radar.length ? radar.map(radarCard).join("") : `<div class="card empty">No current swing setups.</div>`}</div>
    <h2>Strategy health</h2><p class="muted small">Recent results are shown separately from the full backtest as a health check. This label does not override the engine's validated BUY/WATCH decision above.</p>
    <div class="grid live-grid">${health || `<div class="card empty">Strategy results are not available yet.</div>`}</div>`;
}

function trackMini() {
  const t = S.latest.track_summary || {};
  if (!t.closed && !t.open) return `<h2>Live track record</h2><div class="card muted small">The system records every idea it gives and checks the real result. The record starts with the first live run, so check back after a few weeks.</div>`;
  return `<h2>Live track record</h2><div class="card"><div class="kv">
    <div><span>Closed trades</span><b>${t.closed || 0}</b></div>
    <div><span>Win rate</span><b>${pct(t.win_rate, 0, false)}</b></div>
    <div><span>Avg gain per trade</span><b class="${tone(t.avg_return)}">${pct(t.avg_return, 2)}</b></div>
    <div><span>Open now</span><b>${t.open || 0}</b></div></div>
    <button class="btn small" data-goto="record">See all results</button></div>`;
}

// ------------------------------------------------------------------ alerts
function computeAlerts() {
  const out = [];
  // the plan's trades: SELL NOW when a live price reaches the safety price
  for (const d of liveDangers()) {
    out.push({ level: "bad", key: `plan-${d.x.symbol}-stop`, text: `SELL NOW: all your ${d.x.symbol} shares. It fell to the safety price (${px(d.stop)}), now ${px(d.q.price)}.` });
  }
  // same plain-language rules as the My Stocks screen
  for (const p of myPositions()) {
    const s = positionStatus(p);
    if (s.head === "HOLD") continue;
    out.push({ level: s.cls === "ok" ? "info" : s.cls, key: `${p.id}-${s.head}`, text: `${s.head}: ${p.symbol}. ${s.text}` });
  }
  for (const t of S.latest?.open_trades || []) {
    if (t.status === "pending") out.push({ level: "info", key: `${t.id}-pending`, text: `New idea: ${t.symbol}, buy at the next open if the price is ≤ ${px(t.max_entry)}.` });
  }
  return out;
}

function alertsBlock() {
  const a = computeAlerts().filter((x) => x.level !== "info");
  if (!a.length) return "";
  return `<h2>Alerts</h2>` + a.map((x) => `<div class="callout ${x.level}">${esc(x.text)}</div>`).join("");
}

async function notify(title, body, tag) {
  const st = settings();
  if (!st.notify || !("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const reg = await navigator.serviceWorker?.getRegistration();
    if (reg) reg.showNotification(title, { body, tag, icon: "icon-192.png", badge: "icon-192.png" });
    else new Notification(title, { body, tag });
  } catch { /* ignore */ }
}

function checkAlerts() {
  if (!S.latest) return;
  const today = new Date().toISOString().slice(0, 10);
  const alerted = store.get(LS.alerted, {});
  for (const a of computeAlerts()) {
    if (a.level === "info") continue;
    const k = `${a.key}-${today}`;
    if (!alerted[k]) { alerted[k] = 1; notify("Stock Finder alert", a.text, a.key); }
  }
  const ideas = S.latest.actionable || [];
  const seen = store.get(LS.seen, []);
  const fresh = ideas.filter((i) => !seen.includes(i + S.latest.data_date));
  if (fresh.length) {
    notify("New stock ideas", `${fresh.length} new swing idea(s): ${fresh.map((x) => x.split(":")[0]).join(", ")}`, "ideas");
    store.set(LS.seen, [...seen, ...fresh.map((i) => i + S.latest.data_date)].slice(-300));
  }
  // prune old keys
  const keys = Object.keys(alerted);
  if (keys.length > 400) keys.slice(0, keys.length - 400).forEach((k) => delete alerted[k]);
  store.set(LS.alerted, alerted);
}

// ------------------------------------------------------------------ my positions
function myPositionsBlock() {
  const my = myPositions();
  if (!my.length) return "";
  const rows = my.map((p) => {
    const q = livePrice(p.symbol);
    const last = q ? q.price : p.lastPrice;
    const pl = last ? (last / p.price - 1) * 100 : null;
    return `<tr><td><b>${esc(p.symbol)}</b><div class="muted small">${esc(p.kind === "long_term" ? "Long-term" : p.strategy_name || "Swing")} · ${esc(fmtDate(p.date))}</div></td>
      <td class="num">${num(p.qty, 0)} @ ${px(p.price)}</td>
      <td class="num">${last ? px(last) : "–"}<div class="${tone(pl)} small">${pct(pl)}</div></td>
      <td class="num small">${p.stop ? "SL " + px(p.stop) : ""}${p.target ? "<br>T " + px(p.target) : ""}</td>
      <td><button class="btn small" data-sell="${esc(p.id)}" aria-label="Remove ${esc(p.symbol)}">✕</button></td></tr>`;
  }).join("");
  return `<h2>My positions</h2><div class="card table-wrap"><table>
    <thead><tr><th>Stock</th><th class="num">Qty @ buy</th><th class="num">Now</th><th class="num">Exit plan</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="muted small">Prices update every ~15 min during market hours. Tap ✕ after you sell.</p></div>`;
}

function openBuyDialog(pk) {
  const sz = pk.kind === "long_term" ? ltSize(pk) : swingSize(pk);
  const q = livePrice(pk.symbol);
  $("#dlg-body").innerHTML = `<h3>Record purchase: ${esc(pk.symbol)}</h3>
    <p class="muted small">Saved only on this device, so the app can track it and alert you.</p>
    <label class="field">Quantity<input id="bq" type="number" inputmode="numeric" min="1" value="${sz.qty || 1}"></label>
    <label class="field">Buy price (₹)<input id="bp" type="number" inputmode="decimal" step="0.05" value="${(q ? q.price : pk.price) || ""}"></label>
    <label class="field">Stop-loss (₹)<input id="bs" type="number" inputmode="decimal" step="0.05" value="${pk.stop || pk.review_below || ""}"></label>
    <label class="field">Target (₹) <span class="hint">optional</span><input id="bt" type="number" inputmode="decimal" step="0.05" value="${pk.target || ""}"></label>
    <div class="btns"><button class="btn primary" id="bsave">Save</button></div>`;
  const dlg = $("#dlg");
  dlg.showModal();
  $("#bsave").onclick = (e) => {
    e.preventDefault();
    const my = myPositions();
    const matchingPlans = (S.latest?.open_trades || []).filter((t) =>
      t.symbol === pk.symbol && (!pk.strategy || t.strategy === pk.strategy));
    const plan = matchingPlans.find((t) => t.status === "pending")
      || matchingPlans.find((t) => t.status === "open") || matchingPlans[0];
    my.push({
      id: pk.symbol + "-" + Date.now(), symbol: pk.symbol, kind: pk.kind,
      strategy: pk.strategy, strategy_name: pk.strategy_name, planId: plan?.id,
      qty: +$("#bq").value, price: +$("#bp").value, stop: +$("#bs").value || null, target: +$("#bt").value || null,
      date: new Date().toISOString(), lastPrice: pk.price,
    });
    store.set(LS.my, my);
    dlg.close();
    renderAll();
  };
}

// ------------------------------------------------------------------ SWING
function swingCard(pk) {
  const sz = swingSize(pk);
  const q = livePrice(pk.symbol);
  const now = q ? q.price : pk.price;
  const chg = q && q.prev_close ? (q.price / q.prev_close - 1) * 100 : null;
  const conf = (pk.confidence || "Low").toLowerCase();
  const exitLine = pk.target
    ? `<div><span>Target</span><b class="up">${px(pk.target)} <small>(${pct((pk.target / pk.price - 1) * 100)})</small></b></div>`
    : `<div><span>Exit when it closes above</span><b>${px(pk.exit_hint)} <small class="muted">(5-day avg)</small></b></div>`;
  const flags = (pk.red_flags || []).length ? `<div class="callout bad small">⚠ Negative news found: ${pk.red_flags.map(esc).join(", ")}. Avoid until clear.</div>` : "";
  const warns = (pk.warnings || []).length ? `<div class="callout warn small">${pk.warnings.map(esc).join(" · ")}</div>` : "";
  const disabled = pk.strategy_enabled === false ? `<div class="callout warn small">This setup has not been profitable in recent backtests, so it is shown for information only.</div>` : "";
  const news = (pk.news || []).length ? `<details><summary>Latest news (${pk.news.length})</summary><ul class="news">${pk.news.map(newsItem).join("")}</ul></details>` : "";
  return `<article class="card">
    <div class="row"><div><h3>${esc(pk.symbol)}</h3><div class="muted small">${esc(pk.name)}</div></div>
      <div><span class="badge ${pk.action === "BUY" ? "b-buy" : "b-watch"}">${pk.action === "BUY" ? "BUY" : "WATCH"}</span>
      <span class="badge b-${conf}">${esc(pk.confidence)} · ${pk.score}</span></div></div>
    <div class="row" style="margin-top:6px"><span class="muted small">${esc(pk.strategy_name)}</span>
      <span><b>${px(now)}</b> ${chg != null ? `<span class="${tone(chg)} small">${pct(chg, 2)}</span>` : ""}</span></div>
    ${spark(pk.spark)}
    <div class="kv">
      <div><span>Buy up to (next open)</span><b>${px(pk.max_entry)}</b></div>
      <div><span>Stop-loss</span><b class="down">${px(pk.stop)} <small>(${pct(-pk.risk_pct)})</small></b></div>
      ${exitLine}
      <div><span>Hold for up to</span><b>${pk.max_hold_days} trading days</b></div>
      <div><span>Quantity for you</span><b>${sz.qty ? num(sz.qty, 0) + " shares" : "–"}</b></div>
      <div><span>Amount</span><b>${inr(sz.amount)}</b></div>
      <div><span>Max loss if stopped</span><b class="down">${inr(sz.maxLoss)}</b></div>
      <div><span>Past win rate of setup</span><b>${pct(pk.hist_win_rate, 0, false)} <small class="muted">(${num(pk.hist_trades, 0)} trades)</small></b></div>
    </div>
    ${sz.halved ? `<div class="callout warn small">Half size because the market is cautious.</div>` : ""}
    ${flags}${disabled}${warns}
    ${pk.ai_note ? `<div class="callout info small">🤖 ${esc(pk.ai_note)}</div>` : ""}
    <p class="small" style="margin:8px 0">${esc(pk.why)}</p>
    <details><summary>Details & exit plan</summary>
      <p class="small">${esc(pk.how_to_exit)}</p>
      <div class="kv small">
        <div><span>6-month return</span><b class="${tone(pk.ret_6m)}">${pct(pk.ret_6m)}</b></div>
        <div><span>Strength vs market</span><b>Top ${num(100 - (pk.rs_percentile || 0), 0)}%</b></div>
        <div><span>RSI (14)</span><b>${num(pk.rsi14, 0)}</b></div>
        <div><span>Volume vs average</span><b>${num(pk.vol_ratio, 1)}x</b></div>
        <div><span>Delivery %</span><b>${pk.delivery_pct != null ? pct(pk.delivery_pct, 0, false) : "–"}</b></div>
        <div><span>Avg past trade</span><b class="${tone(pk.hist_avg_return)}">${pct(pk.hist_avg_return, 2)} in ~${num(pk.hist_avg_days, 0)}d</b></div>
      </div>
    </details>
    ${news}
    <div class="btns"><button class="btn small" data-buy="${esc(pk.symbol)}|${esc(pk.strategy)}">I bought this</button></div>
  </article>`;
}

function newsItem(n) {
  const s = n.sentiment > 0.2 ? "🟢" : n.sentiment < -0.2 ? "🔴" : "⚪";
  return `<li>${s} <a href="${safeUrl(n.link)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>
    <div class="meta">${esc(n.source || "")} ${n.published ? "· " + esc(ago(n.published)) : ""}</div></li>`;
}

function renderSwing() {
  const el = $("#tab-swing");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const all = L.swing || [];
  const list = S.swingFilter === "buy" ? all.filter((x) => x.action === "BUY") : all;
  const st = settings();
  el.innerHTML = `<h2>Swing trades <span class="muted small">(days to a few weeks)</span></h2>
    <div class="callout info small">Your swing money: <b>${inr((st.capital * st.swingPct) / 100)}</b>. Each trade risks at most <b>${st.riskPct}%</b> of it
      (${inr((st.capital * st.swingPct * st.riskPct) / 10000)}) if the stop-loss is hit. Change this in ⚙ Settings.</div>
    <div class="pill-row"><button data-filter="buy" class="${S.swingFilter === "buy" ? "on" : ""}">Buy ideas (${all.filter((x) => x.action === "BUY").length})</button>
      <button data-filter="all" class="${S.swingFilter === "all" ? "on" : ""}">All signals (${all.length})</button></div>
    ${list.length ? `<div class="grid">${list.map(swingCard).join("")}</div>` : `<div class="card empty">No ideas in this list today.</div>`}
    <h2>How these work</h2>
    ${(L.strategies || []).map((s) => `<div class="card"><h3>${esc(s.name)}</h3><p class="small">${esc(s.why)}</p><p class="small muted">${esc(s.exit)}</p>
      <p class="small">Backtest: <b>${pct(s.stats?.win_rate, 0, false)}</b> winners, average <b class="${tone(s.stats?.avg_return)}">${pct(s.stats?.avg_return, 2)}</b> per trade after costs, over ${num(s.stats?.trades, 0)} trades.</p></div>`).join("")}`;
}

// ------------------------------------------------------------------ LONG-TERM
function renderLongTerm() {
  const el = $("#tab-longterm");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const lt = L.long_term || {};
  const picks = lt.picks || [];
  const st = settings();
  const ltCap = (st.capital * st.ltPct) / 100;
  let total = 0;
  const rows = picks.map((p) => {
    const sz = ltSize(p);
    total += sz.amount;
    const q = livePrice(p.symbol);
    const flags = [p.is_new ? `<span class="badge b-new">NEW</span>` : "", (p.red_flags || []).length ? `<span class="badge b-red">News ⚠</span>` : "",
      ...(p.warnings || []).map((w) => `<span class="badge b-medium">${esc(w)}</span>`)].join(" ");
    return `<tr><td>${p.rank}</td><td><b>${esc(p.symbol)}</b><div class="muted small">${esc(p.industry)}</div>${flags}</td>
      <td class="num">${px(q ? q.price : p.price)}</td><td class="num"><b>${sz.skip ? "–" : num(sz.qty, 0)}</b></td><td class="num">${sz.skip ? `<span class="small muted">Too costly for your amount</span>` : inr(sz.amount)}</td>
      <td class="num ${tone(p.ret_12m)}">${pct(p.ret_12m, 0)}</td>
      <td><button class="btn small" data-ltinfo="${esc(p.symbol)}" aria-label="Details for ${esc(p.symbol)}">ⓘ</button></td></tr>`;
  }).join("");
  const perf = lt.live_performance || [];
  const ch = lt.changes || {};
  el.innerHTML = `<h2>Long-term core portfolio</h2>
    <div class="card">
      <p>The ${picks.length} strongest large and mid-cap companies by <b>steady price momentum</b> (12 and 6-month returns, adjusted for volatility),
      in an uptrend, filtered for debt and losses, max 4 per industry. This is the same idea behind NSE's Nifty200 Momentum 30 index.</p>
      <ol class="stepper small">
        <li>Put an equal amount in each stock (quantities below use your long-term money: <b>${inr(ltCap)}</b>).</li>
        <li>${esc(lt.deployment || "")}</li>
        <li>Review once a month (next: <b>${esc(fmtDate(lt.next_rebalance))}</b>). Sell stocks that drop out, buy the new ones.</li>
        <li>Sell any stock early if it closes more than 15% below its 200-day average (shown in ⓘ).</li>
      </ol>
    </div>
    ${ch.added?.length && ch.removed?.length ? `<div class="callout info">This month: <b>add</b> ${ch.added.map(esc).join(", ")} · <b>sell</b> ${ch.removed.map(esc).join(", ")}</div>` : ""}
    <div class="card table-wrap"><table>
      <thead><tr><th>#</th><th>Stock</th><th class="num">Price</th><th class="num">Qty</th><th class="num">Amount</th><th class="num">1Y</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="empty">No portfolio yet.</td></tr>`}</tbody>
      <tfoot><tr><td></td><td><b>Total</b></td><td></td><td></td><td class="num"><b>${inr(total)}</b></td><td></td><td></td></tr></tfoot>
    </table></div>
    ${perf.length ? `<h2>How past monthly picks did (live)</h2><div class="card table-wrap"><table><thead><tr><th>Portfolio of</th><th class="num">Return since</th><th class="num">Nifty since</th></tr></thead>
      <tbody>${perf.slice(-12).reverse().map((r) => `<tr><td>${esc(fmtDate(r.date))}</td><td class="num ${tone(r.return_pct)}">${pct(r.return_pct)}</td><td class="num ${tone(r.nifty_pct)}">${pct(r.nifty_pct)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}

function openLtInfo(sym) {
  const p = (S.latest.long_term.picks || []).find((x) => x.symbol === sym);
  if (!p) return;
  const f = p.fundamentals || {};
  $("#dlg-body").innerHTML = `<h3>${esc(p.symbol)} <span class="muted small">${esc(p.name)}</span></h3>
    ${spark(p.spark, 60)}
    <div class="kv">
      <div><span>Price</span><b>${px(p.price)}</b></div><div><span>From 52-week high</span><b>${pct(p.pct_from_high)}</b></div>
      <div><span>6-month</span><b class="${tone(p.ret_6m)}">${pct(p.ret_6m)}</b></div><div><span>12-month</span><b class="${tone(p.ret_12m)}">${pct(p.ret_12m)}</b></div>
      <div><span>Volatility (yearly)</span><b>${pct(p.volatility, 0, false)}</b></div><div><span>Review if below</span><b class="down">${px(p.review_below)}</b></div>
      <div><span>ROE</span><b>${f.roe != null ? pct(f.roe, 0, false) : "–"}</b></div><div><span>P/E</span><b>${num(f.pe, 1)}</b></div>
      <div><span>Debt / Equity</span><b>${f.debt_to_equity != null ? num(f.debt_to_equity / 100, 2) + "x" : "–"}</b></div><div><span>Profit growth</span><b>${pct(f.earnings_growth, 0)}</b></div>
    </div>
    ${p.ai_note ? `<div class="callout info small">🤖 ${esc(p.ai_note)}</div>` : ""}
    ${(p.warnings || []).length ? `<div class="callout warn small">${p.warnings.map(esc).join(" · ")}</div>` : ""}
    ${(p.news || []).length ? `<h3 style="margin-top:12px">News</h3><ul class="news">${p.news.map(newsItem).join("")}</ul>` : ""}
    <div class="btns"><button class="btn small" data-buy="${esc(p.symbol)}|lt">I bought this</button></div>`;
  $("#dlg").showModal();
}

// ------------------------------------------------------------------ RESULTS
const working = (s) => s.trades >= 30 && s.avg_return > 0 && s.profit_factor >= 1.1; // same rule as the engine

function rupeePicture(pf) {
  const tot = pf.total_profit;
  const yrs = (pf.yearly || []).map((y) => `<span class="chip ${tone(y.profit)}">${y.year}: ${y.profit >= 0 ? "+" : "−"}${inr(Math.abs(y.profit))}</span>`).join(" ");
  return `<div class="callout info small" style="margin-top:8px">
    <b>With ${inr(pf.money)}, ${pf.slots === 1 ? "one trade at a time" : pf.slots + " trades at a time"}, over the last ${pf.years} years:</b>
    ${num(pf.trades, 0)} trades (about ${num(pf.trades_per_year, 0)} a year), ${outOf10(pf.win_rate)} out of 10 made money.
    Total <b class="${tone(tot)}">${tot >= 0 ? "+" : "−"}${inr(Math.abs(tot))}</b> (about ${inr(pf.per_year_profit)} a year, after all charges).
    The worst dip along the way was about <b class="down">${inr(Math.abs(pf.worst_drop_rs))}</b> (${pct(pf.worst_drop_pct)}).
    <div style="margin-top:6px">${yrs}</div>
    <div class="muted" style="margin-top:6px">The money was in a trade only about ${pf.invested_pct_of_time}% of the time.</div></div>`;
}

function renderRecord() {
  const el = $("#tab-record");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const t = L.track_summary || {};
  const bt = S.bt || {};
  const trades = (S.track?.trades || []).slice().reverse();
  const closed = trades.filter((x) => ["target", "stop", "exit_rule", "time"].includes(x.status));
  const statusTxt = { target: "🎯 Target", stop: "🛑 Stop", exit_rule: "💰 Exit", time: "⏱ Time", open: "🔵 Open", pending: "⏳ Pending", expired: "⏭ Skipped" };
  // plain-language summary first; the detailed tables follow below
  const safeInfo = L.safe?.enabled ? Object.entries(L.safe.methods || {}) : [];
  const safeKeys = new Set(safeInfo.map(([k]) => k));
  const safeLines = safeInfo.map(([k, m]) => `<li><b>${esc(m.name || k)}</b>, the only method used for the daily plan:
      made money <b>${outOf10(m.stats?.win_rate) ?? "–"} out of 10</b> times in ${num(m.stats?.trades, 0)} past trades,
      averaging <span class="${tone(m.stats?.avg_return)}">${pct(m.stats?.avg_return, 2)}</span> per trade after costs.
      ${m.ok ? "✅ Passes every safety check." : `⏸ Paused: ${esc(m.why)}.`}
      ${m.portfolio && m.portfolio.trades ? rupeePicture(m.portfolio) : ""}</li>`).join("");
  const simple = Object.entries(bt.swing || {}).filter(([k, v]) => v.trades && !safeKeys.has(k)).map(([k, v]) => {
    const n = outOf10(v.win_rate);
    const name = (SIMPLE[k] || {}).title || v.name || k;
    return `<li><b>${esc(name)}:</b> made money <b>${n} out of 10</b> times. ${working(v)
      ? `On average each trade <span class="up">gained ${pct(v.avg_return, 1, false)}</span> after costs.`
      : `<span class="down">Not profitable enough, so the app doesn't use it right now.</span>`}</li>`;
  }).join("");
  let html = `<div class="card"><h2 style="margin-top:0">In simple words</h2>
    ${t.closed ? `<p>Since ${esc(fmtDate(t.since))}, the app's real ideas made money <b>${outOf10(t.win_rate)} out of 10</b> times, averaging <b class="${tone(t.avg_return)}">${pct(t.avg_return)}</b> per trade.</p>`
      : `<p>The app has just started recording its real ideas. Check back after a few weeks to see real results.</p>`}
    ${safeLines ? `<p>Tested on the past 5 years of prices:</p><ul class="rules">${safeLines}</ul>
      <details><summary>Other methods (not used in safe mode)</summary><ul class="rules small">${simple}</ul></details>`
      : simple ? `<p>Tested on the past 5 years of prices:</p><ul class="rules">${simple}</ul>` : ""}
    <p class="small muted">No method wins every time. The goal is small losses and bigger or more frequent wins.</p></div>
    <h2>Live track record</h2>
    <div class="card"><p class="small muted">Every BUY idea is recorded the day it's published and followed to its real exit. No hindsight, no editing.${t.since ? " Tracking since " + esc(fmtDate(t.since)) + "." : ""}</p>
    <div class="kv"><div><span>Closed trades</span><b>${t.closed || 0}</b></div><div><span>Win rate</span><b>${pct(t.win_rate, 0, false)}</b></div>
    <div><span>Average per trade</span><b class="${tone(t.avg_return)}">${pct(t.avg_return, 2)}</b></div><div><span>Best / worst</span><b>${pct(t.best)} / ${pct(t.worst)}</b></div></div></div>`;
  if (trades.length) {
    html += `<div class="card table-wrap"><table><thead><tr><th>Stock</th><th>Status</th><th class="num">Entry → Exit</th><th class="num">Result</th></tr></thead><tbody>
      ${trades.slice(0, 60).map((x) => `<tr><td><b>${esc(x.symbol)}</b><div class="muted small">${esc(x.signal_date)}</div></td><td class="small">${statusTxt[x.status] || esc(x.status)}</td>
      <td class="num small">${x.status === "pending" ? "buy ≤ " + px(x.max_entry) : x.status === "expired" ? "–" : px(x.entry) + " → " + (x.exit ? px(x.exit) : px(x.last_price))}</td><td class="num ${tone(x.ret_pct)}">${x.ret_pct != null ? pct(x.ret_pct) : "–"}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  if (!S.bt) { html += `<div class="card empty">Loading backtests…</div>`; el.innerHTML = html; return; }
  const sw = bt.swing || {};
  html += `<h2>Backtest: swing setups</h2>
    <p class="small muted">Simulated on ${esc(bt.data_from || "")} → ${esc(bt.data_to || "")} data, after ${num(bt.cost_round_trip_pct, 2)}% round-trip costs.
    Profit factor = total gains ÷ total losses: above 1.0 made money, above 1.3 is good.</p>
    <div class="grid">${Object.entries(sw).map(([k, s]) => `<div class="card"><div class="row"><h3>${esc(s.name || k)}</h3>
      <span class="badge ${working(s) ? "b-high" : "b-red"}">${working(s) ? "Working" : "Weak - not used"}</span></div>
      <div class="kv">
        <div><span>Trades tested</span><b>${num(s.trades, 0)}</b></div>
        <div><span>Winners</span><b>${pct(s.win_rate, 0, false)}</b></div>
        <div><span>Average per trade</span><b class="${tone(s.avg_return)}">${pct(s.avg_return, 2)}</b></div>
        <div><span>Profit factor</span><b>${s.profit_factor ?? "–"}</b></div>
        <div><span>Avg win / avg loss</span><b><span class="up">${pct(s.avg_win)}</span> / <span class="down">${pct(s.avg_loss)}</span></b></div>
        <div><span>Typical hold</span><b>${num(s.avg_days, 0)} days</b></div>
        <div><span>Last 12 months</span><b>${pct(s.last_12m?.win_rate, 0, false)} win · <span class="${tone(s.last_12m?.avg_return)}">${pct(s.last_12m?.avg_return, 2)}</span></b></div>
        <div><span>Worst losing streak</span><b>${s.max_losing_streak ?? "–"} in a row</b></div>
      </div></div>`).join("")}</div>`;
  for (const [k, s] of Object.entries(sw)) {
    if (!s.yearly) continue;
    html += `<details class="card"><summary>${esc(s.name || k)}: year by year</summary><table><thead><tr><th>Year</th><th class="num">Trades</th><th class="num">Win %</th><th class="num">Avg</th></tr></thead><tbody>
      ${s.yearly.map((y) => `<tr><td>${y.year}</td><td class="num">${y.trades}</td><td class="num">${pct(y.win_rate, 0, false)}</td><td class="num ${tone(y.avg_return)}">${pct(y.avg_return, 2)}</td></tr>`).join("")}</tbody></table></details>`;
  }
  const lt = bt.long_term || {};
  const g = bt.long_term_guarded || {};
  const ap = bt.long_term_app || {};
  if (ap.curve) {
    html += `<h2>Test: the app's long-term suggestions</h2><div class="card">
      <p class="small">About a third in the Nifty 50 index fund, the rest in 3 strong Nifty 50 companies from different sectors, checked monthly.
        Everything is sold while the whole market is falling. Over the last ${ap.years} years.
        (Only the price checks can be tested on the past. The business and news checks are added live.)</p>
      ${lineChart(ap.curve)}
      <div class="table-wrap"><table><thead><tr><th></th><th class="num">Per year</th><th class="num">Worst fall</th><th class="num">Months beating Nifty</th></tr></thead><tbody>
      <tr><td><b>App's suggestions</b></td><td class="num ${tone(ap.cagr)}">${pct(ap.cagr)}</td><td class="num down">${pct(ap.max_drawdown)}</td><td class="num">${pct(ap.pct_months_beat_nifty, 0, false)}</td></tr>
      <tr><td>Nifty 50</td><td class="num">${pct(ap.bench_cagr)}</td><td class="num down">${pct(ap.bench_max_drawdown)}</td><td class="num">–</td></tr>
      </tbody></table></div></div>`;
  }
  if (lt.curve) {
    html += `<h2>Older method (for comparison)</h2><div class="card"><p class="small muted">15 stocks from the top 200 companies, including mid-size ones. Not what the app suggests now.</p>${lineChart(lt.curve)}
      <div class="table-wrap"><table><thead><tr><th></th><th class="num">Yearly return</th><th class="num">Worst fall</th><th class="num">Months beating Nifty</th></tr></thead><tbody>
      <tr><td>Older 15-stock list</td><td class="num up">${pct(lt.cagr)}</td><td class="num down">${pct(lt.max_drawdown)}</td><td class="num">${pct(lt.pct_months_beat_nifty, 0, false)}</td></tr>
      <tr><td>With trend guard <span class="muted small">(50% cash when Nifty &lt; 200-day avg)</span></td><td class="num">${pct(g.cagr)}</td><td class="num down">${pct(g.max_drawdown)}</td><td class="num">${pct(g.pct_months_beat_nifty, 0, false)}</td></tr>
      <tr><td>Nifty 50</td><td class="num">${pct(lt.bench_cagr)}</td><td class="num down">${pct(lt.bench_max_drawdown)}</td><td class="num">–</td></tr>
      </tbody></table></div></div>`;
  }
  html += `<h2>Read this</h2><div class="card small"><ul>${(bt.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}
    <li>A win rate above 50% does not guarantee profit. What matters is win rate × average win vs loss rate × average loss. Always use the stop-loss.</li>
    <li>Expect losing streaks (see "worst streak" above). Keep each trade small so no single loss hurts.</li></ul></div>`;
  el.innerHTML = html;
}

// ------------------------------------------------------------------ NEWS
function renderNews() {
  const el = $("#tab-marketnews");
  const items = S.news?.items || [];
  el.innerHTML = `<h2>Market news</h2>
    ${items.length ? `<div class="card"><ul class="news">${items.map(newsItem).join("")}</ul></div>` : `<div class="card empty">No news loaded.</div>`}
    <p class="small muted">🟢 positive · 🔴 negative · ⚪ neutral (automatic word-based reading, can be wrong). News for each recommended stock is inside its card.</p>`;
}

// ------------------------------------------------------------------ SETTINGS
function renderSettings() {
  const el = $("#tab-settings");
  const st = settings();
  const ch = S.latest?.alert_channels || [];
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  el.innerHTML = `<h2>My money settings</h2>
    <div class="card">
      <label class="field">Money for trading (₹)<span class="hint">Used for the daily plan: ${slotsText()}. Suggested: ₹20,000. Below ₹15,000, fixed broker charges take most of the profit.</span>
        <input id="s-money" type="number" inputmode="numeric" min="2000" step="1000" value="${st.tradeMoney}"></label>
      <label class="field">Protect my money (recommended)<span class="hint">Keeps most of it in an FD / liquid fund so that a bad year in shares is covered by interest.</span>
        <select id="s-protect"><option value="1" ${st.protect ? "selected" : ""}>Yes</option><option value="0" ${st.protect ? "" : "selected"}>No, use all of it for shares</option></select></label>
      <div class="btns"><button class="btn primary" id="s-money-save">Save</button></div>
    </div>
    <h3 style="margin:18px 0 8px">Other settings (long-term investing and detailed screens)</h3>
    <div class="card">
      <label class="field">Total investable capital (₹)<span class="hint">Money you can keep invested for years. Keep your emergency fund separate.</span>
        <input id="s-cap" type="number" inputmode="numeric" min="10000" step="10000" value="${st.capital}"></label>
      <label class="field">Long-term portfolio share (%)<span class="hint">Recommended 60-80% for retirees.</span>
        <input id="s-lt" type="number" inputmode="numeric" min="0" max="100" value="${st.ltPct}"></label>
      <label class="field">Swing trading share (%)<span class="hint">Recommended at most 20-30%.</span>
        <input id="s-sw" type="number" inputmode="numeric" min="0" max="100" value="${st.swingPct}"></label>
      <label class="field">Risk per swing trade (% of swing money)<span class="hint">1% is a professional standard. 0.5% is extra safe.</span>
        <select id="s-risk">${[0.5, 0.75, 1, 1.5, 2].map((v) => `<option ${v == st.riskPct ? "selected" : ""} value="${v}">${v}%</option>`).join("")}</select></label>
      <label class="field">Max in one swing trade (% of swing money)
        <select id="s-maxp">${[10, 15, 20, 25, 33].map((v) => `<option ${v == st.maxPosPct ? "selected" : ""} value="${v}">${v}%</option>`).join("")}</select></label>
      <div class="btns"><button class="btn primary" id="s-save">Save</button></div>
      <p class="small muted">Stored only on this device.</p>
    </div>
    <h2>Alerts</h2>
    <div class="card">
      <p><b>On this device:</b> ${perm === "granted" && st.notify ? "✅ on" : "off"}. The app checks for stop-loss/target hits and new ideas whenever it's open.</p>
      <div class="btns"><button class="btn" id="s-notify">${perm === "granted" && st.notify ? "Turn off" : "Turn on"} notifications</button></div>
      <p style="margin-top:14px"><b>Push alerts when the app is closed:</b> ${ch.length ? "✅ " + ch.map(esc).join(", ") : "not set up yet"}.</p>
      <p class="small muted">The owner of this app can turn on free phone push alerts (ntfy app), Telegram, or Gmail in GitHub settings. See the README.</p>
    </div>
    <h2>Install on your phone</h2>
    <div class="card small"><p><b>Android (Chrome):</b> menu ⋮ → "Add to Home screen".<br><b>iPhone (Safari):</b> Share ⬆ → "Add to Home Screen".</p></div>
    <h2>Data</h2>
    <div class="card small">
      <p>Source: ${esc(S.latest?.data_source || "–")}, last market day ${esc(S.latest?.data_date || "–")}, updated ${esc(ago(S.latest?.generated))}.</p>
      <div class="btns"><button class="btn small" id="s-reload">Refresh now</button><button class="btn small" id="s-clear">Clear my positions</button></div>
    </div>`;
  $("#s-money-save").onclick = () => { store.set(LS.settings, { ...settings(), protect: $("#s-protect").value === "1", protectChosen: true }); saveMoney(+$("#s-money").value); renderSettings(); $("#s-money-save").textContent = "Saved ✓"; };
  $("#s-save").onclick = () => {
    const lt = +$("#s-lt").value, sw = +$("#s-sw").value;
    if (lt + sw > 100) { alert("Long-term + swing cannot be more than 100%."); return; }
    store.set(LS.settings, { ...st, capital: +$("#s-cap").value || DEFAULTS.capital, ltPct: lt, swingPct: sw, riskPct: +$("#s-risk").value, maxPosPct: +$("#s-maxp").value });
    renderAll();
    $("#s-save").textContent = "Saved ✓";
  };
  $("#s-notify").onclick = async () => {
    if (!("Notification" in window)) { alert("This browser does not support notifications. On iPhone, add the app to the Home Screen first."); return; }
    if (st.notify && Notification.permission === "granted") { store.set(LS.settings, { ...st, notify: false }); renderSettings(); return; }
    const p = await Notification.requestPermission();
    store.set(LS.settings, { ...st, notify: p === "granted" });
    if (p === "granted") notify("Alerts are on", "You'll be alerted about stop-loss, targets and new ideas.", "test");
    renderSettings();
  };
  $("#s-reload").onclick = () => loadAll();
  $("#s-clear").onclick = () => { if (confirm("Remove all your recorded positions from this device?")) { store.set(LS.my, []); renderAll(); } };
}

// ================================================================== SIMPLE VIEWS
// Plain-language screens for everyday use. Every detailed screen is still
// available under "More".

const SIMPLE = {
  dip_buy: { title: "Good stock on a short dip", why: "A strong, rising company fell for 2–3 days. Dips like this usually recover within a week." },
  breakout: { title: "Strong stock reaching new highs", why: "The price just rose above its recent highs, with a lot of buying." },
  ema_bounce: { title: "Rising stock bouncing back up", why: "A stock in a healthy uptrend dipped a little and turned up again today." },
};

const MOOD = {
  2: { icon: "🟢", cls: "good", title: "Good day to buy", text: "The overall market is rising." },
  1: { icon: "🟡", cls: "warn", title: "Be careful today", text: "The market is unsure. Buy smaller amounts than usual." },
  0: { icon: "🔴", cls: "bad", title: "Don't buy new stocks", text: "The market is falling. Keep your money safe. Only follow the sell instructions in My Stocks." },
};

const outOf10 = (wr) => (wr == null ? null : Math.max(1, Math.min(9, Math.round(wr / 10))));
const hasSettings = () => !!store.get(LS.settings, null);

function latestPrice(sym) {
  const q = livePrice(sym);
  if (q) return q.price;
  const L = S.latest || {};
  const a = (L.swing || []).find((x) => x.symbol === sym);
  if (a) return a.price;
  const b = (L.long_term?.picks || []).find((x) => x.symbol === sym);
  if (b) return b.price;
  const c = (L.open_trades || []).find((x) => x.symbol === sym && x.last_price);
  return c ? c.last_price : null;
}

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

// What should the user do with a stock they hold?
function positionStatus(p) {
  const L = S.latest || {};
  const q = livePrice(p.symbol);
  const now = q?.price ?? latestPrice(p.symbol) ?? p.lastPrice ?? p.price;
  const sessionLow = q?.day_low ?? now;
  const sessionHigh = q?.day_high ?? now;
  const { plan: systemTrade, exit: sell } = positionSystemContext(p, myPositions());
  let effectiveStop = systemTrade?.status === "open" ? tighterStop(p.stop, systemTrade.stop) : p.stop;
  let effectiveTarget = systemTrade?.status === "open" && systemTrade.target != null
    ? systemTrade.target : p.target;
  if (systemTrade?.status === "pending" && q?.open != null && q.date > systemTrade.signal_date) {
    const validOpen = q.open <= systemTrade.max_entry && q.open > systemTrade.initial_stop;
    const originalRisk = systemTrade.signal_close - systemTrade.initial_stop;
    if (validOpen && Number.isFinite(originalRisk) && originalRisk > 0) {
      effectiveStop = Math.round((q.open - originalRisk) * 100) / 100;
      if (systemTrade.target != null)
        effectiveTarget = Math.round((q.open + systemTrade.target - systemTrade.signal_close) * 100) / 100;
    }
  }
  if (p.kind === "long_term") {
    // the safety price rises with the price: always 12% (10% for funds) below the highest price since buying
    const trailStop = Math.round((p.high ?? p.price) * (1 - trailPct(p)) * 100) / 100;
    effectiveStop = Math.max(effectiveStop ?? 0, trailStop);
  }
  const removed = p.kind === "long_term" && (L.long_term?.changes?.removed || []).includes(p.symbol);
  const plAmt = now != null ? (now - p.price) * p.qty : null;
  const plPct = now != null ? (now / p.price - 1) * 100 : null;
  let st;
  if (effectiveStop && sessionLow != null && sessionLow <= effectiveStop)
    st = { cls: "bad", icon: "🔴", head: "SELL NOW", text: `The price fell to your safety price (${px(effectiveStop)}). Selling now protects your money. Small losses are part of the plan.` };
  else if (effectiveTarget && sessionHigh != null && sessionHigh >= effectiveTarget)
    st = { cls: "good", icon: "🎉", head: "BOOK PROFIT", text: `It reached the goal price (${px(effectiveTarget)}). Sell now to lock in your profit.` };
  else if (sell) {
    const session = sessionInfo(L.data_date || "");
    const head = session.actedToday
      ? (session.phase === "during" ? "SELL NOW" : "SELL IF NOT DONE")
      : "SELL at 9:15 AM";
    const text = session.actedToday
      ? "The plan says this trade is finished. Sell it now if you have not already done so."
      : "The plan says this trade is finished. Sell when the market opens.";
    st = { cls: "good", icon: "💰", head, text };
  }
  else if (p.kind === "long_term" && (L.market?.regime ?? 1) === 0 && !p.symbol.endsWith("GOLDBEES"))
    st = { cls: "warn", icon: "🛡", head: "SELL (market falling)", text: "The whole market has turned down. Sell and keep the money safe. The app will suggest when to buy again." };
  else if (p.kind === "long_term" && p.symbol !== "NIFTYBEES" && p.symbol !== "GOLDBEES"
           && (Date.now() - new Date(p.date).getTime()) / 864e5 >= 28
           && !(L.long_term_safe || []).slice(0, 6).some((x) => x.symbol === p.symbol))
    st = { cls: "warn", icon: "🔁", head: "SELL (monthly check)", text: "It is no longer among the strongest. Sell it and buy the new suggestion instead." };
  else if (removed)
    st = { cls: "warn", icon: "🔁", head: "SELL (monthly change)", text: "This stock has left the long-term list. Sell it and use the money for the new stocks in Invest." };
  else if (p.kind === "swing" && p.sellBy && new Date().toISOString().slice(0, 10) > p.sellBy)
    st = { cls: "warn", icon: "⏱", head: "SELL", text: "The short-term trade's time is up. Sell at the market opening." };
  else if (effectiveStop && now != null && (now - effectiveStop) / now < 0.02)
    st = { cls: "warn", icon: "🟡", head: "HOLD, but watch", text: `The price is close to your safety price (${px(effectiveStop)}).` };
  else
    st = { cls: "ok", icon: "🟢", head: "HOLD", text: p.kind === "long_term"
      ? `Hold. Sell if it falls to ${px(effectiveStop)} (this rises as the price rises). The app checks it every month.`
      : `Nothing to do. The app will tell you when to sell${p.sellBy ? ` (at the latest around ${fmtDate(p.sellBy)})` : ""}.` };
  return { ...st, now, plAmt, plPct, stop: effectiveStop, action: st.head !== "HOLD" && st.head !== "HOLD, but watch" };
}

function welcomeCard() {
  const presets = [100000, 300000, 500000, 1000000];
  return `<div class="card welcome">
    <h2>Welcome 🙏</h2>
    <p>One quick question, so the app can tell you <b>exactly how many shares to buy</b>.</p>
    <p class="big-q">How much money will you use for shares?</p>
    <div class="preset-row">${presets.map((v) => `<button class="btn" data-preset="${v}">${inr(v)}</button>`).join("")}</div>
    <label class="field">Or type an amount (₹)<input id="w-cap" type="number" inputmode="numeric" min="10000" step="10000" placeholder="e.g. 200000"></label>
    <button class="btn primary wide" id="w-save">Save</button>
    <p class="small muted">Use only money you won't need for daily life or emergencies. The app keeps 70% for long-term investing and uses 30% for short trades. You can change this later in More → Settings.</p>
  </div>`;
}

function saveCapital(v) {
  if (!v || v < 10000) { alert("Please enter at least ₹10,000."); return; }
  store.set(LS.settings, { ...settings(), capital: Math.round(v) });
  renderAll();
}

function simpleBuyCard(pk, i) {
  const sz = swingSize(pk);
  const info = SIMPLE[pk.strategy] || { title: pk.strategy_name, why: pk.why };
  const odds = outOf10(pk.hist_win_rate);
  const goal = pk.target
    ? `<div class="line"><span class="ic">🎯</span><div><b>Goal:</b> sell at <b>${px(pk.target)}</b>${sz.qty ? `, a profit of about <b class="up">${inr(sz.gain)}</b>` : ""}</div></div>`
    : `<div class="line"><span class="ic">🎯</span><div><b>When to sell:</b> the app will tell you, usually within a week${sz.qty && pk.reward_pct ? ` (typical profit about <b class="up">${inr((sz.amount * pk.reward_pct) / 100)}</b>)` : ""}.</div></div>`;
  if (!sz.qty) {
    return `<article class="card pick"><div class="pick-head"><span class="num-badge">${i}</span><div><h3>${esc(pk.name)}</h3><div class="muted small">${esc(pk.symbol)}</div></div></div>
      <p>Even 1 share would risk more than is safe for your amount (about ${inr(pk.risk_per_share)} per share). <b>Skip this one.</b></p></article>`;
  }
  return `<article class="card pick">
    <div class="pick-head"><span class="num-badge">${i}</span><div><h3>${esc(pk.name)}</h3><div class="muted small">${esc(pk.symbol)} · ${esc(info.title)}</div></div></div>
    <div class="do">Buy <b>${shares(sz.qty)}</b></div>
    <div class="line"><span class="ic">💵</span><div>Pay up to <b>${px(pk.max_entry)}</b> per share. Total about <b>${inr(sz.amount)}</b>.<br><span class="muted small">Buy when the market opens (9:15 AM). If the price is higher, skip it.</span></div></div>
    <div class="line"><span class="ic">🛡</span><div><b>Safety:</b> sell if it falls to <b>${px(pk.stop)}</b>. The most you can lose is about <b class="down">${inr(sz.maxLoss)}</b>.</div></div>
    ${goal}
    ${odds ? `<div class="line"><span class="ic">📊</span><div>Trades like this made money <b>${odds} out of 10 times</b> in the past.</div></div>` : ""}
    ${sz.halved ? `<div class="callout warn small">Smaller amount than usual, because the market is unsure.</div>` : ""}
    ${pk.ai_note ? `<div class="callout info small">${esc(pk.ai_note)}</div>` : ""}
    <div class="btns two"><button class="btn primary" data-buy="${esc(pk.symbol)}|${esc(pk.strategy)}">✅ I bought it</button>
      <button class="btn" data-detail="${esc(pk.symbol)}|${esc(pk.strategy)}">More details</button></div>
  </article>`;
}

// All buy ideas in simple form (every method, including riskier ones). Under "More".
function renderHome() {
  const el = $("#tab-ideas");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const m = L.market;
  const mood = MOOD[m.regime] || MOOD[1];
  const buys = (L.swing || []).filter((x) => x.action === "BUY");
  const mine = myPositions().map((p) => ({ p, s: positionStatus(p) }));
  const act = mine.filter((x) => x.s.action);
  const dateTxt = new Date().toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long" });
  let html = "";
  if (L.demo) html += `<div class="callout bad">Practice data only. Real ideas appear after the first daily scan.</div>`;
  if (!hasSettings()) html += welcomeCard();
  html += `<p class="hello">${greeting()}! <span class="muted">${esc(dateTxt)}</span></p>
    <div class="card mood ${mood.cls}"><div class="mood-icon">${mood.icon}</div><div><div class="mood-title">${mood.title}</div><div>${mood.text}</div></div></div>
    <div class="summary">
      <div><b>${m.regime === 0 ? 0 : buys.length}</b><span>to buy</span></div>
      <div><b class="${act.length ? "down" : ""}">${act.length}</b><span>to sell</span></div>
      <div><b>${mine.length - act.length}</b><span>to hold</span></div>
    </div>`;
  if (act.length) {
    html += `<h2>⚠️ Do this first</h2>` + act.map(({ p, s }) => `<div class="card st-card ${s.cls}"><div class="status-head">${s.icon} ${s.head}: ${esc(p.symbol)}</div><div>${s.text}</div></div>`).join("")
      + `<button class="btn wide" data-goto="mystocks">Open My Stocks</button>`;
  }
  html += `<h2>Stocks to buy</h2>`;
  if (m.regime === 0) html += `<div class="card empty">No buying today. The market is falling, and protecting your money comes first. 🙏</div>`;
  else if (!buys.length) html += `<div class="card empty">Nothing good enough to buy today. That's normal: on many days the best choice is to wait. 😌</div>`;
  else html += buys.map((pk, i) => simpleBuyCard(pk, i + 1)).join("");
  const lt = L.long_term || {};
  html += `<h2>Long-term investing</h2><div class="card"><p>Your list of <b>${(lt.picks || []).length} strong companies</b> to hold for the long term. Next check-up: <b>${esc(fmtDate(lt.next_rebalance))}</b>.</p>
    <button class="btn primary wide" data-goto="invest">See what to buy</button></div>
    <p class="small muted center">Updated ${esc(ago(L.generated))}. New ideas come every weekday evening.</p>`;
  el.innerHTML = html;
  const ws = $("#w-save");
  if (ws) ws.onclick = () => saveCapital(+$("#w-cap").value);
}

function holdPlanLine(p) {
  if (p.kind === "long_term") {
    const H = S.latest?.expect?.holding || {};
    const r = (isFundSym(p.symbol) ? H.fund : H.stock)?.["6m"];
    return `<div class="small muted">📅 Plan: hold about 6 to 12 months (${esc(fmtDate(addDays(p.date, 182)))} to ${esc(fmtDate(addDays(p.date, 365)))}), checked monthly.</div>
      ${r && r.n >= 8 ? `<div class="small muted">🎯 Typical after 6 months: about ${px(p.price * (1 + r.median / 100))} (${pct(r.median, 0)}); good case ${px(p.price * (1 + r.p75 / 100))}.</div>` : ""}`;
  }
  return p.sellBy ? `<div class="small muted">📅 Time limit: sell by about ${esc(fmtDate(p.sellBy))} at the latest.</div>` : "";
}

function renderMyStocks() {
  const el = $("#tab-mystocks");
  const mine = myPositions().map((p) => ({ p, s: positionStatus(p) }));
  if (!mine.length) {
    el.innerHTML = `<h2>My Stocks</h2><div class="card empty"><p style="font-size:40px;margin:0">💼</p>
      <p>You haven't added any stocks yet.</p><p class="small">After you buy a stock from <b>Today</b> or <b>Invest</b>, tap <b>"I bought it"</b>.
      It will show here, and the app will tell you <b>when to sell</b>.</p><button class="btn primary" data-goto="today">Go to Today</button></div>`;
    return;
  }
  const total = mine.reduce((a, x) => a + (x.s.plAmt || 0), 0);
  const invested = mine.reduce((a, x) => a + x.p.price * x.p.qty, 0);
  mine.sort((a, b) => b.s.action - a.s.action);
  el.innerHTML = `<h2>My Stocks</h2>
    <div class="card total"><div class="muted">Total profit / loss</div><div class="big ${tone(total)}">${total >= 0 ? "+" : "−"}${inr(Math.abs(total))}</div>
      <div class="muted small">on ${inr(invested)} invested</div></div>
    ${mine.map(({ p, s }) => `<article class="card st-card ${s.cls}">
      <div class="status-head">${s.icon} ${s.head}</div>
      <h3>${esc(p.symbol)}</h3>
      <div class="muted small">${esc(p.kind === "long_term" ? "Long-term" : "Short trade")} · bought ${num(p.qty, 0)} at ${px(p.price)} on ${esc(fmtDate(p.date))}</div>
      <div class="pl"><span>Now ${s.now != null ? px(s.now) : "–"}</span>
        <b class="${tone(s.plAmt)}">${s.plAmt == null ? "–" : (s.plAmt >= 0 ? "+" : "−") + inr(Math.abs(s.plAmt))} (${pct(s.plPct)})</b></div>
      <p>${s.text}</p>
      ${s.stop ? `<div class="small muted">🛡 Safety price now ${px(s.stop)}${p.kind === "long_term" ? " (rises as the price rises)" : ""}${p.target ? ` · 🎯 Goal ${px(p.target)}` : ""}</div>` : ""}
      ${holdPlanLine(p)}
      <div class="btns"><button class="btn" data-sold="${esc(p.id)}">I sold it</button></div>
    </article>`).join("")}
    <p class="small muted center">Prices refresh every 15 minutes while the market is open.</p>`;
}

function renderInvest() {
  const el = $("#tab-invest");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const lt = L.long_term || {};
  const picks = lt.picks || [];
  const st = settings();
  const ltCap = (st.capital * st.ltPct) / 100;
  const b = ltBudget();
  const steps = { 2: "Buy in 2 steps: half the shares now, the rest after 2 weeks.", 1: "Buy in 3 steps: one third now, then one third each month.",
    0: "The market is falling. Buy slowly: one sixth each month for 6 months." }[L.market.regime];
  const ch = lt.changes || {};
  let total = 0;
  const rows = picks.map((p, i) => {
    const sz = ltSize(p);
    total += sz.amount;
    return `<div class="lt-row"><span class="num-badge">${i + 1}</span>
      <div class="lt-name"><b>${esc(p.name)}</b><div class="muted small">${esc(p.symbol)}${p.is_new && ch.removed?.length ? ` · <span class="up">new</span>` : ""}</div></div>
      <div class="lt-qty">${sz.skip ? `<span class="small muted">Skip (price too high)</span>` : `<b>${shares(sz.qty)}</b><div class="muted small">${inr(sz.amount)}</div>`}</div>
      ${sz.skip ? "" : `<button class="btn small" data-buy="${esc(p.symbol)}|lt" aria-label="I bought ${esc(p.symbol)}">✅ Bought</button>`}</div>`;
  }).join("");
  el.innerHTML = `<h2>Long-term investing</h2>
    <div class="card"><p>These are <b>strong, growing companies</b>. Hold them for months and years, not days.</p>
      <div class="line"><span class="ic">💰</span><div>Your long-term money: <b>${inr(ltCap)}</b>, about <b>${inr(b.per)}</b> in each company.</div></div>
      <div class="line"><span class="ic">🪜</span><div>${esc(steps)}</div></div>
      <div class="line"><span class="ic">📅</span><div>Check once a month. Next check-up: <b>${esc(fmtDate(lt.next_rebalance))}</b>.</div></div></div>
    ${ch.added?.length && ch.removed?.length ? `<div class="card st-card warn"><div class="status-head">🔁 Changes this month</div>
      <p><b>Sell:</b> ${ch.removed.map(esc).join(", ")}</p><p><b>Buy instead:</b> ${ch.added.map(esc).join(", ")}</p></div>` : ""}
    <div class="card">${rows || `<p class="empty">No list yet.</p>`}
      <div class="lt-total">Total: <b>${inr(total)}</b></div></div>
    <p class="small muted">Tap <b>✅ Bought</b> after buying a stock, and it will move to My Stocks. For charts and company numbers, see More → Long-term details.</p>`;
}

function renderMore() {
  const main = [
    ["help", "❓", "How to use this app", "3 simple steps"],
    ["record", "📊", "Is it working?", "Results so far"],
    ["settings", "⚙️", "Settings", "My money, phone alerts"],
    ["advanced", "🧰", "Advanced", "Detailed screens for experts"],
  ];
  $("#tab-more").innerHTML = `<h2>More</h2><div class="menu">${main.map(([k, ic, t, d]) =>
    `<button class="menu-item" data-goto="${k}"><span class="ic">${ic}</span><span><b>${t}</b><small>${d}</small></span><span class="chev">›</span></button>`).join("")}</div>`;
  renderAdvanced();
}

function renderAdvanced() {
  const items = [
    ["live", "📡", "Live Intelligence", "Near-live quotes, risk and signal confirmation"],
    ["plan", "📋", "Daily plan", "Buy / sell / hold list followed by the app"],
    ["market", "🌐", "Market details", "Nifty chart and market health"],
    ["marketnews", "📰", "Market news", "General headlines"],
  ];
  $("#tab-advanced").innerHTML = `<h2>Advanced</h2><div class="menu">${items.map(([k, ic, t, d]) =>
    `<button class="menu-item" data-goto="${k}"><span class="ic">${ic}</span><span><b>${t}</b><small>${d}</small></span><span class="chev">›</span></button>`).join("")}</div>`;
}

function renderHelp() {
  $("#tab-help").innerHTML = `<h2>How to use this app</h2>
    <div class="card"><ol class="stepper">
      <li><b>Open Suggestions</b> and set <b>Money I have today</b>. The app spreads it over a few holdings: big companies from different sectors, an index fund, sometimes gold. Each stock passed every check: strong business, rising price, no bad news, market OK.</li>
      <li><b>Buy at 9:15 AM</b> in your broker app (Zerodha, Groww…), then tap <b>"I bought this"</b>.</li>
      <li><b>Check My Stocks</b> once a day. It says <b>HOLD</b> or <b>SELL</b>, and when.</li>
    </ol>
    <p class="small muted">Tip: many broker apps let you set the safety price as a <b>GTT / stop-loss order</b> right after buying, so it sells automatically.</p></div>
    <h2>Golden rules</h2>
    <div class="card"><ul class="rules">
      <li>🛡 <b>Always sell at the safety price.</b> Small losses are normal. Big losses are what hurt.</li>
      <li>🤏 <b>Never put too much in one stock.</b> The app already keeps each trade small.</li>
      <li>😌 <b>No trade is fine.</b> Some days and weeks there is nothing to buy.</li>
      <li>📉 <b>Losses in a row will happen.</b> The plan works over many trades, not one.</li>
      <li>🏦 <b>Keep emergency money in the bank or FD,</b> not in shares.</li>
    </ul></div>
    <h2>Words explained</h2>
    <div class="card"><dl class="gloss">
      <dt>Safety price (stop-loss)</dt><dd>If the price falls to this level, sell. It caps your loss.</dd>
      <dt>Goal (target)</dt><dd>The price at which we plan to take profit.</dd>
      <dt>Short trade</dt><dd>Held for a few days to a few weeks.</dd>
      <dt>Long-term</dt><dd>Strong companies held for months or years and checked once a month.</dd>
      <dt>Market mood</dt><dd>🟢 rising, 🟡 unsure, 🔴 falling. Based on the Nifty 50 index.</dd>
      <dt>"X out of 10 times"</dt><dd>How often this kind of trade made money in the past 5 years. It's a guide, not a promise.</dd>
    </dl></div>
    <p class="small muted">${esc(S.latest?.disclaimer || "")}</p>`;
}

function openDetail(sym, strat) {
  const pk = findPick(sym, strat);
  if (!pk) return;
  $("#dlg-body").innerHTML = swingCard(pk);
  $("#dlg").showModal();
}

function setupHelp() {
  return `<div class="card empty"><p style="font-size:40px;margin:0">⏳</p><h2>Getting ready</h2>
    <p>The first stock ideas will appear here after the evening scan (weekdays, 6:15 PM).</p>
    <p class="small muted">Setting this up? Run <b>Daily scan</b> once from the GitHub Actions tab, then refresh this page.</p></div>`;
}

// ================================================================== DAILY PLAN (main screen)
// The engine follows every trade it suggests, so the investor never has to
// press anything: this screen simply says SELL / BUY / HOLD for his amount.

const tradeMoney = () => settings().tradeMoney;
const slotsN = () => S.latest?.safe?.max_open_trades || 1;
const perTrade = () => tradeMoney() / slotsN();
const slotsText = () => (slotsN() === 1 ? "one trade at a time" : `at most ${slotsN()} trades at a time`);
// fixed broker charges (about Rs 16 per sell) eat most of the profit on small trades
const SMALL_AMOUNT = 15000;
const smallAmountWarning = () => (perTrade() < SMALL_AMOUNT
  ? `<div class="callout warn small">With less than ${inr(SMALL_AMOUNT)} per trade, fixed broker charges take most of the profit.
     ${inr(20000)} works much better.</div>` : "");
const moneyIsSet = () => !!(store.get(LS.settings, {}) || {}).tradeMoney;

// India time from the phone clock (getUTC* fields of this Date hold IST wall time)
const istNow = () => new Date(Date.now() + 330 * 60000);

function sessionInfo(dataDate) {
  const n = istNow();
  const today = n.toISOString().slice(0, 10);
  const mins = n.getUTCHours() * 60 + n.getUTCMinutes();
  const wd = n.getUTCDay();                                  // 0 = Sunday
  const weekday = wd >= 1 && wd <= 5;
  const phase = !weekday ? "closed" : mins < 9 * 60 + 15 ? "before" : mins <= 15 * 60 + 30 ? "during" : "after";
  let when = "today";
  if (phase !== "before") when = wd === 5 || wd === 6 ? "on Monday" : "tomorrow";
  // the plan was made on the evening of dataDate; its buy/sell happen at the next opening
  const actedToday = weekday && today > dataDate && phase !== "before";
  return { phase, when, actedToday };
}

function planWelcome() {
  const presets = [5000, 10000, 20000];
  return `<div class="card welcome">
    <h2>Welcome 🙏</h2>
    <p class="big-q">How much money do you have today?</p>
    <p class="small muted">You can change it any day at the top of this screen.</p>
    <div class="preset-row three">${presets.map((v) => `<button class="btn" data-money="${v}">${inr(v)}</button>`).join("")}</div>
    <label class="field">Or type an amount (₹)<input id="w-money" type="number" inputmode="numeric" min="2000" step="1000" placeholder="e.g. 12000"></label>
    <button class="btn primary wide" id="w-money-save">Save</button>
    <p class="small muted">The app uses it for ${slotsText()}. ${inr(20000)} works best: below about ${inr(SMALL_AMOUNT)},
      fixed broker charges take most of the profit. Use only money you won't need for daily life.</p>
  </div>`;
}

function saveMoney(v) {
  if (!v || v < 1000) { alert("Please enter at least ₹1,000."); return; }
  store.set(LS.settings, { ...settings(), tradeMoney: Math.round(v) });
  renderAll();
}

function buyCard(b, when) {
  const qty = Math.floor(perTrade() / b.max_entry);
  if (qty < 1) {
    return `<article class="card st-card"><div class="status-head">🛒 Buy idea: ${esc(b.name)}</div>
      <p>One share costs more than your per-trade amount (${inr(perTrade())}). <b>Skip this one.</b></p></article>`;
  }
  const odds = outOf10(b.hist_win_rate);
  return `<article class="card st-card buy">
    <div class="status-head">🛒 BUY ${esc(when)} at 9:15 AM</div>
    <h3>${esc(b.name)}</h3><div class="muted small">${esc(b.symbol)} · one of India's 50 biggest companies</div>
    <div class="do">Buy <b>${shares(qty)}</b></div>
    <div class="line"><span class="ic">💵</span><div>Pay at most <b>${px(b.max_entry)}</b> per share, about <b>${inr(qty * b.max_entry)}</b> in total.
      <b>If the price is higher, don't buy.</b></div></div>
    <div class="line"><span class="ic">🛡</span><div>If it falls to about <b>${px(b.stop)}</b>, sell. We'll alert you.
      The most you could lose is about <b class="down">${inr(qty * b.risk_per_share)}</b>.</div></div>
    ${odds ? `<div class="line"><span class="ic">📊</span><div>In the past 5 years, this kind of trade made money <b>${odds} out of 10 times</b>
      (${num(b.hist_trades, 0)} trades). Wins averaged <b class="up">${pct(b.hist_avg_win)}</b>, losses <b class="down">${pct(b.hist_avg_loss)}</b>.</div></div>` : ""}
    <div class="line"><span class="ic">⏱</span><div>Usually sold within about ${num(b.hist_avg_days, 0)} days. The app will tell you when.</div></div>
  </article>`;
}

function sellCard(x, when, actedToday) {
  const why = { stop: "It fell to the safety price. Small losses are part of the plan.", target: "It reached the goal price.",
    time: "The time limit is over." }[x.reason] || "The trade is complete: the price has bounced back.";
  const head = actedToday ? "💰 SELL this morning (if not done yet, sell now)" : `💰 SELL ${esc(when)} at 9:15 AM`;
  return `<article class="card st-card ${x.reason === "stop" ? "bad" : "good"}">
    <div class="status-head">${head}</div>
    <h3>All your ${esc(x.symbol)} shares</h3>
    <p>${why}${x.ret_pct != null ? ` Result: about <b class="${tone(x.ret_pct)}">${pct(x.ret_pct)}</b>.` : ""}</p>
    <p class="small muted">If you didn't buy ${esc(x.symbol)}, ignore this.</p></article>`;
}

function sellNowCard(x, q, stop) {
  return `<article class="card st-card bad">
    <div class="status-head">🔴 SELL NOW</div>
    <h3>All your ${esc(x.symbol)} shares</h3>
    <p>It fell to the safety price (${px(stop)}); now ${px(q.price)}. Selling now keeps the loss small.</p>
    <p class="small muted">If you didn't buy ${esc(x.symbol)}, ignore this.</p></article>`;
}

function holdCard(h, q) {
  const now = q ? q.price : h.last_price;
  const chg = now && h.entry ? (now / h.entry - 1) * 100 : null;
  const money = chg != null ? (perTrade() * chg) / 100 : null;
  return `<article class="card st-card ok">
    <div class="status-head">✋ HOLD: nothing to do</div>
    <h3>${esc(h.symbol)}</h3>
    <div class="pl"><span>Bought ${px(h.entry)} on ${esc(fmtDate(h.entry_date))}</span>
      <b class="${tone(chg)}">${pct(chg)}</b></div>
    <p class="small">Now ${px(now)}${money != null ? `: about <b class="${tone(money)}">${money >= 0 ? "+" : "−"}${inr(Math.abs(money))}</b> on ${inr(perTrade())}` : ""}.
      Safety price ${px(h.stop)}. The app will tell you when to sell.</p></article>`;
}

function boughtTodayCard(b, q, dayStop) {
  return `<article class="card st-card ok">
    <div class="status-head">✋ HOLD: bought this morning</div>
    <h3>${esc(b.symbol)}</h3>
    <p class="small">It opened at ${px(q.open)}, inside the buy range. If you bought it, just hold.
      Safety price about ${px(dayStop)} (now ${px(q.price)}).</p></article>`;
}

function skipCard(b, q) {
  return `<article class="card st-card">
    <div class="status-head">⏭ Don't buy ${esc(b.symbol)} today</div>
    <p class="small">It opened at ${px(q.open)}, outside the buy range (up to ${px(b.max_entry)}). Nothing to do.</p></article>`;
}

function missedCard(b) {
  const qty = Math.floor(perTrade() / b.max_entry);
  return `<article class="card st-card">
    <div class="status-head">🛒 This morning's buy</div>
    <h3>${esc(b.name)}</h3>
    <p class="small">${qty ? shares(qty) + " at most " + px(b.max_entry) + " each. " : ""}If you bought it, hold it: the app will tell you when to sell.
      If you missed it, skip it. Another chance will come.</p></article>`;
}

// SELL NOW conditions from live prices (used by the screen and by phone notifications)
function liveDangers() {
  const L = S.latest || {};
  const plan = L.plan || { buy: [], sell: [], hold: [] };
  const out = [];
  for (const h of plan.hold) {
    const q = livePrice(h.symbol);
    if (q && h.stop && (q.day_low ?? q.price) <= h.stop) out.push({ x: h, q, stop: h.stop });
  }
  for (const b of plan.buy) {
    const q = livePrice(b.symbol);
    if (q && q.open && q.date > b.signal_date && q.open <= b.max_entry && q.open > b.stop) {
      const dayStop = Math.round((q.open - b.risk_per_share) * 100) / 100;
      if ((q.day_low ?? q.price) <= dayStop) out.push({ x: b, q, stop: dayStop });
    }
  }
  return out;
}

function renderPlan() {
  const el = $("#tab-plan");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const plan = L.plan || { buy: [], sell: [], hold: [] };
  const safe = L.safe || {};
  const s = sessionInfo(L.data_date);
  const danger = liveDangers();
  const dangerSyms = new Set(danger.map((d) => d.x.symbol));
  const now = danger.map((d) => sellNowCard(d.x, d.q, d.stop));
  const sells = plan.sell.filter((x) => !dangerSyms.has(x.symbol)).map((x) => sellCard(x, s.when, s.actedToday));
  const buys = [], holds = [];
  for (const b of plan.buy) {
    if (dangerSyms.has(b.symbol)) continue;
    const q = livePrice(b.symbol);
    if (s.actedToday && q && q.open && q.date > b.signal_date) {
      if (q.open > b.max_entry || q.open <= b.stop) buys.push(skipCard(b, q));
      else holds.push(boughtTodayCard(b, q, q.open - b.risk_per_share));
    } else if (s.actedToday) buys.push(missedCard(b));
    else buys.push(buyCard(b, s.when));
  }
  for (const h of plan.hold) if (!dangerSyms.has(h.symbol)) holds.push(holdCard(h, livePrice(h.symbol)));
  const todo = now.length + plan.sell.filter((x) => !dangerSyms.has(x.symbol)).length + (s.actedToday ? 0 : plan.buy.length);
  const methods = Object.values(safe.methods || {});
  const paused = safe.enabled && methods.length && !methods.some((m) => m.ok);
  const mood = MOOD[L.market?.regime] || MOOD[1];
  const dateTxt = new Date().toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long" });

  let html = "";
  if (L.demo) html += `<div class="callout bad">Practice data only.</div>`;
  if (!moneyIsSet()) html += planWelcome();
  else html += smallAmountWarning();
  html += `<p class="hello">${greeting()}! <span class="muted">${esc(dateTxt)}</span></p>`;
  if (todo) html += `<div class="card headline"><div class="mood-title">${todo} thing${todo > 1 ? "s" : ""} to do</div>
      <div class="small muted">Do it in your broker app (Zerodha, Groww…). Nothing to press here.</div></div>`;
  else html += `<div class="card headline calm"><div class="mood-icon">🙏</div><div><div class="mood-title">Nothing to do today</div>
      <div class="small">The app suggests a trade only when the odds are clearly good. On most days there is nothing, and that is how it protects your money.</div></div></div>`;
  html += now.join("") + sells.join("") + buys.join("") + holds.join("");
  html += `<div class="card small">
      <div class="line"><span class="ic">🛡</span><div><b>Safe mode:</b> only India's 50 biggest companies, only when the past record is strong,
        ${slotsText()}, about ${inr(perTrade())}${slotsN() > 1 ? " each" : ""}.</div></div>
      ${paused ? `<div class="line"><span class="ic">⏸</span><div><b>New buying is paused.</b> ${esc(methods[0].why)}.</div></div>` : ""}
      ${safe.enabled && L.market?.regime !== 2
        ? `<div class="line"><span class="ic">${mood.icon}</span><div>The safe method buys only when the whole market is rising.
            Right now the market is ${L.market?.regime === 0 ? "falling" : "unsure"}, so it waits. Existing trades are still looked after.</div></div>`
        : `<div class="line"><span class="ic">${mood.icon}</span><div>Market: ${esc(mood.title.toLowerCase())}.</div></div>`}
      <div class="line"><span class="ic">🕕</span><div>New plan every weekday evening after 6:15 PM. Updated ${esc(ago(L.generated))}.</div></div>
    </div>
    <button class="btn wide" data-goto="record">📊 Is it working? See the results</button>`;
  el.innerHTML = html;
  const ws = $("#w-money-save");
  if (ws) ws.onclick = () => saveMoney(+$("#w-money").value);
}

// ================================================================== SUGGESTIONS (main screen)
// Only what to buy and for roughly how long. Reasons are optional ("Why?").
// He marks what he buys with one tap; My Stocks then says when to sell.

const addDays = (iso, n) => { const d = new Date(iso); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };

// Protected plan: the stock part is sized so that, if every stock fell to its
// safety price (at most 12% below), one year of interest on the safe part
// (FD / liquid fund, ~6.5%) would cover it.
const SAFE_YIELD = 0.065, MAX_STOCK_LOSS = 0.12, MIN_SHORT_TERM = 15000;
const MIN_PER_STOCK = 1500, TWO_STOCKS_FROM = 6000;   // below these, charges eat too much

function moneyPlan() {
  const money = tradeMoney();
  if (!settings().protect) return { money, stock: money, safe: 0, protect: false };
  const stock = Math.floor((money * SAFE_YIELD) / (SAFE_YIELD + MAX_STOCK_LOSS) / 500) * 500;
  return { money, stock, safe: money - stock, protect: true };
}


// Diversified basket inside the stock market: big companies from different
// sectors, a Nifty 50 index fund, gold, and a mid-size company only when it
// passed extra-strict checks. Every stock passed business, price, news and
// market checks in the engine. Fewer holdings for small amounts (charges).
function basket() {
  const L = S.latest || {};
  const M = tradeMoney();
  const nHold = M < 3000 ? 1 : M < 6000 ? 2 : M < 12000 ? 3 : 4;
  // market guard (tested): when the whole market is falling, buy nothing
  if ((L.market?.regime ?? 1) === 0) return [];
  const large = L.long_term_safe || [];
  const mids = L.mid_safe || [];
  const funds = L.funds || [];
  const idx = funds.find((f) => f.symbol === "NIFTYBEES");
  const gold = funds.find((f) => f.symbol === "GOLDBEES" && f.uptrend);
  const swing = (L.plan?.buy || [])[0];
  const order = [];
  if (swing && M >= MIN_SHORT_TERM) order.push({ ...swing, _swing: true });
  // tested recipe: Nifty 50 index fund + up to 3 strong companies from different sectors
  order.push(idx, large[0], large[1], mids[0] || large[2], large[2], large[3], large[4]);
  void gold;                                           // gold is not part of the tested recipe
  const seenInd = new Set();
  const cand = order.filter(Boolean).filter((x) => {
    const ind = x.industry || x.symbol;
    if (seenInd.has(ind)) return false;
    seenInd.add(ind);
    return true;
  });
  // index fund gets about a third (half when only 2 holdings); stocks share the rest
  const idxShare = nHold <= 1 ? 1 : nHold === 2 ? 0.5 : 1 / 3;
  const stockN = Math.max(1, nHold - (idx ? 1 : 0));
  const perStock = idx ? (M * (1 - idxShare)) / stockN : M / nHold;
  const amountFor = (x) => (x.symbol === "NIFTYBEES" ? M * idxShare : perStock);
  let chosen = cand.filter((x) => (x.max_entry || x.price * 1.02) <= amountFor(x)).slice(0, nHold);
  const items = chosen.map((x) => {
    const per = amountFor(x);
    const isFund = x.size === "fund";
    const tag = x._swing ? "Short term · about 1 week"
      : isFund ? "Long term · hold for months" : "Long term · 6 to 12 months";
    const type = x._swing ? "Big company · quick bounce trade"
      : isFund ? x.type
      : x.size === "mid" ? `Mid-size company · ${x.industry} · passed extra-strict checks`
      : `Big company · ${x.industry}`;
    const checks = isFund ? "✓ Price rising" : "✓ Strong business · ✓ Price rising · ✓ News fine · ✓ Market OK";
    return {
      kind: x._swing ? "swing" : "long_term", amount: per, symbol: x.symbol, name: x.name,
      strategy: x.strategy, strategy_name: (L.swing || []).find((y) =>
        y.symbol === x.symbol && y.strategy === x.strategy)?.strategy_name,
      planId: x.id, signal_date: x.signal_date, price: x.price,
      max_entry: x.max_entry || +(x.price * 1.02).toFixed(2), stop: x._swing ? x.stop : x.review_below,
      horizon: tag, type, checks: x._swing ? "✓ Strong past record · ✓ Market rising" : checks,
      sellRule: x._swing ? "The app will tell you when to sell (usually within a week)."
        : `Hold for months. Sell if it falls below ${px(x.review_below)}, or when the app says.`,
      why: x._swing ? (SIMPLE[x.strategy] || {}).why
        : isFund ? `${x.type}. Spreads your money so one company can't hurt much.`
        : `Among the strongest steady risers in its sector, with a profitable, low-debt business${x.fundamentals?.roe ? ` (return on equity ${num(x.fundamentals.roe, 0)}%)` : ""}.`,
      odds: x._swing ? outOf10(x.hist_win_rate) : null,
      isFund,
      hist: x._swing ? { p10: x.hist_p10, median: x.hist_median, p75: x.hist_p75, p90: x.hist_p90,
        days: x.hist_days_median, daysMax: x.hist_days_p90, win: x.hist_win_rate, n: x.hist_trades } : null,
      news: x.news || (L.swing || []).find((y) => y.symbol === x.symbol)?.news || [],
    };
  });
  // whole shares leave money unused: add extra shares where they fit,
  // cheapest first, never letting one holding exceed half of the money
  if (items.length && items.length < nHold) {        // fewer affordable holdings: spread the money over them
    for (const it of items) it.amount = M / items.length;
  }
  for (const it of items) it.qty = Math.floor(it.amount / it.max_entry);
  let left = M - items.reduce((a, it) => a + it.qty * it.max_entry, 0);
  for (let guard = 0; guard < 500; guard++) {
    const fit = items.filter((it) => it.max_entry <= left && (it.qty + 1) * it.max_entry <= M * 0.5)
      .sort((a, b) => a.qty * a.max_entry - b.qty * b.max_entry);
    if (!fit.length) break;
    fit[0].qty += 1;
    left -= fit[0].max_entry;
  }
  return items.filter((it) => it.qty > 0);
}

function suggestions() {
  if (!settings().protect) return basket();
  return protectedSuggestions();
}

function protectedSuggestions() {
  const L = S.latest || {};
  const out = [];
  const plan = moneyPlan();
  const swing = L.swing || [];
  for (const b of (L.plan?.buy || [])) {
    const pk = swing.find((x) => x.symbol === b.symbol) || {};
    if (plan.stock < MIN_SHORT_TERM) break;             // short trades only pay off with larger amounts
    out.push({ kind: "swing", amount: plan.stock, symbol: b.symbol, name: b.name,
      strategy: b.strategy, strategy_name: pk.strategy_name, planId: b.id,
      signal_date: b.signal_date, price: b.price, max_entry: b.max_entry, stop: b.stop,
      horizon: "Short term · about 1 week", sellRule: "The app will tell you when to sell (usually within a week).",
      why: (SIMPLE[b.strategy] || {}).why, odds: outOf10(b.hist_win_rate), news: pk.news || [],
      hist: { p10: b.hist_p10, median: b.hist_median, p75: b.hist_p75, p90: b.hist_p90,
        days: b.hist_days_median, daysMax: b.hist_days_p90, win: b.hist_win_rate, n: b.hist_trades } });
  }
  const lt = (L.long_term_safe || (L.long_term?.picks || []).filter((p) => p.large)).filter((p) => !(p.warnings || []).length && !(p.red_flags || []).length
)
  // diversity: at most 2 (protected) or 3 companies, never two from the same industry
  // small amounts: fewer stocks, so the fixed charges don't eat the profit
  const nLong = plan.stock < MIN_PER_STOCK ? 0 : plan.stock < TWO_STOCKS_FROM ? 1 : plan.protect ? 2 : 3;
  const per = nLong ? (plan.protect ? plan.stock / nLong : plan.stock) : 0;
  const usedInd = new Set();
  const chosen = [];
  for (const p of lt) {
    if (chosen.length >= nLong) break;
    if (p.price * 1.02 > per || usedInd.has(p.industry || p.symbol)) continue;
    usedInd.add(p.industry || p.symbol);
    chosen.push(p);
  }
  for (const p of chosen) {
    out.push({ kind: "long_term", amount: per, symbol: p.symbol, name: p.name, price: p.price, max_entry: +(p.price * 1.02).toFixed(2),
      stop: p.review_below, horizon: "Long term · 6 to 12 months",
      sellRule: `Hold for months. Sell if it falls below ${px(p.review_below)}, or when the app says at the monthly check.`,
      why: "A strong, steadily rising company among India's 50 biggest. Ranked by 6 and 12-month strength.",
      odds: null, news: p.news || [] });
  }
  return out.filter((x) => x.max_entry <= x.amount);
}

// ---------------------------------------------------------------- expectations from past cases
const sgn = (v) => (v >= 0 ? "+" : "−");
const moneySigned = (v) => `${sgn(v)}${inr(Math.abs(v))}`;
const isFundSym = (sym) => sym === "NIFTYBEES" || sym === "GOLDBEES";
const trailPct = (p) => (isFundSym(p.symbol) ? 0.10 : 0.12);

function addBusinessDays(iso, n) {
  const d = new Date(iso + "T00:00:00Z");
  let k = 0;
  while (k < n) { d.setUTCDate(d.getUTCDate() + 1); const w = d.getUTCDay(); if (w !== 0 && w !== 6) k++; }
  return d.toISOString().slice(0, 10);
}

// Past results for this kind of holding: bad / typical / good case, in percent.
function expectColumns(x) {
  const H = S.latest?.expect?.holding || {};
  if (x.kind === "swing") {
    const h = x.hist || {};
    if (h.median == null || h.p10 == null) return null;
    const days = h.days || 5;
    return [{ name: `After about ${days} day${days === 1 ? "" : "s"}`, bad: h.p10, typ: h.median, good: h.p75, best: h.p90,
      pos: h.win, n: h.n }];
  }
  const src = (x.isFund ? H.fund : H.stock) || {};
  return [["6m", "After ~6 months"], ["12m", "After ~12 months"]].map(([k, name]) => {
    const r = src[k];
    return r && r.n >= 8 ? { name, bad: r.p10, typ: r.median, good: r.p75, best: r.p90, pos: r.pct_positive, n: r.n } : null;
  }).filter(Boolean);
}

function expectBlock(x, qty) {
  const cols = expectColumns(x);
  if (!cols || !cols.length || !qty) return "";
  const base = qty * x.price;
  const cell = (v) => { const g = (base * v) / 100;
    return `<div><b class="${tone(g)}">${moneySigned(g)}</b></div><div class="muted small">sell price ${px(x.price * (1 + v / 100))} · ${pct(v, 0)}</div>`; };
  const row = (label, key) => `<tr><td>${label}</td>${cols.map((c) => `<td>${cell(c[key])}</td>`).join("")}</tr>`;
  const best = cols.map((c) => `${c.name.toLowerCase()}: ${pct(c.best, 0)} (${moneySigned((base * c.best) / 100)})`).join(" · ");
  return `<div class="expect"><div class="expect-head">What to expect <span class="muted small">(from similar past cases, after charges)</span></div>
    <div class="table-wrap"><table class="expect-table"><thead><tr><th></th>${cols.map((c) => `<th>${esc(c.name)}<div class="muted small">${c.pos != null ? Math.round(c.pos / 10) + " of 10 made money" : ""}</div></th>`).join("")}</tr></thead>
      <tbody>${row("Bad case", "bad")}${row("Typical", "typ")}${row("Good case", "good")}</tbody></table></div>
    <div class="small muted">Bad case: 1 in 10 past cases was this bad or worse. Good case: 1 in 4 did better. Best 1 in 10: ${best}.
      Past results, not a promise.</div></div>`;
}

function holdLine(x) {
  if (x.kind === "swing") {
    const h = x.hist || {};
    return `Usually sold in about ${h.days || 5} days (at most ${h.daysMax || 10}). The app tells you when.`;
  }
  return x.isFund ? "Hold for months; no end date. The app checks every month and tells you if to sell."
    : "Hold 6 to 12 months. The app checks every month and tells you if to sell earlier.";
}

function orderDetails(x, qty) {
  const L = S.latest || {};
  const total = qty * x.max_entry;
  const charges = Math.round(total * 0.003 + 16);
  const maxLoss = Math.max(0, qty * (x.max_entry - x.stop));
  const valid = L.data_date ? addBusinessDays(L.data_date, 3) : null;
  const trail = x.kind === "long_term"
    ? `<div class="line"><span class="ic">⬆</span><div>The safety price <b>rises as the price rises</b>: it always stays ${isFundSym(x.symbol) ? 10 : 12}% below the highest price since you bought. My Stocks shows the current one. Move your sell order up when it changes.</div></div>` : "";
  return `<details><summary>All the trade details</summary>
    <div class="line"><span class="ic">📅</span><div>Suggested on <b>${esc(fmtDate(L.data_date))}</b>. Valid for 3 trading days${valid ? ` (until <b>${esc(fmtDate(valid))}</b>)` : ""}. After that, use the newer list.</div></div>
    <div class="line"><span class="ic">🧾</span><div><b>How to place it in your broker app:</b>
      <ol class="small steps"><li>Search <b>${esc(x.symbol)}</b> (NSE).</li>
        <li>Choose <b>BUY</b> · <b>Delivery</b> (CNC) · <b>Limit order</b>.</li>
        <li>Price <b>${px(x.max_entry)}</b> · Quantity <b>${num(qty, 0)}</b>. A limit order means you never pay more than that.</li>
        <li>Once it's bought, set a <b>stop-loss / GTT sell order</b> for all ${num(qty, 0)} shares with trigger price <b>${px(x.stop)}</b>.</li>
        <li>Tap <b>"I bought this"</b> here and enter the price you paid.</li></ol></div></div>
    <div class="line"><span class="ic">💵</span><div>About <b>${inr(total)}</b> in total, plus about <b>${inr(charges)}</b> in charges for buying and selling.</div></div>
    <div class="line"><span class="ic">🛡</span><div>Most you could lose if the safety price is hit: about <b class="down">${inr(maxLoss)}</b>.</div></div>
    ${trail}
    <div class="line"><span class="ic">⏱</span><div>${esc(holdLine(x))}</div></div>
    <p class="small">${esc(x.why || "")}${x.odds ? ` In the past 5 years, trades like this made money ${x.odds} out of 10 times.` : ""}</p>
    ${x.news.length ? `<ul class="news">${x.news.slice(0, 3).map(newsItem).join("")}</ul>` : ""}
  </details>`;
}

function suggestionCard(x) {
  const amt = x.amount;
  const qty = x.qty ?? Math.floor(amt / x.max_entry);
  const mine = myPositions().some((p) => p.symbol === x.symbol);
  return `<article class="card sugg">
    <div class="row"><div><h3>${esc(x.name)}</h3><div class="muted small">${esc(x.type || x.symbol)}</div></div>
      <span class="badge ${x.kind === "swing" ? "b-watch" : "b-new"}">${esc(x.horizon)}</span></div>
    <div class="do">${qty ? `Buy <b>${shares(qty)}</b>` : `<span class="small">One share costs more than ${inr(amt)}. Skip.</span>`}</div>
    ${qty ? `<div class="small">Pay up to <b>${px(x.max_entry)}</b> (about ${inr(qty * x.max_entry)}). Sell if it falls to <b>${px(x.stop)}</b>${x.kind === "long_term" ? " (rises as the price rises)" : ""}.</div>` : ""}
    ${x.checks ? `<div class="small up" style="margin-top:6px">${esc(x.checks)}</div>` : ""}
    ${qty ? `<div class="small" style="margin-top:6px">⏱ ${esc(holdLine(x))}</div>` : ""}
    ${expectBlock(x, qty)}
    ${qty ? orderDetails(x, qty) : ""}
    ${qty ? `<div class="btns">${mine ? `<span class="small up">✓ In My Stocks</span>`
      : `<button class="btn primary" data-mark="${esc(x.kind)}|${esc(x.symbol)}">✅ I bought this</button>`}</div>` : ""}
  </article>`;
}

// All long-term holdings together, very roughly
function roughGuide(list) {
  const longs = list.filter((x) => x.kind === "long_term" && expectColumns(x).length);
  if (!longs.length) return "";
  const q = (x) => x.qty ?? Math.floor(x.amount / x.max_entry);
  const invested = longs.reduce((a, x) => a + q(x) * x.price, 0);
  const at = (x, i) => { const c = expectColumns(x); return c[Math.min(i, c.length - 1)]; };
  const sum = (key, i) => longs.reduce((a, x) => a + (q(x) * x.price * at(x, i)[key]) / 100, 0);
  const rowFor = (i, label) => `<tr><td>${label}</td><td class="down">${moneySigned(sum("bad", i))}</td>
    <td><b class="${tone(sum("typ", i))}">${moneySigned(sum("typ", i))}</b></td><td class="up">${moneySigned(sum("good", i))}</td></tr>`;
  const two = longs.every((x) => expectColumns(x).length > 1);
  return `<div class="card small"><b>The long-term part together</b> (${inr(invested)} invested), very roughly:
    <div class="table-wrap"><table class="expect-table"><thead><tr><th></th><th>Bad case</th><th>Typical</th><th>Good case</th></tr></thead>
      <tbody>${rowFor(0, "After ~6 months")}${two ? rowFor(1, "After ~12 months") : ""}</tbody></table></div>
    <div class="muted">Holdings rarely all do badly or all do well at the same time, so the real range is usually narrower. Past results, not a promise.</div></div>`;
}

function renderSuggestions() {
  const el = $("#tab-today");
  const L = S.latest;
  if (!L) { el.innerHTML = setupHelp(); return; }
  const list = suggestions();
  const act = myPositions().map((p) => ({ p, s: positionStatus(p) })).filter((x) => x.s.action);
  let html = "";
  const ageDays = L.data_date ? (Date.now() - new Date(L.data_date + "T00:00:00Z").getTime()) / 864e5 : 0;
  if (ageDays > 4.5) html += `<div class="callout bad">These suggestions are from ${esc(fmtDate(L.data_date))} and may be out of date. Wait for the next update before buying.</div>`;
  if (!moneyIsSet()) html += planWelcome();
  else {
    const chips = [5000, 10000, 15000, 20000];
    html += `<div class="card amount-box"><div class="small muted">Money I have today</div>
      <div class="amount-row"><input id="amt-today" type="number" inputmode="numeric" min="1000" step="500" value="${tradeMoney()}" aria-label="Money I have today">
        <button class="btn primary" id="amt-save">Update</button></div>
      <div class="pill-row">${chips.map((v) => `<button data-money="${v}" class="${v === tradeMoney() ? "on" : ""}">${inr(v)}</button>`).join("")}</div></div>`;
  }
  if (act.length) {
    html += `<h2>⚠️ Sell</h2>` + act.map(({ p, s }) => `<div class="card st-card ${s.cls}"><div class="status-head">${s.icon} ${s.head}: ${esc(p.symbol)}</div><div class="small">${s.text}</div></div>`).join("");
  }
  const mp = moneyPlan();
  const nStocks = list.filter((x) => x.kind === "long_term").length;
  if (mp.protect) {
    html += `<div class="card plan-card"><h2 style="margin-top:0">Your ${inr(mp.money)} plan</h2>
      <div class="line"><span class="ic">🏦</span><div><b>${inr(mp.safe)}</b> in a bank FD or a liquid fund (for example LIQUIDBEES). Safe, about 6.5% a year.</div></div>
      <div class="line"><span class="ic">📈</span><div>${nStocks ? `<b>${inr(mp.stock)}</b> in the ${nStocks === 1 ? "stock below" : nStocks + " stocks below, from different sectors,"} held for months.`
        : mp.stock < MIN_PER_STOCK
          ? `This amount is small for shares (charges would eat the profit). Keep it all in the safe part. Shares make sense from about ${inr(Math.ceil(MIN_PER_STOCK * (SAFE_YIELD + MAX_STOCK_LOSS) / SAFE_YIELD / 500) * 500)}.`
          : `No suitable stock today: keep it all in the safe part for now.`}</div></div>
      <p class="small muted">Built so that even if every stock fell to its safety price, the safe part's interest would earn it back within a year.
        Shares are never 100% sure, but this keeps the worst case close to no loss.</p></div>`;
  }
  if (!mp.protect && list.length) {
    html += `<div class="card small"><b>Your ${inr(mp.money)}, spread over ${list.length} different ${list.length === 1 ? "holding" : "holdings"}:</b>
      ${list.map((x) => esc(x.name.replace(/ Ltd\.?$/, ""))).join(" · ")}.
      <div class="muted" style="margin-top:4px">Each stock passed all checks: strong business, rising price, no bad news, market OK.</div></div>`;
  }
  html += roughGuide(list);
  html += `<h2>Suggestions</h2>`;
  html += list.length ? list.map(suggestionCard).join("")
    : `<div class="card empty">🙏 No suggestions today. The app only suggests when the odds are clearly good.</div>`;
  const hasShort = list.some((x) => x.kind === "swing");
  if (!hasShort) {
    const why = (L.market?.regime ?? 1) !== 2 ? "this method buys only when the whole market is rising, and right now it isn't"
      : (L.plan?.buy || []).length ? `a short trade needs at least ${inr(MIN_SHORT_TERM)} (charges eat smaller ones)` : "no stock passed the checks today";
    html += `<div class="card small"><div class="line"><span class="ic">⚡</span><div><b>Short term (about 1 week):</b> none today, because ${why}.</div></div>
      <div class="line"><span class="ic">🚫</span><div><b>Same-day trading:</b> never. SEBI found about 7 in 10 people who trade within the day lose money.</div></div></div>`;
  }
  const until = L.data_date ? addBusinessDays(L.data_date, 3) : null;
  html += `<div class="card small"><div class="line"><span class="ic">🔄</span><div><b>Suggestions can change.</b> They are re-checked every weekday evening,
    so always use the latest list. Today's list is valid for 3 trading days${until ? ` (until ${esc(fmtDate(until))})` : ""}. If the price is above the "pay up to" price, skip it.</div></div></div>
    <p class="small muted center">Buy at the market opening (9:15 AM). Updated ${esc(ago(L.generated))}.</p>`;
  el.innerHTML = html;
  const ws = $("#w-money-save");
  if (ws) ws.onclick = () => saveMoney(+$("#w-money").value);
  const as = $("#amt-save");
  if (as) as.onclick = () => saveMoney(+$("#amt-today").value);
}

function openMarkDialog(kind, sym) {
  const x = suggestions().find((s) => s.kind === kind && s.symbol === sym);
  if (!x) return;
  const qty = x.qty ?? Math.max(1, Math.floor(x.amount / x.max_entry));
  const q = livePrice(sym);
  $("#dlg-body").innerHTML = `<h3>I bought ${esc(x.name)}</h3>
    <label class="field">How many shares?<input id="mq" type="number" inputmode="numeric" min="1" value="${qty}"></label>
    <label class="field">Price paid per share (₹)<input id="mp" type="number" inputmode="decimal" step="0.05" value="${(q ? q.price : x.price) || ""}"></label>
    <div class="btns"><button class="btn primary wide" id="msave">Save</button></div>`;
  $("#dlg").showModal();
  $("#msave").onclick = (e) => {
    e.preventDefault();
    const price = +$("#mp").value || x.price;
    const risk = x.price - x.stop;
    const my = myPositions();
    my.push({ id: sym + "-" + Date.now(), symbol: sym, name: x.name, kind: kind,
      strategy: x.strategy, strategy_name: x.strategy_name, planId: x.planId,
      qty: +$("#mq").value || qty, price,
      stop: kind === "swing" ? +(price - risk).toFixed(2) : x.stop, target: null,
      date: new Date().toISOString(), sellBy: kind === "swing" ? addDays(new Date().toISOString(), 16) : null,
      lastPrice: x.price });
    store.set(LS.my, my);
    $("#dlg").close();
    renderAll();
  };
}

function renderRecoNews() {
  const L = S.latest || {};
  const syms = new Map();
  for (const x of suggestions()) syms.set(x.symbol, { name: x.name, news: x.news });
  for (const p of myPositions()) {
    if (syms.has(p.symbol)) continue;
    const src = [...(L.swing || []), ...(L.long_term_safe || []), ...(L.long_term?.picks || [])].find((x) => x.symbol === p.symbol);
    syms.set(p.symbol, { name: p.name || p.symbol, news: src?.news || [] });
  }
  const blocks = [...syms.entries()].map(([sym, v]) => `<div class="card"><h3>${esc(v.name)} <span class="muted small">${esc(sym)}</span></h3>
    ${v.news.length ? `<ul class="news">${v.news.map(newsItem).join("")}</ul>` : `<p class="small muted">No recent news.</p>`}</div>`).join("");
  $("#tab-news").innerHTML = `<h2>News</h2><p class="small muted">Only for the suggested stocks and the stocks you own.</p>
    ${blocks || `<div class="card empty">No suggestions or stocks yet, so no news.</div>`}`;
}

// ------------------------------------------------------------------ wiring
const MAIN_TABS = ["today", "mystocks", "news", "more"];
const TITLES = { help: "How to use", record: "Is it working?", swing: "All stock ideas", longterm: "Long-term details",
  market: "Market details", news: "Market news", settings: "Settings", mystocks: "My Stocks",
  invest: "Long-term investing", ideas: "All buy ideas", plan: "Daily plan", marketnews: "Market news",
  live: "Live Intelligence", advanced: "Advanced" };

function renderAll() {
  renderStatus();
  renderSuggestions();
  renderPlan();
  renderHome();
  renderMyStocks();
  renderInvest();
  renderMore();
  renderHelp();
  renderLiveDashboard();
  renderMarket();
  renderSwing();
  renderLongTerm();
  if (S.bt || $("#tab-record").classList.contains("active")) renderRecord();
  renderNews();
  renderRecoNews();
  if (!$("#tab-settings").classList.contains("active")) renderSettings();
  $("#disclaimer").textContent = S.latest?.disclaimer || "";
}

function showTab(name) {
  if (!$("#tab-" + name)) name = "today";
  const main = MAIN_TABS.includes(name) ? name : "more";
  document.querySelectorAll(".bottomnav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === main));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  const back = $("#back");
  back.hidden = MAIN_TABS.includes(name);
  $("#page-title").textContent = MAIN_TABS.includes(name) ? "" : (TITLES[name] || "");
  $(".brand").hidden = !back.hidden;
  $("#status").hidden = !back.hidden;
  store.set(LS.tab, name);
  if (name === "record") loadResults();
  if (name === "settings") renderSettings();
  window.scrollTo({ top: 0 });
}

function findPick(sym, strat) {
  const L = S.latest;
  if (strat === "lt") return (L.long_term.picks || []).find((p) => p.symbol === sym);
  return (L.swing || []).find((p) => p.symbol === sym && p.strategy === strat);
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-tab],[data-goto],[data-filter],[data-buy],[data-sell],[data-ltinfo],[data-sold],[data-preset],[data-detail],[data-money],[data-mark],[data-refresh-live]");
  if (!t) return;
  if (t.hasAttribute("data-refresh-live")) { refreshLive(); return; }
  if (t.dataset.preset) { saveCapital(+t.dataset.preset); return; }
  if (t.dataset.money) { saveMoney(+t.dataset.money); return; }
  if (t.dataset.mark) { const [k, sym] = t.dataset.mark.split("|"); openMarkDialog(k, sym); return; }
  if (t.dataset.detail) { const [sym, strat] = t.dataset.detail.split("|"); openDetail(sym, strat); return; }
  if (t.dataset.sold) {
    if (confirm("Remove this stock from My Stocks? (Do this after you have sold it.)")) {
      store.set(LS.my, myPositions().filter((p) => p.id !== t.dataset.sold));
      renderAll();
    }
    return;
  }
  if (t.dataset.tab) showTab(t.dataset.tab);
  else if (t.dataset.goto) showTab(t.dataset.goto);
  else if (t.dataset.filter) { S.swingFilter = t.dataset.filter; renderSwing(); }
  else if (t.dataset.buy) {
    const [sym, strat] = t.dataset.buy.split("|");
    const pk = findPick(sym, strat);
    if (pk) { if ($("#dlg").open) $("#dlg").close(); openBuyDialog(pk); }
  } else if (t.dataset.sell) {
    store.set(LS.my, myPositions().filter((p) => p.id !== t.dataset.sell));
    renderAll();
  } else if (t.dataset.ltinfo) openLtInfo(t.dataset.ltinfo);
});

document.addEventListener("visibilitychange", () => { if (!document.hidden) loadAll(); });

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
showTab((location.hash && location.hash.slice(1)) || store.get(LS.tab, "today"));
loadAll();
setInterval(refreshLive, 60 * 1000);          // live prices (updated by the server every ~15 min)
setInterval(loadAll, 10 * 60 * 1000);         // full data
