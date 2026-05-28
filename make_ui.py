"""
make_ui.py — turn ranked_jobs.json into a standalone HTML feed you can just open.

No server, no npm, no build step. It reads your ranked jobs, bakes the top ones
into a single self-contained HTML file, and you double-click it.

    python3 make_ui.py            # reads ranked_jobs.json -> viewer.html
    open viewer.html              # see your feed

It embeds the top 150 jobs (the rest are almost all 'skip' anyway).
"""
import html as html_mod
import json
import os
import sys

TOP = int(os.environ.get("UI_TOP", "150"))


def shape(jobs):
    out = []
    for j in jobs[:TOP]:
        fit = j.get("fit") or {}
        out.append({
            "company": j.get("company", ""),
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("url", ""),
            "source": j.get("source", "") or j.get("ats", ""),
            "tier": j.get("tier") or fit.get("tier") or "possible",
            "score": j.get("score") if j.get("score") is not None else (fit.get("score") or 0),
            "reasons": j.get("reasons") or fit.get("reasons") or [],
            "matched": j.get("matched") or fit.get("matched_skills") or [],
            "missing": j.get("missing") or fit.get("missing_skills") or [],
            "is_new": bool(j.get("is_new")),
        })
    return out


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobMatch — your ranked feed</title>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Newsreader:opsz,wght@6..72,400;6..72,500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #f6f4ef; color: #1a1a17; font-family: 'Newsreader', Georgia, serif; }
  ::selection { background: #d9e8df; }
  .row { transition: background .15s ease; cursor: pointer; }
  .row:hover { background: #fffdf8; }
  .btn { transition: transform .08s ease; }
  .btn:active { transform: translateY(1px); }
</style></head>
<body><div id="root"></div>
<script>window.JOBS = __DATA__; window.NAME = "__NAME__";</script>
<script type="text/babel">
const FD="'Fraunces', Georgia, serif", FB="'Newsreader', Georgia, serif", FU="'IBM Plex Sans', system-ui, sans-serif";
const TIERS = {
  strong:{label:"Strong fit",dot:"#1f9d55",bg:"#eaf6ef",text:"#176844"},
  possible:{label:"Possible fit",dot:"#c6881b",bg:"#fbf3e2",text:"#8a5d10"},
  skip:{label:"Skip",dot:"#9aa0a6",bg:"#f0f1f2",text:"#5f6368"},
};
const {useState, useMemo} = React;

function Label({children}){return <div style={{fontFamily:FU,fontSize:11,fontWeight:600,textTransform:"uppercase",letterSpacing:".09em",color:"#a39d8e",marginBottom:10}}>{children}</div>;}
function Chips({items,kind}){const s=kind==="match"?{color:"#176844",background:"#eaf6ef",border:"1px solid #cfe7d8"}:{color:"#8a5d10",background:"#fbf3e2",border:"1px solid #ecdcb8"};return <div style={{display:"flex",flexWrap:"wrap",gap:6}}>{items.map((it,i)=><span key={i} style={{...s,fontFamily:FU,fontSize:12.5,fontWeight:500,padding:"3px 10px",borderRadius:7}}>{it}</span>)}</div>;}
function Ring({tier,score}){const t=TIERS[tier]||TIERS.possible;const r=26,c=2*Math.PI*r,off=c*(1-score/100);return(<div style={{position:"relative",width:64,height:64,flexShrink:0}}><svg width="64" height="64" style={{transform:"rotate(-90deg)"}}><circle cx="32" cy="32" r={r} fill="none" stroke="#ece8dd" strokeWidth="5"/><circle cx="32" cy="32" r={r} fill="none" stroke={t.dot} strokeWidth="5" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={off}/></svg><div style={{position:"absolute",inset:0,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center"}}><span style={{fontFamily:FD,fontSize:19,fontWeight:600,lineHeight:1,color:"#1a1a17"}}>{score}</span><span style={{fontFamily:FU,fontSize:8.5,fontWeight:600,textTransform:"uppercase",letterSpacing:".06em",color:t.text}}>fit</span></div></div>);}

function App(){
  const all = window.JOBS || [];
  const [filter,setFilter] = useState("all");
  const [sel,setSel] = useState(0);
  const counts = {all:all.length, strong:all.filter(j=>j.tier==="strong").length, possible:all.filter(j=>j.tier==="possible").length, skip:all.filter(j=>j.tier==="skip").length};
  const list = useMemo(()=>{const base = filter==="all"?all:all.filter(j=>j.tier===filter);const rk={strong:0,possible:1,skip:2};return [...base].sort((a,b)=>(rk[a.tier]??3)-(rk[b.tier]??3)||b.score-a.score);},[filter,all]);
  const job = list[sel] || list[0] || null;

  return (<div style={{minHeight:"100vh"}}>
    <header style={{borderBottom:"1px solid #e4e0d6",position:"sticky",top:0,background:"#f6f4ef",zIndex:10}}>
      <div style={{maxWidth:1180,margin:"0 auto",padding:"20px 28px",display:"flex",justifyContent:"space-between",alignItems:"baseline"}}>
        <div style={{display:"flex",alignItems:"baseline",gap:14}}>
          <span style={{fontFamily:FD,fontSize:26,fontWeight:600,letterSpacing:"-.02em"}}>JobMatch</span>
          <span style={{fontFamily:FU,fontSize:12.5,color:"#8a8578"}}>ranked for <strong style={{color:"#3a382f"}}>{window.NAME}</strong> · Software Engineering</span>
        </div>
        <span style={{fontFamily:FU,fontSize:12,color:"#9a9486"}}>{counts.all} roles · updated today</span>
      </div>
    </header>
    <main style={{maxWidth:1180,margin:"0 auto",padding:28,display:"grid",gridTemplateColumns:"minmax(360px,1fr) minmax(440px,1.25fr)",gap:28,alignItems:"start"}}>
      <section>
        <div style={{display:"flex",gap:8,marginBottom:18,flexWrap:"wrap"}}>
          {[["all","All"],["strong","Strong"],["possible","Possible"],["skip","Skip"]].map(([k,lb])=>{const a=filter===k;return <button key={k} className="btn" onClick={()=>{setFilter(k);setSel(0);}} style={{fontFamily:FU,fontSize:13,fontWeight:500,cursor:"pointer",padding:"7px 14px",borderRadius:999,border:a?"1px solid #1a1a17":"1px solid #e0dcd1",background:a?"#1a1a17":"transparent",color:a?"#f6f4ef":"#56524a"}}>{lb} <span style={{opacity:.55,marginLeft:3}}>{counts[k]}</span></button>;})}
        </div>
        <div style={{border:"1px solid #e4e0d6",borderRadius:14,overflow:"hidden",background:"#fdfcf8"}}>
          {list.map((j,i)=>{const t=TIERS[j.tier]||TIERS.possible;const a=job&&list[sel]===j;return(
            <div key={i} className="row" onClick={()=>setSel(i)} style={{padding:"16px 18px",borderBottom:i<list.length-1?"1px solid #ece8dd":"none",borderLeft:a?"3px solid #1a1a17":"3px solid transparent",background:a?"#fffdf8":"transparent"}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",gap:10}}>
                <span style={{fontFamily:FU,fontSize:12.5,fontWeight:600,color:"#6f6a5d",textTransform:"uppercase",letterSpacing:".04em"}}>{j.company}{j.is_new && <span style={{marginLeft:8,fontSize:10,fontWeight:700,color:"#176844",background:"#eaf6ef",border:"1px solid #cfe7d8",padding:"1px 6px",borderRadius:999,letterSpacing:".02em"}}>NEW</span>}</span>
                <span style={{fontFamily:FU,fontSize:12,fontWeight:600,color:t.text,background:t.bg,padding:"2px 9px",borderRadius:999}}>{j.score}</span>
              </div>
              <div style={{fontFamily:FD,fontSize:18,fontWeight:500,margin:"5px 0 7px",lineHeight:1.18}}>{j.title}</div>
              <div style={{display:"flex",alignItems:"center",gap:10,fontFamily:FU,fontSize:12.5,color:"#8a8578"}}>
                <span style={{display:"inline-flex",alignItems:"center",gap:5}}><span style={{width:6,height:6,borderRadius:999,background:t.dot}}/>{t.label}</span>
                <span>·</span><span>{j.location||"—"}</span>
              </div>
            </div>);})}
        </div>
      </section>
      {job && <section style={{position:"sticky",top:92,border:"1px solid #e4e0d6",borderRadius:16,background:"#fdfcf8",overflow:"hidden"}}>
        <div style={{padding:"26px 28px 22px",borderBottom:"1px solid #ece8dd",display:"flex",justifyContent:"space-between",alignItems:"flex-start",gap:16}}>
          <div>
            <div style={{fontFamily:FU,fontSize:13,fontWeight:600,color:"#6f6a5d",textTransform:"uppercase",letterSpacing:".05em",marginBottom:6}}>{job.company}</div>
            <h1 style={{fontFamily:FD,fontSize:27,fontWeight:600,margin:0,lineHeight:1.12,letterSpacing:"-.02em"}}>{job.title}</h1>
            <div style={{fontFamily:FU,fontSize:13,color:"#8a8578",marginTop:10,display:"flex",gap:9,flexWrap:"wrap"}}><span>{job.location||"—"}</span><span>·</span><span style={{textTransform:"capitalize"}}>{job.source}</span></div>
          </div>
          <Ring tier={job.tier} score={job.score}/>
        </div>
        <div style={{padding:"22px 28px"}}>
          {job.reasons.length>0 && <><Label>Why this {job.tier==="skip"?"isn't":"is"} a fit</Label>
          <ul style={{margin:"0 0 22px",padding:0,listStyle:"none",display:"flex",flexDirection:"column",gap:9}}>
            {job.reasons.map((r,i)=><li key={i} style={{display:"flex",gap:10,fontSize:15.5,lineHeight:1.5,color:"#33312a"}}><span style={{color:(TIERS[job.tier]||TIERS.possible).dot,marginTop:2}}>{job.tier==="skip"?"✕":"→"}</span><span>{r}</span></li>)}
          </ul></>}
          <div style={{display:"flex",gap:26,marginBottom:22,flexWrap:"wrap"}}>
            {job.matched.length>0 && <div style={{flex:1,minWidth:160}}><Label>You match</Label><Chips items={job.matched} kind="match"/></div>}
            {job.missing.length>0 && <div style={{flex:1,minWidth:160}}><Label>Gaps</Label><Chips items={job.missing} kind="gap"/></div>}
          </div>
          {job.url && <a className="btn" href={job.url} target="_blank" rel="noreferrer" style={{display:"block",textAlign:"center",textDecoration:"none",fontFamily:FU,fontSize:14,fontWeight:600,padding:"13px 18px",borderRadius:10,background:"#1a1a17",color:"#f6f4ef"}}>Open the posting →</a>}
        </div>
      </section>}
    </main>
  </div>);
}
ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
</script></body></html>"""


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "ranked_jobs.json"
    if not os.path.exists(src):
        print(f"! {src} not found. Run main.py first to produce it.")
        sys.exit(1)
    with open(src) as f:
        jobs = json.load(f)
    name = "you"
    if os.path.exists("my_profile.json"):
        try:
            name = json.load(open("my_profile.json")).get("name") or "you"
        except Exception:
            pass
    data = shape(jobs)
    out = HTML.replace("__DATA__", json.dumps(data)).replace("__NAME__", html_mod.escape(name))
    with open("viewer.html", "w") as f:
        f.write(out)
    print(f"Wrote viewer.html with top {len(data)} jobs. Run:  open viewer.html")


if __name__ == "__main__":
    main()
