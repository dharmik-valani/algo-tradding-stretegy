const $ = (id) => document.getElementById(id);

const VIEW_META = {
  execute: { title: "Execute", subtitle: "Day capital, live strategies, and paper fills" },
  reports: { title: "Reports", subtitle: "Day / week / month history and exports" },
  settings: { title: "Settings", subtitle: "Session mode and paper controls" },
  analytics: { title: "Execute", subtitle: "Day capital, live strategies, and paper fills" },
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

function pct(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(1)}%`;
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
let currentView = "execute";

function setView(view) {
  if (view === "dashboard" || view === "analytics") view = "execute";
  if (!VIEW_META[view] || view === "analytics") view = "execute";
  currentView = view;
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  const meta = VIEW_META[view];
  $("viewTitle").textContent = meta.title;
  $("viewSubtitle").textContent = meta.subtitle;
  if (location.hash !== `#${view}`) {
    history.replaceState(null, "", `#${view}`);
  }
  const scroller = document.querySelector(".main");
  if (scroller) scroller.scrollTop = 0;
  else window.scrollTo(0, 0);
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

function renderRegistry() {
  const root = $("registryCards");
  $("sumRegistered").textContent = String(catalog.length);
  $("regCount").textContent = String(catalog.length);
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
  let invested = 0;
  let dayPnl = 0;
  let enabled = 0;
  for (const s of strats) {
    const tv = s.trade_view || {};
    const cap = Number(tv.invested != null ? tv.invested : s.starting_cash) || 0;
    const dp = Number(
      tv.day_pnl != null ? tv.day_pnl : (s.realized_pnl || 0) + (s.unrealized_pnl || 0)
    );
    invested += cap;
    dayPnl += dp;
    if (s.enabled) enabled += 1;
  }
  const generated = invested + dayPnl;
  const overall = lastAnalytics.overall || {};
  const set = (id, text, cls) => {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    if (cls !== undefined) el.className = cls;
  };
  set("execInvested", `₹${money(invested)}`);
  set("execGenerated", `₹${money(generated)}`, pnlClass(dayPnl));
  set("execDayPnl", `₹${money(dayPnl)}`, pnlClass(dayPnl));
  set("execWinRate", pct(overall.win_rate));
  set("execDeskLine", `${enabled} / ${strats.length}`);
  const deskCount = $("deskCount");
  if (deskCount) deskCount.textContent = String(strats.length);
}

function renderSummary(session) {
  renderExecDayKpis(session);
}

