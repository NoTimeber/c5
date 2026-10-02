/* C5 扫货看板：每 2 秒拉 /api/state 渲染。 */
(() => {
  const $ = (s) => document.querySelector(s);
  let state = null;
  let stateAt = 0;

  // ---- 工具 ----------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2) => (v == null || Number.isNaN(v)) ? "-" : Number(v).toFixed(d);
  const int = (v) => v == null ? "-" : Number(v).toLocaleString("zh-CN");
  const pad = (n) => String(n).padStart(2, "0");
  const zhe = (d) => d == null ? "-" : `${(d * 10).toFixed(2)}折`;
  const zheCls = (d) => d == null ? "" : d <= 0.75 ? "up" : d >= 0.9 ? "down" : "";
  const badge = (text, cls) => `<span class="badge ${cls}">${esc(text)}</span>`;
  const STATUS = { hit: ["到价", "hit"], watch: ["监控中", "watch"], cooldown: ["冷却", "cooldown"], done: ["已买满", "done"], unknown: ["无行情", "fail"] };
  const PSTATUS = { ok: ["成交", "ok"], unknown: ["未知", "warn"], failed: ["失败", "fail"], cancelled: ["已取消", "muted"] };
  const ORDER = { 1: "待发货", 2: "发货中", 3: "待收货", 10: "已收货", 11: "已取消", 200: "结算", 220: "已撤回" };

  function fmtTime(ts, withDate = false) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    const sameDay = d.toDateString() === new Date().toDateString();
    return withDate || !sameDay ? `${d.getMonth() + 1}-${pad(d.getDate())} ${t}` : t;
  }
  function ago(ts) {
    if (!ts) return "-";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 5) return "刚刚";
    if (s < 60) return `${s} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分前`;
  }
  function dur(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? `${h} 小时 ${m} 分` : `${m} 分 ${Math.floor(sec % 60)} 秒`;
  }

  // ---- 数据 ----------------------------------------------------------
  async function load() {
    try {
      const r = await fetch("/api/state");
      if (!r.ok) throw new Error(r.status);
      state = await r.json();
      stateAt = Date.now();
      setConn(true);
      render();
    } catch {
      setConn(false);
    }
  }
  async function post(url) {
    try {
      const r = await fetch(url, { method: "POST" });
      const j = await r.json();
      if (!j.ok) alert(j.error || "操作失败");
    } catch (e) {
      alert(`请求失败: ${e}`);
    }
    await load();
  }

  // ---- 渲染 ----------------------------------------------------------
  function setConn(on) {
    $("#conn-dot").className = "dot " + (on ? "on" : "off");
    if (!on) $("#cycle-text").textContent = "看板断开";
  }

  function render() {
    renderTop();
    renderKpis();
    renderItems();
    renderPurchases();
    renderLogs();
  }

  function renderTop() {
    const s = state;
    $("#mode-badge").innerHTML = badge(s.mode === "live" ? "实盘" : "模拟", `mode-${s.mode}`);
    $("#strategy").textContent = s.strategy === "quick" ? "快速购买" : "在售列表";
    $("#run-badge").innerHTML = s.cycle.done ? badge("已完成", "done") : s.paused ? badge("已暂停扫货", "paused") : badge("扫货中", "running");
    const c = s.cycle;
    $("#cycle-text").textContent = c.at ? `第 ${c.n} 轮 · ${ago(c.at)}` : "等待第一轮…";
    $("#uptime").textContent = `运行 ${dur(s.now - s.started_at + (Date.now() - stateAt) / 1000)}`;
    const box = $("#error-box");
    box.hidden = !c.error;
    box.textContent = c.error ? `最近一轮失败: ${c.error}` : "";
    const pauseBtn = $("#btn-pause");
    pauseBtn.textContent = s.paused ? "继续扫货" : "暂停扫货";
    pauseBtn.className = "ghost " + (s.paused ? "primary" : "danger");
    $("#btn-orders").hidden = s.mode !== "live";
    $("#btn-steam").disabled = !!s.steam.busy;
    $("#btn-steam").textContent = s.steam.busy ? "刷新中…" : "刷新 Steam 价";
  }

  function renderKpis() {
    const s = state;
    const tile = (label, value, sub = "", klass = "", bar = null) =>
      `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${klass}">${value}</div><div class="sub">${sub}</div>` +
      (bar == null ? "" : `<div class="bar"><i style="width:${Math.min(100, bar)}%"></i></div>`) + `</div>`;
    const b = s.budget;
    const pct = b.total ? (b.spent / b.total) * 100 : null;
    const tiles = [
      tile("已花费", num(b.spent), b.total ? `预算 ${num(b.total)} · 剩 ${num(b.total - b.spent)}` : "总预算不限", "", pct),
      tile("已买入", `${int(b.qty)} 件`, `${s.items.filter((i) => i.status === "done").length} / ${s.items.length} 个箱子买满`),
      s.mode === "live"
        ? tile("C5 余额", num(s.balance.value), s.balance.at ? `${ago(s.balance.at)} 更新` : "还没查到")
        : tile("C5 余额", "模拟", "dry 模式不查余额也不花钱"),
      tile("Steam 价", s.steam.at ? ago(s.steam.at) : "未拉取", `每 ${Math.round(s.steam_refresh_sec / 60)} 分钟刷新`),
    ];
    const hits = s.items.filter((i) => i.status === "hit");
    tiles.push(tile("到价", `${hits.length} 个`, hits.map((i) => i.name).join("、") || "都还没到目标价", hits.length ? "up" : ""));
    $("#kpis").innerHTML = tiles.join("");
  }

  function renderItems() {
    const s = state;
    $("#items-sub").textContent = `轮询 ${s.poll_interval}s`;
    if (!s.items.length) {
      $("#items").innerHTML = `<div class="empty">watchlist 为空</div>`;
      return;
    }
    const rows = s.items.map((i) => {
      const [text, cls] = STATUS[i.status] || [i.status, "muted"];
      const cool = i.status === "cooldown" && i.pause_until ? `<div class="sub-cell">${Math.max(0, Math.round(i.pause_until - s.now))}s</div>` : "";
      const steamSub = i.steam_error ? `<div class="sub-cell down">${esc(i.steam_error)}</div>` : i.steam_at ? `<div class="sub-cell">${ago(i.steam_at)}</div>` : "";
      const spendCap = i.max_spend ? ` / ${num(i.max_spend)}` : "";
      return `<tr>
        <td class="l"><b>${esc(i.name)}</b></td>
        <td class="l">${badge(text, cls)}${cool}</td>
        <td class="${i.c5_lowest != null && i.c5_lowest <= i.c5_target ? "up" : ""}">${num(i.c5_lowest)}</td>
        <td>${num(i.c5_target)}</td>
        <td>${int(i.sell_count)}</td>
        <td>${num(i.purchase_max)}</td>
        <td>${num(i.steam_lowest)}${steamSub}</td>
        <td>${num(i.steam_net)}</td>
        <td class="${zheCls(i.discount_at_lowest)}">${zhe(i.discount_at_lowest)}</td>
        <td class="${zheCls(i.discount_at_target)}">${zhe(i.discount_at_target)}</td>
        <td>${i.bought} / ${i.max_qty}</td>
        <td>${num(i.spent)}${spendCap}</td>
      </tr>`;
    });
    $("#items").innerHTML = `<table>
      <thead><tr>
        <th class="l">箱子</th><th class="l">状态</th><th>C5 最低</th><th>目标价</th><th>在售</th><th>求购最高</th>
        <th>Steam 最低</th><th>净到手</th><th>折(C5最低)</th><th>折(目标价)</th><th>已买</th><th>已花</th>
      </tr></thead><tbody>${rows.join("")}</tbody></table>`;
  }

  function renderPurchases() {
    const list = state.purchases;
    $("#purchases-sub").textContent = list.length ? `最近 ${list.length} 笔` : "";
    if (!list.length) {
      $("#purchases").innerHTML = `<div class="empty">还没有买入</div>`;
      return;
    }
    const rows = list.map((p) => {
      const [text, cls] = PSTATUS[p.status] || [p.status, "muted"];
      const order = p.order_status != null ? (ORDER[p.order_status] || p.order_status) : "";
      return `<tr>
        <td class="l">${fmtTime(p.ts)}</td>
        <td class="l">${badge(text, cls)}</td>
        <td class="l">${esc(p.name)}</td>
        <td>${num(p.actual_pay != null ? p.actual_pay : p.price)}</td>
        <td class="l sub-cell">${esc(order)} ${esc(p.error || "")}</td>
      </tr>`;
    });
    $("#purchases").innerHTML = `<table>
      <thead><tr><th class="l">时间</th><th class="l">状态</th><th class="l">箱子</th><th>价格</th><th class="l">备注</th></tr></thead>
      <tbody>${rows.join("")}</tbody></table>`;
  }

  function renderLogs() {
    const lines = state.logs.slice().reverse();
    $("#logs-sub").textContent = lines.length ? `最近 ${lines.length} 条` : "";
    $("#logs").innerHTML = lines.length
      ? lines.map((l) => `<div class="entry"><span class="t">${fmtTime(l.t)}</span><span class="lv ${esc(l.level)}">${esc(l.level)}</span><span class="body">${esc(l.msg)}</span></div>`).join("")
      : `<div class="empty">暂无日志</div>`;
  }

  // ---- 交互 ----------------------------------------------------------
  $("#btn-pause").addEventListener("click", () => post(state && state.paused ? "/api/resume" : "/api/pause"));
  $("#btn-steam").addEventListener("click", () => post("/api/steam/refresh"));
  $("#btn-reload").addEventListener("click", () => post("/api/watchlist/reload"));
  $("#btn-orders").addEventListener("click", () => post("/api/orders/refresh"));

  const root = document.documentElement;
  const savedTheme = localStorage.getItem("theme");
  if (savedTheme) root.dataset.theme = savedTheme;
  $("#theme-btn").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    localStorage.setItem("theme", root.dataset.theme);
  });

  load();
  setInterval(load, 2000);
})();
