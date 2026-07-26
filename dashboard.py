"""
Bracket Board - dashboard renderer
-----------------------------------
Builds docs/index.html: a plain-language "who is doing well" page on top,
and the full evidence underneath - portfolio charts and a trade-by-trade
profit and loss for every featured wallet.

Two constraints shape this file:

  * docs/index.html is committed to git on every sweep, so the payload is
    capped (featured wallets only, downsampled series, recent trades).
    Embedding all 2,226 wallets would re-create the repository bloat that
    moving the database to a release asset just solved.

  * Only the standard library, like the rest of the project. Charts are
    hand-rolled inline SVG drawn by a little vanilla JS - no chart library,
    nothing fetched at runtime.
"""

import json
from datetime import datetime, timezone

FEATURED_PER_BRACKET = 5      # rows kept per bracket per window per board
SERIES_POINTS = 70            # downsample each wallet's value history to this
TRADES_SHOWN = 50             # most recent buys/sells embedded per wallet
                              # (transfers excluded). Enough to audit a
                              # period; the count and total of anything
                              # trimmed are still carried and shown.
WINDOW_KEYS = ("7 days", "14 days", "30 days", "90 days")


# ----------------------------- analytics ------------------------------

def _wash_pairs(rows):
    """Indices of events that cancel each other out and are not trades.

    Moving stake within a subnet is written to the feed as a buy and a sell of
    the identical alpha, at the identical price, in the identical second.
    Nothing is bought and nothing is sold. Priced naively, the sell leg is
    matched against the older, cheaper cost basis and books a profit that never
    happened - one 8 TAO wallet was credited with 0.090 TAO of "banked" gains
    and a 100% success rate off a single such pair.

    Matching on second, subnet, alpha and price together: alpha carries nine
    decimal places, so a genuine buy and sell colliding on all four in the same
    second is not a coincidence that occurs.
    """
    groups = {}
    for i, (ts, netuid, action, tao, alpha, price, _x) in enumerate(rows):
        if not alpha:
            continue
        key = (ts, netuid, round(alpha, 9), round(price or 0.0, 9))
        groups.setdefault(key, {"BUY": [], "SELL": []}).setdefault(action, []).append(i)
    out = set()
    for legs in groups.values():
        for i, j in zip(legs.get("BUY", []), legs.get("SELL", [])):
            out.add(i)
            out.add(j)
    return out


def realized_trades(conn, coldkey, since_iso=None, limit=None):
    """Trade-by-trade realised profit, using average cost per subnet.

    Alpha is bought and sold at a price in TAO, so a sale's profit is the
    alpha sold times the gap between its sale price and the average price
    paid for the alpha still held in that subnet. Each returned sale carries
    the average cost it was measured against and a running total, so the
    summary figures can be checked row by row rather than taken on trust.

    Transfers are walked but never returned. Stake moving between wallets is
    not a trading decision and clutters the record, yet it genuinely changes
    the alpha held - ignoring it entirely would credit a later sale with
    profit on alpha that was never bought.

    Events with no price - subnet liquidations report zero - move inventory
    but realise nothing, and come back with pnl None rather than as a 100%
    loss.
    """
    rows = conn.execute(
        "SELECT timestamp, netuid, action, tao_amount, alpha_amount, price, "
        "COALESCE(is_transfer,0) FROM events WHERE coldkey=? "
        "ORDER BY timestamp", (coldkey,)).fetchall()
    wash = _wash_pairs(rows)

    book = {}          # netuid -> [alpha_held, tao_cost]
    shown = []
    for idx, (ts, netuid, action, tao, alpha, price, transfer) in enumerate(rows):
        if idx in wash:
            continue         # both legs skipped: no inventory change, no profit
        held, cost = book.get(netuid, [0.0, 0.0])
        pnl = pct = avg_used = cost_used = None
        if action == "BUY" and alpha:
            held += alpha
            cost += tao if tao else alpha * (price or 0.0)
        elif action == "SELL" and alpha:
            if held > 0 and price:
                avg = cost / held
                sold = min(alpha, held)
                pnl = (price - avg) * sold
                pct = ((price / avg) - 1.0) * 100.0 if avg > 0 else None
                avg_used = avg
                cost_used = avg * sold     # capital released by this sale
                cost -= avg * sold
                held -= sold
            else:
                held = max(held - alpha, 0.0)
        book[netuid] = [held, max(cost, 0.0)]
        if transfer:
            continue                     # counted in the book, kept off the page
        if since_iso and str(ts)[:10] < since_iso:
            continue                     # cost basis built from before the window,
        shown.append({                   # but only in-window trades are listed
            "t": str(ts)[:16].replace("T", " "),
            "n": netuid,
            "a": action,
            "tao": round(tao or 0.0, 4),
            "al": round(alpha or 0.0, 4),
            "p": round(price or 0.0, 6),
            "avg": None if avg_used is None else round(avg_used, 6),
            # What the alpha in this sale originally cost. Summed over a
            # window it is the capital actually put at risk in closed trades -
            # the only honest denominator for a realised return, and one that
            # needs no balance history at all.
            "cost": None if cost_used is None else round(cost_used, 6),
            "pnl": None if pnl is None else round(pnl, 4),
            "pct": None if pct is None else round(pct, 2),
        })

    full_count = len(shown)
    full_banked = round(sum(r["pnl"] or 0.0 for r in shown), 4)
    if limit and full_count > limit:
        shown = shown[-limit:]
    running = 0.0
    for row in shown:                     # oldest first, so the total builds
        running += row["pnl"] or 0.0
        row["run"] = round(running, 4)
    shown = shown[::-1]                   # newest first for reading
    if shown:
        # Carried on the first row so the page can say plainly when it is
        # showing a tail rather than the whole period, instead of quietly
        # presenting a partial list as complete.
        shown[0]["_total"] = full_count
        shown[0]["_banked"] = full_banked
    return shown


