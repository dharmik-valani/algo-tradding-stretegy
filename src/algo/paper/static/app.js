const $ = (id) => document.getElementById(id);

const VIEW_META = {
  analytics: { title: "Analytics", subtitle: "Day / week / month and strategy performance" },
  execute: { title: "Execute", subtitle: "Day capital, live strategies, and paper fills" },
  reports: { title: "Reports", subtitle: "Trade / strategy / daily exports" },
  settings: { title: "Settings", subtitle: "Session mode and paper controls" },
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function money(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

/** Compact INR for desk columns: 1L, 5L, 10k, 2.5k, etc. */
function moneyShort(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 10000000) {
    const cr = abs / 10000000;
    return `${sign}${cr % 1 === 0 ? cr.toFixed(0) : cr.toFixed(1).replace(/\.0$/, "")}Cr`;
  }
  if (abs >= 100000) {
    const lakh = abs / 100000;
    return `${sign}${lakh % 1 === 0 ? lakh.toFixed(0) : lakh.toFixed(1).replace(/\.0$/, "")}L`;
  }
  if (abs >= 1000) {
    const k = abs / 1000;
    return `${sign}${k % 1 === 0 ? k.toFixed(0) : k.toFixed(1).replace(/\.0$/, "")}k`;
  }
  return `${sign}${money(abs)}`;
}

/** Format ISO timestamps as Asia/Kolkata (IST) for the whole UI. */
function formatIst(iso, withSeconds = true) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return String(iso).replace("T", " ").replace(/\+.*$/, "").slice(0, 19);
  }
  const opts = {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  };
  if (withSeconds) opts.second = "2-digit";
  // en-GB → DD/MM/YYYY; rebuild as YYYY-MM-DD HH:mm:ss for journal readability
  const parts = new Intl.DateTimeFormat("en-GB", opts).formatToParts(d);
  const get = (t) => parts.find((p) => p.type === t)?.value || "";
  const date = `${get("year")}-${get("month")}-${get("day")}`;
  const time = withSeconds
    ? `${get("hour")}:${get("minute")}:${get("second")}`
    : `${get("hour")}:${get("minute")}`;
  return `${date} ${time}`;
}

/** Two-line IST stamp for tight journal columns (date / time). */
function formatIstStack(iso, withSeconds = true) {
  if (!iso) return "—";
  const full = formatIst(iso, withSeconds);
  if (!full || full === "—") return "—";
  const sp = full.lastIndexOf(" ");
  if (sp <= 0) return escapeHtml(full);
  const date = full.slice(0, sp);
  const time = full.slice(sp + 1);
  return `<span class="dt-stack"><span class="dt-date">${escapeHtml(date)}</span><span class="dt-time">${escapeHtml(time)}</span></span>`;
}

/** HH:mm (IST) for desk Entry/Exit cells. */
function formatIstTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    const m = String(iso).match(/(\d{2}:\d{2})/);
    return m ? m[1] : "";
  }
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (t) => parts.find((p) => p.type === t)?.value || "";
  return `${get("hour")}:${get("minute")}`;
}

function levelCell(value, hint, timeIso) {
  const time = formatIstTime(timeIso);
  const timeLine = time
    ? `<div class="meta pnl-sub" title="IST fill time">${escapeHtml(time)} IST</div>`
    : "";
  if (value == null || value === "" || value === "—") {
    return `<div>—</div>${hint ? `<div class="meta pnl-sub">${escapeHtml(hint)}</div>` : ""}${timeLine}`;
  }
  return `<div>${value}</div>${hint ? `<div class="meta pnl-sub">${escapeHtml(hint)}</div>` : ""}${timeLine}`;
}

function pct(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(1)}%`;
}

/** Win cell: rate + W/L counts when available. */
function winCell(a) {
  if (!a || (a.win_rate == null && a.wins == null && a.losses == null)) return "—";
  const wr = a.win_rate != null ? pct(a.win_rate) : "—";
  const w = Number(a.wins || 0);
  const l = Number(a.losses || 0);
  if (!w && !l && a.win_rate == null) return "—";
  const sub = w + l > 0 ? `${w}W/${l}L` : "";
  return sub
    ? `<div>${wr}</div><div class="meta pnl-sub" title="Wins / losses on closed trades (breakeven excluded)">${sub}</div>`
    : wr;
}

function capitalCell(allocated, used, { usedLabel = "used" } = {}) {
  const dep = used != null && !Number.isNaN(Number(used)) ? Number(used) : 0;
  if (dep > 0) {
    return `<div title="Capital in trades = qty × entry">₹${moneyShort(dep)}</div><div class="meta pnl-sub">${escapeHtml(usedLabel)}</div>`;
  }
  return `<div title="No capital in trades yet">₹0</div><div class="meta pnl-sub">${escapeHtml(usedLabel)}</div>`;
}

function pnlClass(n) {
  if (n > 0) return "pos";
  if (n < 0) return "neg";
  return "";
}

function isOrbId(id) {
  return id === "nifty_opt_orb" || id === "nifty_opt_orb_ce" || id === "nifty_opt_orb_pe";
}

function isZenId(id) {
  return id === "zen_credit_spread";
}

function isBasketId(id) {
  return (
    id === "nifty500_gainer_orb" ||
    id === "nifty500_loser_orb" ||
    id === "nifty500_gainer_orb_cash" ||
    id === "nifty500_loser_orb_cash" ||
    id === "nifty500_top_gainer_brk" ||
    id === "nifty500_top_loser_brk"
  );
}

let catalog = [];
let instruments = { indices: [], stocks: [], option_underlyings: [], timeframes: ["1m", "5m", "15m"] };
let lastSession = { strategies: [] };
let lastAnalytics = { strategies: [], overall: {} };
let lastAnalyticsBoard = null;
let analyticsBoardSeq = 0;
let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth() + 1;
let analyticsSelectSig = "";
let currentView = "analytics";

function setView(view) {
  if (view === "dashboard") view = "analytics";
  if (!VIEW_META[view]) view = "analytics";
  currentView = view;
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  const meta = VIEW_META[view] || VIEW_META.analytics;
  setText("viewTitle", meta.title);
  setText("viewSubtitle", meta.subtitle);
  if (location.hash !== `#${view}`) {
    history.replaceState(null, "", `#${view}`);
  }
  const scroller = document.querySelector(".main");
  if (scroller) scroller.scrollTop = 0;
  else window.scrollTo(0, 0);
  if (view === "analytics") refreshAnalyticsBoard();
  if (view === "reports") refreshReports();
  if (view === "settings") refreshDhanStatus();
}

async function boot() {
  catalog = await api("/api/strategies/catalog");
  instruments = await api("/api/instruments");
  const sel = $("strategyId");
  sel.innerHTML = catalog.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
  sel.addEventListener("change", onStrategyChange);
  $("assetKind").addEventListener("change", fillSymbols);
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
  const initial = location.hash.slice(1);
  if (initial && VIEW_META[initial]) currentView = initial;
  // Default Analytics to today so day KPIs match the market session.
  if ($("anFrom") && !$("anFrom").value) {
    const today = isoDay(new Date());
    $("anFrom").value = today;
    $("anTo").value = today;
  }
  renderRegistry();
  onStrategyChange();
  await refresh();
  setView(currentView);
}

function currentStrategy() {
  return catalog.find((x) => x.id === $("strategyId").value);
}

function onStrategyChange() {
  const s = currentStrategy();
  $("strategyHint").textContent = s ? s.description : "";
  const kinds = s?.asset_kinds || ["index", "stock", "option"];
  const ak = $("assetKind");
  [...ak.options].forEach((o) => {
    o.hidden = !kinds.includes(o.value);
  });
  if (!kinds.includes(ak.value)) ak.value = kinds[0];
  fillSymbols();
  renderParamFields(s);
  if (isOrbId(s?.id)) {
    $("timeframe").value = "1m";
    $("assetKind").value = "option";
    fillSymbols();
  }
  if (isZenId(s?.id)) {
    $("timeframe").value = "5m";
    $("assetKind").value = "index";
    fillSymbols();
    if ($("symbol")) $("symbol").value = "NIFTY";
  }
  if (isBasketId(s?.id)) {
    $("timeframe").value = "1m";
    $("assetKind").value = "stock";
    fillSymbols();
  }
  highlightRegistry(s?.id);
}

