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
  // 美元区钱包：折 就是汇率（人民币 / 1 美元），5.5 以下算便宜，6.5 以上接近官方汇率没利润
  const rateCls = (r) => r == null ? "" : r <= 5.5 ? "up" : r >= 6.5 ? "down" : "";
  const usdWallet = () => !!(state && state.usd_wallet);
  const cur = (v, d = 2) => (usdWallet() && v != null ? "$" : "") + num(v, d);
  const badge = (text, cls) => `<span class="badge ${cls}">${esc(text)}</span>`;
  const STATUS = { hit: ["到价", "hit"], watch: ["监控中", "watch"], cooldown: ["冷却", "cooldown"], done: ["已买满", "done"], unknown: ["无行情", "fail"], nosteam: ["等 Steam 价", "warn"] };
  const PSTATUS = { ok: ["成交", "ok"], unknown: ["未知", "warn"], failed: ["失败", "fail"], cancelled: ["已取消", "muted"] };
  const ORDER = { 1: "待发货", 2: "发货中", 3: "待收货", 10: "已收货", 11: "已取消", 200: "结算", 220: "已撤回" };

  function fmtTime(ts, withDate = false) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    const sameDay = d.toDateString() === new Date().toDateString();
    return withDate || !sameDay ? `${d.getMonth() + 1}-${pad(d.getDate())} ${t}` : t;
  }
  function fmtDate(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  function ago(ts) {
    if (!ts) return "-";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 5) return "刚刚";
    if (s < 60) return `${s} 秒前`;
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分前`;
    return `${Math.floor(s / 86400)} 天前`;
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
  async function post(url, body) {
    let j = null;
    try {
      const opts = { method: "POST" };
      if (body !== undefined) {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(body);
      }
      const r = await fetch(url, opts);
      j = await r.json();
      if (!j.ok) alert(j.error || "操作失败");
    } catch (e) {
      alert(`请求失败: ${e}`);
    }
    await load();
    return j;
  }

  // ---- 渲染 ----------------------------------------------------------
  function setConn(on) {
    $("#conn-dot").className = "dot " + (on ? "on" : "off");
    if (!on) $("#cycle-text").textContent = "看板断开";
  }

  // 用户正在选中文字时不重建表格和日志，否则 2 秒一次的重绘会把选区冲掉，没法复制
  function selectionInside(el) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    const node = sel.getRangeAt(0).commonAncestorContainer;
    return el.contains(node.nodeType === 1 ? node : node.parentNode);
  }
  function render() {
    renderTop();
    renderRateForm();
    const main = document.querySelector("main");
    if (selectionInside(main)) return;   // 正在选中：只更新顶栏，内容区保持不动
    renderKpis();
    renderItems();
    renderSteam();
    renderPurchases();
    renderLogs();
  }

  function renderTop() {
    const s = state;
    $("#mode-badge").innerHTML = badge(s.mode === "live" ? "实盘" : "模拟", `mode-${s.mode}`);
    $("#strategy").textContent = s.strategy === "quick" ? "快速购买" : "在售列表";
    $("#version").textContent = s.version ? `v${s.version}` : "-";
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
    const blocked = s.steam.blocked_for > 0;
    $("#btn-steam").disabled = !!s.steam.busy || blocked;
    $("#btn-steam").textContent = s.steam.busy ? "刷新中…" : blocked ? "Steam 限流中" : "刷新 Steam 价";
  }

  function renderKpis() {
    const s = state;
    const tile = (label, value, sub = "", klass = "", bar = null) =>
      `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${klass}">${value}</div><div class="sub">${sub}</div>` +
      (bar == null ? "" : `<div class="bar"><i style="width:${Math.min(100, bar)}%"></i></div>`) + `</div>`;
    const b = s.budget;
    const pct = b.total ? (b.spent / b.total) * 100 : null;
    const r = s.rate || {};
    const sr = r.steam;
    const tiles = [
      tile("已花费", num(b.spent), b.total ? `预算 ${num(b.total)} · 剩 ${num(b.total - b.spent)}` : "总预算不限", "", pct),
      tile("已买入", `${int(b.qty)} 件`, `${s.items.filter((i) => i.status === "done").length} / ${s.items.length} 个箱子买满`),
      s.mode === "live"
        ? tile("C5 余额", num(s.balance.value), s.balance.at ? `${ago(s.balance.at)} 更新` : "还没查到")
        : tile("C5 余额", "模拟", "dry 模式不查余额也不花钱"),
      s.steam.blocked_for > 0
        ? tile("Steam 价", "限流中", `Steam 429，约 ${Math.ceil(s.steam.blocked_for / 60)} 分钟后自动重试，期间沿用旧价`, "down small")
        : tile("Steam 价", s.steam.at ? ago(s.steam.at) : "未拉取", `每 ${Math.round(s.steam_refresh_sec / 60)} 分钟刷新`),
      usdWallet()
        ? tile("钱包币种", "美元区", "Steam 价和到手都是美元，C5 人民币价 ÷ 到手美元 = 汇率", "small")
        : sr
          ? tile("Steam 汇率", `${num(sr.rate, 3)} <span class="muted">元/USD</span>`, `按 ${esc(sr.name)} ¥${num(sr.cny)} / $${num(sr.usd)} · ${ago(sr.at)}`)
          : tile("Steam 汇率", "未知", r.steam_error ? esc(r.steam_error) : "等第一次 Steam 刷新", "small"),
      r.target != null
        ? tile("目标汇率", `${num(r.target)} <span class="muted">元/USD</span>`,
          usdWallet() ? `目标价 = 到手美元 × ${num(r.target)}，随 Steam 价自动算`
            : r.discount != null ? `= ${zhe(r.discount)}，目标价随 Steam 价自动算` : "等 Steam 汇率后生效，期间不买",
          usdWallet() || r.discount != null ? "" : "small")
        : tile("目标汇率", "未设置", "目标价用 watchlist 里的 max_price", "small"),
    ];
    const hits = s.items.filter((i) => i.status === "hit");
    tiles.push(tile("到价", `${hits.length} 个`, hits.map((i) => i.name).join("、") || "都还没到目标价", hits.length ? "up" : ""));
    $("#kpis").innerHTML = tiles.join("");
  }

  // 输入框是静态元素，不随 2 秒一次的渲染重建；只在服务端的值变了、而且用户没在编辑时同步进来
  let shownRate;
  function renderRateForm() {
    const input = $("#rate-input");
    const target = state.rate ? state.rate.target : null;
    if (target !== shownRate && document.activeElement !== input) {
      input.value = target == null ? "" : String(target);
      shownRate = target;
    }
    $("#rate-clear").hidden = target == null;
  }

  function renderItems() {
    const s = state;
    $("#items-sub").textContent = `轮询 ${s.poll_interval}s`;
    if (!s.items.length) {
      $("#items").innerHTML = `<div class="empty">watchlist 为空</div>`;
      return;
    }
    const usd = usdWallet();
    const rows = s.items.map((i) => {
      const [text, cls] = STATUS[i.status] || [i.status, "muted"];
      const cool = i.status === "cooldown" && i.pause_until ? `<div class="sub-cell">${Math.max(0, Math.round(i.pause_until - s.now))}s</div>` : "";
      const steamSub = i.steam_error ? `<div class="sub-cell down">${esc(i.steam_error)}</div>` : i.steam_at ? `<div class="sub-cell">${ago(i.steam_at)}</div>` : "";
      const spendCap = i.max_spend ? ` / ${num(i.max_spend)}` : "";
      const targetSub = i.target_auto ? `<div class="sub-cell">${i.c5_target == null ? "等 Steam 价" : "按汇率"}</div>` : "";
      let sellSub;
      if (i.sell_src === "history") {
        const pct = Math.round((i.sell_share || s.sell_volume_share) * 100);
        const depth = (i.sell_orders || []).map((o) => `${cur(o[0])} ${int(o[1])} 件`).concat(i.sell_more ? [`${cur(i.sell_more[0])} 或更高 ${int(i.sell_more[1])} 件`] : []).join("；");
        const queue = i.queue_ahead != null ? ` · 前面排 ${i.queue_min ? "≥ " : ""}${int(i.queue_ahead)} 件` : "";
        sellSub = `<div class="sub-cell" title="最近 ${s.sell_window_days} 天共成交 ${int(i.sell_total)} 件，其中 ${int(i.sell_volume)} 件（${pct}%）在这个价或更高价成交；窗口内最高小时中位价 ${cur(i.sell_high)}${i.sell_usd != null ? `。成交历史是美元价，$${num(i.sell_usd, 3)} 按 Steam 汇率换算` : ""}${depth ? `。当前卖单：${depth}` : ""}">${s.sell_window_days} 天 ${pct}% 成交 ≥ 此价 · 最高 ${cur(i.sell_high)}${queue}</div>`;
      }
      else if (i.history_error) sellSub = `<div class="sub-cell down" title="${esc(i.history_error)}">按最低价（历史失败）</div>`;
      else sellSub = `<div class="sub-cell">按最低价</div>`;
      return `<tr>
        <td class="l"><b>${esc(i.name)}</b></td>
        <td class="l">${badge(text, cls)}${cool}</td>
        <td class="${i.c5_lowest != null && i.c5_target != null && i.c5_lowest <= i.c5_target ? "up" : ""}">${num(i.c5_lowest)}</td>
        <td>${num(i.c5_target)}${targetSub}</td>
        <td>${int(i.sell_count)}</td>
        <td>${num(i.purchase_max)}</td>
        <td>${cur(i.steam_lowest)}${steamSub}</td>
        <td>${cur(i.steam_sell)}${sellSub}</td>
        <td>${cur(i.steam_net)}</td>
        ${usd ? "" : `<td class="${zheCls(i.discount_at_lowest)}">${zhe(i.discount_at_lowest)}</td>`}
        <td class="${rateCls(i.rate_at_lowest)}">${num(i.rate_at_lowest)}</td>
        ${usd ? `<td class="${rateCls(i.rate_at_target)}">${num(i.rate_at_target)}</td>`
              : `<td class="${zheCls(i.discount_at_target)}">${zhe(i.discount_at_target)}</td>`}
        <td>${i.bought} / ${i.max_qty}</td>
        <td>${num(i.spent)}${spendCap}</td>
      </tr>`;
    });
    $("#items").innerHTML = `<table>
      <thead><tr>
        <th class="l">箱子</th><th class="l">状态</th><th>C5 最低</th><th>目标价</th><th>在售</th><th>求购最高</th>
        <th>Steam 最低</th><th title="算净到手用的卖出价：登录 Steam 后是最近几天成交历史的最高小时中位价，否则是当前最低挂单价">挂单价</th><th>净到手</th>
        ${usd ? "" : "<th>折(C5最低)</th>"}<th title="按 C5 最低价买入，1 美元 Steam 余额花多少人民币">汇率(C5最低)</th>
        ${usd ? `<th title="按目标价买入，1 美元 Steam 余额花多少人民币">汇率(目标价)</th>` : "<th>折(目标价)</th>"}<th>已买</th><th>已花</th>
      </tr></thead><tbody>${rows.join("")}</tbody></table>`;
  }

  // ---- Steam 登录 ----------------------------------------------------
  let pollTimer = null;
  async function pollLogin() {
    try {
      const r = await fetch("/api/steam/login/poll", { method: "POST" });
      const j = await r.json();
      if (!j.ok || j.logged_in) {
        clearInterval(pollTimer);
        pollTimer = null;
        await load();
      }
    } catch { /* 下次再试 */ }
  }
  function renderSteam() {
    const sl = state.steam_login || {};
    const accounts = state.steam_accounts || [];
    const form = $("#steam-login-form"), guard = $("#steam-guard-form"), status = $("#steam-status"), logout = $("#steam-logout");
    const pct = Math.round((state.sell_volume_share || 0.3) * 100);
    $("#steam-sub").textContent = accounts.length ? `账号池 ${accounts.length} 个，可用 ${sl.usable}` : "挂单价按最低价";
    logout.hidden = !accounts.length;
    if (sl.pending) {
      const g = sl.guard || [];
      const needCode = g.includes("device_code") || g.includes("email");
      $("#steam-code").hidden = !needCode;
      $("#steam-code-btn").hidden = !needCode;
      $("#steam-guard-hint").textContent = g.includes("device_code") ? "输入 Steam 手机令牌上的验证码，或直接在手机 Steam App 上点确认"
        : g.includes("email") ? "输入发到邮箱的验证码" : "请在 Steam 手机 App 上点确认";
      status.textContent = "等待验证…";
      form.hidden = true; guard.hidden = false;
      if (!pollTimer) pollTimer = setInterval(pollLogin, 3000);
    } else {
      const err = sl.error ? `<span class="down">${esc(sl.error)}</span> ` : "";
      status.innerHTML = accounts.length
        ? `${err}已登录 ${accounts.length} 个账号，查成交历史时轮流用，每个请求同时换代理出口。挂单价按最近 ${state.sell_window_days} 天成交历史算：有 ${pct}% 的成交在此价或更高价成交。被限流的账号自动歇 10 分钟，标“需重新登录”的要在下面重新登一次。继续添加账号：`
        : `${err}未登录：挂单价按当前最低挂单价算。登录后按最近几天的成交历史算（能大批量卖出的价），更接近挂单卖出的实际到手。可以登多个账号轮着用，用没有库存和余额的小号即可。`;
      form.hidden = false; guard.hidden = true;
    }
    if (!sl.pending && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    $("#steam-accounts").innerHTML = accounts.length ? `<table>
      <thead><tr><th class="l">账号</th><th class="l">状态</th><th>登录态到期</th><th>成交历史请求</th><th>最近使用</th><th></th></tr></thead>
      <tbody>${accounts.map((a) => {
        const st = a.dead ? badge("需重新登录", "fail") : a.cooldown_for > 0 ? badge(`歇 ${Math.ceil(a.cooldown_for / 60)} 分钟`, "warn") : a.error ? badge("有警告", "warn") : badge("正常", "ok");
        const note = a.error ? `<div class="sub-cell" title="${esc(a.error)}">${esc(a.error.slice(0, 40))}</div>` : "";
        return `<tr><td class="l"><b>${esc(a.account)}</b><div class="sub-cell">${esc(a.steamid)}</div></td><td class="l">${st}${note}</td>
          <td>${fmtDate(a.refresh_exp)}</td><td>${int(a.requests)}</td><td>${a.last_used ? ago(a.last_used) : "-"}</td>
          <td><button class="ghost danger" type="button" data-steamid="${esc(a.steamid)}" data-account="${esc(a.account)}">退出</button></td></tr>`;
      }).join("")}</tbody></table>` : "";
    renderProxy();
  }

  function renderProxy() {
    const p = state.proxy || {};
    $("#proxy-clear").hidden = p.source !== "dashboard";
    $("#proxy-status").textContent = p.active
      ? `当前：${p.active}（${p.source === "dashboard" ? "看板设置" : ".env 配置"}）${p.exit_ip ? `，出口 IP ${p.exit_ip}` : ""}`
      : "未配置，直连 Steam";
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

  let logsKey = "";
  function renderLogs() {
    const lines = state.logs.slice().reverse();
    const key = lines.length ? `${lines.length}:${lines[0].t}:${lines[lines.length - 1].t}` : "";
    if (key === logsKey) return;        // 日志没变就不重建，选区和滚动位置都保得住
    logsKey = key;
    $("#logs-sub").textContent = lines.length ? `最近 ${lines.length} 条` : "";
    $("#logs").innerHTML = lines.length
      ? lines.map((l) => `<div class="entry"><span class="t">${fmtTime(l.t)}</span><span class="lv ${esc(l.level)}">${esc(l.level)}</span><span class="body">${esc(l.msg)}</span></div>`).join("")
      : `<div class="empty">暂无日志</div>`;
  }
  function logsAsText() {
    return (state ? state.logs : []).map((l) => `${fmtTime(l.t, true)} ${l.level} ${l.msg}`).join("\n");
  }
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    }
  }

  // ---- 交互 ----------------------------------------------------------
  $("#btn-pause").addEventListener("click", () => post(state && state.paused ? "/api/resume" : "/api/pause"));
  $("#btn-steam").addEventListener("click", () => post("/api/steam/refresh"));
  $("#btn-reload").addEventListener("click", () => post("/api/watchlist/reload"));
  $("#btn-orders").addEventListener("click", () => post("/api/orders/refresh"));
  $("#btn-copy-logs").addEventListener("click", async () => {
    const btn = $("#btn-copy-logs");
    const ok = await copyText(logsAsText());
    btn.textContent = ok ? "已复制" : "复制失败";
    setTimeout(() => { btn.textContent = "复制"; }, 1500);
  });
  $("#rate-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#rate-input").value.trim();
    if (!v) { alert("先填目标汇率，比如 5.20"); return; }
    $("#rate-input").blur();
    post("/api/rate", { rate: Number(v) });
  });
  $("#rate-clear").addEventListener("click", () => {
    if (confirm("清除目标汇率？之后目标价用 watchlist 里的 max_price。")) post("/api/rate", { rate: null });
  });
  $("#steam-login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const account = $("#steam-account").value.trim(), password = $("#steam-password").value;
    $("#steam-password").value = "";
    post("/api/steam/login", { account, password });
  });
  $("#steam-guard-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const code = $("#steam-code").value.trim();
    if (!code) { alert("先填验证码"); return; }
    $("#steam-code").value = "";
    post("/api/steam/guard", { code });
  });
  $("#steam-cancel").addEventListener("click", () => post("/api/steam/login/cancel"));
  $("#proxy-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const proxy = $("#proxy-input").value.trim();
    if (!proxy) { alert("先填代理地址，比如 http://用户名:密码@地址:端口"); return; }
    $("#proxy-status").textContent = "正在通过代理测试…";
    const j = await post("/api/proxy", { proxy });
    if (j && j.ok) $("#proxy-input").value = "";
  });
  $("#proxy-clear").addEventListener("click", () => {
    if (confirm("清除看板上设置的 Steam 代理？之后回退到 .env 里的配置（没有就直连）。")) post("/api/proxy", { proxy: null });
  });
  $("#steam-logout").addEventListener("click", () => {
    if (confirm("退出全部 Steam 账号？之后挂单价按当前最低价算。")) post("/api/steam/logout", {});
  });
  $("#steam-accounts").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-steamid]");
    if (!btn) return;
    if (confirm(`退出账号 ${btn.dataset.account}？`)) post("/api/steam/logout", { steamid: btn.dataset.steamid });
  });

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