function renderAnalytics() {
  /* Analytics tab removed — day KPIs live on Execute. */
}

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
      const generated = Number(tv.generated != null ? tv.generated : invested + dayPnl);
      const total = tv.desk_settled ? dayPnl : realized + unreal;
      const basket = s.basket || [];
      const active = basket.filter((l) => l.in_trade);
      const selected = (s.params?.selected || []).length || basket.length || 0;
      const inTrade = s.legs_in_trade != null ? s.legs_in_trade : active.length;
      const posQty = s.position?.quantity || 0;
      const isOpen = !!(inTrade || (posQty && !basket.length) || tv.in_trade);

      let stocks;
      if (basket.length || selected) {
        stocks = `${inTrade}/${selected || basket.length}`;
      } else {
        stocks = posQty ? String(Math.abs(posQty)) : "0";
      }
      const posLabel = basket.length ? "In trade" : "Pos";

      let market = "—";
      let entry = "—";
      let sl = "—";
      let tp = "—";
      let levelsHint = "";
      if (basket.length) {
        if (active.length === 1) {
          market = money(active[0].last ?? s.last_price);
          entry = active[0].avg != null ? money(active[0].avg) : (s.entry_price != null ? money(s.entry_price) : "—");
          sl = active[0].stop != null ? money(active[0].stop) : "—";
          tp = active[0].target != null ? money(active[0].target) : "—";
          levelsHint = "open";
        } else if (active.length > 1) {
          market = `${active.length} open`;
          entry = "multi";
          sl = "multi";
          tp = "multi";
          levelsHint = "open";
        } else {
          if (s.last_price != null) market = money(s.last_price);
          const lc = tv.last_closed;
          if (lc && lc.entry != null) {
            entry = money(lc.entry);
            levelsHint = "last closed";
            sl = "flat";
            tp = lc.exit != null ? money(lc.exit) : "flat";
          } else {
            entry = "flat";
            sl = "flat";
            tp = "flat";
            levelsHint = "no open trade";
          }
        }
      } else if (isOpen && s.entry_price != null) {
        market = s.last_price != null ? money(s.last_price) : "—";
        entry = money(s.entry_price);
        sl = s.stop_price != null ? money(s.stop_price) : "—";
        tp = s.target_price != null ? money(s.target_price) : "—";
        levelsHint = "open";
      } else {
        market = s.last_price != null ? money(s.last_price) : "—";
        const lc = tv.last_closed;
        if (lc && lc.entry != null) {
          entry = money(lc.entry);
          levelsHint = "last closed";
          sl = "flat";
          tp = lc.exit != null ? money(lc.exit) : "flat";
        } else {
          entry = "flat";
          sl = "flat";
          tp = "flat";
          levelsHint = "no open trade";
        }
      }

      const pnlSub = tv.desk_settled
        ? `settled day`
        : Math.abs(unreal) > 1e-9
          ? `R ${money(realized)} · U ${money(unreal)}`
          : Math.abs(realized) > 1e-9
            ? `realized`
            : `flat`;
      const note = escapeHtml(s.note || s.last_signal || "—");
      const nameSub = escapeHtml(
        [s.params?.option_type, s.timeframe, basket.length ? "basket" : s.instrument]
          .filter(Boolean)
          .join(" · ")
      );
      const status = s.enabled ? "run" : "paused";
      const toggleLabel = s.enabled ? "Pause" : "Resume";
      const toggleAct = s.enabled ? "pause" : "start";

      return `
      <tr class="click-row desk-row ${s.enabled ? "is-run" : "is-paused"} ${isOpen ? "has-open" : "is-flat"}" data-id="${escapeHtml(id)}" tabindex="0" role="button" aria-label="Open settings for ${escapeHtml(s.name)}">
        <td data-label="Status"><span class="badge ${s.enabled ? "on" : ""}">${status}</span></td>
        <td data-label="Strategy" class="col-strategy">
          <div class="row-title">${escapeHtml(s.name)}</div>
          <div class="meta">${nameSub}</div>
        </td>
        <td data-label="Capital" class="mono" title="Paper capital for this strategy">₹${money(invested)}</td>
        <td data-label="Generated" class="mono ${pnlClass(dayPnl)}" title="Capital + day PnL">
          <div>₹${money(generated)}</div>
          <div class="meta pnl-sub">day ₹${money(dayPnl)}</div>
        </td>
        <td data-label="PnL" class="mono ${pnlClass(total)}" title="Open row PnL (0 after EOD settle)">
          <div>₹${money(total)}</div>
          <div class="meta pnl-sub">${escapeHtml(pnlSub)}</div>
        </td>
        <td data-label="Signal" class="col-note">${note}</td>
        <td data-label="Market" class="mono" title="Current market / premium mark">${market}</td>
        <td data-label="Entry" class="mono" title="${levelsHint === "last closed" ? "Last closed entry (not an open trade)" : levelsHint === "open" ? "Open trade entry" : "No open trade"}">
          <div>${entry}</div>
          ${levelsHint && levelsHint !== "open" ? `<div class="meta pnl-sub">${escapeHtml(levelsHint)}</div>` : ""}
        </td>
        <td data-label="${posLabel}" class="mono">${stocks}</td>
        <td data-label="Stop" class="mono">${sl}</td>
        <td data-label="Target" class="mono">${tp}</td>
        <td data-label="Win" class="mono">${pct(a.win_rate)}</td>
        <td data-label="Actions" class="desk-actions-cell">
          <div class="desk-row-actions">
            <button type="button" data-act="${toggleAct}" class="desk-btn ${s.enabled ? "" : "primary"}">${toggleLabel}</button>
            <button type="button" data-act="remove" class="desk-btn ghost">Delete</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");

  root.innerHTML = `
    <p class="hint desk-legend">
      <strong>Capital</strong> = paper cash assigned (e.g. ₹5L).
      <strong>Generated</strong> = capital + day PnL.
      After <strong>15:20</strong> settle, row PnL shows day result then clears to 0; Reports keep history.
    </p>
    <div class="table-wrap desk-table-wrap">
      <table class="data-table dense cards-on-mobile" id="execStatsTable">
        <thead>
          <tr>
            <th>Status</th>
            <th>Strategy</th>
            <th title="Paper capital">Capital</th>
            <th title="Capital + day PnL">Generated</th>
            <th title="Live / settled day PnL">PnL</th>
            <th>Signal</th>
            <th title="Current mark">Market</th>
            <th title="Open entry, or last closed when flat">Entry</th>
            <th>Pos</th>
            <th>Stop</th>
            <th>Target</th>
            <th>Win</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

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