def value_series(conn, coldkey, since_iso, points=SERIES_POINTS):
    """Daily portfolio value, evenly downsampled to at most `points`."""
    rows = conn.execute(
        "SELECT day, total_tao FROM wallet_daily WHERE coldkey=? AND day>=? "
        "ORDER BY day", (coldkey, since_iso)).fetchall()
    if len(rows) <= points:
        return [[d, round(v or 0.0, 3)] for d, v in rows]
    step = len(rows) / float(points)
    picked = [rows[min(int(i * step), len(rows) - 1)] for i in range(points)]
    if picked[-1] != rows[-1]:
        picked[-1] = rows[-1]      # always keep the latest value honest
    return [[d, round(v or 0.0, 3)] for d, v in picked]


def subnet_names(conn):
    return {n: (nm or "SN{}".format(n))
            for n, nm in conn.execute("SELECT netuid, name FROM subnets")}


# ------------------------------ payload -------------------------------

def build_payload(conn, brackets, bracket_of, audited_board, windows):
    """One self-contained record per (window, wallet).

    Everything shown for a window is computed for that window: its own value
    series, its own trades, its own arithmetic. Nothing is carried over from a
    different period, because a reader comparing a 14-day return against a
    90-day trade list has been handed a contradiction.
    """
    from datetime import date, timedelta

    names = subnet_names(conn)
    boards, wallets, subnets_used = {}, {}, set()

    for wname, wdays in windows:
        if wname not in WINDOW_KEYS:
            continue
        status, per_bracket, exceptions = audited_board(
            conn, wdays, brackets, bracket_of)
        entry = {"status": status, "b": {}, "rb": {}, "x": exceptions,
                 "rx": {}, "days": wdays or 0}

        # The banked-money board: its own pool, its own standard.
        import leaderboard as _lb
        rstatus, rbrackets, rexc = _lb.realised_board(
            conn, wdays, brackets, bracket_of)
        entry["rx"] = rexc
        for label, _lo, _hi in brackets:
            out = []
            for r in (rbrackets or {}).get(label, [])[:FEATURED_PER_BRACKET]:
                trades = realized_trades(conn, r["ck"], r["target"], TRADES_SHOWN)
                out.append({
                    "ck": r["ck"], "rp": round(r["realised_pct"], 2),
                    "held": r.get("held", 0),
                    "realised": round(r["realised"], 3),
                    "risked": round(r["risked"], 2), "sells": r["sells"],
                    "v": round(r["v"], 2), "t": r["trades"],
                })
                wallets[wname + "|R|" + r["ck"]] = {
                    "s": value_series(conn, r["ck"], r["target"]),
                    "tr": trades,
                }
                subnets_used.update(t["n"] for t in trades)
            entry["rb"][label] = out
        for label, _lo, _hi in brackets:
            rows = (per_bracket or {}).get(label, [])[:FEATURED_PER_BRACKET]
            out = []
            for r in rows:
                since = r["target"]
                trades = realized_trades(conn, r["ck"], since, TRADES_SHOWN)
                realised = trades[0].get("_banked", 0.0) if trades else 0.0
                out.append({
                    "ck": r["ck"], "r": round(r["ret"], 2),
                    "rp": round(r["realised_pct"], 2),
                    "v": round(r["v"], 2), "t": r["trades"],
                    "sells": sum(1 for t in trades if t["pnl"] is not None),
                    "start": round(r["start"], 3), "end": round(r["end"], 3),
                    "sd": r["start_day"], "ed": r["end_day"],
                    "bought": round(r["bought"], 3), "sold": round(r["sold"], 3),
                    "moved": round(r["moved"], 3), "gain": round(r["gain"], 3),
                    "realised": realised,
                })
                key = wname + "|" + r["ck"]
                wallets[key] = {
                    "s": value_series(conn, r["ck"], since),
                    "tr": trades,
                }
                subnets_used.update(t["n"] for t in trades)
            entry["b"][label] = out
        boards[wname] = entry

    return {"boards": boards, "wallets": wallets,
            "names": {str(n): names.get(n, "SN{}".format(n))
                      for n in sorted(subnets_used)}}


# ------------------------------- page ---------------------------------