function fillSymbols() {
  const kind = $("assetKind").value;
  const sid = $("strategyId")?.value;
  let list = instruments.indices;
  if (kind === "stock") list = instruments.stocks;
  if (kind === "option") list = instruments.option_underlyings;
  if (isBasketId(sid)) {
    list = [{ symbol: "BASKET", name: "Multi-symbol basket" }];
  }
  $("symbol").innerHTML = (list || [])
    .map((x) => `<option value="${x.symbol}">${x.symbol} — ${x.name}</option>`)
    .join("");
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderParamFields(s) {
  const root = $("paramFields");
  const schema = s?.param_schema || [];
  const defaults = s?.default_params || {};
  if (!schema.length) {
    root.innerHTML = "";
    return;
  }
  root.innerHTML = schema
    .map((f) => {
      const val = defaults[f.key] ?? "";
      const tip = f.help || f.example || "";
      const tipAttr = tip ? ` title="${escapeHtml(f.help || f.example)}"` : "";
      const tipBtn = tip
        ? `<button type="button" class="tip" aria-label="Help"${tipAttr}>?</button>`
        : "";
      const example = f.example
        ? `<span class="field-example">${escapeHtml(f.example)}</span>`
        : "";
      const head = `<span class="field-label">${escapeHtml(f.label)}${tipBtn}</span>`;
      if (f.type === "select") {
        const opts = (f.options || [])
          .map((o) => `<option value="${o}" ${String(val) === String(o) ? "selected" : ""}>${o}</option>`)
          .join("");
        return `<label class="field">${head}<select data-param="${f.key}">${opts}</select>${example}</label>`;
      }
      const inputType = f.type === "time" ? "time" : "number";
      const step = f.step != null ? `step="${f.step}"` : f.type === "number" ? 'step="1"' : "";
      const min = f.min != null ? `min="${f.min}"` : "";
      const max = f.max != null ? `max="${f.max}"` : "";
      return `<label class="field">${head}<input data-param="${f.key}" type="${inputType}" value="${val}" ${step} ${min} ${max} />${example}</label>`;
    })
    .join("");
}

function collectParams() {
  const params = {};
  document.querySelectorAll("[data-param]").forEach((el) => {
    const key = el.getAttribute("data-param");
    let v = el.value;
    if (el.type === "number") v = v === "" ? null : Number(v);
    if (v === "true") v = true;
    if (v === "false") v = false;
    params[key] = v;
  });
  return params;
}

function deskCountFor(strategyId) {
  return (lastSession.strategies || []).filter((s) => s.strategy_id === strategyId).length;
}

function setText(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  if (cls !== undefined) el.className = cls;
}

function renderRegistry() {
  const root = $("registryCards");
  if (!root) return;
  setText("sumRegistered", String(catalog.length));
  setText("regCount", String(catalog.length));
  root.innerHTML = catalog
    .map((s) => {
      const onDesk = deskCountFor(s.id);
      const kinds = (s.asset_kinds || []).join(" · ");
      const side = s.default_params?.option_type;
      const tone = s.id.endsWith("_ce") || side === "CE" ? "ce" : s.id.endsWith("_pe") || side === "PE" ? "pe" : "";
      const defaults = Object.entries(s.default_params || {})
        .filter(([k]) => !["entry_start"].includes(k))
        .slice(0, 4)
        .map(([k, v]) => `${k}=${v}`)
        .join(" · ");
      const mark = (s.name || s.id || "?").slice(0, 2).toUpperCase();
      return `
      <article class="reg-card ${tone}" data-reg-id="${s.id}">
        <div class="reg-top">
          <div class="reg-identity">
            <span class="reg-mark ${tone}" aria-hidden="true">${escapeHtml(mark)}</span>
            <div class="reg-title-block">
              <h3>${escapeHtml(s.name)}</h3>
              <div class="meta">id: <code>${escapeHtml(s.id)}</code>${kinds ? ` · ${escapeHtml(kinds)}` : ""}</div>
            </div>
          </div>
          <span class="badge ${onDesk ? "on" : ""}">${onDesk ? `${onDesk} on desk` : "registered"}</span>
        </div>
        <p class="reg-desc">${escapeHtml(s.description || "")}</p>
        <div class="reg-chips">
          ${(defaults || "defaults ready")
            .split(" · ")
            .filter(Boolean)
            .slice(0, 4)
            .map((chip) => `<span class="chip">${escapeHtml(chip)}</span>`)
            .join("")}
        </div>
        <div class="actions reg-actions">
          <button type="button" data-reg-act="use" class="primary">Use</button>
          <button type="button" data-reg-act="add" class="ghost">Quick add</button>
        </div>
      </article>`;
    })
    .join("");

  root.querySelectorAll("[data-reg-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.closest(".reg-card").dataset.regId;
      const act = btn.dataset.regAct;
      if (act === "use") {
        openConfigureModal(id);
        return;
      }
      if (act === "add") {
        try {
          await quickAdd(id);
          await refresh();
          setView("execute");
        } catch (err) {
          alert(err.message);
        }
      }
    });
  });
  highlightRegistry($("strategyId").value);
}

function openConfigureModal(strategyId) {
  const s = catalog.find((x) => x.id === strategyId);
  if (!s) return;
  $("strategyId").value = strategyId;
  onStrategyChange();
  $("cfgTitle").textContent = `Configure · ${s.name}`;
  $("cfgSub").innerHTML = `<code>${escapeHtml(s.id)}</code>`;
  $("cfgEnable").checked = true;
  $("cfgStartLive").checked = false;
  const modal = $("configureModal");
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  setView("execute");
}