let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth() + 1;

function reportQuery() {
  const from = $("rpFrom")?.value || "";
  const to = $("rpTo")?.value || "";
  const strat = ($("rpStrategy")?.value || "").trim();
  const q = new URLSearchParams();
  if (from) q.set("from_date", from);
  if (to) q.set("to_date", to);
  if (strat) q.set("strategy_id", strat);
  return q.toString();
}

async function refreshReports() {
  try {
    const q = reportQuery();
    const exportLink = $("btnExportCsv");
    const exportFull = $("btnExportCsvFull");
    const exportPdf = $("btnExportPdf");
    if (exportLink) exportLink.href = `/api/reports/export.csv${q ? `?${q}` : ""}`;
    if (exportFull) {
      const fullQ = new URLSearchParams(q);
      fullQ.set("kind", "full");
      exportFull.href = `/api/reports/export.csv?${fullQ.toString()}`;
    }
    if (exportPdf) exportPdf.href = `/api/reports/export.pdf${q ? `?${q}` : ""}`;
    const [summary, trades, sels, daily, cal] = await Promise.all([
      api(`/api/reports/summary${q ? `?${q}` : ""}`),
      api(`/api/reports/trades${q ? `?${q}` : ""}`),
      api("/api/reports/selections"),
      api(`/api/reports/daily${q ? `?${q}` : ""}`),
      api(`/api/reports/calendar?year=${calYear}&month=${calMonth}`),
    ]);
    $("rpTrades").textContent = String(summary.trades || 0);
    $("rpClosed").textContent = String(summary.closed || 0);
    $("rpWinRate").textContent = pct(summary.win_rate);
    $("rpAvgWin").textContent = summary.avg_win != null ? `₹${money(summary.avg_win)}` : "—";
    $("rpAvgLoss").textContent = summary.avg_loss != null ? `₹${money(summary.avg_loss)}` : "—";
    const net = $("rpNet");
    net.textContent = `₹${money(summary.net_pnl || 0)}`;
    net.className = pnlClass(summary.net_pnl || 0);
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    $("calLabel").textContent = `${months[cal.month - 1]} ${cal.year}`;
    $("calMonthPnl").textContent = `₹${money(cal.month_pnl || 0)}`;
    $("calMonthPnl").className = pnlClass(cal.month_pnl || 0);
    $("calGrid").innerHTML = (cal.cells || [])
      .map((c) => {
        if (!c.date) return `<div class="cal-cell empty"></div>`;
        const day = c.date.slice(-2);
        const pnl = c.pnl;
        const cls = pnl == null ? "" : pnlClass(pnl);
        return `<div class="cal-cell ${cls}"><span class="d">${day}</span><strong>${pnl == null ? "—" : `₹${money(pnl)}`}</strong></div>`;
      })
      .join("");
    const dbody = $("reportDailyBody");
    const days = daily.days || [];
    dbody.innerHTML = days.length
      ? days.map((d) => `<tr>
          <td data-label="Date">${d.date}</td>
          <td data-label="Trades">${d.trades}</td>
          <td data-label="Closed">${d.closed}</td>
          <td data-label="Win rate">${pct(d.win_rate)}</td>
          <td data-label="Net PnL" class="${pnlClass(d.net_pnl)}">₹${money(d.net_pnl)}</td>
          <td data-label="Symbols">${(d.symbols || []).join(", ")}</td></tr>`).join("")
      : `<tr><td colspan="6" class="empty-cell">No daily data yet.</td></tr>`;
    const tbody = $("reportTradesBody");
    const list = trades.trades || [];
    tbody.innerHTML = list.length
      ? list.slice(0, 150).map((t) => {
          const entry = (t.entry_at || "").replace("T", " ").slice(0, 19);
          const exit = (t.exit_at || "").replace("T", " ").slice(0, 19) || "—";
          return `<tr>
            <td data-label="Entry">${entry}</td>
            <td data-label="Exit">${exit}</td>
            <td data-label="Strategy">${t.strategy_id}</td>
            <td data-label="Symbol">${t.symbol}</td>
            <td data-label="Side">${t.side}</td>
            <td data-label="Entry ₹">${money(t.entry_price)}</td>
            <td data-label="Exit ₹">${t.exit_price != null ? money(t.exit_price) : "—"}</td>
            <td data-label="SL / TP">${t.stop_price != null ? money(t.stop_price) : "—"} / ${t.target_price != null ? money(t.target_price) : "—"}</td>
            <td data-label="PnL" class="${pnlClass(t.realized_pnl || 0)}">${t.realized_pnl != null ? `₹${money(t.realized_pnl)}` : "—"}</td>
            <td data-label="Status">${t.status}${t.exit_reason ? ` · ${escapeHtml(t.exit_reason)}` : ""}</td></tr>`;
        }).join("")
      : `<tr><td colspan="10" class="empty-cell">No stored trades yet.</td></tr>`;
    const sbody = $("reportSelBody");
    const selsList = sels.selections || [];
    sbody.innerHTML = selsList.length
      ? selsList.slice(0, 100).map((r) => {
          const when = (r.selected_at || "").replace("T", " ").slice(0, 19);
          return `<tr>
            <td data-label="When">${when}</td>
            <td data-label="Strategy">${r.strategy_id}</td>
            <td data-label="Symbol">${r.symbol}</td>
            <td data-label="Mode">${r.mode}</td>
            <td data-label="% chg">${r.pct_change != null ? Number(r.pct_change).toFixed(2) : "—"}</td>
            <td data-label="Open">${money(r.open)}</td>
            <td data-label="High">${money(r.high)}</td>
            <td data-label="Low">${money(r.low)}</td></tr>`;
        }).join("")
      : `<tr><td colspan="8" class="empty-cell">No basket selections stored yet.</td></tr>`;
  } catch (err) {
    console.warn(err);
  }
}