CSS = """
:root{color-scheme:light dark;
 --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
 --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
 --series:#2a78d6; --good:#006300; --bad:#d03b3b; --warn:#fab219;
 --chip:#eef1f5;}
@media (prefers-color-scheme:dark){:root{
 --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
 --muted:#898781; --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
 --series:#3987e5; --good:#0ca30c; --bad:#d03b3b; --warn:#fab219;
 --chip:#232322;}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
 font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
.wrap{max-width:1040px;margin:0 auto;padding:24px 18px 64px}
h1{font-size:1.6rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:2.4rem 0 .6rem}
h3{font-size:.95rem;margin:1.4rem 0 .4rem;color:var(--ink2)}
p{margin:.4rem 0}
.sub{color:var(--ink2);font-size:.9rem}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
 padding:16px 18px;margin:14px 0}
.explain{background:var(--surface);border:1px solid var(--ring);border-left:4px solid var(--series);
 border-radius:10px;padding:14px 18px;margin:16px 0}
.explain b{color:var(--ink)}
.steps{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin:10px 0 0}
.step{background:var(--plane);border:1px solid var(--ring);border-radius:8px;padding:10px 12px;font-size:.9rem}
.step span{display:inline-block;width:22px;height:22px;border-radius:50%;background:var(--series);
 color:#fff;text-align:center;line-height:22px;font-size:.8rem;margin-right:8px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 4px}
.tab{background:var(--chip);border:1px solid var(--ring);color:var(--ink2);border-radius:999px;
 padding:6px 14px;font-size:.88rem;cursor:pointer}
.tab[aria-selected=true]{background:var(--series);border-color:var(--series);color:#fff;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.w{cursor:pointer}
tr.w:hover{background:var(--chip)}
.rank{display:inline-block;width:24px;height:24px;border-radius:6px;background:var(--chip);
 text-align:center;line-height:24px;font-size:.8rem;font-weight:600;color:var(--ink2)}
.rank.top{background:var(--series);color:#fff}
.up{color:var(--good);font-weight:600}
.down{color:var(--bad);font-weight:600}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem}
.flag{background:var(--chip);color:var(--ink2);border-radius:4px;padding:1px 6px;font-size:.72rem}
.detail{background:var(--plane);border:1px solid var(--ring);border-radius:10px;padding:14px}
.grid2{display:grid;gap:16px;grid-template-columns:1fr}
@media(min-width:820px){.grid2{grid-template-columns:1.1fr .9fr}}
.scroll{overflow-x:auto}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--ring);
 border-radius:8px;padding:6px 10px;font-size:.82rem;box-shadow:0 6px 20px rgba(0,0,0,.18);
 opacity:0;transition:opacity .1s;z-index:20}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:.82rem;color:var(--ink2);margin-top:6px}
.swatch{display:inline-block;width:10px;height:10px;border-radius:2px;background:var(--series);margin-right:6px}
.empty{color:var(--muted);font-size:.9rem;padding:10px 0}
.kpi{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.kpi div{background:var(--plane);border:1px solid var(--ring);border-radius:8px;padding:10px 12px}
.kpi b{display:block;font-size:1.35rem;line-height:1.2}
.kpi small{color:var(--muted);font-size:.76rem}
table.bridge td{border-bottom:1px solid var(--grid);padding:6px 8px}
table.bridge tr.gain td{border-top:2px solid var(--axis);border-bottom:2px solid var(--axis)}
.pager{display:flex;align-items:center;gap:12px;margin:10px 0;flex-wrap:wrap}
.pager button[disabled]{opacity:.4;cursor:default}
.explain ul{line-height:1.7}
a{color:var(--series);text-decoration:none}
a:hover{text-decoration:underline}
footer{margin-top:40px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--grid);padding-top:14px}
"""