function closeConfigureModal() {
  const modal = $("configureModal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

function highlightRegistry(id) {
  document.querySelectorAll(".reg-card").forEach((el) => {
    el.classList.toggle("active", el.dataset.regId === id);
  });
}

async function quickAdd(strategyId) {
  const s = catalog.find((x) => x.id === strategyId);
  if (!s) throw new Error("Unknown strategy");
  const kind = (s.asset_kinds && s.asset_kinds[0]) || "index";
  let list = instruments.indices;
  if (kind === "stock") list = instruments.stocks;
  if (kind === "option") list = instruments.option_underlyings;
  const symbol = (list && list[0] && list[0].symbol) || "NIFTY";
  const timeframe = isOrbId(strategyId) ? "1m" : isBasketId(strategyId) ? "1m" : isZenId(strategyId) ? "5m" : "5m";
  await api("/api/session/strategies", {
    method: "POST",
    body: JSON.stringify({
      strategy_id: strategyId,
      symbol: isZenId(strategyId) ? "NIFTY" : isBasketId(strategyId) ? "BASKET" : symbol,
      asset_kind: isZenId(strategyId) ? "index" : isBasketId(strategyId) ? "stock" : kind,
      timeframe,
      quantity: 1,
      starting_cash: 500000,
      params: { ...(s.default_params || {}) },
      enabled: true,
    }),
  });
}

async function addOrbPair() {
  const data = await api("/api/session/strategies/orb-pair", { method: "POST" });
  if (data.orb_pair?.errors?.length && !data.orb_pair?.added?.length) {
    throw new Error(data.orb_pair.errors.join("; "));
  }
  return data;
}

function analyticsMap() {
  const map = {};
  for (const row of lastAnalytics.strategies || []) {
    map[row.instance_id] = row;
  }
  return map;
}

function renderExecDayKpis(session) {
  const strats = session.strategies || [];
  let capitalInUse = 0;
  let dayPnl = 0;
  let enabled = 0;
  for (const s of strats) {
    const tv = s.trade_view || {};
    const used = Number(
      tv.deployed_day != null
        ? tv.deployed_day
        : tv.deployed != null
          ? tv.deployed
          : tv.notional || 0
    ) || 0;
    const dp = Number(
      tv.day_pnl != null ? tv.day_pnl : (s.realized_pnl || 0) + (s.unrealized_pnl || 0)
    );
    capitalInUse += used;
    dayPnl += dp;
    if (s.enabled) enabled += 1;
  }
  const overall = lastAnalytics.overall || {};
  setText("execInvested", `₹${money(capitalInUse)}`);
  setText("execDayPnl", `₹${money(dayPnl)}`, pnlClass(dayPnl));
  setText("execWinRate", pct(overall.win_rate));
  setText("execDeskLine", `${enabled} / ${strats.length}`);
  setText("deskCount", String(strats.length));
}

function renderSummary(session) {
  renderExecDayKpis(session);
}

function analyticsQuery() {
  const from = $("anFrom")?.value || "";
  const to = $("anTo")?.value || "";
  const strat = ($("anStrategy")?.value || "").trim();
  const q = new URLSearchParams();
  if (from) q.set("from_date", from);
  if (to) q.set("to_date", to);
  if (strat) q.set("strategy_id", strat);
  return q.toString();
}

function moneyOrDash(n, prefix = "₹") {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return `${prefix}${money(n)}`;
}

function fillAnalyticsStrategySelect(byStrategy) {
  const sel = $("anStrategy");
  if (!sel) return;
  const ranked = (byStrategy || []).slice().sort((a, b) => (b.net_pnl || 0) - (a.net_pnl || 0));
  const seen = new Set(ranked.map((r) => r.strategy_id));
  const extras = (catalog || [])
    .map((c) => c.id)
    .filter((id) => id && !seen.has(id));
  const sig = [
    ...ranked.map((r) => `${r.strategy_id}:${r.net_pnl}:${r.win_rate}`),
    ...extras,
  ].join("|");
  const current = sel.value;
  // Rebuild only when ranking/options change — avoids select flicker + change loops.
  if (sig === analyticsSelectSig && sel.options.length > 1) {
    if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
    return;
  }
  analyticsSelectSig = sig;
  const opts = [`<option value="">All strategies</option>`];
  ranked.forEach((r, i) => {
    const label = `${i + 1}. ${r.strategy_id} · ₹${money(r.net_pnl || 0)} · ${pct(r.win_rate)}`;
    opts.push(`<option value="${escapeHtml(r.strategy_id)}">${escapeHtml(label)}</option>`);
  });
  extras.forEach((id) => {
    opts.push(`<option value="${escapeHtml(id)}">${escapeHtml(id)} (no closed trades)</option>`);
  });
  sel.innerHTML = opts.join("");
  if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
  else if (!current) sel.value = "";
}

function renderAnalyticsBoard(board) {
  lastAnalyticsBoard = board;
  const summary = board?.summary || {};
  const capital = board?.capital || {};
  const live = board?.live || {};
  const net = Number(summary.net_pnl || capital.net_pnl || 0);
  const balance = capital.total_balance != null ? capital.total_balance : capital.invested;
  const used = capital.used != null ? capital.used : null;
  setText("anBalance", moneyOrDash(balance));
  setText("anUsed", moneyOrDash(used));
  setText("anNet", moneyOrDash(net), pnlClass(net));
  setText("anWinRate", pct(summary.win_rate));
  setText("anPf", summary.profit_factor != null ? String(summary.profit_factor) : "—");
  setText("anExpectancy", moneyOrDash(summary.expectancy));
  setText("anAvgWin", moneyOrDash(summary.avg_win));
  setText("anAvgLoss", moneyOrDash(summary.avg_loss));
  setText("anAvgTrade", moneyOrDash(summary.avg_trade));
  setText("anMaxWin", moneyOrDash(summary.largest_win));
  setText("anMaxLoss", moneyOrDash(summary.largest_loss));
  setText("anStored", String(summary.trades || 0));
  setText("anClosedOpen", `${summary.closed || 0} / ${summary.open || 0}`);
  setText("anWinLoss", `${summary.wins || 0} / ${summary.losses || 0}`);
  const liveWr = live.win_rate != null ? live.win_rate : (lastAnalytics.overall || {}).win_rate;
  setText("anLiveWr", pct(liveWr));

  const byStrat = summary.by_strategy || [];
  const rank = board?.strategy_rank || byStrat;
  fillAnalyticsStrategySelect(rank);
  setText("anStratCount", String(byStrat.length));
  const sbody = $("anStrategyBody");
  if (sbody) {
    sbody.innerHTML = byStrat.length
      ? byStrat
          .map(
            (r, i) => `<tr class="an-strat-row">
          <td data-label="#">${i + 1}</td>
          <td data-label="Strategy"><strong>${escapeHtml(r.strategy_id)}</strong></td>
          <td data-label="Trades">${r.trades || 0}</td>
          <td data-label="Win rate">${pct(r.win_rate)}</td>
          <td data-label="Avg win" class="pos">${moneyOrDash(r.avg_win)}</td>
          <td data-label="Avg loss" class="neg">${moneyOrDash(r.avg_loss)}</td>
          <td data-label="PF">${r.profit_factor != null ? r.profit_factor : "—"}</td>
          <td data-label="Net PnL" class="${pnlClass(r.net_pnl)}"><strong>₹${money(r.net_pnl || 0)}</strong></td>
        </tr>`
          )
          .join("")
      : `<tr><td colspan="8" class="empty-cell">No closed trades in this range.</td></tr>`;
  }

  const days = board?.daily?.days || [];
  setText("anDayCount", String(days.length));
  const dbody = $("anDailyBody");
  if (dbody) {
    dbody.innerHTML = days.length
      ? days
          .map(
            (d) => `<tr>
          <td data-label="Date">${d.date}</td>
          <td data-label="Trades">${d.trades}</td>
          <td data-label="Closed">${d.closed}</td>
          <td data-label="Win rate">${pct(d.win_rate)}</td>
          <td data-label="Avg win" class="pos">${moneyOrDash(d.avg_win)}</td>
          <td data-label="Avg loss" class="neg">${moneyOrDash(d.avg_loss)}</td>
          <td data-label="Net PnL" class="${pnlClass(d.net_pnl)}">₹${money(d.net_pnl)}</td>
          <td data-label="Symbols">${(d.symbols || []).slice(0, 12).join(", ") || "—"}${(d.symbols || []).length > 12 ? "…" : ""}</td>
        </tr>`
          )
          .join("")
      : `<tr><td colspan="8" class="empty-cell">No daily data yet.</td></tr>`;
  }
}

function renderCalendar(cal) {
  if (!cal) return;
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  setText("calLabel", `${months[cal.month - 1]} ${cal.year}`);
  setText("calMonthPnl", `₹${money(cal.month_pnl || 0)}`, pnlClass(cal.month_pnl || 0));
  const calGrid = $("calGrid");
  if (!calGrid) return;
  calGrid.innerHTML = (cal.cells || [])
    .map((c) => {
      if (!c.date) return `<div class="cal-cell empty"></div>`;
      const day = c.date.slice(-2);
      const pnl = c.pnl;
      const cls = pnl == null ? "" : pnlClass(pnl);
      return `<div class="cal-cell ${cls}"><span class="d">${day}</span><strong>${pnl == null ? "—" : `₹${money(pnl)}`}</strong></div>`;
    })
    .join("");
}

async function refreshAnalyticsBoard() {
  const seq = ++analyticsBoardSeq;
  try {
    const q = analyticsQuery();
    const strat = ($("anStrategy")?.value || "").trim();
    const calQ = new URLSearchParams({ year: String(calYear), month: String(calMonth) });
    if (strat) calQ.set("strategy_id", strat);
    const [board, cal] = await Promise.all([
      api(`/api/analytics/board${q ? `?${q}` : ""}`),
      api(`/api/reports/calendar?${calQ.toString()}`),
    ]);
    if (seq !== analyticsBoardSeq) return;
    renderAnalyticsBoard(board);
    renderCalendar(cal);
  } catch (err) {
    if (seq !== analyticsBoardSeq) return;
    console.warn(err);
    setText("sessionMsg", err.message || String(err));
  }
}

function tickAnalyticsLive() {
  if (currentView !== "analytics") return;
  const overall = lastAnalytics.overall || {};
  setText("anLiveWr", pct(overall.win_rate));
}

function renderAnalytics() {
  /* no-op — board loads via filters / setView / Refresh */
}

let deskExpanded = new Set();

function renderExecStats(session) {
  const root = $("execDeskList");
  if (!root) return;
  const map = analyticsMap();
  const strats = session.strategies || [];
  if (!strats.length) {
    root.innerHTML = `<div class="empty">Desk is empty. Add a strategy below to start paper trading.</div>`;
    return;
  }
  const rows = strats
    .map((s) => {
      const id = s.instance_id || s.strategy_id;
      const a = map[id] || {};
      const realized = s.realized_pnl || 0;
      const unreal = s.unrealized_pnl || 0;
      const tv = s.trade_view || {};
      const invested = Number(tv.invested != null ? tv.invested : s.starting_cash) || 0;
      const dayPnl = Number(
        tv.day_pnl != null ? tv.day_pnl : realized + unreal
      );
      const total = tv.desk_settled ? dayPnl : realized + unreal;
      const basket = s.basket || [];
      const isBasket = basket.length > 0 || (s.params?.selected || []).length > 0;
      const active = basket.filter((l) => l.in_trade);
      const selected = (s.params?.selected || []).length || basket.length || 0;
      const inTrade = s.legs_in_trade != null ? s.legs_in_trade : active.length;
      const posQty = s.position?.quantity || 0;
      const isOpen = !!(inTrade || (posQty && !isBasket) || tv.in_trade);
      const settled = !!tv.desk_settled;
      const lc = tv.last_closed || null;
      const maxTrades = Math.max(
        1,
        Number(tv.max_trades_per_day || s.params?.max_trades_per_day || s.params?.top_n || 10) || 10
      );
      const tradesUsed = Math.max(
        0,
        Number(
          tv.trades_today_count != null
            ? tv.trades_today_count
            : basket.filter((l) => l.in_trade || Number(l.trades_today || 0) >= 1).length
        ) || 0
      );

      let stocks;
      let posTitle;
      let qtyCell;
      let qtyTitle;
      if (isBasket) {
        stocks = `${settled ? 0 : inTrade}<div class="meta pnl-sub">${tradesUsed}/${maxTrades} day</div>`;
        posTitle = `Open legs · day trades used/max (cap ${maxTrades}/strategy/day)`;
        const openQtySum = active.reduce((n, l) => n + Math.abs(Number(l.qty) || 0), 0);
        const doneQtySum = basket
          .filter((l) => !l.in_trade && Number(l.trades_today || 0) >= 1)
          .reduce((n, l) => n + Math.abs(Number(l.qty) || Number(l.filled_qty) || 0), 0);
        qtyCell =
          openQtySum > 0
            ? String(openQtySum)
            : doneQtySum > 0
              ? String(doneQtySum)
              : "—";
        qtyTitle = settled
          ? "Sum of share qty on today's closed legs (review until next session)"
          : "Sum of share qty on open legs (or last exits if flat)";
      } else {
        stocks = isOpen ? "1" : "0";
        posTitle = "Open trade count (0 or 1 — once per day)";
        const q =
          Math.abs(posQty) ||
          (isOpen ? Number(s.quantity) || 0 : 0) ||
          Number(tv.qty) ||
          (lc && lc.quantity ? Number(lc.quantity) : 0) ||
          0;
        qtyCell = q > 0 ? String(q) : "—";
        qtyTitle = "Position qty (open) or last exit qty";
      }

      // Market = live mark; after exit keep entry/exit/SL/TP for review (1 trade/day).
      let market = "—";
      let entry = "—";
      let exitPx = "—";
      let sl = "—";
      let tp = "—";
      let levelsHint = settled ? "settled day" : "no open trade";
      let entryAt = null;
      let exitAt = null;

      if (settled && isBasket) {
        // Keep today's basket review visible after EOD — expand legs for full detail.
        const doneLegs = basket.filter(
          (l) => Number(l.trades_today || 0) >= 1 || l.exit != null || l.entry != null
        );
        if (doneLegs.length === 1) {
          const d0 = doneLegs[0];
          market = d0.last != null ? money(d0.last) : d0.exit != null ? money(d0.exit) : "—";
          entry = d0.entry != null ? money(d0.entry) : "—";
          exitPx = d0.exit != null ? money(d0.exit) : "—";
          sl = d0.stop != null ? money(d0.stop) : "—";
          tp = d0.target != null ? money(d0.target) : "—";
          levelsHint = "settled";
          entryAt = d0.entry_at || null;
          exitAt = d0.exit_at || null;
        } else if (doneLegs.length > 1) {
          market = `${doneLegs.length} done`;
          entry = "multi";
          exitPx = "multi";
          sl = "multi";
          tp = "multi";
          levelsHint = "settled · expand for legs";
        } else {
          levelsHint = "settled · no fills";
        }
      } else if (settled && !isBasket && lc && (lc.exit != null || lc.entry != null)) {
        market = s.last_price != null ? money(s.last_price) : lc.exit != null ? money(lc.exit) : "—";
        entry = lc.entry != null ? money(lc.entry) : "—";
        exitPx = lc.exit != null ? money(lc.exit) : "—";
        sl = lc.stop != null ? money(lc.stop) : s.stop_price != null ? money(s.stop_price) : "—";
        tp = lc.target != null ? money(lc.target) : s.target_price != null ? money(s.target_price) : "—";
        levelsHint = "settled";
        entryAt = lc.entry_at || tv.entry_at || null;
        exitAt = lc.exit_at || tv.exit_at || null;
      } else if (settled) {
        levelsHint = "settled";
      } else if (isBasket) {
        if (active.length === 1) {
          market = money(active[0].last);
          entry = active[0].entry != null ? money(active[0].entry) : (active[0].avg != null ? money(active[0].avg) : "—");
          exitPx = "—";
          sl = active[0].stop != null ? money(active[0].stop) : "—";
          tp = active[0].target != null ? money(active[0].target) : "—";
          levelsHint = "open";
          entryAt = active[0].entry_at || null;
        } else if (active.length > 1) {
          market = `${active.length} open`;
          entry = "multi";
          exitPx = "—";
          sl = "multi";
          tp = "multi";
          levelsHint = "open · expand for legs";
        } else if (lc && (lc.exit != null || lc.entry != null)) {
          market = "—";
          entry = lc.entry != null ? money(lc.entry) : "—";
          exitPx = lc.exit != null ? money(lc.exit) : "—";
          sl = lc.stop != null ? money(lc.stop) : (s.stop_price != null ? money(s.stop_price) : "—");
          tp = lc.target != null ? money(lc.target) : (s.target_price != null ? money(s.target_price) : "—");
          levelsHint = "last exit";
          entryAt = lc.entry_at || tv.entry_at || null;
          exitAt = lc.exit_at || tv.exit_at || null;
        }
      } else if (isOpen && s.entry_price != null) {
        market = s.last_price != null ? money(s.last_price) : "—";
        entry = money(s.entry_price);
        exitPx = "—";
        sl = s.stop_price != null ? money(s.stop_price) : "—";
        tp = s.target_price != null ? money(s.target_price) : "—";
        levelsHint = "open";
        entryAt = tv.entry_at || null;
      } else if (lc && (lc.exit != null || lc.entry != null)) {
        market = s.last_price != null ? money(s.last_price) : "—";
        entry = lc.entry != null ? money(lc.entry) : (s.entry_price != null ? money(s.entry_price) : "—");
        exitPx = lc.exit != null ? money(lc.exit) : "—";
        sl =
          lc.stop != null
            ? money(lc.stop)
            : s.stop_price != null
              ? money(s.stop_price)
              : tv.stop != null
                ? money(tv.stop)
                : "—";
        tp =
          lc.target != null
            ? money(lc.target)
            : s.target_price != null
              ? money(s.target_price)
              : tv.target != null
                ? money(tv.target)
                : "—";
        levelsHint = "last exit";
        entryAt = lc.entry_at || tv.entry_at || null;
        exitAt = lc.exit_at || tv.exit_at || null;
      }

      const pnlSub = settled
        ? `settled day`
        : Math.abs(unreal) > 1e-9
          ? `R ${money(realized)} · U ${money(unreal)}`
          : Math.abs(realized) > 1e-9
            ? `realized`
            : `flat`;
      const nameSub = escapeHtml(
        [s.params?.option_type, s.timeframe, isBasket ? "basket" : s.instrument]
          .filter(Boolean)
          .join(" · ")
      );
      const status = s.enabled ? "run" : "paused";
      const toggleLabel = s.enabled ? "Pause" : "Resume";
      const toggleAct = s.enabled ? "pause" : "start";
      // Auto-expand baskets that already scanned / have open legs so qty rows are visible.
      if (isBasket && basket.length > 0 && (inTrade > 0 || selected > 0) && !deskExpanded.has(`seen:${id}`)) {
        deskExpanded.add(id);
        deskExpanded.add(`seen:${id}`);
      }
      const expanded = deskExpanded.has(id);
      const canExpand = isBasket && basket.length > 0;
      const expandBtn = canExpand
        ? `<button type="button" class="desk-expand" data-expand="${escapeHtml(id)}" aria-expanded="${expanded}" title="${expanded ? "Collapse legs" : "Expand legs"}">${expanded ? "▼" : "▶"}</button>`
        : "";

      const parent = `
      <tr class="click-row desk-row ${s.enabled ? "is-run" : "is-paused"} ${isOpen ? "has-open" : "is-flat"} ${expanded ? "is-expanded" : ""}" data-id="${escapeHtml(id)}" tabindex="0" role="button" aria-label="Open settings for ${escapeHtml(s.name)}">
        <td data-label="Strategy" class="col-strategy">
          <div class="row-title">
            ${expandBtn}
            <span class="badge ${s.enabled ? "on" : ""}">${status}</span>
            <span class="strat-name">${escapeHtml(s.name)}</span>
          </div>
          <div class="meta">${nameSub}${canExpand ? ` · max ${maxTrades}/day` : " · 1 trade/day"}</div>
        </td>
        <td data-label="Capital" class="mono">${capitalCell(
          invested,
          Number(
            tv.deployed_day != null
              ? tv.deployed_day
              : tv.deployed != null
                ? tv.deployed
                : tv.notional || 0
          ) || 0,
          { usedLabel: isOpen ? "in use" : "used" }
        )}</td>
        <td data-label="PnL" class="mono ${pnlClass(total)}" title="Day / live PnL · ₹${money(total)}">
          <div>₹${moneyShort(total)}</div>
          <div class="meta pnl-sub">${escapeHtml(pnlSub)}</div>
        </td>
        <td data-label="Market" class="mono" title="Live mark only while in trade">${market}</td>
        <td data-label="Entry" class="mono" title="Fill price when trade opens">
          ${levelCell(entry, levelsHint === "open" ? "" : levelsHint, entryAt)}
        </td>
        <td data-label="Exit" class="mono" title="Fill price when trade closes">
          ${levelCell(exitPx, levelsHint === "last exit" ? "last exit" : (isOpen ? "open" : ""), exitAt)}
        </td>
        <td data-label="Open" class="mono" title="${escapeHtml(posTitle)}">${stocks}</td>
        <td data-label="Qty" class="mono" title="${escapeHtml(qtyTitle)}">${qtyCell}</td>
        <td data-label="Stop" class="mono">${sl}</td>
        <td data-label="Target" class="mono">${tp}</td>
        <td data-label="Win" class="mono" title="Win rate = wins ÷ (wins + losses) on closed paper trades">${winCell(a)}</td>
        <td data-label="Actions" class="desk-actions-cell">
          <div class="desk-row-actions">
            <button type="button" data-act="${toggleAct}" class="desk-btn ${s.enabled ? "" : "primary"}">${toggleLabel}</button>
            <button type="button" data-act="remove" class="desk-btn ghost">Delete</button>
          </div>
        </td>
      </tr>`;

      if (!canExpand || !expanded) return parent;

      const childRows = basket
        .map((leg) => {
          const legOpen = !!leg.in_trade;
          const legCap = Number(leg.allocated != null ? leg.allocated : leg.capital != null ? leg.capital : invested / Math.max(basket.length, 1));
          const legUsed = Number(leg.notional != null ? leg.notional : leg.deployed) || 0;
          const legPnl = Number(leg.leg_pnl != null ? leg.leg_pnl : (legOpen ? leg.unrealized : leg.realized) || 0);
          const legMarket = leg.last != null ? money(leg.last) : "—";
          const legEntry =
            leg.entry != null
              ? money(leg.entry)
              : legOpen && leg.avg != null
                ? money(leg.avg)
                : "—";
          const legExit = !legOpen && leg.exit != null ? money(leg.exit) : "—";
          // Keep SL/TP/Qty visible after exit so the day's trade can be reviewed.
          const legSl = leg.stop != null ? money(leg.stop) : "—";
          const legTp = leg.target != null ? money(leg.target) : "—";
          const legQty = Math.abs(Number(leg.qty) || Number(leg.filled_qty) || Number(leg.planned_qty) || 0);
          const legQtyLabel = legQty > 0 ? String(legQty) : "—";
          const tradedToday = Number(leg.trades_today || 0) >= 1;
          const legWin =
            leg.leg_win === true ? "W" : leg.leg_win === false ? "L" : tradedToday ? "—" : "—";
          const legEntryHint = "";
          const canChart = !!(leg.entry != null || leg.exit != null || legOpen);
          const chartBtn = canChart
            ? `<button type="button" class="desk-btn ghost desk-chart-btn" data-chart-leg="1" title="Open trade chart">Chart</button>`
            : `<span class="meta">leg</span>`;
          const tradePayload = encodeURIComponent(
            JSON.stringify({
              symbol: leg.symbol || "",
              side: leg.side || "",
              entry: leg.entry,
              exit: leg.exit,
              stop: leg.stop,
              target: leg.target,
              entry_at: leg.entry_at || null,
              exit_at: leg.exit_at || null,
              qty: leg.qty,
              pnl: leg.leg_pnl,
              in_trade: legOpen,
              strategy: s.name,
            })
          );
          return `
          <tr class="desk-leg-row ${legOpen ? "has-open" : "is-flat"} ${canChart ? "is-chartable" : ""}" data-parent="${escapeHtml(id)}" data-trade="${tradePayload}">
            <td data-label="Symbol" class="col-strategy">
              <div class="row-title leg-indent">
                <span class="badge ${legOpen ? "on" : ""}">${legOpen ? "open" : tradedToday ? "done" : "wait"}</span>
                <span class="strat-name desk-leg-sym" title="Click for trade chart">${escapeHtml(leg.symbol || "")}</span>
              </div>
              <div class="meta">${escapeHtml(leg.status || "")}${leg.side ? ` · ${escapeHtml(String(leg.side))}` : ""}${tradedToday && !legOpen ? " · 1/day" : ""}</div>
            </td>
            <td data-label="Capital" class="mono">${capitalCell(legCap, legUsed, { usedLabel: legOpen ? "in use" : "used" })}</td>
            <td data-label="PnL" class="mono ${pnlClass(legPnl)}">₹${moneyShort(legPnl)}</td>
            <td data-label="Market" class="mono">${legMarket}</td>
            <td data-label="Entry" class="mono">${levelCell(legEntry, legEntryHint, leg.entry_at)}</td>
            <td data-label="Exit" class="mono">${levelCell(legExit, legOpen ? "open" : (leg.exit != null ? "exited" : ""), leg.exit_at)}</td>
            <td data-label="Open" class="mono" title="0 = flat, 1 = in trade">${legOpen ? "1" : "0"}</td>
            <td data-label="Qty" class="mono" title="Trade-wise share quantity">${legQtyLabel}</td>
            <td data-label="Stop" class="mono">${legSl}</td>
            <td data-label="Target" class="mono">${legTp}</td>
            <td data-label="Win" class="mono" title="This leg's closed trade result">${legWin}</td>
            <td data-label="Actions" class="desk-actions-cell">${chartBtn}</td>
          </tr>`;
        })
        .join("");

      return parent + childRows;
    })
    .join("");

  root.innerHTML = `
    <p class="hint desk-legend tight">
      ▶ Capital = money in trades (qty × entry). PnL = day result.
      Baskets: max ${10} trades/strategy/day — after that, no new entries. 1 trade/symbol/day.
      After flatten, today's entry/exit/qty/SL/TP/PnL stay visible until next session.
      Entry/Exit show fill time (IST). Click leg symbol or Chart for 1m SL/TP overlay.
      Win = wins÷(wins+losses) on closed trades.
    </p>
    <div class="table-wrap desk-table-wrap">
      <table class="data-table dense cards-on-mobile" id="execStatsTable">
        <thead>
          <tr>
            <th>Strategy</th>
            <th title="Capital currently in trades (qty × entry)">Capital</th>
            <th title="Live / day PnL">PnL</th>
            <th title="Live mark while in trade">Market</th>
            <th title="Entry fill">Entry</th>
            <th title="Exit fill">Exit</th>
            <th title="Open legs · day trades used / max (default 10)">Open</th>
            <th title="Share / lot quantity">Qty</th>
            <th>Stop</th>
            <th>Target</th>
            <th title="Win rate = wins ÷ (wins+losses); leg shows W/L for that trade">Win</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  root.querySelectorAll(".desk-expand").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const sid = btn.dataset.expand;
      if (!sid) return;
      if (deskExpanded.has(sid)) deskExpanded.delete(sid);
      else deskExpanded.add(sid);
      renderExecStats(lastSession);
    });
  });

  root.querySelectorAll(".desk-leg-row.is-chartable").forEach((row) => {
    const openChart = (e) => {
      if (e) e.stopPropagation();
      try {
        const raw = row.getAttribute("data-trade");
        if (!raw) return;
        openTradeChart(JSON.parse(decodeURIComponent(raw)));
      } catch (err) {
        alert(err.message || "Could not open chart");
      }
    };
    row.querySelector(".desk-chart-btn")?.addEventListener("click", openChart);
    row.querySelector(".desk-leg-sym")?.addEventListener("click", (e) => {
      e.stopPropagation();
      openChart(e);
    });
  });

  root.querySelectorAll(".desk-row").forEach((row) => {
    const id = row.dataset.id;
    const open = () => openStrategyModal(id);
    row.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      open();
    });
    row.addEventListener("keydown", (e) => {
      if (e.target !== row) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });
    row.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const act = btn.dataset.act;
        try {
          if (act === "remove") {
            if (!confirm("Delete this strategy from the desk?")) return;
            await api(`/api/session/strategies/${encodeURIComponent(id)}`, { method: "DELETE" });
          }
          if (act === "pause") {
            await api(`/api/session/strategies/${encodeURIComponent(id)}/pause`, { method: "POST" });
          }
          if (act === "start") {
            await api(`/api/session/strategies/${encodeURIComponent(id)}/start`, {
              method: "POST",
              body: JSON.stringify({ mode: "live", poll_seconds: Number($("poll")?.value) || 5 }),
            });
          }
          await refresh();
        } catch (err) {
          alert(err.message);
        }
      });
    });
  });
}

function renderDeskCards(rootId, session) {
  const root = $(rootId);
  if (!root) return;
  if (!session.strategies || !session.strategies.length) {
    root.innerHTML = `<div class="empty">Desk is empty. Add strategies on Execute — they stay saved after refresh.</div>`;
    return;
  }
  const map = analyticsMap();
  root.innerHTML = session.strategies
    .map((s) => {
      const total = (s.realized_pnl || 0) + (s.unrealized_pnl || 0);
      const id = s.instance_id || s.strategy_id;
      const a = map[id] || {};
      const status = s.enabled ? "run" : "paused";
      const toggleLabel = s.enabled ? "Pause" : "Resume";
      const toggleAct = s.enabled ? "pause" : "start";
      const sub = escapeHtml(
        [s.asset_kind || "index", s.instrument, s.timeframe].filter(Boolean).join(" · ")
      );
      return `
      <article class="exec-card ${s.enabled ? "is-run" : "is-paused"}" data-id="${id}">
        <button type="button" class="exec-card-main" data-act="open">
          <div class="exec-card-top">
            <div class="exec-card-title">
              <strong>${escapeHtml(s.name)}</strong>
              <span class="badge ${s.enabled ? "on" : ""}">${status}</span>
            </div>
            <span class="exec-card-chevron" aria-hidden="true">›</span>
          </div>
          <div class="meta exec-card-sub">${sub}</div>
          <div class="exec-kpi">
            <div class="exec-kpi-cell">
              <span>PnL</span>
              <strong class="${pnlClass(total)}">₹${money(total)}</strong>
            </div>
            <div class="exec-kpi-cell">
              <span>Signal</span>
              <strong>${escapeHtml(s.last_signal || "—")}</strong>
            </div>
            <div class="exec-kpi-cell">
              <span>Win%</span>
              <strong>${pct(a.win_rate)}</strong>
            </div>
            <div class="exec-kpi-cell">
              <span>Pos</span>
              <strong class="mono">${s.position?.quantity || 0}</strong>
            </div>
          </div>
        </button>
        <div class="exec-card-actions">
          <button type="button" data-act="${toggleAct}" class="${s.enabled ? "" : "primary"}">${toggleLabel}</button>
          <button type="button" data-act="remove" class="ghost">Delete</button>
        </div>
      </article>`;
    })
    .join("");

  root.querySelectorAll(".exec-card").forEach((card) => {
    const id = card.dataset.id;
    card.querySelector("[data-act=open]")?.addEventListener("click", () => openStrategyModal(id));
    card.querySelectorAll("button[data-act]").forEach((btn) => {
      if (btn.dataset.act === "open") return;
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const act = btn.dataset.act;
        try {
          if (act === "remove") {
            if (!confirm("Delete this strategy from the desk?")) return;
            await api(`/api/session/strategies/${encodeURIComponent(id)}`, { method: "DELETE" });
          }
          if (act === "pause") {
            await api(`/api/session/strategies/${encodeURIComponent(id)}/pause`, { method: "POST" });
          }
          if (act === "start") {
            await api(`/api/session/strategies/${encodeURIComponent(id)}/start`, {
              method: "POST",
              body: JSON.stringify({ mode: "live", poll_seconds: Number($("poll")?.value) || 5 }),
            });
          }
          await refresh();
        } catch (err) {
          alert(err.message);
        }
      });
    });
  });
}

let modalInstanceId = null;

function isOrbOptionId(id) {
  return id === "nifty_opt_orb" || id === "nifty_opt_orb_ce" || id === "nifty_opt_orb_pe";
}

function strategyHowItWorks(s) {
  const cat = catalog.find((c) => c.id === s.strategy_id);
  const base = (cat?.description || s.description || "").trim();
  if (!isOrbOptionId(s.strategy_id)) {
    return base;
  }
  const tv = s.trade_view || {};
  const p = s.params || {};
  const opt = String(tv.option_type || p.option_type || (s.strategy_id.endsWith("_pe") ? "PE" : "CE")).toUpperCase();
  const side = opt === "PE" ? "Put (PE)" : "Call (CE)";
  const open = p.session_open || tv.session_open || "09:15";
  const range = p.range_minutes ?? tv.range_minutes ?? 15;
  const stopPts = p.stop_points ?? tv.stop_points ?? 17;
  const targetPts = p.target_points ?? tv.target_points ?? 34;
  const hold = p.hold_minutes ?? tv.hold_minutes ?? 15;
  return (
    `${base}\n\n` +
    `Step by step: (1) From ${open} IST, watch ${side} premium for ${range} minutes and mark the high. ` +
    `(2) When premium breaks above that high → buy (paper fill at that premium). ` +
    `(3) While in trade: Stop = Entry − ${stopPts} pts, Target = Entry + ${targetPts} pts. ` +
    `(4) Also force-exit after ${hold} minutes if still open. ` +
    `Numbers on Market / Entry / Stop / Target are option premium ₹, not NIFTY index points.`
  );
}

function tradeLevelsHtml(s) {
  const tv = s.trade_view || {};
  const market = s.last_price ?? tv.market;
  const entry = s.entry_price ?? tv.entry;
  const stop = s.stop_price ?? tv.stop;
  const target = s.target_price ?? tv.target;
  const inTrade = !!(tv.in_trade || s.legs_in_trade || (s.position && s.position.quantity));
  const realized = s.realized_pnl ?? tv.realized_pnl ?? 0;
  const unreal = s.unrealized_pnl ?? tv.unrealized_pnl ?? 0;
  const lc = tv.last_closed;
  const opt = tv.option_type || s.params?.option_type || "";
  const strike = tv.strike || s.params?.strike;
  const spot = tv.spot;
  const rangeHigh = tv.range_high;
  const metaBits = [
    opt ? `${opt}` : null,
    strike ? `Strike ${strike}` : null,
    spot != null ? `Spot ₹${money(spot)}` : null,
    rangeHigh != null ? `Range high ₹${money(rangeHigh)}` : null,
  ].filter(Boolean);

  const showEntry = inTrade ? entry : lc?.entry;
  const showStop = inTrade ? stop : null;
  const showTarget = inTrade ? target : lc?.exit;
  const entryLabel = inTrade ? "Entry" : "Last entry";
  const targetLabel = inTrade ? "Target" : "Last exit";
  const stopLabel = inTrade ? "Stop-loss" : "Stop";

  return `
    <h3 class="modal-sec">Trade levels ${inTrade ? "(in trade)" : "(flat — no open position)"}</h3>
    ${metaBits.length ? `<p class="hint trade-meta">${escapeHtml(metaBits.join(" · "))}</p>` : ""}
    <div class="kv-grid" style="margin-bottom:0.55rem">
      <div class="kv"><div class="kv-k">Realized PnL</div><div class="kv-v ${pnlClass(realized)}">₹${money(realized)}</div></div>
      <div class="kv"><div class="kv-k">Unrealized PnL</div><div class="kv-v ${pnlClass(unreal)}">₹${money(unreal)}</div></div>
      <div class="kv"><div class="kv-k">Closed trades</div><div class="kv-v">${tv.closed_count != null ? tv.closed_count : (lc ? "≥1" : "0")}</div></div>
    </div>
    <div class="levels-grid">
      <div class="level-card">
        <div class="kv-k">Market now</div>
        <div class="kv-v level-val">₹${market != null ? money(market) : "—"}</div>
        <div class="level-hint">Current mark</div>
      </div>
      <div class="level-card ${showEntry != null ? "is-active" : ""}">
        <div class="kv-k">${entryLabel}</div>
        <div class="kv-v level-val">₹${showEntry != null ? money(showEntry) : "—"}</div>
        <div class="level-hint">${inTrade ? "Price you bought at" : "From last closed trade"}</div>
      </div>
      <div class="level-card level-stop">
        <div class="kv-k">${stopLabel}</div>
        <div class="kv-v level-val">${inTrade && showStop != null ? `₹${money(showStop)}` : "flat"}</div>
        <div class="level-hint">${inTrade ? "Exit if price hits this" : "Only while a trade is open"}</div>
      </div>
      <div class="level-card level-target">
        <div class="kv-k">${targetLabel}</div>
        <div class="kv-v level-val">₹${showTarget != null ? money(showTarget) : "—"}</div>
        <div class="level-hint">${inTrade ? "Take-profit level" : "Exit of last closed trade"}</div>
      </div>
    </div>
    ${
      inTrade && entry != null && stop != null && target != null
        ? `<p class="hint levels-explain">Open from <strong>₹${money(entry)}</strong>. Target <strong>₹${money(target)}</strong>, stop <strong>₹${money(stop)}</strong>. Market <strong>₹${market != null ? money(market) : "—"}</strong>.</p>`
        : `<p class="hint levels-explain">No open position (Pos = 0), so live Entry/Stop/Target are cleared. PnL above is from <strong>closed</strong> trades${lc ? ` (last: entry ₹${money(lc.entry)} → exit ₹${money(lc.exit)}, P&amp;L ₹${money(lc.pnl)})` : ""}.</p>`
    }
  `;
}

function openStrategyModal(instanceId) {
  const s = (lastSession.strategies || []).find((x) => (x.instance_id || x.strategy_id) === instanceId);
  if (!s) return;
  modalInstanceId = instanceId;
  const modal = $("strategyModal");
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  $("modalTitle").textContent = s.name;
  $("modalSub").innerHTML = `<code>${instanceId}</code> · ${s.enabled ? "enabled" : "paused"} · signal ${s.last_signal}`;
  const startBtn = $("modalStart");
  const pauseBtn = $("modalPause");
  if (startBtn && pauseBtn) {
    if (s.enabled) {
      startBtn.classList.add("hidden");
      pauseBtn.classList.remove("hidden");
      pauseBtn.textContent = "Pause";
    } else {
      startBtn.classList.remove("hidden");
      startBtn.textContent = "Resume";
      pauseBtn.classList.add("hidden");
    }
  }
  const cat = catalog.find((c) => c.id === s.strategy_id);
  const schema = cat?.param_schema || [];
  const params = s.params || {};
  const skip = new Set(["selected", "selection", "legs"]);
  const rows = Object.entries(params)
    .filter(([k, v]) => !skip.has(k) && (v == null || typeof v !== "object"))
    .map(([k, v]) => {
      const field = schema.find((f) => f.key === k);
      const label = field?.label || k;
      return `<div class="kv compact">
        <div class="kv-k">${escapeHtml(label)}</div>
        <div class="kv-v">${escapeHtml(String(v))}</div>
      </div>`;
    })
    .join("");
  const legs = (s.basket || params.legs || [])
    .map((leg) => {
      const status = leg.status || (leg.in_trade ? "in trade" : "waiting");
      const range =
        leg.range_low != null && leg.range_high != null
          ? `${money(leg.range_low)}–${money(leg.range_high)}`
          : "—";
      return `<tr class="${leg.in_trade ? "leg-active" : "leg-wait"}">
        <td data-label="Symbol">${escapeHtml(leg.symbol || "")}</td>
        <td data-label="Qty">${leg.qty}</td>
        <td data-label="Market">${money(leg.last)}</td>
        <td data-label="Entry">${leg.avg != null ? money(leg.avg) : "—"}</td>
        <td data-label="In trade">${leg.in_trade ? "yes" : "no"}</td>
        <td data-label="Range">${range}</td>
        <td data-label="Status" class="leg-status">${escapeHtml(status)}</td>
        <td data-label="Stop">${leg.stop != null ? money(leg.stop) : "—"}</td>
        <td data-label="Target">${leg.target != null ? money(leg.target) : "—"}</td></tr>`;
    })
    .join("");
  const logs = (s.logs || []).slice(-15).join("\n") || "—";
  const total = (s.realized_pnl || 0) + (s.unrealized_pnl || 0);
  const how = strategyHowItWorks(s);
  $("modalBody").innerHTML = `
    <div class="how-box">
      <div class="kv-k">How this strategy works</div>
      <p class="how-text">${escapeHtml(how)}</p>
    </div>
    ${tradeLevelsHtml(s)}
    <div class="kv-grid">
      <div class="kv"><div class="kv-k">Asset</div><div class="kv-v">${s.asset_kind} · ${s.instrument} · ${s.timeframe}</div></div>
      <div class="kv"><div class="kv-k">Qty / Cash</div><div class="kv-v">${s.quantity} · ₹${money(s.cash)}</div></div>
      <div class="kv"><div class="kv-k">PnL</div><div class="kv-v ${pnlClass(total)}">₹${money(total)}</div></div>
    </div>
    ${legs ? `<h3 class="modal-sec">Basket legs</h3><div class="table-wrap"><table class="data-table cards-on-mobile"><thead><tr><th>Symbol</th><th>Qty</th><th>Market</th><th>Entry</th><th>In trade</th><th>Range</th><th>Status</th><th>Stop</th><th>Target</th></tr></thead><tbody>${legs}</tbody></table></div>` : ""}
    <h3 class="modal-sec">Configured options</h3>
    <div class="kv-grid options-grid">${rows || "<p class='hint'>No params</p>"}</div>
    <h3 class="modal-sec">Recent logs</h3>
    <pre class="logs">${escapeHtml(logs)}</pre>
  `;
}

function closeStrategyModal() {
  const modal = $("strategyModal");
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  modalInstanceId = null;
}


function reportQuery() {
  const from = $("rpFrom")?.value || "";
  const to = $("rpTo")?.value || "";
  const strat = ($("rpStrategy")?.value || "").trim();
  const kind = ($("rpReportType")?.value || "trades").trim();
  const q = new URLSearchParams();
  if (from) q.set("from_date", from);
  if (to) q.set("to_date", to);
  if (strat) q.set("strategy_id", strat);
  if (kind) q.set("kind", kind);
  return q;
}

function reportType() {
  return ($("rpReportType")?.value || "trades").trim();
}

function syncReportPanels() {
  const kind = reportType();
  const showTrades = kind === "trades" || kind === "full";
  const showStrategy = kind === "strategy" || kind === "full";
  const showDaily = kind === "daily" || kind === "full";
  const showBasket = kind === "basket" || kind === "full";
  const setHidden = (id, hidden) => {
    const el = $(id);
    if (el) el.hidden = !!hidden;
  };
  setHidden("rpPanelTrades", !showTrades);
  setHidden("rpPanelStrategy", !showStrategy);
  setHidden("rpPanelDaily", !showDaily);
  setHidden("rpPanelBasket", !showBasket);
}

function fillReportsStrategySelect(byStrategy) {
  const sel = $("rpStrategy");
  if (!sel || sel.tagName !== "SELECT") return;
  const current = sel.value;
  const ranked = (byStrategy || []).slice().sort((a, b) => (b.net_pnl || 0) - (a.net_pnl || 0));
  const seen = new Set(ranked.map((r) => r.strategy_id));
  const extras = (catalog || []).map((c) => c.id).filter((id) => id && !seen.has(id));
  const opts = [`<option value="">All strategies</option>`];
  ranked.forEach((r, i) => {
    opts.push(
      `<option value="${escapeHtml(r.strategy_id)}">${escapeHtml(`${i + 1}. ${r.strategy_id} · ₹${money(r.net_pnl || 0)}`)}</option>`
    );
  });
  extras.forEach((id) => {
    opts.push(`<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`);
  });
  sel.innerHTML = opts.join("");
  if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
}

async function refreshReports() {
  try {
    syncReportPanels();
    const q = reportQuery();
    const qStr = q.toString();
    const kind = reportType();
    const exportLink = $("btnExportCsv");
    const exportPdf = $("btnExportPdf");
    if (exportLink) exportLink.href = `/api/reports/export.csv?${qStr}`;
    if (exportPdf) {
      const pdfQ = new URLSearchParams(q);
      pdfQ.delete("kind");
      exportPdf.href = `/api/reports/export.pdf${pdfQ.toString() ? `?${pdfQ}` : ""}`;
    }
    const fetches = [
      api(`/api/reports/summary${qStr ? `?${qStr}` : ""}`),
    ];
    const selQ = new URLSearchParams();
    const from = $("rpFrom")?.value || "";
    const to = $("rpTo")?.value || "";
    const strat = ($("rpStrategy")?.value || "").trim();
    if (from) selQ.set("from_date", from);
    if (to) selQ.set("to_date", to);
    if (strat) selQ.set("strategy_id", strat);
    if (kind === "basket" || kind === "full") {
      fetches.push(api(`/api/reports/selections${selQ.toString() ? `?${selQ}` : ""}`));
    } else {
      fetches.push(Promise.resolve({ selections: [] }));
    }
    if (kind === "trades" || kind === "full") {
      fetches.push(api(`/api/reports/trades${qStr ? `?${qStr}` : ""}`));
    } else {
      fetches.push(Promise.resolve({ trades: [] }));
    }
    if (kind === "daily" || kind === "full") {
      fetches.push(api(`/api/reports/daily${qStr ? `?${qStr}` : ""}`));
    } else {
      fetches.push(Promise.resolve({ days: [] }));
    }
    const [summary, sels, trades, daily] = await Promise.all(fetches);
    fillReportsStrategySelect(summary.by_strategy || []);
    setText("rpTrades", String(summary.trades || 0));
    setText("rpClosed", String(summary.closed || 0));
    setText("rpWinRate", pct(summary.win_rate));
    setText("rpAvgWin", summary.avg_win != null ? `₹${money(summary.avg_win)}` : "—");
    setText("rpAvgLoss", summary.avg_loss != null ? `₹${money(summary.avg_loss)}` : "—");
    const net = $("rpNet");
    if (net) {
      net.textContent = `₹${money(summary.net_pnl || 0)}`;
      net.className = pnlClass(summary.net_pnl || 0);
    }

    const byStrat = summary.by_strategy || [];
    setText("rpStratCount", String(byStrat.length));
    const stratBody = $("reportStrategyBody");
    if (stratBody) {
      stratBody.innerHTML = byStrat.length
        ? byStrat
            .map(
              (r, i) => `<tr>
            <td data-label="#">${i + 1}</td>
            <td data-label="Strategy"><strong>${escapeHtml(r.strategy_id)}</strong></td>
            <td data-label="Trades">${r.trades || 0}</td>
            <td data-label="Win rate">${pct(r.win_rate)}</td>
            <td data-label="Avg win" class="pos">${r.avg_win != null ? `₹${money(r.avg_win)}` : "—"}</td>
            <td data-label="Avg loss" class="neg">${r.avg_loss != null ? `₹${money(r.avg_loss)}` : "—"}</td>
            <td data-label="PF">${r.profit_factor != null ? r.profit_factor : "—"}</td>
            <td data-label="Net PnL" class="${pnlClass(r.net_pnl)}"><strong>₹${money(r.net_pnl || 0)}</strong></td>
          </tr>`
            )
            .join("")
        : `<tr><td colspan="8" class="empty-cell">No strategy data yet.</td></tr>`;
    }

    const dbody = $("reportDailyBody");
    const days = daily.days || [];
    setText("rpDayCount", String(days.length));
    if (dbody) {
      dbody.innerHTML = days.length
        ? days
            .map(
              (d) => `<tr>
          <td data-label="Date">${d.date}</td>
          <td data-label="Trades">${d.trades}</td>
          <td data-label="Closed">${d.closed}</td>
          <td data-label="Win rate">${pct(d.win_rate)}</td>
          <td data-label="Net PnL" class="${pnlClass(d.net_pnl)}">₹${money(d.net_pnl)}</td>
          <td data-label="Symbols">${(d.symbols || []).join(", ")}</td></tr>`
            )
            .join("")
        : `<tr><td colspan="6" class="empty-cell">No daily data yet.</td></tr>`;
    }

    const tbody = $("reportTradesBody");
    const list = trades.trades || [];
    const shown = list.slice(0, 150);
    const buys = list.filter((t) => /^(buy|long)$/i.test(String(t.side || ""))).length;
    const sells = list.filter((t) => /^(sell|short)$/i.test(String(t.side || ""))).length;
    setText("rpTradeCount", String(list.length));
    setText(
      "rpTradeBreakdown",
      list.length
        ? `${buys} buy · ${sells} sell${list.length > shown.length ? ` · showing ${shown.length}` : ""}`
        : "0 trades"
    );
    if (tbody) {
      tbody.innerHTML = list.length
        ? shown
            .map((t) => {
              const entry = formatIstStack(t.entry_at);
              const exit = t.exit_at ? formatIstStack(t.exit_at) : "—";
              const reason = t.exit_reason ? escapeHtml(t.exit_reason) : "";
              return `<tr>
            <td data-label="Entry" class="col-dt">${entry}</td>
            <td data-label="Exit" class="col-dt">${exit}</td>
            <td data-label="Strategy" class="col-strat-id">${escapeHtml(t.strategy_id || "")}</td>
            <td data-label="Symbol">${escapeHtml(t.symbol || "")}</td>
            <td data-label="Side">${escapeHtml(t.side || "")}</td>
            <td data-label="Qty" class="mono">${t.quantity != null ? t.quantity : "—"}</td>
            <td data-label="Entry ₹">${money(t.entry_price)}</td>
            <td data-label="Exit ₹">${t.exit_price != null ? money(t.exit_price) : "—"}</td>
            <td data-label="SL / TP">${t.stop_price != null ? money(t.stop_price) : "—"} / ${t.target_price != null ? money(t.target_price) : "—"}</td>
            <td data-label="PnL" class="${pnlClass(t.realized_pnl || 0)}">${t.realized_pnl != null ? `₹${money(t.realized_pnl)}` : "—"}</td>
            <td data-label="Status" class="col-status">
              <div class="status-main">${escapeHtml(t.status || "—")}</div>
              ${reason ? `<div class="status-sub">${reason}</div>` : ""}
            </td></tr>`;
            })
            .join("")
        : `<tr><td colspan="11" class="empty-cell">No stored trades yet.</td></tr>`;
    }

    const sbody = $("reportSelBody");
    const selsList = sels.selections || [];
    setText("rpSelCount", String(selsList.length));
    if (sbody) {
      sbody.innerHTML = selsList.length
        ? selsList
            .slice(0, 100)
            .map((r) => {
              const when = formatIstStack(r.selected_at);
              return `<tr>
            <td data-label="When" class="col-dt">${when}</td>
            <td data-label="Strategy">${escapeHtml(r.strategy_id || "")}</td>
            <td data-label="Symbol">${escapeHtml(r.symbol || "")}</td>
            <td data-label="Mode">${escapeHtml(r.mode || "")}</td>
            <td data-label="% chg">${r.pct_change != null ? Number(r.pct_change).toFixed(2) : "—"}</td>
            <td data-label="Open">${money(r.open)}</td>
            <td data-label="High">${money(r.high)}</td>
            <td data-label="Low">${money(r.low)}</td></tr>`;
            })
            .join("")
        : `<tr><td colspan="8" class="empty-cell">No basket selections stored yet.</td></tr>`;
    }
  } catch (err) {
    console.warn(err);
  }
}

function render(session) {
  lastSession = session;
  setText("sessionMsg", session.message || "");
  const pill = $("statusPill");
  if (pill) {
    pill.textContent = session.running ? `${session.mode} · running` : session.mode || "idle";
    pill.classList.toggle("running", !!session.running);
  }
  renderSummary(session);
  renderRegistry();
  renderExecStats(session);
  // Poll must not re-fetch journal Analytics/Reports boards (caused KPI flicker).
  tickAnalyticsLive();
}

async function refresh() {
  try {
    const [session, analytics] = await Promise.all([
      api("/api/session"),
      api("/api/analytics").catch(() => lastAnalytics || {}),
    ]);
    lastAnalytics = analytics;
    render(session);
  } catch (err) {
    $("sessionMsg").textContent = err.message || String(err);
  }
}

async function startSession() {
  await api("/api/session/start", {
    method: "POST",
    body: JSON.stringify({
      mode: $("mode").value,
      poll_seconds: Number($("poll").value),
      max_bars: 2500,
    }),
  });
  await refresh();
}

async function stopSession() {
  await api("/api/session/stop", { method: "POST" });
  await refresh();
}

$("btnAdd").addEventListener("click", async () => {
  try {
    const enabled = !!$("cfgEnable")?.checked;
    const startLive = !!$("cfgStartLive")?.checked;
    await api("/api/session/strategies", {
      method: "POST",
      body: JSON.stringify({
        strategy_id: $("strategyId").value,
        symbol: $("symbol").value,
        asset_kind: $("assetKind").value,
        timeframe: $("timeframe").value,
        quantity: Number($("qty").value),
        starting_cash: Number($("cash").value),
        params: collectParams(),
        enabled,
      }),
    });
    if (startLive && !lastSession.running) {
      await startSession();
    } else {
      await refresh();
    }
    closeConfigureModal();
    setView("execute");
  } catch (err) {
    alert(err.message);
  }
});

async function onOrbPair() {
  try {
    await addOrbPair();
    await refresh();
    setView("execute");
  } catch (err) {
    alert(err.message);
  }
}

$("btnOrbPair")?.addEventListener("click", onOrbPair);
$("btnOrbPair2")?.addEventListener("click", onOrbPair);
$("btnStart").addEventListener("click", () => startSession().catch((e) => alert(e.message)));
$("btnStart2").addEventListener("click", () => startSession().catch((e) => alert(e.message)));
$("btnStop").addEventListener("click", () => stopSession().catch((e) => alert(e.message)));
$("btnStop2").addEventListener("click", () => stopSession().catch((e) => alert(e.message)));
$("btnReset").addEventListener("click", async () => {
  if (!confirm("Reset paper session?")) return;
  await api("/api/session/reset", { method: "POST" });
  await refresh();
});

$("btnReloadDesk")?.addEventListener("click", async () => {
  if (
    !confirm(
      "Reload desk from shared database (Supabase)?\n\nThis replaces the strategies in this browser session with whatever Render / DB currently has. Do not leave laptop + Render both running live."
    )
  ) {
    return;
  }
  try {
    const data = await api("/api/session/reload", { method: "POST" });
    await refresh();
    alert(
      data.message ||
        `Reloaded ${data.strategies ?? 0} strategies from database.`
    );
  } catch (err) {
    alert(err.message || String(err));
  }
});

$("btnRefreshReports")?.addEventListener("click", () => refreshReports());
$("rpFrom")?.addEventListener("change", () => refreshReports());
$("rpTo")?.addEventListener("change", () => refreshReports());
$("rpStrategy")?.addEventListener("change", () => refreshReports());
$("rpReportType")?.addEventListener("change", () => refreshReports());

$("btnRefreshAnalytics")?.addEventListener("click", () => refreshAnalyticsBoard());
$("anFrom")?.addEventListener("change", () => refreshAnalyticsBoard());
$("anTo")?.addEventListener("change", () => refreshAnalyticsBoard());
$("anStrategy")?.addEventListener("change", () => refreshAnalyticsBoard());

function isoDay(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function applyDateRange(range, fromId, toId, onDone) {
  const to = new Date();
  const from = new Date();
  if (range === "today") {
    // from = to = today
  } else if (range === "week") {
    const day = to.getDay();
    const mondayOffset = day === 0 ? -6 : 1 - day;
    from.setDate(to.getDate() + mondayOffset);
  } else if (range === "7d") {
    from.setDate(to.getDate() - 6);
  } else if (range === "month") {
    from.setDate(1);
  } else if (range === "all") {
    if ($(fromId)) $(fromId).value = "";
    if ($(toId)) $(toId).value = "";
    onDone();
    return;
  }
  if ($(fromId)) $(fromId).value = isoDay(from);
  if ($(toId)) $(toId).value = isoDay(to);
  onDone();
}

document.querySelectorAll("[data-range]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyDateRange(btn.dataset.range, "rpFrom", "rpTo", () => refreshReports());
  });
});

document.querySelectorAll("[data-an-range]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyDateRange(btn.dataset.anRange, "anFrom", "anTo", () => refreshAnalyticsBoard());
  });
});

$("calPrev")?.addEventListener("click", () => {
  calMonth -= 1;
  if (calMonth < 1) {
    calMonth = 12;
    calYear -= 1;
  }
  if (currentView === "analytics") refreshAnalyticsBoard();
  else refreshReports();
});
$("calNext")?.addEventListener("click", () => {
  calMonth += 1;
  if (calMonth > 12) {
    calMonth = 1;
    calYear += 1;
  }
  if (currentView === "analytics") refreshAnalyticsBoard();
  else refreshReports();
});
document.querySelectorAll("[data-close-modal]").forEach((el) => {
  el.addEventListener("click", closeStrategyModal);
});
document.querySelectorAll("[data-close-configure]").forEach((el) => {
  el.addEventListener("click", closeConfigureModal);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("configureModal")?.classList.contains("hidden")) closeConfigureModal();
  else if (!$("strategyModal")?.classList.contains("hidden")) closeStrategyModal();
});
$("modalStart")?.addEventListener("click", async () => {
  if (!modalInstanceId) return;
  try {
    await api(`/api/session/strategies/${encodeURIComponent(modalInstanceId)}/start`, {
      method: "POST",
      body: JSON.stringify({ mode: "live", poll_seconds: Number($("poll").value) || 15 }),
    });
    await refresh();
    openStrategyModal(modalInstanceId);
  } catch (e) {
    alert(e.message);
  }
});
$("modalPause")?.addEventListener("click", async () => {
  if (!modalInstanceId) return;
  try {
    await api(`/api/session/strategies/${encodeURIComponent(modalInstanceId)}/pause`, { method: "POST" });
    await refresh();
    openStrategyModal(modalInstanceId);
  } catch (e) {
    alert(e.message);
  }
});
$("modalDelete")?.addEventListener("click", async () => {
  if (!modalInstanceId) return;
  if (!confirm("Delete this strategy from the desk?")) return;
  try {
    await api(`/api/session/strategies/${encodeURIComponent(modalInstanceId)}`, { method: "DELETE" });
    closeStrategyModal();
    await refresh();
  } catch (e) {
    alert(e.message);
  }
});

async function refreshDhanStatus() {
  const el = $("dhanStatus");
  if (!el) return;
  try {
    const s = await api("/api/dhan/status");
    const plan = s.profile?.dataPlan || "—";
    const valid = s.profile?.tokenValidity || s.expiry_time || "—";
    const left = s.seconds_left != null ? `${Math.round(s.seconds_left / 3600)}h left` : "unknown";
    const store = s.storage ? ` · store=${s.storage}` : "";
    let mode;
    if (s.renewable) {
      mode = s.auto_renew ? "auto RenewToken (SELF)" : "SELF · renewable";
    } else if (s.totp_configured) {
      mode = s.auto_renew ? "auto generateAccessToken (TOTP)" : "PARTNER · TOTP remint ready";
    } else {
      mode = "PARTNER · paste daily or add TOTP secret";
    }
    const note = s.renew_note ? ` · ${s.renew_note}` : "";
    const warn = s.warning ? ` · ${s.warning}` : "";
    if (s.ok) {
      el.textContent = `Connected · Data plan: ${plan} · Token valid: ${valid} (${left})${store} · ${mode}${note}`;
      el.style.color = "var(--accent)";
    } else {
      el.textContent = `Not connected · ${s.error || "paste a fresh access token"} · ${left}${store} · ${mode}${note}${warn}`;
      el.style.color = "var(--bad)";
    }
  } catch (err) {
    el.textContent = `Status error: ${err.message}`;
    el.style.color = "var(--bad)";
  }
}

$("btnDhanRefresh")?.addEventListener("click", () => refreshDhanStatus());
$("btnDhanSave")?.addEventListener("click", async () => {
  try {
    const token = $("dhanToken")?.value?.trim();
    if (!token) {
      alert("Paste an access token first");
      return;
    }
    await api("/api/dhan/token", { method: "POST", body: JSON.stringify({ access_token: token }) });
    $("dhanToken").value = "";
    await refreshDhanStatus();
    alert("Token saved. Restart live session to attach WebSocket feed.");
  } catch (err) {
    alert(err.message);
  }
});
$("btnDhanCreds")?.addEventListener("click", async () => {
  try {
    const pin = $("dhanPin")?.value?.trim() ?? "";
    const totp_secret = $("dhanTotp")?.value?.trim() ?? "";
    if (!pin && !totp_secret) {
      alert("Enter PIN and/or TOTP secret");
      return;
    }
    const body = {};
    if (pin) body.pin = pin;
    if (totp_secret) body.totp_secret = totp_secret;
    await api("/api/dhan/credentials", { method: "POST", body: JSON.stringify(body) });
    if ($("dhanPin")) $("dhanPin").value = "";
    if ($("dhanTotp")) $("dhanTotp").value = "";
    await refreshDhanStatus();
    alert("PIN/TOTP saved to .env. Use Generate via TOTP or Renew / remint now.");
  } catch (err) {
    alert(err.message);
  }
});
$("btnDhanGenerate")?.addEventListener("click", async () => {
  try {
    await api("/api/dhan/generate", { method: "POST", body: "{}" });
    await refreshDhanStatus();
    alert("New token minted via PIN+TOTP.");
  } catch (err) {
    alert(err.message);
  }
});
$("btnDhanRenew")?.addEventListener("click", async () => {
  try {
    await api("/api/dhan/renew", { method: "POST", body: "{}" });
    await refreshDhanStatus();
    alert("Token check done (renew only runs when <12h left).");
  } catch (err) {
    alert(err.message);
  }
});

async function openTradeChart(trade) {
  const modal = $("tradeChartModal");
  const canvas = $("tradeChartCanvas");
  const title = $("tradeChartTitle");
  const sub = $("tradeChartSub");
  const status = $("tradeChartStatus");
  if (!modal || !canvas) {
    alert("Chart UI missing — hard refresh the page.");
    return;
  }
  const sym = String(trade.symbol || "").toUpperCase();
  title.textContent = `${sym} · trade chart`;
  const side = trade.side || (trade.in_trade ? "open" : "closed");
  sub.textContent = [
    trade.strategy,
    side,
    trade.entry != null ? `entry ₹${money(trade.entry)}` : null,
    trade.exit != null ? `exit ₹${money(trade.exit)}` : null,
    formatIstTime(trade.entry_at) ? `@ ${formatIstTime(trade.entry_at)}` : null,
    formatIstTime(trade.exit_at) ? `→ ${formatIstTime(trade.exit_at)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  status.textContent = "Loading 1m bars…";
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");

  try {
    const data = await api(`/api/bars?symbol=${encodeURIComponent(sym)}&interval=1m&range=1d`);
    const bars = data.bars || [];
    if (!bars.length) {
      status.textContent = "No bars returned (feed/token may be cooling).";
      drawTradeChart(canvas, [], trade);
      return;
    }
    status.textContent = `${bars.length} × 1m bars · entry/exit/SL/TP overlay`;
    drawTradeChart(canvas, bars, trade);
  } catch (err) {
    status.textContent = err.message || "Failed to load bars";
    drawTradeChart(canvas, [], trade);
  }
}

function closeTradeChart() {
  const modal = $("tradeChartModal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

function drawTradeChart(canvas, bars, trade) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 720;
  const cssH = canvas.clientHeight || 360;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#0f1419";
  ctx.fillRect(0, 0, cssW, cssH);

  const pad = { l: 12, r: 64, t: 16, b: 28 };
  const plotW = cssW - pad.l - pad.r;
  const plotH = cssH - pad.t - pad.b;

  const levels = [trade.entry, trade.exit, trade.stop, trade.target]
    .map((x) => (x == null || Number.isNaN(Number(x)) ? null : Number(x)))
    .filter((x) => x != null);
  if (!bars.length && !levels.length) {
    ctx.fillStyle = "#8b98a5";
    ctx.font = "13px sans-serif";
    ctx.fillText("No chart data", pad.l, pad.t + 20);
    return;
  }

  let lo = levels.length ? Math.min(...levels) : Infinity;
  let hi = levels.length ? Math.max(...levels) : -Infinity;
  for (const b of bars) {
    lo = Math.min(lo, Number(b.l), Number(b.o), Number(b.c));
    hi = Math.max(hi, Number(b.h), Number(b.o), Number(b.c));
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) {
    lo = (levels[0] || 100) * 0.99;
    hi = (levels[0] || 100) * 1.01;
  }
  const padPx = (hi - lo) * 0.08 || 1;
  lo -= padPx;
  hi += padPx;
  const yOf = (px) => pad.t + ((hi - px) / (hi - lo)) * plotH;
  const n = Math.max(bars.length, 1);
  const slot = plotW / n;

  // Profit / loss zones (TradingView-style bands between entry↔target / entry↔stop).
  const entry = trade.entry != null ? Number(trade.entry) : null;
  const stop = trade.stop != null ? Number(trade.stop) : null;
  const target = trade.target != null ? Number(trade.target) : null;
  const isShort = String(trade.side || "").toLowerCase().includes("short");
  if (entry != null && target != null) {
    const y1 = yOf(entry);
    const y2 = yOf(target);
    ctx.fillStyle = "rgba(34, 197, 94, 0.12)";
    ctx.fillRect(pad.l, Math.min(y1, y2), plotW, Math.abs(y2 - y1));
  }
  if (entry != null && stop != null) {
    const y1 = yOf(entry);
    const y2 = yOf(stop);
    ctx.fillStyle = "rgba(239, 68, 68, 0.12)";
    ctx.fillRect(pad.l, Math.min(y1, y2), plotW, Math.abs(y2 - y1));
  }

  // Candles
  bars.forEach((b, i) => {
    const x = pad.l + i * slot + slot / 2;
    const o = Number(b.o);
    const h = Number(b.h);
    const l = Number(b.l);
    const c = Number(b.c);
    const up = c >= o;
    ctx.strokeStyle = up ? "#22c55e" : "#ef4444";
    ctx.fillStyle = up ? "#22c55e" : "#ef4444";
    ctx.beginPath();
    ctx.moveTo(x, yOf(h));
    ctx.lineTo(x, yOf(l));
    ctx.stroke();
    const bodyTop = yOf(Math.max(o, c));
    const bodyH = Math.max(1, Math.abs(yOf(o) - yOf(c)));
    const bw = Math.max(1, slot * 0.6);
    ctx.fillRect(x - bw / 2, bodyTop, bw, bodyH);
  });

  const drawLevel = (px, color, label) => {
    if (px == null || Number.isNaN(Number(px))) return;
    const y = yOf(Number(px));
    ctx.strokeStyle = color;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(pad.l + plotW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillText(`${label} ${Number(px).toFixed(2)}`, pad.l + plotW + 4, y + 4);
  };
  drawLevel(entry, "#e7e9ea", "E");
  drawLevel(trade.exit, "#38bdf8", "X");
  drawLevel(stop, "#ef4444", "SL");
  drawLevel(target, "#22c55e", "TP");

  // Entry / exit time markers on x-axis
  const markTime = (iso, color, tag) => {
    if (!iso || !bars.length) return;
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return;
    let best = 0;
    let bestDiff = Infinity;
    bars.forEach((b, i) => {
      const bt = new Date(b.t).getTime();
      const d = Math.abs(bt - t);
      if (d < bestDiff) {
        bestDiff = d;
        best = i;
      }
    });
    const x = pad.l + best * slot + slot / 2;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, pad.t + plotH);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.font = "10px sans-serif";
    ctx.fillText(tag, x + 2, pad.t + 12);
  };
  markTime(trade.entry_at, "#e7e9ea", "IN");
  markTime(trade.exit_at, "#38bdf8", "OUT");

  // Side caption
  ctx.fillStyle = "#8b98a5";
  ctx.font = "11px sans-serif";
  ctx.fillText(
    `${isShort ? "SHORT" : "LONG"}${trade.pnl != null ? ` · PnL ₹${money(trade.pnl)}` : ""}`,
    pad.l,
    cssH - 8
  );
}

$("tradeChartModal")?.querySelectorAll("[data-close-trade-chart]").forEach((el) => {
  el.addEventListener("click", closeTradeChart);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeTradeChart();
});

boot().catch((err) => {
  $("sessionMsg").textContent = err.message;
});

setInterval(() => {
  refresh().catch(() => {});
}, 2000);
