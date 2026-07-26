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

FEATURED_PER_BRACKET = 6      # rows kept per bracket per window
SERIES_POINTS = 90            # downsample each wallet's value history to this
TRADES_SHOWN = 30             # most recent buys/sells per wallet (transfers excluded)
WINDOW_KEYS = ("7 days", "14 days", "30 days", "90 days")


# ----------------------------- analytics ------------------------------

def realized_trades(conn, coldkey, limit=TRADES_SHOWN):
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

    book = {}          # netuid -> [alpha_held, tao_cost]
    shown = []
    for ts, netuid, action, tao, alpha, price, transfer in rows:
        held, cost = book.get(netuid, [0.0, 0.0])
        pnl = pct = avg_used = None
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
                cost -= avg * sold
                held -= sold
            else:
                held = max(held - alpha, 0.0)
        book[netuid] = [held, max(cost, 0.0)]
        if transfer:
            continue                     # counted in the book, kept off the page
        shown.append({
            "t": str(ts)[:16].replace("T", " "),
            "n": netuid,
            "a": action,
            "tao": round(tao or 0.0, 4),
            "al": round(alpha or 0.0, 4),
            "p": round(price or 0.0, 6),
            "avg": None if avg_used is None else round(avg_used, 6),
            "pnl": None if pnl is None else round(pnl, 4),
            "pct": None if pct is None else round(pct, 2),
        })

    shown = shown[-limit:]
    running = 0.0
    for row in shown:                     # oldest first, so the total builds
        running += row["pnl"] or 0.0
        row["run"] = round(running, 4)
    return shown[::-1]                    # newest first for reading


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

def build_payload(conn, brackets, bracket_of, compute_board, windows):
    from datetime import date, timedelta

    boards, featured = {}, {}
    for wname, wdays in windows:
        if wname not in WINDOW_KEYS:
            continue
        status, per_bracket = compute_board(conn, wdays, brackets, bracket_of)
        entry = {"status": status, "b": {}}
        for label, _lo, _hi in brackets:
            rows = (per_bracket or {}).get(label, [])[:FEATURED_PER_BRACKET]
            entry["b"][label] = [
                {"ck": ck, "r": round(ret, 2), "pnl": round(pnl, 3),
                 "v": round(v1, 2), "s": subs, "t": trades, "c": int(clean)}
                for ck, ret, pnl, v1, subs, trades, clean in rows]
            for row in rows:
                featured[row[0]] = wdays or 90
        boards[wname] = entry

    names = subnet_names(conn)
    wallets = {}
    for ck, wdays in featured.items():
        since = (date.today() - timedelta(days=max(wdays, 90) + 5)).isoformat()
        wallets[ck] = {
            "s": value_series(conn, ck, since),
            "tr": realized_trades(conn, ck),
        }
    used = sorted({t["n"] for w in wallets.values() for t in w["tr"]})
    return {"boards": boards, "wallets": wallets,
            "names": {str(n): names.get(n, "SN{}".format(n)) for n in used}}


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

function tradeRows(tr){
  if(!tr.length) return '<div class="empty">No buys or sells recorded.</div>';
  let h=`<div class="scroll"><table><tr><th>When</th><th>Subnet</th><th>Action</th>
    <th class="num">Alpha</th><th class="num">Price paid/got</th>
    <th class="num">Avg cost held</th><th class="num">Profit on this sale</th>
    <th class="num">Running total</th></tr>`;
  for(const t of tr){
    const name=D.names[String(t.n)]||('SN'+t.n);
    const buy=t.a==='BUY';
    let profit='<span style="color:var(--muted)">— not sold yet</span>', avg='<span style="color:var(--muted)">—</span>';
    if(t.pnl!==null&&t.pct!==null){
      const c=t.pnl>=0?'up':'down';
      profit=`<span class="${c}">${pct(t.pct)}</span><br><small style="color:var(--muted)">${fmt(t.pnl,3)} TAO</small>`;
      avg=fmt(t.avg,5);
    }
    h+=`<tr><td class="mono">${t.t}</td><td>${name}</td>
      <td>${buy?'bought':'sold'}</td>
      <td class="num">${fmt(t.al,3)}</td>
      <td class="num">${t.p?fmt(t.p,5):'—'}</td>
      <td class="num">${avg}</td>
      <td class="num">${profit}</td>
      <td class="num ${t.run>=0?'up':'down'}">${fmt(t.run,3)}</td></tr>`;
  }
  return h+`</table></div>
   <p class="sub">Each sale is priced against <b>Avg cost held</b> - the average
   price paid for the alpha that wallet still held in that subnet at that moment.
   Sell above it and the row is green. <b>Running total</b> adds the sales up in
   order, so the last row equals the net figure above. Buys realise nothing, which
   is why their profit column is empty. Transfers between wallets are left out
   entirely, though they still count towards the alpha held.</p>`;
}

/* Clip the history to the window being ranked, so the chart is the evidence
   for the number above it rather than a different period entirely. */
function windowSeries(series){
  const days=parseInt(win,10); if(!days||!series.length) return series;
  const end=new Date(series[series.length-1][0]+'T00:00:00Z');
  const cut=new Date(end.getTime()-days*86400000).toISOString().slice(0,10);
  const clipped=series.filter(p=>p[0]>=cut);
  return clipped.length>1?clipped:series;
}