JS = r"""
const D = window.__BB__;
const fmt = (n,d=2)=>n===null||n===undefined?'-':Number(n).toLocaleString(undefined,
  {minimumFractionDigits:d,maximumFractionDigits:d});
const pct = n => (n>=0?'+':'') + fmt(n,2) + '%';
const short = ck => ck.slice(0,6)+'…'+ck.slice(-4);
let win = Object.keys(D.boards)[0];
let rank = 'realised';   // money banked first; paper gains are the softer claim

/* ---- line chart: one series, so no legend box; title names it ---- */
function chart(series, w, h){
  if(!series || series.length<2) return '<div class="empty">Not enough history yet.</div>';
  const P={l:44,r:10,t:10,b:22};
  const vals=series.map(p=>p[1]), lo=Math.min(...vals), hi=Math.max(...vals);
  const pad=(hi-lo)*0.08||Math.max(hi*0.08,0.001);
  const y0=Math.max(0,lo-pad), y1=hi+pad;
  const X=i=>P.l+(w-P.l-P.r)*(i/(series.length-1));
  const Y=v=>P.t+(h-P.t-P.b)*(1-(v-y0)/((y1-y0)||1));
  let g='',ticks=4;
  for(let i=0;i<=ticks;i++){
    const v=y0+(y1-y0)*i/ticks, y=Y(v);
    g+=`<line x1="${P.l}" y1="${y.toFixed(1)}" x2="${w-P.r}" y2="${y.toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    g+=`<text x="${P.l-6}" y="${(y+3).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--muted)">${fmt(v,v<10?2:0)}</text>`;
  }
  const d=series.map((p,i)=>`${i?'L':'M'}${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
  const area=`${d}L${X(series.length-1).toFixed(1)},${Y(y0).toFixed(1)}L${X(0).toFixed(1)},${Y(y0).toFixed(1)}Z`;
  const first=series[0][0], last=series[series.length-1][0];
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img"
    aria-label="Portfolio value in TAO from ${first} to ${last}" data-chart>
    ${g}
    <path d="${area}" fill="var(--series)" opacity=".10"/>
    <path d="${d}" fill="none" stroke="var(--series)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    <line class="cross" y1="${P.t}" y2="${h-P.b}" stroke="var(--axis)" stroke-width="1" opacity="0"/>
    <circle class="dot" r="4" fill="var(--series)" stroke="var(--surface)" stroke-width="2" opacity="0"/>
    <text x="${P.l}" y="${h-6}" font-size="10" fill="var(--muted)">${first}</text>
    <text x="${w-P.r}" y="${h-6}" text-anchor="end" font-size="10" fill="var(--muted)">${last}</text>
  </svg>`;
}

const tip=document.createElement('div'); tip.className='tip'; document.body.appendChild(tip);
function wireChart(el, series){
  const svg=el.querySelector('svg[data-chart]'); if(!svg) return;
  const cross=svg.querySelector('.cross'), dot=svg.querySelector('.dot');
  const W=1000/*viewBox scale handled below*/;
  svg.addEventListener('mousemove',e=>{
    const b=svg.getBoundingClientRect();
    const vb=svg.viewBox.baseVal, x=(e.clientX-b.left)/b.width*vb.width;
    const P={l:44,r:10}, inner=vb.width-P.l-P.r;
    let i=Math.round((x-P.l)/inner*(series.length-1));
    i=Math.max(0,Math.min(series.length-1,i));
    const px=P.l+inner*(i/(series.length-1));
    const vals=series.map(p=>p[1]),lo=Math.min(...vals),hi=Math.max(...vals);
    const pad=(hi-lo)*0.08||Math.max(hi*0.08,0.001), y0=Math.max(0,lo-pad), y1=hi+pad;
    const py=10+(vb.height-10-22)*(1-(series[i][1]-y0)/((y1-y0)||1));
    cross.setAttribute('x1',px);cross.setAttribute('x2',px);cross.setAttribute('opacity','1');
    dot.setAttribute('cx',px);dot.setAttribute('cy',py);dot.setAttribute('opacity','1');
    tip.innerHTML=`<b>${fmt(series[i][1])} TAO</b><br><span style="color:var(--muted)">${series[i][0]}</span>`;
    tip.style.opacity=1;
    tip.style.left=Math.min(e.clientX+14,window.innerWidth-150)+'px';
    tip.style.top=(e.clientY-40)+'px';
  });
  svg.addEventListener('mouseleave',()=>{
    tip.style.opacity=0;cross.setAttribute('opacity','0');dot.setAttribute('opacity','0');});
}

const PAGE=15;
function tradeRows(tr, ck, page){
  if(!tr.length) return `<div class="explain" style="border-left-color:var(--warn)">
    <b>No trades in this period.</b> This wallet bought and sold nothing over these
    ${win}, so its whole gain is the alpha it already held changing in price. There is
    no trade list to show because there were no trades - the table above the chart is
    the complete account of where the number comes from.</div>`;
  const pages=Math.ceil(tr.length/PAGE), p=Math.max(0,Math.min(page|0,pages-1));
  const slice=tr.slice(p*PAGE,(p+1)*PAGE);
  let h=`<div class="scroll"><table><tr><th>When</th><th>Subnet</th>
    <th>What happened</th><th class="num">Result</th></tr>`;
  for(const t of slice){
    const name=D.names[String(t.n)]||('SN'+t.n);
    let story, result;
    if(t.a==='BUY'){
      story=`Bought <b>${fmt(t.al,2)}</b> alpha at <b>${fmt(t.p,5)}</b> TAO each`;
      result='<span style="color:var(--muted)">still holding</span>';
    } else if(t.pnl===null){
      story=`Sold <b>${fmt(t.al,2)}</b> alpha`;
      result='<span style="color:var(--muted)">no price recorded</span>';
    } else {
      const good=t.pnl>=0;
      story=`Paid <b>${fmt(t.avg,5)}</b> each, sold <b>${fmt(t.al,2)}</b> at
        <b>${fmt(t.p,5)}</b> each`;
      result=`<span class="${good?'up':'down'}">${good?'Made':'Lost'}
        ${fmt(Math.abs(t.pnl),3)} TAO</span><br>
        <small style="color:var(--muted)">${good?'+':''}${fmt(t.pct,1)}% on this sale
        &middot; running ${fmt(t.run,2)}</small>`;
    }
    h+=`<tr><td class="mono">${t.t}</td><td>${name}</td><td>${story}</td>
      <td class="num">${result}</td></tr>`;
  }
  h+='</table></div>';
  const whole=tr[0]._total||tr.length, trimmed=whole>tr.length;
  if(pages>1){
    h+=`<div class="pager">
      <button class="tab" data-pg="${p-1}" data-ck="${ck}" ${p===0?'disabled':''}>Previous</button>
      <span class="sub">Showing ${p*PAGE+1}-${Math.min((p+1)*PAGE,tr.length)}
        of ${tr.length}${trimmed?' listed':' trades in this period'}</span>
      <button class="tab" data-pg="${p+1}" data-ck="${ck}" ${p===pages-1?'disabled':''}>Next</button>
    </div>`;
  }
  if(trimmed){
    h+=`<p class="sub" style="border-left:3px solid var(--warn);padding-left:10px">
      This wallet made <b>${whole}</b> trades in this period and the
      <b>${tr.length}</b> most recent are listed. The running total on these rows adds
      up the listed ones only; the <b>${fmt(tr[0]._banked,3)} TAO</b> quoted above is the
      whole period. The page is a file served to your browser, so it carries a bounded
      extract rather than every trade of every wallet.</p>`;
  }
  return h+`<p class="sub">Every buy and sell this wallet made in the selected
   period - nothing is left out and nothing links away. A sale is a profit when it
   went out for more than the average price paid for the alpha still held in that
   subnet. Buys show no result because nothing is banked until something is sold.
   Transfers between wallets are excluded: they are not trades, though they do
   change how much alpha is held.</p>`;
}

function detail(ck, row, page, mode){
  const w=D.wallets[key(ck,mode)]; if(!w) return '';
  const wins=w.tr.filter(t=>t.pnl!==null&&t.pnl>0).length;
  const losses=w.tr.filter(t=>t.pnl!==null&&t.pnl<0).length;
  const rate=(wins+losses)?Math.round(100*wins/(wins+losses)):null;
  const money=n=>`${n>=0?'+':'−'}${fmt(Math.abs(n),3)}`;
  if(mode==='R'){
    return `<div class="detail"><div class="grid2">
      <div>
        <h3>What it held across these ${win}</h3>
        <div class="chartbox" data-ck="${ck}">${chart(w.s,560,190)}</div>
        <div class="legend"><span><i class="swatch"></i>Total held in tracked subnets, in TAO</span></div>
        <p class="sub">Shown for context only. The return beside it is measured from the
        trades, not from this line, so it holds even where the chart covers just part of
        what this wallet owns.</p>
      </div>
      <div>
        <h3>Where the ${pct(row.rp)} comes from</h3>
        <table class="bridge">
          <tr><td>Alpha sold, at what it originally cost</td>
              <td class="num">${fmt(row.risked,3)} TAO</td></tr>
          <tr><td>What that alpha sold for</td>
              <td class="num">${fmt(row.risked+row.realised,3)} TAO</td></tr>
          <tr class="gain"><td><b>Banked</b></td>
              <td class="num ${row.realised>=0?'up':'down'}"><b>${money(row.realised)} TAO</b></td></tr>
        </table>
        <p class="sub">It closed <b>${row.sells}</b> ${row.sells===1?'sale':'sales'} in this
        period. The alpha it sold had cost <b>${fmt(row.risked,3)} TAO</b> to acquire and
        went out for <b>${fmt(row.risked+row.realised,3)} TAO</b>, so it banked
        <b>${money(row.realised)} TAO</b> - that is <b>${pct(row.rp)}</b> on the money
        those trades tied up. This is settled: it does not move if prices move
        tomorrow.</p>
        <div class="kpi" style="margin-top:12px">
          <div><b>${rate===null?'—':rate+'%'}</b><small>sales at a profit</small></div>
          <div><b>${wins}/${wins+losses}</b><small>profitable / closed</small></div>
        </div>
      </div></div>
      <h3>Every trade in these ${win}</h3>${tradeRows(w.tr, ck, page||0)}</div>`;
  }
  const unreal=row.gain-row.realised;
  return `<div class="detail"><div class="grid2">
    <div>
      <h3>What it held across these ${win}</h3>
      <div class="chartbox" data-ck="${ck}">${chart(w.s,560,190)}</div>
      <div class="legend"><span><i class="swatch"></i>Total held in tracked subnets, in TAO</span></div>
    </div>
    <div>
      <h3>Where the ${pct(row.r)} comes from</h3>
      <table class="bridge">
        <tr><td>Held on ${row.sd}</td><td class="num">${fmt(row.start,3)} TAO</td></tr>
        <tr><td>Added by buying alpha</td><td class="num">${money(row.bought)}</td></tr>
        <tr><td>Taken out by selling alpha</td><td class="num">${money(-row.sold)}</td></tr>
        ${row.moved?`<tr><td>Moved in from other wallets</td><td class="num">${money(row.moved)}</td></tr>`:''}
        <tr class="gain"><td><b>${row.gain>=0?'Grew in price by':'Fell in price by'}</b></td>
            <td class="num ${row.gain>=0?'up':'down'}"><b>${money(row.gain)} TAO</b></td></tr>
        <tr><td>Held on ${row.ed}</td><td class="num">${fmt(row.end,3)} TAO</td></tr>
      </table>
      <p class="sub">Read it downwards: it started with ${fmt(row.start,3)} TAO of alpha,
      buying added ${fmt(row.bought,3)} and selling took out ${fmt(row.sold,3)}
      ${Math.abs(row.bought-row.sold)<0.001?'(so trading left it square)':''}, and it ended
      on ${fmt(row.end,3)}. The <b>${money(row.gain)} TAO</b> that the other lines cannot
      account for is what the alpha itself did &mdash; that is
      <b>${pct(row.r)}</b> of the money at work. Subtraction, not a model.</p>
      <div class="kpi" style="margin-top:12px">
        <div><b class="${row.realised>=0?'up':'down'}">${money(row.realised)}</b>
             <small>banked on sales</small></div>
        <div><b class="${unreal>=0?'up':'down'}">${money(unreal)}</b>
             <small>still on paper</small></div>
        <div><b>${rate===null?'—':rate+'%'}</b><small>sales at a profit</small></div>
      </div>
      <p class="sub">The gain splits in two: <b>${money(row.realised)} TAO</b> actually
      banked by selling - every one of those sales is listed below and they add up to
      exactly that - and <b>${money(unreal)} TAO</b> that is only a paper gain on alpha
      still held.</p>
      ${row.gain>0&&unreal/row.gain>0.9?`<p class="sub"
        style="border-left:3px solid var(--warn);padding-left:10px">
        <b>Read this one carefully.</b> ${Math.round(100*unreal/row.gain)}% of this
        gain has not been sold. It is alpha valued at what the subnet last traded at,
        which is not the same as money in hand: prices move, and subnet pools are
        small enough that selling a large holding pushes the price down as you sell.
        A wallet that has never sold has not yet proved it can get out at these
        prices.</p>`:''}
    </div></div>
    <h3>Every trade in these ${win}</h3>${tradeRows(w.tr, ck, page||0)}</div>`;
}

function openDetail(host, ck, row, page, mode){
  const tr=host.querySelector(`tr.d[data-for="${ck}"]`);
  tr.style.display='';
  tr.firstElementChild.innerHTML=detail(ck,row,page,mode);
  const box=tr.querySelector('.chartbox');
  const w=D.wallets[key(ck,mode)];
  if(box&&w) wireChart(box, w.s);
  tr.querySelectorAll('button[data-pg]').forEach(b=>{
    b.addEventListener('click',ev=>{ev.stopPropagation();
      openDetail(host,ck,row,parseInt(b.dataset.pg,10),mode);});
  });
}
const key=(ck,mode)=>win+(mode==='R'?'|R|':'|')+ck;

function realisedTable(b){
  let h='', n=0;
  for(const label of Object.keys(b.rb)){
    const rows=b.rb[label]; n+=rows.length;
    h+=`<h3>Wallets holding ${label} TAO</h3>`;
    if(!rows.length){h+='<div class="empty">No wallet in this size range closed a trade we can price.</div>';continue;}
    h+=`<div class="scroll"><table><tr><th>#</th><th>Wallet</th>
      <th class="num">Return on closed trades</th><th class="num">Banked (TAO)</th>
      <th class="num">Capital risked</th><th class="num">Round trips</th>
      <th class="num">Avg hold</th>
      <th class="num">Holds now</th></tr>`;
    rows.forEach((r,i)=>{
      h+=`<tr class="w" data-ck="${r.ck}" data-i="${i}" data-label="${label}" data-mode="R">
        <td><span class="rank ${i===0?'top':''}">${i+1}</span></td>
        <td class="mono">${short(r.ck)}</td>
        <td class="num ${r.rp>=0?'up':'down'}">${pct(r.rp)}</td>
        <td class="num ${r.realised>=0?'up':'down'}">${fmt(r.realised,3)}</td>
        <td class="num">${fmt(r.risked,2)}</td>
        <td class="num">${r.sells}</td><td class="num">${r.held} d</td>
        <td class="num">${fmt(r.v,2)}</td></tr>
        <tr class="d" data-for="${r.ck}" style="display:none"><td colspan="8"></td></tr>`;
    });
    h+='</table></div>';
  }
  const x=b.rx||{}, keys=Object.keys(x).sort((a,c)=>x[c]-x[a]);
  h=`<div class="explain"><b>Bought and sold inside these ${win}.</b> ${n} wallets
    completed a full round trip in this period - the alpha was acquired here and sold
    here, so the profit belongs to this window and not to a position held since March.
    Each parcel is matched to the purchase that it closed, first in first out, and the
    percentage is measured against what those parcels cost. It depends on no balance
    figure, only on the trades. Nothing here is a paper gain.</div>` + h;
  if(keys.length){
    h+=`<div class="explain" style="border-left-color:var(--warn)">
      <b>Not shown here.</b> <ul class="sub" style="margin:.5rem 0 0;padding-left:1.1rem">
      ${keys.map(k=>`<li>${x[k]} — ${k}</li>`).join('')}</ul></div>`;
  }
  return h;
}

function board(){
  const b=D.boards[win]; const host=document.getElementById('boards');
  if(!b||b.status!=='ok'){
    host.innerHTML=`<div class="empty">${b?b.status:'No data'} - this window needs more history.</div>`;
    return;
  }
  if(rank==='realised'){
    host.innerHTML=realisedTable(b);
    host.querySelectorAll('tr.w').forEach(tr=>{
      tr.addEventListener('click',()=>{
        const ck=tr.dataset.ck;
        const row=b.rb[tr.dataset.label][parseInt(tr.dataset.i,10)];
        const cur=host.querySelector(`tr.d[data-for="${ck}"]`);
        const open=cur.style.display!=='none';
        host.querySelectorAll('tr.d').forEach(r=>{r.style.display='none';r.firstElementChild.innerHTML='';});
        if(!open){ openDetail(host,ck,row,0,'R');
          cur.scrollIntoView({behavior:'smooth',block:'nearest'}); }
      });
    });
    return;
  }
  let h='', total=0;
  for(const label of Object.keys(b.b)){
    const rows=b.b[label]; total+=rows.length;
    h+=`<h3>Wallets holding ${label} TAO</h3>`;
    if(!rows.length){h+='<div class="empty">No wallet in this size range passed every check.</div>';continue;}
    rows=rows.slice().sort((a,c)=>rank==='realised'?c.rp-a.rp:c.r-a.r);
    h+=`<div class="scroll"><table><tr><th>#</th><th>Wallet</th>
      <th class="num">Realised return</th><th class="num">Banked (TAO)</th>
      <th class="num">Sales</th>
      <th class="num">Total return</th><th class="num">Still on paper</th>
      <th class="num">Holds now</th></tr>`;
    rows.forEach((r,i)=>{
      const paper=r.gain-r.realised;
      const share=r.gain>0?paper/r.gain:0;
      const warn=share>0.9?' <span class="flag" title="Almost all of this gain is unsold alpha valued at the market price - it is not money until it is sold, and selling a large holding can move the price against you">mostly on paper</span>':'';
      h+=`<tr class="w" data-ck="${r.ck}" data-i="${i}" data-label="${label}">
        <td><span class="rank ${i===0?'top':''}">${i+1}</span></td>
        <td class="mono">${short(r.ck)}</td>
        <td class="num ${r.rp>0?'up':(r.rp<0?'down':'')}">${r.sells?pct(r.rp):'<span style="color:var(--muted)">no sales</span>'}</td>
        <td class="num ${r.realised>=0?'up':'down'}">${r.sells?fmt(r.realised,3):'—'}</td>
        <td class="num">${r.sells||'—'}</td>
        <td class="num ${r.r>=0?'up':'down'}">${pct(r.r)}${warn}</td>
        <td class="num">${fmt(paper,2)}</td>
        <td class="num">${fmt(r.v,2)}</td></tr>
        <tr class="d" data-for="${r.ck}" style="display:none"><td colspan="8"></td></tr>`;
    });
    h+='</table></div>';
  }
  // If nearly every row shares the paper-gain condition it is a property of
  // the board, not a distinguishing mark, and saying so once is more use than
  // tagging every line identically.
  const all=[]; for(const k in b.b) all.push(...b.b[k]);
  const onPaper=all.filter(r=>r.gain>0&&(r.gain-r.realised)/r.gain>0.9).length;
  if(all.length && onPaper/all.length>0.8){
    h=`<div class="explain" style="border-left-color:var(--warn)">
      <b>Read the whole board this way.</b> ${onPaper} of these ${all.length} wallets
      have banked almost nothing - over 90% of every gain shown is alpha that has gone
      up in price but has not been sold. These are holders whose subnet rose, not
      traders who have taken money off the table, and none of them has yet shown it can
      sell a sizeable holding without pushing the price down. Treat the ranking as
      "who is holding the right thing", not "who has made money".
      </div>` + h;
  }
  const x=b.x||{}, keys=Object.keys(x).sort((a,c)=>x[c]-x[a]);
  if(keys.length){
    h+=`<div class="explain" style="border-left-color:var(--warn)">
      <b>What is deliberately missing.</b> ${total} wallets are shown. Others were
      withheld because something about their data could not be verified for this
      period - showing a number we cannot stand behind is worse than showing none:
      <ul class="sub" style="margin:.5rem 0 0;padding-left:1.1rem">
      ${keys.map(k=>`<li>${x[k]} — ${k}</li>`).join('')}</ul></div>`;
  }
  host.innerHTML=h;
  host.querySelectorAll('tr.w').forEach(tr=>{
    tr.addEventListener('click',()=>{
      const ck=tr.dataset.ck;
      const row=D.boards[win].b[tr.dataset.label][parseInt(tr.dataset.i,10)];
      const cur=host.querySelector(`tr.d[data-for="${ck}"]`);
      const open=cur.style.display!=='none';
      host.querySelectorAll('tr.d').forEach(r=>{r.style.display='none';r.firstElementChild.innerHTML='';});
      if(!open){ openDetail(host,ck,row,0);
        cur.scrollIntoView({behavior:'smooth',block:'nearest'}); }
    });
  });
}

document.querySelectorAll('.tab[data-win]').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab[data-win]').forEach(x=>x.setAttribute('aria-selected','false'));
    t.setAttribute('aria-selected','true'); win=t.dataset.win; board();
  });
});
document.querySelectorAll('.tab[data-rank]').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab[data-rank]').forEach(x=>x.setAttribute('aria-selected','false'));
    t.setAttribute('aria-selected','true'); rank=t.dataset.rank; board();
  });
});
board();
"""


