<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PranUltimate Scanner</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@300;400;500;600&display=swap');

    :root {
      --bg:        #0b0e13;
      --surface:   #111520;
      --border:    #1e2535;
      --accent:    #00e5ff;
      --green:     #00c896;
      --yellow:    #f5c518;
      --orange:    #ff7c3a;
      --muted:     #4a5568;
      --text:      #e2e8f0;
      --subtext:   #718096;
      --mono:      'JetBrains Mono', monospace;
      --sans:      'Inter', sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      min-height: 100vh;
    }

    /* ── Header ── */
    header {
      border-bottom: 1px solid var(--border);
      padding: 20px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      background: var(--bg);
      z-index: 10;
    }

    .logo {
      font-family: var(--mono);
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 0.08em;
      color: var(--accent);
    }

    .logo span { color: var(--subtext); font-weight: 400; }

    .scan-meta {
      font-family: var(--mono);
      font-size: 11px;
      color: var(--subtext);
      text-align: right;
      line-height: 1.6;
    }

    .scan-meta .live { color: var(--green); }

    /* ── Layout ── */
    main {
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 24px;
    }

    /* ── Timeframe selector ── */
    .tf-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 32px;
    }

    .tf-btn {
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 600;
      padding: 8px 16px;
      border-radius: 4px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--subtext);
      cursor: pointer;
      transition: all 0.15s;
      letter-spacing: 0.05em;
    }

    .tf-btn:hover { border-color: var(--accent); color: var(--accent); }

    .tf-btn.active {
      background: var(--accent);
      color: var(--bg);
      border-color: var(--accent);
    }

    /* ── Summary bar ── */
    .summary {
      display: flex;
      gap: 24px;
      margin-bottom: 24px;
      flex-wrap: wrap;
    }

    .stat {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .stat-val {
      font-family: var(--mono);
      font-size: 22px;
      font-weight: 600;
      color: var(--text);
    }

    .stat-label {
      font-size: 11px;
      color: var(--subtext);
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    /* ── Table ── */
    .table-wrap {
      overflow-x: auto;
      border-radius: 8px;
      border: 1px solid var(--border);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    thead tr {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }

    th {
      padding: 12px 16px;
      text-align: left;
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--subtext);
      white-space: nowrap;
    }

    tbody tr {
      border-bottom: 1px solid var(--border);
      transition: background 0.1s;
    }

    tbody tr:last-child { border-bottom: none; }
    tbody tr:hover { background: var(--surface); }

    td {
      padding: 13px 16px;
      vertical-align: middle;
      white-space: nowrap;
    }

    .symbol {
      font-family: var(--mono);
      font-weight: 600;
      font-size: 14px;
      color: var(--text);
      letter-spacing: 0.03em;
    }

    .price {
      font-family: var(--mono);
      font-size: 13px;
      color: var(--text);
    }

    .resistance {
      font-family: var(--mono);
      font-size: 12px;
      color: var(--subtext);
    }

    /* Status badges */
    .badge {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 3px;
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.06em;
      white-space: nowrap;
    }

    .badge-breakout    { background: rgba(0,200,150,0.15); color: var(--green); border: 1px solid rgba(0,200,150,0.3); }
    .badge-1candle     { background: rgba(245,197,24,0.12); color: var(--yellow); border: 1px solid rgba(245,197,24,0.3); }
    .badge-2candle     { background: rgba(255,124,58,0.12); color: var(--orange); border: 1px solid rgba(255,124,58,0.3); }
    .badge-near        { background: rgba(0,229,255,0.10); color: var(--accent); border: 1px solid rgba(0,229,255,0.25); }
    .badge-owned       { background: rgba(167,139,250,0.12); color: #a78bfa; border: 1px solid rgba(167,139,250,0.3); }
    .badge-nosetup     { background: rgba(74,85,104,0.12); color: var(--muted); border: 1px solid rgba(74,85,104,0.3); }
    .badge-choppy      { background: rgba(255,180,60,0.12); color: #ffb43c; border: 1px solid rgba(255,180,60,0.3); }
    .badge-touch       { background: rgba(148,163,184,0.10); color: #94a3b8; border: 1px solid rgba(148,163,184,0.25); }

    /* SP Stocks tab — accent-bordered, set apart from timeframe buttons */
    .tf-btn-sp {
      margin-left: 12px;
      border-color: var(--green);
      color: var(--green);
    }
    .tf-btn-sp:hover { border-color: var(--green); color: var(--green); }
    .tf-btn-sp.active { background: var(--green); color: var(--bg); border-color: var(--green); }

    /* Choppy Stocks tab — distinct amber accent, sits after SP Stocks */
    .tf-btn-choppy {
      margin-left: 8px;
      border-color: #ffb43c;
      color: #ffb43c;
    }
    .tf-btn-choppy:hover { border-color: #ffb43c; color: #ffb43c; }
    .tf-btn-choppy.active { background: #ffb43c; color: var(--bg); border-color: #ffb43c; }

    /* Timeframe pill in the SP table */
    .tf-pill {
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 600;
      padding: 2px 9px;
      border-radius: 3px;
      background: rgba(0,229,255,0.08);
      color: var(--accent);
      border: 1px solid rgba(0,229,255,0.2);
    }

    /* RSI pill */
    .rsi {
      font-family: var(--mono);
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 3px;
      background: rgba(0,229,255,0.08);
      color: var(--accent);
      border: 1px solid rgba(0,229,255,0.15);
    }

    /* Volume bar */
    .vol-wrap { display: flex; align-items: center; gap: 8px; }
    .vol-bar-bg { width: 60px; height: 4px; background: var(--border); border-radius: 2px; }
    .vol-bar    { height: 4px; border-radius: 2px; background: var(--accent); max-width: 60px; }
    .vol-text   { font-family: var(--mono); font-size: 11px; color: var(--subtext); }

    /* Empty state */
    .empty {
      padding: 60px 24px;
      text-align: center;
      color: var(--subtext);
      font-size: 14px;
      line-height: 1.8;
    }

    .empty strong { display: block; font-size: 18px; color: var(--muted); margin-bottom: 8px; }

    /* Loading */
    .loading {
      padding: 60px 24px;
      text-align: center;
      color: var(--subtext);
      font-family: var(--mono);
      font-size: 13px;
    }

    .dot-anim::after {
      content: '';
      animation: dots 1.2s steps(3, end) infinite;
    }

    @keyframes dots {
      0%   { content: ''; }
      33%  { content: '.'; }
      66%  { content: '..'; }
      100% { content: '...'; }
    }

    /* Responsive */
    @media (max-width: 600px) {
      header { padding: 14px 16px; }
      main   { padding: 20px 12px; }
      .tf-btn { font-size: 11px; padding: 7px 12px; }
      th, td  { padding: 10px 10px; }
    }
  </style>
</head>
<body>

<header>
  <div class="logo">PRAN<span>ULTIMATE</span> &nbsp;/&nbsp; SCANNER</div>
  <div class="scan-meta" id="meta">Loading<span class="dot-anim"></span></div>
</header>

<main>
  <!-- Timeframe buttons -->
  <div class="tf-row" id="tf-row"></div>

  <!-- Summary -->
  <div class="summary" id="summary"></div>

  <!-- Table -->
  <div id="table-container">
    <div class="loading">Fetching scan results<span class="dot-anim"></span></div>
  </div>
</main>

<script>
  const TIMEFRAMES = ["1H","2H","3H","4H","1D","1W"];
  const SP_TAB = "SP";
  const CHOPPY_TAB = "CHOPPY";
  let allData = {};
  let spData = [];
  let choppyData = [];
  let activeТF = "1D";

  // ── Fetch results ──────────────────────────────────────────────────────────
  async function fetchResults() {
    try {
      const res = await fetch("./results.json");
      const data = await res.json();

      if (data.error && !data.results) {
        document.getElementById("table-container").innerHTML =
          `<div class="empty"><strong>No scan yet</strong>${data.error}</div>`;
        document.getElementById("meta").textContent = "Awaiting first scan";
        return;
      }

      allData = data.results || {};
      spData  = data.sp_stocks || [];
      choppyData = data.choppy_stocks || [];

      // Meta
      const spUpdatedLine = data.sp_updated_at
        ? `<br>SP Stocks updated: <span class="live">${data.sp_updated_at}</span>`
        : "";
      document.getElementById("meta").innerHTML =
        `Full scan: <span class="live">${data.generated_at || "—"}</span>${spUpdatedLine}<br>
         Total signals: <span class="live">${data.total_signals || 0}</span>`;

      buildTFButtons();
      renderTable(activeТF);

    } catch (e) {
      document.getElementById("table-container").innerHTML =
        `<div class="empty"><strong>Connection error</strong>Make sure the server is running.</div>`;
    }
  }

  // ── Timeframe buttons ──────────────────────────────────────────────────────
  function buildTFButtons() {
    const row = document.getElementById("tf-row");
    row.innerHTML = "";
    TIMEFRAMES.forEach(tf => {
      const count = (allData[tf] || []).length;
      const btn = document.createElement("button");
      btn.className = "tf-btn" + (tf === activeТF ? " active" : "");
      btn.textContent = `${tf}  ${count > 0 ? `(${count})` : ""}`;
      btn.onclick = () => {
        activeТF = tf;
        document.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        renderTable(tf);
      };
      row.appendChild(btn);
    });

    // SP Stocks tab — visually separated, always last.
    const spCount = spData.filter(s => s.timeframe).length;
    const spBtn = document.createElement("button");
    spBtn.className = "tf-btn tf-btn-sp" + (activeТF === SP_TAB ? " active" : "");
    spBtn.textContent = `★ SP STOCKS ${spCount > 0 ? `(${spCount})` : ""}`;
    spBtn.onclick = () => {
      activeТF = SP_TAB;
      document.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("active"));
      spBtn.classList.add("active");
      renderSPTable();
    };
    row.appendChild(spBtn);

    // Choppy Stocks tab — no strict prior-uptrend origin, but genuinely
    // rangebound right now (no clean place in the regular tabs for these).
    const choppyCount = choppyData.length;
    const choppyBtn = document.createElement("button");
    choppyBtn.className = "tf-btn tf-btn-choppy" + (activeТF === CHOPPY_TAB ? " active" : "");
    choppyBtn.textContent = `◌ CHOPPY ${choppyCount > 0 ? `(${choppyCount})` : ""}`;
    choppyBtn.onclick = () => {
      activeТF = CHOPPY_TAB;
      document.querySelectorAll(".tf-btn").forEach(b => b.classList.remove("active"));
      choppyBtn.classList.add("active");
      renderChoppyTable();
    };
    row.appendChild(choppyBtn);
  }

  // ── Render dispatcher ──────────────────────────────────────────────────────
  function renderTable(tf) {
    if (tf === SP_TAB) { renderSPTable(); return; }
    if (tf === CHOPPY_TAB) { renderChoppyTable(); return; }
    renderTimeframeTable(tf);
  }

  // ── Render a standard timeframe table ──────────────────────────────────────
  function renderTimeframeTable(tf) {
    const stocks = allData[tf] || [];
    const container = document.getElementById("table-container");
    const summary   = document.getElementById("summary");

    // Summary stats
    const breakouts = stocks.filter(s => s.status === "BREAKOUT").length;
    const post1     = stocks.filter(s => s.status === "1 CANDLE POST BREAKOUT").length;
    const post2     = stocks.filter(s => s.status === "2 CANDLES POST BREAKOUT").length;
    const near      = stocks.filter(s => s.distance_pct !== undefined).length;

    summary.innerHTML = `
      <div class="stat"><div class="stat-val">${stocks.length}</div><div class="stat-label">Signals — ${tf}</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--green)">${breakouts}</div><div class="stat-label">Fresh Breakout</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--yellow)">${post1}</div><div class="stat-label">1 Candle Post</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--orange)">${post2}</div><div class="stat-label">2 Candles Post</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--accent)">${near}</div><div class="stat-label">Near Breakout</div></div>
    `;

    if (stocks.length === 0) {
      container.innerHTML = `<div class="empty"><strong>No signals</strong>No PranUltimate setups detected on the ${tf} timeframe in today's scan.</div>`;
      return;
    }

    // Sort: BREAKOUT first, then 1 candle, then 2 candle, then near-breakout
    // (closest to ceiling first). Near-breakout status strings are dynamic
    // ("NEAR BREAKOUT (1.2%)") so they're detected via distance_pct, not a
    // fixed status lookup.
    function sortKey(s) {
      if (s.status === "BREAKOUT") return 0;
      if (s.status === "1 CANDLE POST BREAKOUT") return 1;
      if (s.status === "2 CANDLES POST BREAKOUT") return 2;
      if (s.owned_by) return 3;           // reassigned from a lower TF
      return 4;                            // near-breakout
    }
    const sorted = [...stocks].sort((a, b) => {
      const ka = sortKey(a), kb = sortKey(b);
      if (ka !== kb) return ka - kb;
      if (ka === 4) return (a.distance_pct ?? 99) - (b.distance_pct ?? 99);
      return 0;
    });

    const rows = sorted.map(s => {
      const volRatio = Math.min((s.volume / s.vol_avg) * 30, 60);
      const volMult  = (s.volume / s.vol_avg).toFixed(1);
      const isNear   = s.distance_pct !== undefined;
      const isOwned  = s.owned_by !== undefined;

      let badgeClass = "badge-breakout";
      if (s.status === "1 CANDLE POST BREAKOUT") badgeClass = "badge-1candle";
      if (s.status === "2 CANDLES POST BREAKOUT") badgeClass = "badge-2candle";
      if (isNear) badgeClass = "badge-near";
      if (isOwned) badgeClass = "badge-owned";

      // For reassigned signals, show which lower TF the breakout actually fired on.
      const statusLabel = isOwned
        ? `OWNED (fired ${s.fired_on})`
        : s.status;

      const distCell = isNear
        ? `<span class="rsi">${s.distance_pct.toFixed(1)}%</span>`
        : `<span class="resistance">—</span>`;

      return `
        <tr>
          <td><span class="symbol">${s.symbol}</span></td>
          <td><span class="badge ${badgeClass}">${statusLabel}</span></td>
          <td><span class="price">₹${s.close.toLocaleString("en-IN")}</span></td>
          <td><span class="resistance">₹${s.resistance.toLocaleString("en-IN")}</span></td>
          <td>${distCell}</td>
          <td><span class="rsi">${s.rsi}</span></td>
          <td>
            <div class="vol-wrap">
              <div class="vol-bar-bg"><div class="vol-bar" style="width:${volRatio}px"></div></div>
              <span class="vol-text">${volMult}x</span>
            </div>
          </td>
          <td><span class="resistance">₹${s.ema200.toLocaleString("en-IN")}</span></td>
        </tr>`;
    }).join("");

    container.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Status</th>
              <th>Close</th>
              <th>Resistance</th>
              <th>Distance</th>
              <th>RSI</th>
              <th>Volume</th>
              <th>200 EMA</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // ── Render the SP Stocks table ─────────────────────────────────────────────
  function renderSPTable() {
    const container = document.getElementById("table-container");
    const summary   = document.getElementById("summary");

    const withSetup = spData.filter(s => s.timeframe).length;
    const broke     = spData.filter(s => {
      const st = s.status || "";
      return st === "BREAKOUT" || st === "1 CANDLE POST BREAKOUT" ||
             st === "2 CANDLES POST BREAKOUT" || st.startsWith("BROKE OUT");
    }).length;
    const near      = spData.filter(s => (s.status || "").startsWith("NEAR BREAKOUT")).length;
    const noSetup   = spData.filter(s => !s.timeframe).length;

    summary.innerHTML = `
      <div class="stat"><div class="stat-val">${spData.length}</div><div class="stat-label">SP Stocks</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--green)">${broke}</div><div class="stat-label">Broke Out</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--accent)">${near}</div><div class="stat-label">Near Breakout</div></div>
      <div class="stat"><div class="stat-val" style="color:var(--muted)">${noSetup}</div><div class="stat-label">No Setup</div></div>
    `;

    if (spData.length === 0) {
      container.innerHTML = `<div class="empty"><strong>No SP data</strong>Run the scanner to populate the SP Stocks list.</div>`;
      return;
    }

    const rows = spData.map(s => {
      const st = s.status || "";
      const isBreakout  = st === "BREAKOUT" || st === "1 CANDLE POST BREAKOUT" ||
                         st === "2 CANDLES POST BREAKOUT" || st.startsWith("BROKE OUT");
      const isNear      = st.startsWith("NEAR BREAKOUT");
      const isTouchOnly = st.startsWith("TOUCHED 200 EMA");
      const noSetup     = !s.timeframe;

      let badgeClass = "badge-near";
      if (st === "BREAKOUT") badgeClass = "badge-breakout";
      else if (st === "1 CANDLE POST BREAKOUT") badgeClass = "badge-1candle";
      else if (st === "2 CANDLES POST BREAKOUT" || st.startsWith("BROKE OUT")) badgeClass = "badge-2candle";
      else if (isTouchOnly) badgeClass = "badge-touch";
      else if (noSetup) badgeClass = "badge-nosetup";

      const tfCell    = s.timeframe ? `<span class="tf-pill">${s.timeframe}</span>` : `<span class="resistance">—</span>`;
      const closeCell = (s.close !== undefined && s.close !== null) ? `₹${s.close.toLocaleString("en-IN")}` : "—";
      const resCell    = (s.resistance !== undefined && s.resistance !== null) ? `₹${s.resistance.toLocaleString("en-IN")}` : "—";
      const distCell  = isNear
        ? `<span class="rsi">${s.distance_pct.toFixed(1)}%</span>`
        : (isBreakout ? `<span class="resistance">at/above</span>` : `<span class="resistance">—</span>`);
      const rsiCell   = (s.rsi !== undefined) ? s.rsi : "—";

      return `
        <tr>
          <td><span class="symbol">${s.symbol}</span></td>
          <td>${tfCell}</td>
          <td><span class="badge ${badgeClass}">${st}</span></td>
          <td><span class="price">${closeCell}</span></td>
          <td><span class="resistance">${resCell}</span></td>
          <td>${distCell}</td>
          <td><span class="rsi">${rsiCell}</span></td>
        </tr>`;
    }).join("");

    container.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Timeframe</th>
              <th>Status</th>
              <th>Close</th>
              <th>Breakout Level</th>
              <th>Distance</th>
              <th>RSI</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // ── Render the Choppy Stocks table ─────────────────────────────────────────
  // No strict prior-uptrend origin (every candidate failed the 3% margin
  // test), but genuinely rangebound right now — anchored at the lowest low
  // in the window instead of a real "first low". Only ever shows
  // CONSOLIDATING rows (no breakout states) — a stock that broke out and is
  // just drifting afterward is excluded by detect_chop_signal itself.
  function renderChoppyTable() {
    const container = document.getElementById("table-container");
    const summary   = document.getElementById("summary");

    summary.innerHTML = `
      <div class="stat"><div class="stat-val">${choppyData.length}</div><div class="stat-label">Choppy Stocks</div></div>
      <div class="stat"><div class="stat-val" style="color:#ffb43c">${choppyData.length}</div><div class="stat-label">Rangebound</div></div>
    `;

    if (choppyData.length === 0) {
      container.innerHTML = `<div class="empty"><strong>No choppy stocks</strong>Run the scanner to populate this list. These are stocks with no clear prior uptrend (so they don't qualify for the regular tabs), but that are genuinely rangebound right now.</div>`;
      return;
    }

    const rows = choppyData.map(s => {
      const closeCell = (s.close !== undefined) ? `₹${s.close.toLocaleString("en-IN")}` : "—";
      const floorCell = (s.first_low !== undefined) ? `₹${s.first_low.toLocaleString("en-IN")}` : "—";
      const resCell   = (s.resistance !== undefined) ? `₹${s.resistance.toLocaleString("en-IN")}` : "—";
      const distCell  = (s.distance_pct !== undefined) ? `<span class="rsi">${s.distance_pct.toFixed(1)}%</span>` : "—";
      const rsiCell   = (s.rsi !== undefined) ? s.rsi : "—";
      const rangeCell = (s.range_candles !== undefined) ? s.range_candles : "—";

      return `
        <tr>
          <td><span class="symbol">${s.symbol}</span></td>
          <td><span class="tf-pill">${s.timeframe}</span></td>
          <td><span class="badge badge-choppy">${s.status}</span></td>
          <td><span class="price">${closeCell}</span></td>
          <td><span class="resistance">${floorCell}</span></td>
          <td><span class="resistance">${resCell}</span></td>
          <td>${distCell}</td>
          <td><span class="rsi">${rsiCell}</span></td>
          <td><span class="resistance">${rangeCell}</span></td>
        </tr>`;
    }).join("");

    container.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Timeframe</th>
              <th>Status</th>
              <th>Close</th>
              <th>Floor</th>
              <th>Ceiling</th>
              <th>Distance</th>
              <th>RSI</th>
              <th>Candles in Box</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  fetchResults();
</script>
</body>
</html>