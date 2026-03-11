"""
odx_renderer/render_html.py — Renderer HTML per il formato ODX

Converte qualsiasi file .odx in una pagina HTML standalone:
  - Zero dipendenze esterne obbligatorie (font via CDN opzionali)
  - Layout responsivo con reflow adattivo
  - Vista OCR con confidence per parola e tooltip interattivi
  - Light/dark mode con memoria locale
  - Ricerca full-text client-side (Ctrl+F)
  - TOC auto-generato con scroll spy
  - Print CSS per stampa fedele
  - Sidebar con metadati e layer inspector

Uso:
    from odx_renderer.render_html import ODXHTMLRenderer
    renderer = ODXHTMLRenderer()
    renderer.render_to_file("documento.odx", "documento.html")
"""

import sys, json, re
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from odxlib import ODXReader

try:
    from lxml import etree as ET
    LXML = True
except ImportError:
    import xml.etree.ElementTree as ET
    LXML = False

ODX_NS = "https://odx-format.org/ns/semantic/0.1"


# ─────────────────────────────────────────────────────────────
#  SEMANTIC XML → HTML
# ─────────────────────────────────────────────────────────────

class SemanticRenderer:
    def render(self, xml_bytes: bytes) -> str:
        try:
            root = ET.fromstring(xml_bytes) if LXML else ET.fromstring(xml_bytes.decode("utf-8"))
            return self._node(root)
        except Exception as e:
            return f'<p class="odx-error">Errore rendering semantic: {e}</p>'

    def _node(self, n) -> str:
        tag = n.tag.replace(f"{{{ODX_NS}}}", "")
        ch  = "".join(self._node(c) for c in n)

        if tag == "document":   return ch
        if tag == "section":
            role = n.get("role","body"); sid = n.get("id","")
            return f'<section class="odx-section odx-role-{role}" id="{sid}">{ch}</section>\n'
        if tag == "heading":
            lv = min(int(n.get("level","2")),6); eid = n.get("id","")
            return f'<h{lv} id="{eid}" class="odx-heading">{(n.text or "").strip()}</h{lv}>\n'
        if tag == "paragraph":
            eid = n.get("id",""); inner = (n.text or "").strip() + ch
            return f'<p id="{eid}" class="odx-para">{inner}</p>\n' if inner.strip() else ""
        if tag == "emphasis":
            t = "strong" if n.get("type")=="strong" else "em"
            return f'<{t}>{(n.text or "")}{ch}</{t}>'
        if tag == "link":
            return f'<a href="{n.get("href","#")}">{(n.text or "").strip()}</a>'
        if tag == "figure":
            return f'<figure id="{n.get("id","")}" class="odx-figure">{ch}</figure>\n'
        if tag == "image":
            alt = n.get("alt",""); ref = n.get("asset-ref","")
            return f'<div class="odx-img-ph" aria-label="{alt}">🖼 {alt or ref}</div>'
        if tag == "caption":
            return f'<figcaption class="odx-caption">{(n.text or "").strip()}{ch}</figcaption>\n'
        if tag == "table":
            return f'<div class="odx-table-wrap"><table class="odx-table" aria-label="{n.get("aria-label","")}">{ch}</table></div>\n'
        if tag in ("thead","tbody","tfoot"): return f'<{tag}>{ch}</{tag}>'
        if tag == "row":  return f'<tr>{ch}</tr>\n'
        if tag == "cell":
            role = n.get("role","cell"); t = "th" if role=="columnheader" else "td"
            sc = f' scope="{n.get("scope")}"' if n.get("scope") else ""
            return f'<{t}{sc}>{(n.text or "").strip()}{ch}</{t}>'
        if tag == "list":
            ht = "ol" if n.get("type")=="ordered" else "ul"
            return f'<{ht} class="odx-list">{ch}</{ht}>\n'
        if tag == "item":  return f'<li>{(n.text or "").strip()}{ch}</li>\n'
        if tag == "footnote":
            return f'<aside class="odx-footnote"><sup>†</sup> {(n.text or "").strip()}</aside>\n'
        if tag == "formula":
            return f'<div class="odx-formula" aria-label="{n.get("alt","")}"><code>{(n.text or "").strip()}</code></div>\n'
        if tag == "reference":
            return f'<div class="odx-ref">{(n.text or "").strip()}{ch}</div>\n'
        txt = (n.text or "").strip()
        return f'<span class="odx-{tag}">{txt}{ch}</span>' if txt else ch