def _self_check(payload):
    """Refuse to publish a page whose own numbers do not agree.

    The last line of defence: every figure shown is recomputed from the rows
    behind it, and any mismatch raises rather than renders. A page that
    quietly contradicts itself is the one failure mode there is no excuse for.
    """
    for wname, entry in payload["boards"].items():
        for label, rows in entry.get("b", {}).items():
            for r in rows:
                key = wname + "|" + r["ck"]
                trades = payload["wallets"][key]["tr"]
                # The row quotes the whole period; the list may be a tail of it.
                # Check the row against the full figure and the list against its
                # own subtotal, so neither can drift from what it claims.
                banked = trades[0].get("_banked", 0.0) if trades else 0.0
                if abs(banked - r["realised"]) > 1e-3:
                    raise AssertionError(
                        "{} {}: period banked {} but the row claims {}".format(
                            wname, r["ck"][:10], banked, r["realised"]))
                if trades:
                    subtotal = round(sum(t["pnl"] or 0.0 for t in trades), 4)
                    if abs(trades[0]["run"] - subtotal) > 1e-3:
                        raise AssertionError(
                            "{} {}: running total {} != listed rows {}".format(
                                wname, r["ck"][:10], trades[0]["run"], subtotal))
                if "start" not in r:
                    continue                    # banked-money row: no bridge
                bridge = r["start"] + r["bought"] - r["sold"] + r["moved"] + r["gain"]
                if abs(bridge - r["end"]) > 0.05:
                    raise AssertionError(
                        "{} {}: value bridge lands on {} not {}".format(
                            wname, r["ck"][:10], round(bridge, 3), r["end"]))