function detail(ck){
  const w=D.wallets[ck]; if(!w) return '';
  const wins=w.tr.filter(t=>t.pnl!==null&&t.pnl>0).length;
  const losses=w.tr.filter(t=>t.pnl!==null&&t.pnl<0).length;
  const net=w.tr.reduce((a,t)=>a+(t.pnl||0),0);
  const rate=(wins+losses)?Math.round(100*wins/(wins+losses)):null;
  const ser=windowSeries(w.s);
  return `<div class="detail"><div class="grid2">
    <div>
      <h3>What it held over the last ${win}</h3>
      <div class="chartbox" data-ck="${ck}">${chart(ser,560,190)}</div>
      <div class="legend"><span><i class="swatch"></i>Total held in tracked subnets, in TAO</span></div>
      <p class="sub">The <b>Return</b> above is the change in this line, ignoring money
      paid in or taken out. The trades on the right are a different question - what it
      banked when it actually sold. A wallet can be up here while selling badly, or
      down here while every sale it made was profitable.</p>
    </div>
    <div>
      <h3>What its sales actually banked</h3>
      <div class="kpi">
        <div><b>${rate===null?'—':rate+'%'}</b><small>sales made at a profit</small></div>
        <div><b class="${net>=0?'up':'down'}">${fmt(net,2)}</b><small>net TAO banked</small></div>
        <div><b>${wins}/${wins+losses}</b><small>profitable / total sales</small></div>
      </div>
      <p class="sub" style="margin-top:10px">Worked out from the ${w.tr.length}
      buys and sells listed below - add up the profit column and you get the
      ${fmt(net,2)} TAO figure. Nothing here is an estimate.</p>
    </div></div>
    <h3>Every buy and sell, oldest to newest at the bottom</h3>${tradeRows(w.tr)}
    <p class="sub"><a href="https://taostats.io/account/${ck}" target="_blank"
      rel="noopener">Open this wallet on taostats ↗</a></p></div>`;
}

function board(){
  const b=D.boards[win]; const host=document.getElementById('boards');
  if(!b||b.status!=='ok'){
    host.innerHTML=`<div class="empty">${b?b.status:'No data'} - this window needs more history.</div>`;
    return;
  }
  let h='';
  for(const label of Object.keys(b.b)){
    const rows=b.b[label];
    h+=`<h3>Wallets holding ${label} TAO</h3>`;
    if(!rows.length){h+='<div class="empty">No wallet in this size range qualified.</div>';continue;}
    h+=`<div class="scroll"><table><tr><th>#</th><th>Wallet</th><th class="num">Return</th>
      <th class="num">Profit (TAO)</th><th class="num">Holds now</th>
      <th class="num">Trades</th><th></th></tr>`;
    rows.forEach((r,i)=>{
      h+=`<tr class="w" data-ck="${r.ck}"><td><span class="rank ${i===0?'top':''}">${i+1}</span></td>
        <td class="mono">${short(r.ck)}</td>
        <td class="num ${r.r>=0?'up':'down'}">${pct(r.r)}</td>
        <td class="num">${fmt(r.pnl,2)}</td><td class="num">${fmt(r.v,2)}</td>
        <td class="num">${r.t}</td>
        <td>${r.c?'':'<span class="flag" title="Some balance jumps could not be explained by recorded trades">gaps</span>'}</td></tr>
        <tr class="d" data-for="${r.ck}" style="display:none"><td colspan="7"></td></tr>`;
    });
    h+='</table></div>';
  }
  host.innerHTML=h;
  host.querySelectorAll('tr.w').forEach(tr=>{
    tr.addEventListener('click',()=>{
      const ck=tr.dataset.ck, row=host.querySelector(`tr.d[data-for="${ck}"]`);
      const open=row.style.display!=='none';
      host.querySelectorAll('tr.d').forEach(r=>{r.style.display='none';r.firstElementChild.innerHTML='';});
      if(!open){
        row.style.display='';
        row.firstElementChild.innerHTML=detail(ck);
        const box=row.querySelector('.chartbox');
        if(box) wireChart(box, windowSeries(D.wallets[ck].s));
        row.scrollIntoView({behavior:'smooth',block:'nearest'});
      }
    });
  });
}

document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-selected','false'));
    t.setAttribute('aria-selected','true'); win=t.dataset.win; board();
  });
});
board();
"""


def render(conn, brackets, bracket_of, compute_board, windows, meta):
    payload = build_payload(conn, brackets, bracket_of, compute_board, windows)
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
<p class="sub" style="margin-top:12px"><b>Return</b> is the gain on the money actually
invested, so someone who simply deposited more does not appear to be winning. It is a
different measure from the <b>profit on each sale</b> shown inside a wallet: the first
tracks what the holdings are worth, the second what was banked when something was sold.
Wallets that never bought alpha - miners, validators, subnet owners - are left out,
because their balance grows from running a subnet, not from picking one.</p>
</div>

{hero}

<h2>The leaderboard</h2>
<div class="tabs" role="tablist">{tabs}</div>
<p class="sub">Ranked within each size group. Click a wallet for its full record.</p>
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