# ─────────────────────────────────────────────────────────────
#  OCR LAYER → HTML
# ─────────────────────────────────────────────────────────────

class OCRRenderer:
    def render_panel(self, ocr: dict) -> str:
        if not ocr: return '<p class="ocr-empty">Nessun layer OCR.</p>'
        pages = ocr.get("pages", [])
        tw    = ocr.get("total_words", 0)
        conf  = ocr.get("overall_confidence", 0)
        conf_pct = int(conf * 100)
        cls = "high" if conf >= .90 else "medium" if conf >= .70 else "low"
        rev = '<span class="ocr-rev">⚠ Revisione consigliata</span>' if ocr.get("requires_review") else ""

        out = [f"""
<div class="ocr-stats">
  <div class="ocr-stat"><span class="sv">{tw}</span><span class="sl">parole</span></div>
  <div class="ocr-stat"><span class="sv conf-{cls}">{conf_pct}%</span><span class="sl">confidence</span></div>
  <div class="ocr-stat"><span class="sv">{len(pages)}</span><span class="sl">pagine</span></div>
  {rev}
</div>
<div class="ocr-legend">
  <span class="leg g">● ≥90%</span>
  <span class="leg y">● 70-89%</span>
  <span class="leg o">● 50-69%</span>
  <span class="leg r">● &lt;50%</span>
  <span class="leg" style="margin-left:8px;color:var(--ink3)">Passa il mouse su una parola per la confidence</span>
</div>"""]
        for p in pages:
            out.append(self._render_page(p))
        return "\n".join(out)

    def _render_page(self, p: dict) -> str:
        pn    = p.get("page", 1)
        words = p.get("words", [])
        src   = p.get("source_type","scan")
        prep  = p.get("preprocessing", {})
        stats = p.get("confidence_stats", {})
        if not words:
            return f'<div class="ocr-page"><p class="ocr-empty">Pagina {pn} — nessuna parola ({src})</p></div>'

        total = sum(stats.values()) or 1
        bar = "".join(
            f'<div class="cb-seg {c}" style="width:{stats.get(c,0)/total*100:.1f}%"></div>'
            for c in ("green","yellow","orange","red") if stats.get(c,0)
        )
        angle  = prep.get("deskew_angle_deg", 0)
        binar  = prep.get("binarization","")
        clahe  = prep.get("clahe_applied", False)
        dpi    = prep.get("estimated_dpi","")
        prep_s = " · ".join(filter(None,[
            f"deskew {angle:+.1f}°" if abs(angle)>.1 else None,
            binar or None,
            "CLAHE" if clahe else None,
            f"{dpi} DPI" if dpi else None
        ])) or "—"

        words_html = self._render_words(words)
        return f"""
<div class="ocr-page" id="ocr-p{pn}">
  <div class="ocr-ph">
    <span class="ocr-pn">Pagina {pn}</span>
    <span class="ocr-src">{src}</span>
    <span class="ocr-prep">{prep_s}</span>
  </div>
  <div class="conf-bar">{bar}</div>
  <div class="ocr-body">{words_html}</div>
</div>"""

    def _render_words(self, words: list) -> str:
        out = []
        for w in words:
            text = w.get("text","")
            conf = w.get("confidence", 0)
            col  = w.get("display_color","red")
            pct  = int(conf * 100)
            conflict = w.get("conflict", False)
            wid  = w.get("id","")
            alts = w.get("alternatives",[])
            alt_s = ""
            if len(alts) > 1:
                alt_s = "<br><small>Alt: " + ", ".join(
                    f"{a['text']} {int(a['prob']*100)}%"
                    for a in alts[1:3]
                ) + "</small>"
            cf = ' data-conflict="true"' if conflict else ""
            out.append(
                f'<span class="ow ow-{col}"{cf} id="{wid}"'
                f' tabindex="0" role="img" aria-label="{text} {pct}%">'
                f'{text}<span class="ott">{pct}%{"⚡" if conflict else ""}{alt_s}</span></span>'
            )
        return " ".join(out)


# ─────────────────────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────────────────────

CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Spectral:ital,wght@0,300;0,400;0,600;1,300;1,400&family=JetBrains+Mono:wght@300;400;600&display=swap');
:root{
  --bg:#f7f5f0;--surface:#fff;--border:#e0dbd0;--border2:#c0b8a8;
  --ink:#1a1612;--ink2:#4a4035;--ink3:#9a8a78;
  --accent:#1a3a5c;--accent2:#2d6a9f;--code:#f0ede6;
  --sh:0 1px 3px rgba(26,22,18,.07),0 4px 16px rgba(26,22,18,.05);
  --r:4px;--fw:680px;
  --fb:'Spectral',Georgia,serif;--fm:'JetBrains Mono','Consolas',monospace;
  --tr:.18s ease;
}
[data-theme=dark]{
  --bg:#13110e;--surface:#1c1914;--border:#2a2420;--border2:#3a3028;
  --ink:#e4ddd4;--ink2:#a09080;--ink3:#6a5a4a;
  --accent:#7ab3e0;--accent2:#9ecbf4;--code:#201e18;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:17px;scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);font-family:var(--fb);font-weight:300;line-height:1.75;min-height:100vh;transition:background var(--tr),color var(--tr)}

/* topbar */
#tb{position:fixed;top:0;left:0;right:0;z-index:100;height:48px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;padding:0 18px;transition:background var(--tr)}
.tb-brand{font-family:var(--fm);font-size:13px;font-weight:600;color:var(--accent);letter-spacing:.06em}
.tb-title{font-size:12px;color:var(--ink2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#si{background:var(--bg);border:1px solid var(--border);border-radius:var(--r);padding:5px 10px;font-size:12px;font-family:var(--fm);color:var(--ink);width:190px;outline:none;transition:border var(--tr),background var(--tr)}
#si:focus{border-color:var(--accent2)}
#sc{font-family:var(--fm);font-size:10px;color:var(--ink3);white-space:nowrap}
.tbtn{background:transparent;border:1px solid var(--border);border-radius:var(--r);padding:4px 10px;font-size:11px;font-family:var(--fm);color:var(--ink2);cursor:pointer;white-space:nowrap;transition:all var(--tr)}
.tbtn:hover{background:var(--bg);color:var(--ink);border-color:var(--border2)}
.tbtn.on{background:var(--accent);color:#fff;border-color:var(--accent)}

/* layout */
#lay{display:grid;grid-template-columns:210px 1fr;padding-top:48px;min-height:100vh}
#sb{position:sticky;top:48px;height:calc(100vh - 48px);overflow-y:auto;border-right:1px solid var(--border);padding:20px 14px;font-size:12px;background:var(--surface);transition:background var(--tr)}
.sb-lbl{font-family:var(--fm);font-size:9px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3);margin-bottom:7px;padding-bottom:4px;border-bottom:1px solid var(--border)}
.sb-sec{margin-bottom:22px}
.mr{margin-bottom:9px}
.mk{font-family:var(--fm);font-size:9px;color:var(--ink3);display:block}
.mv{font-size:11px;color:var(--ink2);word-break:break-word}
.lbadge{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:9px;font-family:var(--fm);font-size:9px;background:var(--bg);border:1px solid var(--border);color:var(--ink3);margin:2px 2px 2px 0}
.lbadge.ok{border-color:#4caf50;color:#2e7d32;background:rgba(76,175,80,.08)}
[data-theme=dark] .lbadge.ok{color:#81c784}
.toc-list{list-style:none}
.toc-item a{display:block;padding:3px 6px;color:var(--ink2);text-decoration:none;border-radius:2px;font-size:11px;line-height:1.4;transition:all var(--tr)}
.toc-item a:hover{background:var(--bg);color:var(--ink)}
.toc-item.lv1 a{font-weight:600}
.toc-item.lv2 a{padding-left:14px}
.toc-item.lv3 a{padding-left:26px;font-size:10px}

/* main */
#mn{min-width:0;padding:44px 40px 80px}
#doc{max-width:var(--fw);margin:0 auto}
#doc.hide{display:none}
.dh{margin-bottom:44px;padding-bottom:28px;border-bottom:1px solid var(--border)}
.dt{font-size:2.3rem;font-weight:300;line-height:1.15;letter-spacing:-.01em;margin-bottom:10px}
.by{font-family:var(--fm);font-size:11px;color:var(--ink3);display:flex;flex-wrap:wrap;gap:14px}
.by-i+.by-i{border-left:1px solid var(--border2);padding-left:14px}

/* semantic elements */
.odx-section{margin-bottom:28px}
.odx-role-abstract{background:var(--code);border-left:3px solid var(--accent);padding:18px 22px;border-radius:0 var(--r) var(--r) 0;margin-bottom:28px;font-style:italic;color:var(--ink2)}
.odx-heading{font-weight:600;color:var(--ink);margin-top:1.8em;margin-bottom:.45em;line-height:1.3;letter-spacing:-.01em}
h1.odx-heading{font-size:1.85rem;font-weight:300;border-bottom:1px solid var(--border);padding-bottom:7px}
h2.odx-heading{font-size:1.3rem}
h3.odx-heading{font-size:1.05rem}
h4.odx-heading{font-size:1rem;font-style:italic}
.odx-para{margin-bottom:.95em;text-align:justify;hyphens:auto}
.odx-figure{margin:28px 0;text-align:center}
.odx-img-ph{background:var(--code);border:1px dashed var(--border2);padding:22px;border-radius:var(--r);color:var(--ink3);font-family:var(--fm);font-size:12px}
.odx-caption{font-size:.82rem;color:var(--ink3);margin-top:7px;font-style:italic}
.odx-table-wrap{overflow-x:auto;margin:22px 0}
.odx-table{width:100%;border-collapse:collapse;font-size:.88rem}
.odx-table th,.odx-table td{padding:7px 11px;border:1px solid var(--border);text-align:left;vertical-align:top}
.odx-table th{background:var(--code);font-weight:600;font-size:.82rem;font-family:var(--fm);color:var(--ink2)}
.odx-table tr:nth-child(even) td{background:rgba(0,0,0,.015)}
[data-theme=dark] .odx-table tr:nth-child(even) td{background:rgba(255,255,255,.025)}
.odx-list{margin:.45em 0 .9em 1.4em}
.odx-list li{margin-bottom:.25em}
.odx-formula{background:var(--code);border:1px solid var(--border);padding:11px 15px;border-radius:var(--r);font-family:var(--fm);font-size:.88rem;margin:14px 0;overflow-x:auto}
.odx-footnote{font-size:.8rem;color:var(--ink3);padding:7px 11px;border-left:2px solid var(--border2);margin:14px 0}
.odx-ref{font-size:.82rem;color:var(--ink2);padding:3px 0 3px 14px;border-left:2px solid var(--border)}
mark.srch{background:rgba(255,196,0,.40);color:inherit;border-radius:2px;padding:0 1px}
[data-theme=dark] mark.srch{background:rgba(255,196,0,.28)}

/* OCR panel */
#ocrp{display:none;max-width:var(--fw);margin:0 auto}
#ocrp.on{display:block}
.ocr-stats{display:flex;align-items:center;gap:22px;padding:18px 0 14px;border-bottom:1px solid var(--border);margin-bottom:18px;flex-wrap:wrap}
.ocr-stat{text-align:center}
.sv{display:block;font-size:1.75rem;font-weight:300;font-family:var(--fm);line-height:1;color:var(--ink)}
.sl{font-family:var(--fm);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3)}
.conf-high{color:#2e7d32} .conf-medium{color:#e65100} .conf-low{color:#b71c1c}
[data-theme=dark] .conf-high{color:#81c784} [data-theme=dark] .conf-medium{color:#ffb74d} [data-theme=dark] .conf-low{color:#ef9a9a}
.ocr-rev{background:rgba(255,152,0,.12);border:1px solid rgba(255,152,0,.35);color:#e65100;padding:3px 10px;border-radius:10px;font-size:11px;font-family:var(--fm)}
[data-theme=dark] .ocr-rev{color:#ffb74d}
.ocr-legend{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px;font-size:11px;font-family:var(--fm)}
.leg.g{color:#2e7d32} .leg.y{color:#f57f17} .leg.o{color:#e65100} .leg.r{color:#b71c1c}
[data-theme=dark] .leg.g{color:#81c784} [data-theme=dark] .leg.y{color:#fff176} [data-theme=dark] .leg.o{color:#ffb74d} [data-theme=dark] .leg.r{color:#ef9a9a}
.conf-bar{height:5px;display:flex;border-radius:3px;overflow:hidden;background:var(--border);margin:6px 0 14px}
.cb-seg{height:100%}
.cb-seg.green{background:#4caf50} .cb-seg.yellow{background:#ffc400} .cb-seg.orange{background:#ff6d00} .cb-seg.red{background:#f44336}
.ocr-page{margin-bottom:28px;padding-bottom:22px;border-bottom:1px solid var(--border)}
.ocr-ph{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;font-family:var(--fm);font-size:10px}
.ocr-pn{font-weight:600;color:var(--accent2)}
.ocr-src{background:var(--bg);border:1px solid var(--border);padding:1px 5px;border-radius:7px;color:var(--ink3)}
.ocr-prep{color:var(--ink3)}
.ocr-body{line-height:2;font-size:.94rem}
.ocr-empty{color:var(--ink3);font-style:italic;font-size:.88rem}
/* word spans */
.ow{position:relative;display:inline;border-radius:2px;cursor:help;padding:0 1px;transition:background var(--tr)}
.ow-green{border-bottom:2px solid rgba(76,175,80,.55)}
.ow-yellow{border-bottom:2px solid rgba(255,196,0,.65);background:rgba(255,196,0,.07)}
.ow-orange{border-bottom:2px solid rgba(255,109,0,.65);background:rgba(255,109,0,.09)}
.ow-red{border-bottom:2px solid rgba(244,67,54,.65);background:rgba(244,67,54,.09)}
.ow[data-conflict]{outline:1px dashed rgba(244,67,54,.45);outline-offset:1px}
.ow:hover,.ow:focus{background:rgba(0,0,0,.055);outline:none}
[data-theme=dark] .ow:hover,[data-theme=dark] .ow:focus{background:rgba(255,255,255,.07)}
.ott{display:none;position:absolute;bottom:calc(100% + 5px);left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);font-family:var(--fm);font-size:10px;padding:3px 7px;border-radius:var(--r);white-space:nowrap;z-index:200;pointer-events:none;box-shadow:var(--sh)}
.ott::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:4px solid transparent;border-top-color:var(--ink)}
.ow:hover .ott,.ow:focus .ott{display:block}

/* print */
@media print{
  #tb,#sb{display:none!important}
  #lay{display:block;padding-top:0}
  #mn{padding:0}
  #doc{max-width:100%}
  .ow{border-bottom:none!important;background:none!important}
  #ocrp{display:none!important}
}
/* responsive */
@media(max-width:800px){
  :root{--fw:100%}
  #lay{grid-template-columns:1fr}
  #sb{position:static;height:auto;border-right:none;border-bottom:1px solid var(--border)}
  #mn{padding:22px 16px 60px}
  .dt{font-size:1.75rem}
  #si{width:130px}
}
@media(max-width:480px){
  .tb-title{display:none}
  #mn{padding:14px 10px 60px}
}
"""

# ─────────────────────────────────────────────────────────────
#  JS
# ─────────────────────────────────────────────────────────────

JS = r"""
// Theme
function toggleTheme(){
  const d=document.documentElement,b=document.getElementById('tb-th');
  if(d.dataset.theme==='dark'){d.removeAttribute('data-theme');b.textContent='◑ Dark';localStorage.setItem('odx-th','light');}
  else{d.dataset.theme='dark';b.textContent='◑ Light';localStorage.setItem('odx-th','dark');}
}
(function(){
  const s=localStorage.getItem('odx-th');
  const pd=window.matchMedia('(prefers-color-scheme: dark)').matches;
  if(s==='dark'||(s===null&&pd)){
    document.documentElement.dataset.theme='dark';
    document.addEventListener('DOMContentLoaded',function(){
      const b=document.getElementById('tb-th');
      if(b)b.textContent='◑ Light';
    });
  }
})();

// OCR toggle
function toggleOCR(){
  const op=document.getElementById('ocrp');
  const dp=document.getElementById('doc');
  const b=document.getElementById('tb-ocr');
  if(!op)return;
  const on=op.classList.toggle('on');
  dp.classList.toggle('hide',on);
  b.classList.toggle('on',on);
  b.textContent=on?'⎋ Documento':'◎ Vista OCR';
  if(op.getAttribute('aria-hidden'))op.removeAttribute('aria-hidden');
}

// Search
let sm=[],si2=0;
function clearSrch(){
  document.querySelectorAll('mark.srch').forEach(m=>{
    const p=m.parentNode;
    p.replaceChild(document.createTextNode(m.textContent),m);
    p.normalize();
  });
  sm=[];si2=0;
  document.getElementById('sc').textContent='';
}
function doSearch(q){
  clearSrch();
  if(!q||q.length<2)return;
  const c=document.getElementById('doc');
  if(!c)return;
  const w=document.createTreeWalker(c,NodeFilter.SHOW_TEXT);
  const re=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi');
  const ns=[];let n;
  while((n=w.nextNode()))ns.push(n);
  ns.forEach(nd=>{
    if(!nd.textContent.match(re))return;
    const sp=document.createElement('span');
    sp.innerHTML=nd.textContent.replace(re,m=>`<mark class="srch">${m}</mark>`);
    nd.parentNode.replaceChild(sp,nd);
  });
  sm=Array.from(document.querySelectorAll('mark.srch'));
  const sc=document.getElementById('sc');
  if(!sm.length){sc.textContent='nessun risultato';return;}
  sc.textContent=`1 / ${sm.length}`;
  sm[0].scrollIntoView({behavior:'smooth',block:'center'});
  sm[0].style.outline='2px solid #ff6d00';
}
function snav(d){
  if(!sm.length)return;
  sm[si2].style.outline='';
  si2=(si2+d+sm.length)%sm.length;
  sm[si2].style.outline='2px solid #ff6d00';
  sm[si2].scrollIntoView({behavior:'smooth',block:'center'});
  document.getElementById('sc').textContent=`${si2+1} / ${sm.length}`;
}
document.addEventListener('DOMContentLoaded',function(){
  const inp=document.getElementById('si');
  if(inp){
    let t;
    inp.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(()=>doSearch(inp.value),280);});
    inp.addEventListener('keydown',e=>{
      if(e.key==='Enter'){e.shiftKey?snav(-1):snav(1);}
      if(e.key==='Escape'){inp.value='';clearSrch();}
    });
  }
  document.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&e.key==='f'){
      const inp=document.getElementById('si');
      if(inp){e.preventDefault();inp.focus();inp.select();}
    }
  });
});

// TOC scroll spy
document.addEventListener('DOMContentLoaded',function(){
  const hs=document.querySelectorAll('.odx-heading[id]');
  const ls=document.querySelectorAll('.toc-item a');
  if(!hs.length||!ls.length)return;
  const obs=new IntersectionObserver(entries=>{
    entries.forEach(e=>{
      if(e.isIntersecting){
        ls.forEach(l=>l.style.fontWeight='');
        const l=document.querySelector(`.toc-item a[href="#${e.target.id}"]`);
        if(l)l.style.fontWeight='700';
      }
    });
  },{rootMargin:'-15% 0px -70% 0px'});
  hs.forEach(h=>obs.observe(h));
});
"""


# ─────────────────────────────────────────────────────────────
#  RENDERER PRINCIPALE
# ─────────────────────────────────────────────────────────────

class ODXHTMLRenderer:
    """
    Converte un file .odx in una pagina HTML standalone completa.
    Zero dipendenze esterne obbligatorie.
    """
    def __init__(self):
        self.sem = SemanticRenderer()
        self.ocr = OCRRenderer()

    def render(self, odx_path: str) -> str:
        r = ODXReader(odx_path)
        meta = r.get_meta()
        text = r.get_text()
        xml  = r.get_semantic_xml()
        ocr  = r.get_ocr()
        info = r.get_info()

        title    = meta.get("title", Path(odx_path).stem)
        lang     = meta.get("lang", "en")
        authors  = meta.get("authors", [])
        created  = (meta.get("created_at") or "")[:10]
        uuid_s   = meta.get("uuid","")
        odxv     = meta.get("odx_version","0.1")
        dtype    = meta.get("document_type","other")
        sfmt     = meta.get("source_format","native")
        npages   = meta.get("page_count","—")
        segs     = info.get("segments_present",[])

        # Body
        if xml:
            body = self.sem.render(xml)
        elif text:
            body = "\n".join(
                f'<p class="odx-para">{p.strip()}</p>'
                for p in text.split("\n\n") if p.strip()
            )
        else:
            body = '<p class="odx-para">Documento vuoto.</p>'

        # OCR
        ocr_html = self.ocr.render_panel(ocr)
        has_ocr  = bool(ocr)
        ocr_btn  = ('<button class="tbtn" id="tb-ocr" onclick="toggleOCR()">◎ Vista OCR</button>'
                    if has_ocr else "")

        # TOC
        toc = self._toc(body)

        # Sidebar meta
        author_s = ", ".join(a.get("name","") for a in authors) or "—"
        meta_html = "".join(
            f'<div class="mr"><span class="mk">{k}</span><span class="mv">{v}</span></div>'
            for k, v in [
                ("Titolo", title), ("Autori", author_s), ("Lingua", lang),
                ("Data", created or "—"), ("Tipo", dtype),
                ("Sorgente", sfmt), ("Pagine", str(npages)),
            ]
        )
        all_layers = ["meta","semantic","layout","text","assets","ocr","diff","sign"]
        badges = "".join(
            f'<span class="lbadge {"ok" if l in segs else ""}">{l if l in segs else "· "+l}</span>'
            for l in all_layers
        )

        # Byline
        by_parts = []
        if author_s != "—": by_parts.append(f'<span class="by-i">{author_s}</span>')
        if created:          by_parts.append(f'<span class="by-i">{created}</span>')
        by_parts.append(f'<span class="by-i">ODX {odxv}</span>')
        by_parts.append(f'<span class="by-i" style="font-size:9px;opacity:.45">{uuid_s[:8]}…</span>')
        byline = "\n".join(by_parts)

        toc_sec = (f'<div class="sb-sec"><div class="sb-lbl">Indice</div>'
                   f'<ul class="toc-list">{toc}</ul></div>') if toc else ""

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        return f"""<!DOCTYPE html>
<html lang="{lang}" data-theme="">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{meta.get('description', title)}">
<meta name="generator" content="odx-renderer v0.1 — {ts}">
<title>{title} · ODX</title>
<style>{CSS}</style>
</head>
<body>

<nav id="tb">
  <span class="tb-brand">.odx</span>
  <span class="tb-title">{title}</span>
  <input id="si" type="search" placeholder="Cerca… (Ctrl+F)" aria-label="Cerca nel documento">
  <span id="sc" aria-live="polite"></span>
  <button class="tbtn" onclick="snav(-1)" aria-label="Precedente">↑</button>
  <button class="tbtn" onclick="snav(1)" aria-label="Successivo">↓</button>
  {ocr_btn}
  <button class="tbtn" id="tb-th" onclick="toggleTheme()">◑ Dark</button>
</nav>

<div id="lay">
  <aside id="sb">
    <div class="sb-sec">
      <div class="sb-lbl">Metadati</div>
      {meta_html}
    </div>
    <div class="sb-sec">
      <div class="sb-lbl">Layer ODX</div>
      {badges}
    </div>
    {toc_sec}
  </aside>
  <main id="mn">
    <article id="doc">
      <header class="dh">
        <h1 class="dt">{title}</h1>
        <div class="by">{byline}</div>
      </header>
      {body}
    </article>
    <div id="ocrp" aria-hidden="true">
      <h2 style="font-weight:300;font-size:1.35rem;margin-bottom:6px">Layer OCR</h2>
      <p style="font-family:var(--fm);font-size:10px;color:var(--ink3);margin-bottom:4px">
        Ogni parola riconosciuta è annotata con la confidence dell'engine OCR.
      </p>
      {ocr_html}
    </div>
  </main>
</div>

<script>{JS}</script>
</body>
</html>"""

    def render_to_file(self, odx_path: str, output_path: Optional[str] = None) -> str:
        if output_path is None:
            output_path = str(Path(odx_path).with_suffix(".html"))
        html = self.render(odx_path)
        Path(output_path).write_text(html, encoding="utf-8")
        size = Path(output_path).stat().st_size
        print(f"[ODXRenderer] ✅ {output_path} ({size:,} byte)")
        return output_path

    def _toc(self, html: str) -> str:
        headings = re.findall(
            r'<h([1-4])[^>]*id="([^"]*)"[^>]*class="odx-heading"[^>]*>([^<]+)</h\1>', html
        )
        if not headings: return ""
        return "\n".join(
            f'<li class="toc-item lv{lv}"><a href="#{hid}">{txt.strip()[:50]}</a></li>'
            for lv, hid, txt in headings[:24]
        )