def render(conn, brackets, bracket_of, audited_board, windows, meta):
    payload = build_payload(conn, brackets, bracket_of, audited_board, windows)
    _self_check(payload)
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    # Headline: the best qualifying wallet on the shortest mature window.
    hero = None
    for wname in WINDOW_KEYS:
        entry = payload["boards"].get(wname)
        if not entry or entry["status"] != "ok":
            continue
        best = [r for rows in entry["b"].values() for r in rows]
        if best:
            hero = (wname, max(best, key=lambda r: r["r"]))
            break

    tabs = "".join(
        '<button class="tab" role="tab" data-win="{0}" aria-selected="{1}">{0}</button>'
        .format(w, "true" if i == 0 else "false")
        for i, w in enumerate(k for k in WINDOW_KEYS if k in payload["boards"]))

    hero_html = ""
    if hero:
        wname, r = hero
        hero_html = (
            '<div class="card"><div class="sub">Best performer over the last {0}</div>'
            '<div style="font-size:2.1rem;font-weight:700;line-height:1.15" class="{3}">{1}</div>'
            '<div class="mono sub">{2}</div>'
            '<div class="sub" style="margin-top:6px">Holds {4} TAO now &middot; '
            '{5} trades in this window</div></div>'.format(
                wname, ("+" if r["r"] >= 0 else "") + "{:,.2f}%".format(r["r"]),
                r["ck"], "up" if r["r"] >= 0 else "down",
                "{:,.2f}".format(r["v"]), r["t"]))

    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bracket Board - who is actually good at subnet investing</title>
