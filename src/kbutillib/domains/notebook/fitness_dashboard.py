"""Self-contained interactive fitness/model dashboard generator.

Given the *native JSON* outputs of the KBDL pipeline — a model-reconstruction
result and a fitness-model-analysis result — render ONE HTML dashboard (map data
+ all overlay data + JS inlined). Escher itself is loaded from the standalone
``dist`` build (the same build Escher's ``save_html`` uses) via a ``<script src>``
tag by default; pass ``inline_escher=True`` for a fully self-contained/offline
file (which then requires network only at build time). The same code visualizes
any genome because it keys purely on ModelSEED reaction ids.

Design (approved JS-swap shell):
  * one Escher map, recolored in-browser via ``builder.set_reaction_data`` when
    the CONDITION or FVA-SOLUTION dropdown changes (categorical reaction_scale
    over fitness classes: essential / active / unused / blocked);
  * two toggleable BADGE layers on the map — reactions whose genes carry an
    EXPERIMENTAL vs a PROPAGATED RB-TnSeq signal (distinct glyphs);
  * tabular tabs: genes+annotations, reactions+classes, conditions, concordance.

Public entry point: :func:`build_dashboard_html` (wrapped by
``EscherUtils.create_fitness_dashboard``).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

_GENE_RE = re.compile(r"[A-Za-z0-9_]+")

# fitness class -> (numeric code for reaction_scale, color, display)
_CLASS_STYLE = {
    "essential": (3, "#C44E52"),
    "active": (2, "#55A868"),
    "functional": (2, "#55A868"),
    "unused": (1, "#4C72B0"),
    "blocked": (0, "#4A5568"),
    "essential-forward": (3, "#C44E52"),
    "essential-reverse": (3, "#B0413F"),
    "variable": (2, "#55A868"),
}
_REACTION_SCALE = [
    {"type": "value", "value": 0, "color": "#4A5568", "size": 8},
    {"type": "value", "value": 1, "color": "#4C72B0", "size": 14},
    {"type": "value", "value": 2, "color": "#55A868", "size": 20},
    {"type": "value", "value": 3, "color": "#C44E52", "size": 26},
]


def _genes_in_gpr(gpr: str) -> set:
    if not gpr:
        return set()
    toks = {t for t in _GENE_RE.findall(gpr) if t.lower() not in ("and", "or")}
    return toks


def _load(obj_or_path):
    if isinstance(obj_or_path, (str, Path)):
        return json.load(open(obj_or_path))
    return obj_or_path


def _class_code(cls: str):
    return _CLASS_STYLE.get(str(cls), (None, "#999"))[0]


def _assemble(model_result, fitness_result, map_json,
              propagated_fitness=None, experimental_genes=None,
              annotation=None, agreement=None):
    """Turn native JSON into the compact data model the dashboard JS consumes."""
    model = model_result.get("model", model_result)
    reactions = model.get("reactions", [])
    recon_gaa = model_result.get("gaa_data", {}) if isinstance(model_result, dict) else {}

    # reaction -> genes (from GPR)
    rxn_genes = {rx["id"]: _genes_in_gpr(str(rx.get("gene_reaction_rule", ""))) for rx in reactions}
    gene_rxns = defaultdict(set)
    for rid, gs in rxn_genes.items():
        for g in gs:
            gene_rxns[g].add(rid)

    # map reaction id set (only paint/keep data for reactions on the map)
    layout = map_json[1] if isinstance(map_json, list) else map_json.get("layout", {})
    map_rxn_ids = {r.get("bigg_id") for r in layout.get("reactions", {}).values()}
    map_rxn_coord = {r.get("bigg_id"): (r.get("label_x"), r.get("label_y"))
                     for r in layout.get("reactions", {}).values()}

    # per-condition reaction class + flux  (from fitness sim)
    fgaa = fitness_result.get("gaa_data", {})
    sim_rxn = fgaa.get("v2_fitness_simulation_reaction", [])
    class_by_cond = defaultdict(dict)   # cond -> {rxn: code}
    flux_by_cond = defaultdict(dict)
    raw_class_by_cond = defaultdict(dict)
    cond_counter = defaultdict(Counter)
    for r in sim_rxn:
        cond = r["condition_id"]; rid = r["reaction_id"]; cls = r.get("class")
        cond_counter[cond][cls] += 1
        if rid in map_rxn_ids:
            code = _class_code(cls)
            if code is not None:
                class_by_cond[cond][rid] = code
            if abs(r.get("flux") or 0) > 1e-9:
                flux_by_cond[cond][rid] = round(r["flux"], 4)
        raw_class_by_cond[cond][rid] = cls
    conditions = sorted(class_by_cond)

    # FVA solutions from the reconstruction (per medium), painted by FVA class
    fva_solutions = {}
    for row in recon_gaa.get("v2_reaction_fva", []):
        sol = str(row.get("model_fva_id", "recon-FVA")).split(":")[-1] or "recon-FVA"
        rid = row.get("reaction_id")
        if rid in map_rxn_ids:
            code = _class_code(row.get("class"))
            if code is not None:
                fva_solutions.setdefault(sol, {})[rid] = code

    # badges: propagated (genes with a propagated score) / experimental (genes in experimental set)
    prop_genes = set(propagated_fitness.keys()) if propagated_fitness else set()
    exp_genes = set(experimental_genes or [])
    badges = {}
    for rid in map_rxn_ids:
        gs = rxn_genes.get(rid, set())
        has_prop = bool(gs & prop_genes)
        has_exp = bool(gs & exp_genes)
        if has_prop or has_exp:
            x, y = map_rxn_coord.get(rid, (None, None))
            badges[rid] = {"x": x, "y": y, "prop": has_prop, "exp": has_exp}

    # ---- standardized tables (all derived from the KBDL native dumps) ----
    ann = annotation or {}
    agr = (agreement or {}).get("per_gene_confident", {}) if agreement else {}
    NSF = {"function", "role", "product"}
    ref = "Complete" if "Complete" in raw_class_by_cond else (conditions[0] if conditions else None)

    # reference-condition per-reaction flux + class (ALL model reactions, not just map)
    ref_flux, ref_class = {}, {}
    for r in sim_rxn:
        if r["condition_id"] == ref:
            ref_flux[r["reaction_id"]] = r.get("flux")
            ref_class[r["reaction_id"]] = r.get("class")
    # reconstruction FVA row (class/min/max) per reaction
    fva_row = {row.get("reaction_id"): row for row in recon_gaa.get("v2_reaction_fva", [])}

    def _gene_annotation(g):
        for tool in ("RAST", "BAKTA", "PROKKA", "DRAM2", "KOFAMSCAN"):
            for k, vv in ((ann.get(g) or {}).get(tool) or {}).items():
                if k.lower() in NSF:
                    for v in (vv or []):
                        if isinstance(v, str) and v.strip():
                            return v
        return ""

    def _gene_consistency(g):
        info = agr.get(g) or {}
        n, grps = info.get("n_tools_mapped") or 0, info.get("groups") or []
        return round(100 * max((len(x) for x in grps), default=0) / n) if n and grps else None

    def _prop_scores(g):
        d = propagated_fitness.get(g, {}) if propagated_fitness else {}
        return {c: v for c, v in d.items() if isinstance(v, (int, float)) and abs(v) > 1e-9}

    ess_rxns = {rid for rid, cls in ref_class.items() if str(cls).startswith("essential")}

    def _concordance(g):
        predicted = bool(gene_rxns.get(g, set()) & ess_rxns)
        sc = list(_prop_scores(g).values())
        measured = min(sc) if sc else None
        meas_imp = measured is not None and measured < -1.0
        cat = ("confirmed" if predicted and meas_imp else "predicted-only" if predicted
               else "data-only" if meas_imp else "neither")
        return predicted, measured, cat

    # GENE table (annotation-oriented): descriptor + reaction ids, % consistency,
    # propagated-fitness contribution, fitness-sim flux, concordance
    genes_tbl, concordance_tbl, fitness_detail_tbl = [], [], []
    cc = Counter()
    for g in sorted(set(ann) | set(gene_rxns) | prop_genes):
        ps = _prop_scores(g)
        predicted, measured, cat = _concordance(g)
        cc[cat] += 1
        rids = sorted(gene_rxns.get(g, ()))
        gflux = max((abs(ref_flux.get(r) or 0) for r in rids), default=0)
        genes_tbl.append({
            "gene": g,
            "annotation": _gene_annotation(g),
            "reactions": (", ".join(rids[:4]) + (f" +{len(rids) - 4}" if len(rids) > 4 else "")) if rids else "",
            "pct_consistency": _gene_consistency(g),
            "fit_conditions": len(ps),
            "fit_min": (round(min(ps.values()), 3) if ps else None),
            "sim_flux_ref": round(gflux, 3),
            "concordance": cat,
        })
        if cat != "neither":
            concordance_tbl.append({"gene": g, "predicted_essential": predicted,
                                    "measured_min_fitness": (round(measured, 3) if measured is not None else None),
                                    "category": cat})
        for c, v in ps.items():                        # per-condition propagated contribution
            if abs(v) > 1.0:                           # impactful only, to bound the table
                fitness_detail_tbl.append({"gene": g, "condition": c,
                                           "propagated_fitness": round(v, 3), "concordance": cat})
    fitness_detail_tbl = sorted(fitness_detail_tbl, key=lambda r: r["propagated_fitness"])[:1500]

    # REACTION table (model-oriented): same shape but reaction-centric + FVA results
    def _rxn_consistency(rid):
        cats = [(agr.get(g) or {}).get("cat") for g in rxn_genes.get(rid, set())]
        cats = [c for c in cats if c]
        return round(100 * sum(c in ("consensus", "single-tool") for c in cats) / len(cats)) if cats else None

    reactions_tbl = []
    for rx in reactions:
        rid = rx["id"]
        gs = sorted(rxn_genes.get(rid, ()))
        fva = fva_row.get(rid, {})
        gsc = [min(_prop_scores(g).values()) for g in gs if _prop_scores(g)]
        reactions_tbl.append({
            "reaction": rid,
            "name": rx.get("name", ""),
            "genes": (", ".join(gs[:3]) + (f" +{len(gs) - 3}" if len(gs) > 3 else "")) if gs else "",
            "annotation": _gene_annotation(gs[0]) if gs else "",
            "pct_consistency": _rxn_consistency(rid),
            "fva_class": fva.get("class", ""),
            "fva_min": (round(fva["min"], 3) if fva.get("min") is not None else None),
            "fva_max": (round(fva["max"], 3) if fva.get("max") is not None else None),
            "sim_class_ref": ref_class.get(rid, ""),
            "sim_flux_ref": (round(ref_flux[rid], 3) if ref_flux.get(rid) is not None else None),
            "fit_min": (round(min(gsc), 3) if gsc else None),
            "on_map": rid in map_rxn_ids,
        })

    # conditions table (class counts per condition)
    conditions_tbl = [{
        "condition": c,
        "essential": cond_counter[c].get("essential", 0),
        "active": cond_counter[c].get("active", 0) + cond_counter[c].get("functional", 0),
        "unused": cond_counter[c].get("unused", 0),
        "blocked": cond_counter[c].get("blocked", 0),
    } for c in conditions]

    return {
        "conditions": conditions,
        "default_condition": ref,
        "reaction_scale": _REACTION_SCALE,
        "class_by_cond": class_by_cond,
        "flux_by_cond": flux_by_cond,
        "fva_solutions": fva_solutions,
        "badges": badges,
        "tables": {
            "genes": genes_tbl,
            "reactions": reactions_tbl,
            "conditions": conditions_tbl,
            "fitness_detail": fitness_detail_tbl,
            "concordance": concordance_tbl,
            "concordance_summary": dict(cc),
        },
        "counts": {
            "conditions": len(conditions),
            "map_reactions": len(map_rxn_ids),
            "badged_reactions": len(badges),
            "prop_genes": len(prop_genes),
            "exp_genes": len(exp_genes),
        },
    }


ESCHER_CDN = "https://unpkg.com/escher@1.8.1/dist/escher.min.js"


# Escher's dist bundles are built with webpack ``output.publicPath: 'auto'``. That
# runtime derives the public path from ``document.currentScript.src``, falling back
# to scanning <script> tags for one whose src matches /^http(s?):/ — and it THROWS
# ("Automatic publicPath is not supported in this browser") when neither yields a
# URL. Both fail for an inlined bundle: an inline <script> has src "", and the
# fallback's http(s)-only regex can never be satisfied by a file:// page. The bundle
# then aborts before assigning window.escher and the map silently renders blank.
#
# publicPath is only ever consulted for lazy-loaded chunks, which a single inlined
# bundle never requests, so any truthy value is safe. Shim currentScript to report a
# SCRIPT element carrying the page URL, deferring to the native getter whenever it
# does supply a src (i.e. the <script src> path, which is unaffected).
_PUBLICPATH_SHIM = (
    "<script>/* escher inline-bundle publicPath shim */(function(){"
    "var d=Object.getOwnPropertyDescriptor(Document.prototype,'currentScript');"
    "Object.defineProperty(document,'currentScript',{configurable:true,get:function(){"
    "var c=d&&d.get?d.get.call(document):null;"
    "return (c&&c.src)?c:{tagName:'SCRIPT',src:location.href};}});})();</script>"
)


def build_dashboard_html(model_result, fitness_result, map_json, escher_js=None,
                         propagated_fitness=None, experimental_genes=None,
                         annotation=None, agreement=None, title="Fitness · Model dashboard",
                         subtitle="", escher_url: str = ESCHER_CDN) -> str:
    """Assemble + render the full dashboard HTML string.

    Escher is loaded from ``escher_url`` (the standalone ``dist`` build, the same
    one Escher's own ``save_html`` references) via a ``<script src>`` tag. Pass
    ``escher_js`` (any Escher bundle as a JS string, including the pip package's
    ``static/escher.min.js``) for a fully self-contained/offline file; it is
    emitted after ``_PUBLICPATH_SHIM``, without which an inlined bundle throws on
    webpack's auto-publicPath and the map renders blank. The Jupyter-widget build
    does render standalone once that shim is in place.
    """
    if escher_js:
        escher_include = _PUBLICPATH_SHIM + "<script>" + escher_js + "</script>"
    else:
        escher_include = '<script src="%s" crossorigin="anonymous"></script>' % escher_url
    model_result = _load(model_result)
    fitness_result = _load(fitness_result)
    map_json = _load(map_json)
    if propagated_fitness is not None:
        propagated_fitness = _load(propagated_fitness)
        if isinstance(propagated_fitness, dict) and "fitness" in propagated_fitness:
            propagated_fitness = propagated_fitness["fitness"]
    if annotation is not None:
        annotation = _load(annotation)
    if agreement is not None:
        agreement = _load(agreement)

    D = _assemble(model_result, fitness_result, map_json, propagated_fitness,
                  experimental_genes, annotation, agreement)
    payload = {
        "map": map_json,
        "reaction_scale": D["reaction_scale"],
        "conditions": D["conditions"],
        "default_condition": D["default_condition"],
        "class_by_cond": D["class_by_cond"],
        "fva_solutions": D["fva_solutions"],
        "badges": D["badges"],
        "tables": D["tables"],
    }
    payload_json = json.dumps(payload, default=list)
    return _HTML_TEMPLATE.replace("__ESCHER_INCLUDE__", escher_include) \
                         .replace("__PAYLOAD__", payload_json) \
                         .replace("__TITLE__", title) \
                         .replace("__SUBTITLE__", subtitle) \
                         .replace("__COUNTS__", json.dumps(D["counts"]))


_HTML_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>__TITLE__</title>
<style>
:root{--bg:#0f1720;--panel:#18222e;--ink:#e8eef4;--muted:#93a4b3;--line:#26333f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1300px;margin:0 auto;padding:16px}
h1{font-size:19px;margin:0 0 2px}.sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:10px}
.controls label{color:var(--muted);font-size:12px;margin-right:5px}
select{background:#0d141c;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;margin-left:auto}
.chip{display:inline-flex;align-items:center;gap:5px}.sw{width:11px;height:11px;border-radius:2px;display:inline-block}
#map-container{height:620px;background:#0b1118;border:1px solid var(--line);border-radius:10px;position:relative}
.tabs{display:flex;gap:6px;margin:14px 0 0}.tab{padding:7px 13px;background:var(--panel);border:1px solid var(--line);border-bottom:none;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;color:var(--muted)}
.tab.active{color:var(--ink);background:#1e2b3a}
.tabbody{background:var(--panel);border:1px solid var(--line);border-radius:0 10px 10px 10px;padding:10px;max-height:440px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:0;background:#1e2b3a;cursor:pointer}tr:hover td{background:#16202c}
.pill{padding:1px 7px;border-radius:9px;font-size:11px}
.badge-exp{fill:none;stroke:#f0c040;stroke-width:3}.badge-prop{fill:#8172B3;fill-opacity:.85;stroke:#cbb;stroke-width:.5}
.muted{color:var(--muted)}
</style></head><body><div class="wrap">
<h1>__TITLE__</h1><p class="sub">__SUBTITLE__</p>
<div class="controls">
  <div><label>Fitness condition</label><select id="condSel"></select></div>
  <div><label>FVA solution</label><select id="fvaSel"><option value="">— (use condition) —</option></select></div>
  <div><label><input type="checkbox" id="expTog"> exp badge</label>
       <label><input type="checkbox" id="propTog" checked> prop badge</label></div>
  <div class="legend">
    <span class="chip"><span class="sw" style="background:#C44E52"></span>essential</span>
    <span class="chip"><span class="sw" style="background:#55A868"></span>active</span>
    <span class="chip"><span class="sw" style="background:#4C72B0"></span>unused</span>
    <span class="chip"><span class="sw" style="background:#4A5568"></span>blocked</span>
    <span class="chip"><span class="sw" style="background:#8172B3"></span>prop RB-TnSeq</span>
    <span class="chip"><span class="sw" style="border:2px solid #f0c040;background:none"></span>exp RB-TnSeq</span>
  </div>
</div>
<div id="status" style="font-size:12px;color:#93a4b3;margin:0 0 6px"></div>
<div id="map-container"></div>
<div class="tabs" id="tabs"></div><div class="tabbody" id="tabbody"></div>
<p class="muted" style="font-size:11px;margin-top:8px">Lines are colored by fitness CLASS (not flux). Badges mark reactions whose genes carry an RB-TnSeq signal. Data: <span id="counts"></span></p>
</div>
__ESCHER_INCLUDE__
<script>
const P = __PAYLOAD__;
document.getElementById('counts').textContent = JSON.stringify(__COUNTS__);
// ---- populate dropdowns ----
const condSel=document.getElementById('condSel'), fvaSel=document.getElementById('fvaSel');
P.conditions.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;if(c===P.default_condition)o.selected=true;condSel.appendChild(o);});
Object.keys(P.fva_solutions||{}).forEach(s=>{const o=document.createElement('option');o.value=s;o.textContent=s;fvaSel.appendChild(o);});
// ---- Escher builder (with visible diagnostics for headless-built dashboards) ----
let builder=null;
const statusEl=document.getElementById('status');
function status(msg){ statusEl.textContent=msg; }
function currentData(){
  const fva=fvaSel.value;
  if(fva && P.fva_solutions[fva]) return P.fva_solutions[fva];
  return P.class_by_cond[condSel.value]||{};
}
function paint(){ try{ if(builder) builder.set_reaction_data(currentData()); }catch(e){ status('paint error: '+e.message);} }
try{
  if(typeof escher==='undefined') throw new Error('escher global not defined (inline bundle failed to load)');
  if(!escher.libs || !escher.libs.d3_select) throw new Error('escher.libs.d3_select missing (unexpected escher build)');
  const initData=currentData();
  window.builder = builder = escher.Builder(P.map, null, null, escher.libs.d3_select('#map-container'), {
    fill_screen:false, menu:'zoom', scroll_behavior:'zoom', use_3d_transform:false,
    reaction_styles:['color','size'], reaction_no_data_color:'#31404f', reaction_no_data_size:6,
    reaction_data:initData, reaction_scale:P.reaction_scale, never_ask_before_quit:true,
    first_load_callback:function(){ /* NB: escher invokes this unbound, so `this` is Window --
      do NOT reassign builder here; the escher.Builder() return value below is the real
      Builder and the only object carrying set_reaction_data. */ drawBadges();
      const n=document.querySelectorAll('#map-container .reaction').length;
      status('map loaded · '+n+' reactions drawn · '+Object.keys(initData).length+' painted ('+condSel.value+')'); }
  });
  status('builder created; rendering…  (window.builder available in console)');
}catch(e){ status('ESCHER INIT ERROR: '+e.message+'  — open devtools console for the stack.'); console.error(e); }
// ---- badge layer (injected into escher zoom group so it pans/zooms) ----
function zoomGroup(){
  const el=document.querySelector('#map-container .reactions')||document.querySelector('#map-container .zoom-g');
  return el ? el.parentNode : null;
}
function drawBadges(){
  const g0=zoomGroup(); if(!g0) return;
  let layer=document.getElementById('badge-layer');
  if(layer) layer.remove();
  const NS='http://www.w3.org/2000/svg';
  layer=document.createElementNS(NS,'g'); layer.setAttribute('id','badge-layer');
  const showExp=document.getElementById('expTog').checked, showProp=document.getElementById('propTog').checked;
  Object.entries(P.badges).forEach(([rid,b])=>{
    if(b.x==null||b.y==null) return;
    if(b.prop && showProp){const c=document.createElementNS(NS,'circle');c.setAttribute('cx',b.x);c.setAttribute('cy',b.y);c.setAttribute('r',10);c.setAttribute('class','badge-prop');layer.appendChild(c);}
    if(b.exp && showExp){const r=document.createElementNS(NS,'circle');r.setAttribute('cx',b.x);r.setAttribute('cy',b.y);r.setAttribute('r',16);r.setAttribute('class','badge-exp');layer.appendChild(r);}
  });
  g0.appendChild(layer);
}
condSel.onchange=()=>{paint();};
fvaSel.onchange=()=>{paint();};
document.getElementById('expTog').onchange=drawBadges;
document.getElementById('propTog').onchange=drawBadges;
// ---- tables ----
const TABS=[['genes','Genes (annotation-oriented)'],['reactions','Reactions (model-oriented)'],['fitness_detail','Fitness detail'],['conditions','Conditions'],['concordance','Concordance']];
const tabsEl=document.getElementById('tabs'), body=document.getElementById('tabbody');
function renderTable(rows){
  if(!rows||!rows.length) return '<p class="muted">no rows</p>';
  const cols=Object.keys(rows[0]);
  let h='<table><thead><tr>'+cols.map(c=>`<th onclick="sortT('${c}')">${c}</th>`).join('')+'</tr></thead><tbody>';
  h+=rows.map(r=>'<tr>'+cols.map(c=>`<td>${r[c]===null?'':r[c]}</td>`).join('')+'</tr>').join('');
  return h+'</tbody></table>';
}
let cur='genes', sortCol=null, sortAsc=true;
function show(t){cur=t;sortCol=null;tabsEl.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.t===t));
  let extra='';
  if(t==='concordance'&&P.tables.concordance_summary) extra='<p class="muted">summary: '+JSON.stringify(P.tables.concordance_summary)+'</p>';
  body.innerHTML=extra+renderTable(P.tables[t]);}
window.sortT=function(c){const rows=P.tables[cur];if(sortCol===c)sortAsc=!sortAsc;else{sortCol=c;sortAsc=true;}
  rows.sort((a,b)=>{let x=a[c],y=b[c];if(x==null)return 1;if(y==null)return -1;return (x>y?1:x<y?-1:0)*(sortAsc?1:-1);});show(cur);};
TABS.forEach(([t,lbl])=>{const d=document.createElement('div');d.className='tab';d.dataset.t=t;d.textContent=lbl;d.onclick=()=>show(t);tabsEl.appendChild(d);});
show('genes');
</script></body></html>"""