function render(session) {
  lastSession = session;
  $("sessionMsg").textContent = session.message || "";
  const pill = $("statusPill");
  pill.textContent = session.running ? `${session.mode} · running` : session.mode || "idle";
  pill.classList.toggle("running", !!session.running);
  renderSummary(session);
  renderRegistry();
  renderExecStats(session);
  if (currentView === "reports") refreshReports();
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

$("btnRefreshReports")?.addEventListener("click", () => refreshReports());
$("rpFrom")?.addEventListener("change", () => refreshReports());
$("rpTo")?.addEventListener("change", () => refreshReports());
$("rpStrategy")?.addEventListener("change", () => refreshReports());

function isoDay(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

document.querySelectorAll("[data-range]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const range = btn.dataset.range;
    const to = new Date();
    const from = new Date();
    if (range === "today") {
      // from = to = today
    } else if (range === "week") {
      const day = to.getDay(); // 0 Sun … 6 Sat
      const mondayOffset = day === 0 ? -6 : 1 - day;
      from.setDate(to.getDate() + mondayOffset);
    } else if (range === "7d") {
      from.setDate(to.getDate() - 6);
    } else if (range === "month") {
      from.setDate(1);
    } else if (range === "all") {
      if ($("rpFrom")) $("rpFrom").value = "";
      if ($("rpTo")) $("rpTo").value = "";
      refreshReports();
      return;
    }
    if ($("rpFrom")) $("rpFrom").value = isoDay(from);
    if ($("rpTo")) $("rpTo").value = isoDay(to);
    refreshReports();
  });
});

$("calPrev")?.addEventListener("click", () => {
  calMonth -= 1;
  if (calMonth < 1) {
    calMonth = 12;
    calYear -= 1;
  }
  refreshReports();
});
$("calNext")?.addEventListener("click", () => {
  calMonth += 1;
  if (calMonth > 12) {
    calMonth = 1;
    calYear += 1;
  }
  refreshReports();
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
    const auto = s.renewable
      ? (s.auto_renew ? "auto-renew on (SELF)" : "renewable SELF")
      : "PARTNER · no API renew (paste daily)";
    const totp = "";
    if (s.ok) {
      el.textContent = `Connected · Data plan: ${plan} · Token valid: ${valid} (${left}) · ${auto}${totp}`;
      el.style.color = "var(--accent)";
    } else {
      el.textContent = `Not connected · ${s.error || "paste a fresh access token"} · ${left} · ${auto}${totp}`;
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
$("btnDhanRenew")?.addEventListener("click", async () => {
  try {
    await api("/api/dhan/renew", { method: "POST", body: "{}" });
    await refreshDhanStatus();
    alert("Token renewed.");
  } catch (err) {
    alert(err.message);
  }
});

boot().catch((err) => {
  $("sessionMsg").textContent = err.message;
});

setInterval(() => {
  refresh().catch(() => {});
}, 2000);