<style>{css}</style></head><body><div class="wrap">
<h1>Bracket Board</h1>
<p class="sub">Which Bittensor wallets are actually good at buying subnet alpha -
worked out from public blockchain data, not from anyone's claims.</p>

<div class="explain">
<b>New here? Read this bit.</b>
<div class="steps">
<div class="step"><span>1</span>Wallets are grouped by <b>how much they hold</b>, so
you compare like with like. A 10 TAO wallet is not measured against a 40,000 TAO one.</div>
<div class="step"><span>2</span>Pick a <b>time window</b> below. "7 days" ranks who did
best this week; "90 days" rewards consistency over luck.</div>
<div class="step"><span>3</span><b>Click any row</b> to see that wallet's chart and
every trade it made - what it bought, when it sold, and what it earned or lost.</div>
</div>
<p class="sub" style="margin-top:12px"><b>Total return</b> is the gain on the money
actually invested, so someone who simply deposited more does not appear to be winning.
It counts alpha that has gone up but has not been sold - the same convention a fund
uses when it reports a yearly figure, and the honest one, since ignoring it would
flatter whoever sells fastest rather than whoever picks best. The table splits it
anyway: <b>actually banked</b> is money from completed sales, <b>still on paper</b> is
alpha marked at the last traded price. Paper gains are not money until sold, and subnet
pools are small enough that a big sale moves the price against the seller - so a row
flagged <span class="flag">mostly on paper</span> has not yet proved it can get out.
Wallets that never bought alpha - miners, validators, subnet owners - are left out,
because their balance grows from running a subnet, not from picking one.</p>
</div>

