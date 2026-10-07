#!/usr/bin/env python3
"""
Generate reports/history.html from history.json + job_state.json backfill.
Called automatically by job_monitor.py after each run, or run manually.

Usage:
    python view_history.py              # regenerate and open in browser
    python view_history.py --no-open    # regenerate only
    python view_history.py --days 7     # filter to last 7 days before opening
    python view_history.py --min-score 40
"""
import json, argparse, webbrowser, os
from pathlib import Path
from datetime import datetime, timedelta
from report_safety import safe_report_job

OUTPUT = Path('reports/history.html')
PRI_ORDER = {'URGENT': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}


def load_all_jobs():
    jobs, seen = [], set()

    if Path('history.json').exists():
        try:
            jobs = json.loads(Path('history.json').read_text(encoding='utf-8'))
            for j in jobs:
                seen.add((j.get('company', ''), j.get('title', '').lower()))
        except Exception:
            pass

    # Backfill older entries from job_state.json that predate history.json
    if Path('job_state.json').exists():
        try:
            state = json.loads(Path('job_state.json').read_text(encoding='utf-8'))
            for company, entries in state.items():
                if company == '__meta__' or not isinstance(entries, dict):
                    continue
                for meta in entries.values():
                    title = meta.get('title', '')
                    key = (company, title.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    jobs.append({
                        'found_at': meta.get('first_seen', ''),
                        'company': company,
                        'title': title,
                        'url': meta.get('url', ''),
                        'location': meta.get('location', ''),
                        'score': meta.get('score') or 0,
                        'priority': meta.get('priority') or '—',
                    })
        except Exception:
            pass

    return jobs


def render(jobs):
    jobs = [safe_report_job(job) for job in jobs]
    os.makedirs('reports', exist_ok=True)

    def fmt(iso):
        try:
            return datetime.fromisoformat(iso).strftime('%Y-%m-%d')
        except Exception:
            return (iso or '')[:10] or '—'

    rows = ''
    for j in sorted(jobs, key=lambda x: (x.get('found_at', ''), PRI_ORDER.get(x.get('priority', ''), 9)), reverse=True):
        pri = j.get('priority') or '—'
        cls = pri.lower() if pri in ('URGENT', 'HIGH', 'MEDIUM', 'LOW') else ''
        url, title = j.get('url', ''), j.get('title', '')
        linked = f"<a href='{url}' target='_blank'>{title}</a>" if url else title
        score = j.get('score') or 0
        bar = f"<span class='bar' style='width:{score//2}px'></span>" if score else ''
        loc_raw = (j.get('location', '') or '').lower()
        india_markers = ('bangalore', 'bengaluru', 'hyderabad', 'mumbai', 'delhi',
                         'pune', 'chennai', 'kolkata', 'noida', 'gurgaon',
                         'gurugram', 'india', ', ind')
        loc_region = 'india' if any(m in loc_raw for m in india_markers) else 'other'
        rows += (
            f"<tr class='{cls}'"
            f" data-date='{fmt(j.get('found_at',''))}'"
            f" data-company='{(j.get('company','') or '').lower()}'"
            f" data-title='{title.lower()}'"
            f" data-pri='{pri}'"
            f" data-score='{score}'"
            f" data-region='{loc_region}'>"
            f"<td>{fmt(j.get('found_at',''))}</td>"
            f"<td>{j.get('company','')}</td>"
            f"<td>{linked}</td>"
            f"<td>{j.get('location','')}</td>"
            f"<td>{bar}{score or '—'}</td>"
            f"<td><span class='badge {cls}'>{pri}</span></td>"
            f"</tr>\n"
        )

    companies = sorted({j.get('company', '') for j in jobs if j.get('company')})
    co_opts = ''.join(f"<option>{c}</option>" for c in companies)
    generated = datetime.now().strftime('%Y-%m-%d %H:%M')
    last_run_label = ''
    try:
        state = json.loads(Path('job_state.json').read_text(encoding='utf-8'))
        lr = state.get('__meta__', {}).get('last_run_at', '')
        if lr:
            last_run_label = datetime.fromisoformat(lr).strftime('%Y-%m-%d %H:%M')
    except Exception:
        pass
    last_run_part = f' &mdash; Last run: {last_run_label}' if last_run_label else ''

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>AI Job History</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,sans-serif;background:#f7f8fa;color:#1a1a1a;padding:1.5rem}}
  h1{{font-size:1.25rem;font-weight:700;margin-bottom:.2rem}}
  .meta{{font-size:.8rem;color:#888;margin-bottom:1rem}}
  .toolbar{{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1rem;align-items:center}}
  .toolbar input,.toolbar select{{padding:5px 9px;border:1px solid #ccc;border-radius:5px;
    font-size:.84rem;background:#fff;outline:none}}
  .toolbar input:focus,.toolbar select:focus{{border-color:#4a90e2}}
  #count{{font-size:.82rem;color:#666;margin-left:auto}}
  .pager{{display:flex;gap:.3rem;margin-top:.8rem;align-items:center;flex-wrap:wrap}}
  .pager button{{padding:4px 10px;border:1px solid #ccc;border-radius:4px;background:#fff;
    cursor:pointer;font-size:.82rem}}
  .pager button:hover{{background:#e8eef8}} .pager button.active{{background:#4a90e2;color:#fff;border-color:#4a90e2}}
  .pager span{{font-size:.8rem;color:#666;margin:0 .3rem}}
  .wrap{{overflow-x:auto;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
  table{{width:100%;border-collapse:collapse;font-size:.86rem;background:#fff}}
  th{{background:#f0f0f0;text-align:left;padding:9px 11px;border-bottom:2px solid #ddd;
    cursor:pointer;user-select:none;white-space:nowrap}}
  th:hover{{background:#e4e4e4}}
  td{{padding:8px 11px;border-bottom:1px solid #eee;vertical-align:top}}
  tr.urgent td{{background:#fff3f3}} tr.high td{{background:#fff8f0}}
  tr.medium td{{background:#fffdf0}}
  tr:hover td{{background:#eef3ff!important}}
  tr.hidden{{display:none}}
  a{{color:#0055bb;text-decoration:none}} a:hover{{text-decoration:underline}}
  .badge{{font-size:.72rem;padding:2px 7px;border-radius:10px;font-weight:600;background:#eee;color:#666}}
  .badge.urgent{{background:#ffd5d5;color:#c00}}
  .badge.high{{background:#ffe5cc;color:#a50}}
  .badge.medium{{background:#fff5cc;color:#880}}
  .bar{{display:inline-block;height:6px;border-radius:3px;background:#4a90e2;
    vertical-align:middle;margin-right:5px;min-width:2px}}
  #runBtn{{padding:5px 13px;border:1px solid #4a90e2;border-radius:5px;background:#4a90e2;
    color:#fff;cursor:pointer;font-size:.84rem;font-weight:600}}
  #runBtn:hover{{background:#357abd}}
  #runBtn:disabled{{background:#aaa;border-color:#aaa;cursor:not-allowed}}
  #runMsg{{font-size:.78rem;color:#888;margin-left:.3rem}}
  #runLog{{display:none;margin-top:.5rem;padding:.5rem .75rem;background:#1a1a1a;color:#f0f0f0;
    font-family:monospace;font-size:.75rem;border-radius:5px;white-space:pre-wrap;
    max-height:180px;overflow-y:auto;width:100%}}
</style>
</head><body>
<nav style='display:flex;gap:8px;margin-bottom:1.2rem;align-items:center;padding-bottom:.9rem;border-bottom:1px solid #e0e0e0'>
  <span style='font-family:monospace;font-size:.82rem;color:#888;margin-right:4px'>~/ai-job-monitor</span>
  <a href='/' style='padding:5px 14px;border-radius:5px;background:#4a90e2;color:#fff;font-size:.84rem;font-weight:600;text-decoration:none'>📋 Job History</a>
  <a href='/obs' style='padding:5px 14px;border-radius:5px;border:1px solid #ccc;background:#fff;color:#444;font-size:.84rem;font-weight:600;text-decoration:none'>📡 Observability</a>
</nav>
<h1>AI Job History &mdash; {len(jobs)} roles</h1>
<p class='meta'>Generated {generated}{last_run_part}</p>

<div class='toolbar'>
  <input id='q' placeholder='Search title / company…' oninput='f()' style='width:210px'>
  <select id='co' onchange='f()'><option value=''>All companies</option>{co_opts}</select>
  <select id='pri' onchange='f()'>
    <option value=''>All priorities</option>
    <option>URGENT</option><option>HIGH</option><option>MEDIUM</option><option>LOW</option>
  </select>
  <select id='loc' onchange='f()'>
    <option value=''>All locations</option>
    <option value='india'>India only</option>
    <option value='other'>Non-India</option>
  </select>
  <select id='days' onchange='f()'>
    <option value='0'>All time</option>
    <option value='1'>Today</option>
    <option value='3'>Last 3 days</option>
    <option value='7'>Last 7 days</option>
    <option value='30'>Last 30 days</option>
  </select>
  <input id='sc' type='number' value='0' min='0' max='100' style='width:70px'
    title='Min score' placeholder='Min score' oninput='f()'>
  <button id='runBtn' onclick='triggerRun()'>&#9654; Run Now</button>
  <span id='runMsg'></span>
  <span id='count' style='margin-left:auto'></span>
</div>
<pre id='runLog'></pre>

<div class='wrap'>
<table>
<thead><tr>
  <th onclick='s(0)'>Date ▾</th>
  <th onclick='s(1)'>Company</th>
  <th onclick='s(2)'>Role</th>
  <th onclick='s(3)'>Location</th>
  <th onclick='s(4)'>Score</th>
  <th onclick='s(5)'>Priority</th>
</tr></thead>
<tbody id='body'>
{rows}
</tbody>
</table>
</div>
<div class='pager' id='pager'></div>

<script>
const PAGE=25;
let sc=0,sd=-1,pg=1;
const P={{URGENT:0,HIGH:1,MEDIUM:2,LOW:3}};
const allRows=[...document.querySelectorAll('#body tr')];

function matchRow(r,q,co,pri,ms,cut,loc){{
  return(!q||(r.dataset.title||'').includes(q)||(r.dataset.company||'').includes(q))&&
    (!co||r.dataset.company===co)&&(!pri||r.dataset.pri===pri)&&
    (!loc||r.dataset.region===loc)&&
    (parseInt(r.dataset.score)||0)>=ms&&(!cut||(r.dataset.date||'')>=cut);
}}

function f(){{
  const q=(document.getElementById('q').value||'').toLowerCase();
  const co=(document.getElementById('co').value||'').toLowerCase();
  const pri=document.getElementById('pri').value;
  const loc=document.getElementById('loc').value;
  const days=parseInt(document.getElementById('days').value)||0;
  const ms=parseInt(document.getElementById('sc').value)||0;
  const cut=days?new Date(Date.now()-days*864e5).toISOString().slice(0,10):'';
  const vis=allRows.filter(r=>matchRow(r,q,co,pri,ms,cut,loc));
  pg=1;
  render(vis);
  document.getElementById('count').textContent=vis.length+' of {len(jobs)} shown';
}}

function render(vis){{
  const total=Math.max(1,Math.ceil(vis.length/PAGE));
  pg=Math.min(pg,total);
  const start=(pg-1)*PAGE, end=start+PAGE;
  allRows.forEach(r=>r.classList.add('hidden'));
  vis.slice(start,end).forEach(r=>r.classList.remove('hidden'));

  // Build pager
  const pager=document.getElementById('pager');
  if(vis.length<=PAGE){{pager.innerHTML='';return;}}
  let html=`<span>Page ${{pg}} of ${{total}}</span>`;
  if(pg>1) html+=`<button onclick='goPage(${{pg-1}})'>‹ Prev</button>`;
  // window of up to 7 page buttons
  const lo=Math.max(1,pg-3), hi=Math.min(total,pg+3);
  if(lo>1) html+=`<button onclick='goPage(1)'>1</button>${{lo>2?'<span>…</span>':''}}`;
  for(let i=lo;i<=hi;i++) html+=`<button class='${{i===pg?"active":""}}' onclick='goPage(${{i}})'>${{i}}</button>`;
  if(hi<total) html+=`${{hi<total-1?'<span>…</span>':''}}<button onclick='goPage(${{total}})'>${{total}}</button>`;
  if(pg<total) html+=`<button onclick='goPage(${{pg+1}})'>Next ›</button>`;
  pager.innerHTML=html;
  // store vis for goPage
  pager._vis=vis;
}}

function goPage(n){{
  pg=n;
  render(document.getElementById('pager')._vis);
  window.scrollTo(0,0);
}}

function s(c){{
  if(sc===c)sd*=-1;else{{sc=c;sd=-1;}}
  document.querySelectorAll('th').forEach((h,i)=>
    h.textContent=h.textContent.replace(/ [▾▴]/,'')+(i===c?(sd<0?' ▾':' ▴'):''));
  const b=document.getElementById('body');
  [...allRows].sort((a,b2)=>{{
    const va=a.querySelectorAll('td')[c].textContent.trim();
    const vb=b2.querySelectorAll('td')[c].textContent.trim();
    if(c===4)return((parseFloat(va)||0)-(parseFloat(vb)||0))*sd;
    if(c===5)return((P[va]??9)-(P[vb]??9))*-sd;
    return va.localeCompare(vb)*sd;
  }}).forEach(r=>b.appendChild(r));
  // refresh allRows order
  allRows.length=0; document.querySelectorAll('#body tr').forEach(r=>allRows.push(r));
  f();
}}
f();
const _srv=location.host==='localhost:8765'?'':'http://localhost:8765';
let _runStart=null,_wasRunning=false;
const _btn=()=>document.getElementById('runBtn');
const _msg=()=>document.getElementById('runMsg');
const _log=()=>document.getElementById('runLog');

function _elapsed(){{
  if(!_runStart)return'';
  const s=Math.round((Date.now()-_runStart)/1000);
  return s<60?s+'s':Math.floor(s/60)+'m '+s%60+'s';
}}

function _showLog(text,isError){{
  const el=_log();
  if(!text){{el.style.display='none';el.textContent='';return;}}
  el.style.display='block';
  el.style.background=isError?'#2d0a0a':'#1a1a1a';
  el.textContent=text;
}}

function _applyStatus(d){{
  const btn=_btn(),msg=_msg();
  if(d.running){{
    _wasRunning=true;
    btn.disabled=true;btn.textContent='⏳ Running…';
    msg.textContent='Running… '+_elapsed()+' elapsed';
    msg.style.color='#888';
    _showLog('',false);
    setTimeout(checkStatus,3000);
    return;
  }}
  // run just finished
  btn.disabled=false;btn.textContent='▶ Run Now';
  if(d.last===null){{msg.textContent='';msg.style.color='';return;}}

  if(d.ok===false||d.exit_code!==0){{
    // failure
    const reason=d.error||('Exit code '+d.exit_code);
    msg.textContent='❌ Failed: '+reason;
    msg.style.color='#c0392b';
    _showLog(d.log_tail||'(no output)',true);
  }}else{{
    // success
    const jobs=d.new_jobs;
    // "no new jobs" alone reads as "there are no jobs" when the table below is
    // full of today's finds. Say what the count actually describes: this run.
    const label=jobs===null?'done':(jobs===0?'no new jobs this run':jobs+' new job'+(jobs===1?'':'s')+'!');
    msg.textContent='✅ '+label+' — '+new Date(d.last).toLocaleTimeString();
    msg.style.color='#27ae60';
    _showLog('',false);
    if(_wasRunning&&jobs>0)setTimeout(()=>location.reload(),1500);
  }}
  _wasRunning=false;
}}

function checkStatus(){{
  fetch(_srv+'/status').then(r=>r.json())
    .then(d=>_applyStatus(d))
    .catch(()=>{{
      _msg().textContent='⚠ serve.py not running';
      _msg().style.color='#c0392b';
    }});
}}

function triggerRun(){{
  _btn().disabled=true;_btn().textContent='⏳ Starting…';
  _msg().textContent='';_msg().style.color='';
  _showLog('',false);
  fetch(_srv+'/run',{{method:'POST'}})
    .then(r=>{{
      if(r.status===409){{_msg().textContent='Already running';_msg().style.color='#e67e22';checkStatus();return;}}
      if(!r.ok){{_msg().textContent='Server error ('+r.status+')';_msg().style.color='#c0392b';_btn().disabled=false;_btn().textContent='▶ Run Now';return;}}
      _runStart=Date.now();_wasRunning=true;
      _applyStatus({{running:true,last:null,ok:null,exit_code:null,new_jobs:null,error:null,log_tail:''}});
    }})
    .catch(()=>{{
      _msg().textContent='⚠ serve.py not running';_msg().style.color='#c0392b';
      _btn().disabled=false;_btn().textContent='▶ Run Now';
    }});
}}
checkStatus();
</script>
</body></html>"""

    OUTPUT.write_text(html, encoding='utf-8')
    return OUTPUT.resolve()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=0)
    parser.add_argument('--min-score', type=int, default=0)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()

    jobs = load_all_jobs()
    if args.days:
        cut = (datetime.now() - timedelta(days=args.days)).isoformat()
        jobs = [j for j in jobs if (j.get('found_at') or '') >= cut]
    if args.min_score:
        jobs = [j for j in jobs if (j.get('score') or 0) >= args.min_score]

    path = render(jobs)
    print(f"History: {len(jobs)} jobs → {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())


if __name__ == '__main__':
    main()
