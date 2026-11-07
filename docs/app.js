/* Stock Finder India - client app (no build step, no dependencies).
 * Reads JSON produced by the engine (GitHub Actions) from ./data/ and does all
 * personal calculations (position size, P&L) on the device. Nothing personal
 * ever leaves the phone/laptop: settings and "my positions" live in localStorage. */
"use strict";

const DATA = "data/";
const LS = { settings: "sf.settings", my: "sf.my", seen: "sf.seenIdeas", alerted: "sf.alerted", tab: "sf.tab" };
const DEFAULTS = { tradeMoney: 20000, capital: 500000, ltPct: 70, swingPct: 30, riskPct: 1, maxPosPct: 20, notify: false };
const S = { latest: null, live: null, news: null, bt: null, track: null, swingFilter: "buy" };

// ------------------------------------------------------------------ storage (safe)
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const settings = () => ({ ...DEFAULTS, ...store.get(LS.settings, {}) });
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

function livePrice(sym) {
  const q = S.live && S.live.quotes && S.live.quotes[sym];
  return q ? q : null;
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

async function loadAll() {
  try {
    const [latest, live, news] = await Promise.allSettled([getJSON("latest.json"), getJSON("live.json"), getJSON("market_news.json")]);
    if (latest.status === "fulfilled") S.latest = latest.value;
    if (live.status === "fulfilled") S.live = live.value;
    if (news.status === "fulfilled") S.news = news.value;
  } catch (e) { console.warn(e); }
  renderAll();
  checkAlerts();
}

async function refreshLive() {
  try { S.live = await getJSON("live.json"); renderAll(); checkAlerts(); } catch { /* offline */ }
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
  const liveOn = S.live && S.live.market_open;
  const upd = liveOn ? S.live.updated : L.generated;
  el.innerHTML = `<span class="dot ${liveOn ? "live" : ""}"></span>${liveOn ? "Market open · prices" : "Market closed · data"} ${esc(ago(upd))}`;
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

  html += alertsBlock();
  html += myPositionsBlock();

  html += `<h2>Today's swing ideas</h2>`;
  if (!buys.length) {
    const reason = m.regime === 0 ? "The market is in a downtrend, so the system is not suggesting new swing trades. Protecting capital comes first."
      : "No setup met all the quality rules today. That's normal: on many days the best trade is no trade.";
    html += `<div class="card empty">😌 ${reason}<br><br><button class="btn small" data-goto="swing">See watch-list ideas</button></div>`;
  } else {
    html += `<p class="muted small">Buy at the next market open, only within the buy range. Place the stop-loss right after buying.</p><div class="grid">${buys.map(swingCard).join("")}</div>`;
  }

  html += `<h2>Long-term portfolio</h2><div class="card">`;
  const ch = lt.changes || {};
  if (ch.added && ch.added.length && ch.removed && ch.removed.length) {
    html += `<p><b>Changes this month:</b> add ${ch.added.map(esc).join(", ")}; sell ${ch.removed.map(esc).join(", ")}.</p>`;
  }
  html += `<p>${(lt.picks || []).length} stocks, equal amounts. ${esc(lt.deployment || "")}</p>
    <p class="muted small">Next review: ${esc(fmtDate(lt.next_rebalance))}</p>
    <button class="btn primary" data-goto="longterm">Open portfolio plan</button></div>`;

  html += trackMini();
  el.innerHTML = html;
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
    my.push({
      id: pk.symbol + "-" + Date.now(), symbol: pk.symbol, kind: pk.kind, strategy_name: pk.strategy_name,
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
  if (lt.curve) {
    html += `<h2>Backtest: long-term portfolio</h2><div class="card">${lineChart(lt.curve)}
      <div class="table-wrap"><table><thead><tr><th></th><th class="num">Yearly return</th><th class="num">Worst fall</th><th class="num">Months beating Nifty</th></tr></thead><tbody>
      <tr><td>Model portfolio</td><td class="num up">${pct(lt.cagr)}</td><td class="num down">${pct(lt.max_drawdown)}</td><td class="num">${pct(lt.pct_months_beat_nifty, 0, false)}</td></tr>
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
  const el = $("#tab-news");
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
  $("#s-money-save").onclick = () => { saveMoney(+$("#s-money").value); renderSettings(); $("#s-money-save").textContent = "Saved ✓"; };
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
  const now = latestPrice(p.symbol) ?? p.lastPrice ?? p.price;
  const sell = (L.sell_today || []).find((x) => x.symbol === p.symbol);
  const removed = p.kind === "long_term" && (L.long_term?.changes?.removed || []).includes(p.symbol);
  const plAmt = now != null ? (now - p.price) * p.qty : null;
  const plPct = now != null ? (now / p.price - 1) * 100 : null;
  let st;
  if (p.stop && now != null && now <= p.stop)
    st = { cls: "bad", icon: "🔴", head: "SELL NOW", text: `The price fell to your safety price (${px(p.stop)}). Selling now protects your money. Small losses are part of the plan.` };
  else if (p.target && now != null && now >= p.target)
    st = { cls: "good", icon: "🎉", head: "BOOK PROFIT", text: `It reached the goal price (${px(p.target)}). Sell now to lock in your profit.` };
  else if (sell && p.kind !== "long_term")
    st = { cls: "good", icon: "💰", head: "SELL at 9:15 AM", text: "The plan says this trade is finished. Sell when the market opens." };
  else if (removed)
    st = { cls: "warn", icon: "🔁", head: "SELL (monthly change)", text: "This stock has left the long-term list. Sell it and use the money for the new stocks in Invest." };
  else if (p.stop && now != null && (now - p.stop) / now < 0.02)
    st = { cls: "warn", icon: "🟡", head: "HOLD, but watch", text: `The price is close to your safety price (${px(p.stop)}).` };
  else
    st = { cls: "ok", icon: "🟢", head: "HOLD", text: "Nothing to do. The app will tell you when to sell." };
  return { ...st, now, plAmt, plPct, action: st.head !== "HOLD" && st.head !== "HOLD, but watch" };
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
      ${p.stop ? `<div class="small muted">🛡 Safety price ${px(p.stop)}${p.target ? ` · 🎯 Goal ${px(p.target)}` : ""}</div>` : ""}
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
  const items = [
    ["help", "❓", "How to use this app", "3 simple steps, and what the words mean"],
    ["mystocks", "💼", "My Stocks", "Track stocks you bought on your own"],
    ["invest", "🌱", "Long-term investing", "Strong companies to hold for years"],
    ["ideas", "💡", "All buy ideas", "Every idea, including riskier ones"],
    ["swing", "📈", "All stock ideas (detailed)", "Charts, technical numbers, news"],
    ["longterm", "🧾", "Long-term details", "Company numbers for each stock"],
    ["market", "🌐", "Market details", "Nifty chart and market health"],
    ["news", "📰", "Market news", "Latest headlines"],
    ["settings", "⚙️", "Settings", "My money, alerts, install on phone"],
  ];
  $("#tab-more").innerHTML = `<h2>More</h2><div class="menu">${items.map(([k, ic, t, d]) =>
    `<button class="menu-item" data-goto="${k}"><span class="ic">${ic}</span><span><b>${t}</b><small>${d}</small></span><span class="chev">›</span></button>`).join("")}</div>`;
}

function renderHelp() {
  $("#tab-help").innerHTML = `<h2>How to use this app</h2>
    <div class="card"><ol class="stepper">
      <li><b>Each evening after 6:15 PM</b> (or at 8:45 AM), look at <b>Today</b>, or at the message on your phone. It says exactly what to do: <b>SELL</b>, <b>BUY</b> or <b>HOLD</b>, and how many shares.</li>
      <li><b>At 9:15 AM</b>, do it in your broker app (Zerodha, Groww…). Buy only up to the price shown. If it's higher, don't buy.</li>
      <li><b>If the app says "SELL NOW"</b> during the day, sell. That's all. There's nothing to press in this app.</li>
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
  const presets = [10000, 15000, 20000];
  return `<div class="card welcome">
    <h2>Welcome 🙏</h2>
    <p class="big-q">How much money will you use for trading?</p>
    <div class="preset-row three">${presets.map((v) => `<button class="btn" data-money="${v}">${inr(v)}</button>`).join("")}</div>
    <label class="field">Or type an amount (₹)<input id="w-money" type="number" inputmode="numeric" min="2000" step="1000" placeholder="e.g. 12000"></label>
    <button class="btn primary wide" id="w-money-save">Save</button>
    <p class="small muted">The app uses it for ${slotsText()}. ${inr(20000)} works best: below about ${inr(SMALL_AMOUNT)},
      fixed broker charges take most of the profit. Use only money you won't need for daily life.</p>
  </div>`;
}

function saveMoney(v) {
  if (!v || v < 2000) { alert("Please enter at least ₹2,000."); return; }
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
    if (q && h.stop && q.price <= h.stop) out.push({ x: h, q, stop: h.stop });
  }
  for (const b of plan.buy) {
    const q = livePrice(b.symbol);
    if (q && q.open && q.date > b.signal_date && q.open <= b.max_entry && q.open > b.stop) {
      const dayStop = q.open - b.risk_per_share;
      if (q.price <= dayStop) out.push({ x: b, q, stop: dayStop });
    }
  }
  return out;
}

function renderPlan() {
  const el = $("#tab-today");
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

// ------------------------------------------------------------------ wiring
const MAIN_TABS = ["today", "record", "more"];
const TITLES = { help: "How to use", record: "Is it working?", swing: "All stock ideas", longterm: "Long-term details",
  market: "Market details", news: "Market news", settings: "Settings", mystocks: "My Stocks",
  invest: "Long-term investing", ideas: "All buy ideas" };

function renderAll() {
  renderStatus();
  renderPlan();
  renderHome();
  renderMyStocks();
  renderInvest();
  renderMore();
  renderHelp();
  renderMarket();
  renderSwing();
  renderLongTerm();
  if (S.bt || $("#tab-record").classList.contains("active")) renderRecord();
  renderNews();
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
  $("#page-title").textContent = TITLES[name] || "";
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
  const t = e.target.closest("[data-tab],[data-goto],[data-filter],[data-buy],[data-sell],[data-ltinfo],[data-sold],[data-preset],[data-detail],[data-money]");
  if (!t) return;
  if (t.dataset.preset) { saveCapital(+t.dataset.preset); return; }
  if (t.dataset.money) { saveMoney(+t.dataset.money); return; }
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