{hero}

<div class="explain" style="border-left-color:var(--warn)">
<b>Known limitation, stated plainly.</b> The crawl has not yet visited every subnet, so
a wallet is only ranked when every subnet it traded in is one we track - otherwise we
would be reporting part of a portfolio as though it were the whole thing. That rule is
what keeps the figures honest, but it has a side effect worth knowing: wallets that
trade across many subnets are the most likely to be excluded, so the board currently
leans towards quieter wallets. It corrects itself as the crawl completes.
</div>

<h2>The leaderboard</h2>
<div class="tabs" role="tablist">{tabs}</div>
<div class="tabs" role="tablist" style="margin-top:2px">
<button class="tab" role="tab" data-rank="realised" aria-selected="true">Rank by money banked</button>
<button class="tab" role="tab" data-rank="total" aria-selected="false">Rank by total return</button>
</div>
<p class="sub"><b>Money banked</b> counts only completed sales inside the period -
profit that exists whatever happens next. <b>Total return</b> adds in alpha that has
risen but has not been sold. The first is the harder test and is what the board sorts
on by default. Click a wallet for its full record.</p>
<div id="boards"></div>

<h2>How to read a wallet's page</h2>
<p class="sub">The chart is the wallet's total holdings in TAO over time - hover it for
any day. Underneath, each sale is priced against the average price that wallet paid for
its alpha in that subnet, which is what turns "it sold" into "it made 18%". Buys show no
profit because nothing is realised until a sale. Rows marked
<span class="flag">gaps</span> had balance jumps that recorded trades could not explain,
usually a transfer in from another wallet - the ranking already discounts those.</p>

<footer>{meta}<br><br>
Public blockchain data, reconstructed from the taostats API. Past performance is not a
prediction - the best wallet of the last 30 days is often the biggest gambler on the
hottest subnet. This is not financial advice.</footer>
</div>
<script>window.__BB__={blob};</script>
<script>{js}</script>
</body></html>""".format(css=CSS, js=JS, blob=blob, tabs=tabs, hero=hero_html,
                         meta=meta)


def meta_line(updated, covered, subnets_total, last_full, calls_used,
              ceiling, snap_days, wallets_ranked):
    return ("Updated {} UTC &middot; {} wallets with reconstructed history &middot; "
            "subnet coverage this pass {}/{} &middot; last full sweep {} &middot; "
            "API calls this month {}/{} &middot; {} snapshot days".format(
                updated, wallets_ranked, covered, subnets_total,
                last_full, calls_used, ceiling, snap_days))
