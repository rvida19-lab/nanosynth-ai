"""
NanoSynth AI — Local Research Server
Created by: Rocco Vidaurre-Kirsch
Run with: python app.py
Then open http://localhost:5000 in your browser
"""

from flask import Flask, request, jsonify, send_from_directory, send_file
import anthropic
import requests
import json
import os
import re
import time
import threading
import io
from datetime import datetime

# svglib is used instead of cairosvg to convert the diagram SVG for the PDF.
# cairosvg needs native Cairo C libraries that do NOT install via pip on Windows
# (it fails with 'no library called "cairo-2" was found'). svglib is pure Python,
# installs cleanly on every platform, and keeps the diagram as vector art in the
# PDF rather than rasterizing it to PNG — so it stays sharp at any zoom level.
from svglib.svglib import svg2rlg
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, BaseDocTemplate, PageTemplate, Frame, NextPageTemplate,
    Paragraph, Spacer, Table, TableStyle, Image as RLImage,
    HRFlowable, PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

app = Flask(__name__, static_folder="static")

NCBI_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_API_KEY = os.environ.get("NCBI_API_KEY")  # optional, raises rate limit from 3/sec to 10/sec

# ── NCBI rate limiting ─────────────────────────────────────────────────────
# NCBI throttles E-utilities BY IP: 3 requests/sec without an API key, 10/sec
# with one. On a laptop that never mattered — one person, one query at a time.
# On a shared deployment every visitor comes from the same server IP, and a
# single generation fires up to ~11 queries (grounding + similar designs +
# one per synthesis method). Two people generating at once will trip the limit
# and start collecting 429s, which silently degrades grounding for everyone.
#
# So all E-utilities traffic goes through one throttle. It is deliberately a
# process-wide lock rather than per-request: the limit is per IP, not per user.
# Spacing is set BELOW the documented ceiling, not exactly at it: pacing at
# precisely 1/3 s puts 3 calls in every 1-second window with zero margin, so any
# clock jitter or retry tips it to 4 and earns a 429. The extra ~0.3 s per
# generation is irrelevant next to two Claude calls.
_NCBI_MIN_INTERVAL = 0.12 if NCBI_API_KEY else 0.40   # ~8.3/sec keyed, ~2.5/sec not
_ncbi_lock = threading.Lock()
_ncbi_last_call = [0.0]   # list so the closure can mutate it


def _ncbi_get(endpoint, params, timeout=10):
    """
    Performs a rate-limited GET against NCBI E-utilities.

    Every PubMed call in this file goes through here. Callers handle their own
    exceptions exactly as before; this only paces the requests and attaches the
    API key when one is configured.
    """
    if NCBI_API_KEY:
        params = {**params, "api_key": NCBI_API_KEY}

    with _ncbi_lock:
        elapsed = time.monotonic() - _ncbi_last_call[0]
        wait = _NCBI_MIN_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        _ncbi_last_call[0] = time.monotonic()

    return requests.get(f"{NCBI_EUTILS_BASE}/{endpoint}", params=params, timeout=timeout)

# ── Simple in-memory PubMed cache ──────────────────────────────────────────
# Keyed on (disease, drug, retmax, fetch_abstracts) so identical queries —
# whether from the Related Literature card or the grounding step — reuse
# results instead of re-hitting NCBI. Lives only for the server process;
# restarting `python app.py` clears it. That's intentional for a research
# tool where you want fresh data on a new session, but fast reruns within one.
_PUBMED_CACHE = {}
_CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours — long enough to help repeat testing
                                    # in a session, short enough that newly
                                    # published papers surface within a day
_CACHE_MAX_ENTRIES = 200           # simple eviction guard against unbounded growth

def _cache_key(disease, drug, retmax, fetch_abstracts):
    return (disease.strip().lower(), drug.strip().lower(), retmax, fetch_abstracts)

def _cache_get(key):
    entry = _PUBMED_CACHE.get(key)
    if not entry:
        return None
    papers, query, cached_at = entry
    if time.time() - cached_at > _CACHE_TTL_SECONDS:
        del _PUBMED_CACHE[key]
        return None
    return papers, query

def _cache_set(key, papers, query):
    if len(_PUBMED_CACHE) >= _CACHE_MAX_ENTRIES:
        # Evict the oldest entry rather than letting the cache grow forever
        oldest_key = min(_PUBMED_CACHE, key=lambda k: _PUBMED_CACHE[k][2])
        del _PUBMED_CACHE[oldest_key]
    _PUBMED_CACHE[key] = (papers, query, time.time())


# ── Anthropic client (reads ANTHROPIC_API_KEY env var, or set key in .env) ──
def get_client(api_key=None):
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("No API key provided. Set ANTHROPIC_API_KEY env var or enter it in the UI.")
    return anthropic.Anthropic(api_key=key)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ──────────────────────────────────────────────────────────────────────────
# Literature search (PubMed via NCBI E-utilities)
# ──────────────────────────────────────────────────────────────────────────

ARCHITECTURES = (
    "mesoporous_core_shell", "vesicle", "lipid_nanoparticle", "micelle",
    "polymer_matrix", "inorganic_core_ligand", "dendrimer",
)

# Models drift on enums. Map the plausible things they might emit back onto the
# canonical key rather than silently falling through to the default topology —
# drawing an LNP as a mesoporous particle is exactly the class of error we are
# trying to eliminate.
_ARCH_SYNONYMS = {
    "lnp": "lipid_nanoparticle", "lipid": "lipid_nanoparticle",
    "lipid_nanoparticles": "lipid_nanoparticle", "ionizable_lipid": "lipid_nanoparticle",
    "liposome": "vesicle", "polymersome": "vesicle", "exosome": "vesicle",
    "bilayer": "vesicle", "vesicular": "vesicle",
    "msn": "mesoporous_core_shell", "mesoporous": "mesoporous_core_shell",
    "mesoporous_silica": "mesoporous_core_shell", "core_shell": "mesoporous_core_shell",
    "polymeric_micelle": "micelle", "block_copolymer": "micelle",
    "plga": "polymer_matrix", "nanogel": "polymer_matrix", "albumin": "polymer_matrix",
    "solid_lipid_nanoparticle": "polymer_matrix", "sln": "polymer_matrix",
    "gold": "inorganic_core_ligand", "aunp": "inorganic_core_ligand",
    "quantum_dot": "inorganic_core_ligand", "inorganic": "inorganic_core_ligand",
    "pamam": "dendrimer", "hyperbranched": "dendrimer",
}


ARCH_LABELS = {
    "mesoporous_core_shell": "Mesoporous core–shell (solid core, gated mesopores)",
    "vesicle":               "Vesicle (lipid bilayer enclosing an aqueous lumen)",
    "lipid_nanoparticle":    "Lipid nanoparticle (dense ionizable-lipid interior, no lumen)",
    "micelle":               "Polymeric micelle (amphiphile monolayer, hydrophobic core)",
    "polymer_matrix":        "Polymer matrix (drug dispersed through a degradable matrix)",
    "inorganic_core_ligand": "Inorganic core with surface-conjugated ligands",
    "dendrimer":             "Dendrimer (hyperbranched, payload in voids and termini)",
}


def _normalize_architecture(value):
    """
    Coerces the model's architecture field onto one of the seven canonical
    topologies. Falls back to mesoporous_core_shell only as a last resort.
    """
    if not value:
        return "mesoporous_core_shell"
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key in ARCHITECTURES:
        return key
    if key in _ARCH_SYNONYMS:
        return _ARCH_SYNONYMS[key]
    # Substring rescue: "lipid_nanoparticle_(lnp)" and similar
    for canon_key in ARCHITECTURES:
        if canon_key in key:
            return canon_key
    for syn, canon_key in _ARCH_SYNONYMS.items():
        if syn in key:
            return canon_key
    return "mesoporous_core_shell"


def _fetch_summaries(id_list):
    """
    Fetches esummary records for a list of PMIDs and normalizes them into the
    paper dicts used throughout the app. Shared by the grounding search and the
    similar-designs search so both produce identical, citable structures.
    """
    if not id_list:
        return []
    esum_params = {"db": "pubmed", "id": ",".join(id_list), "retmode": "json"}
    esum_resp = _ncbi_get("esummary.fcgi", esum_params, timeout=10)
    esum_resp.raise_for_status()
    summary = esum_resp.json().get("result", {})

    papers = []
    for pid in summary.get("uids", []):
        rec = summary.get(pid, {})
        # Author list: Vancouver style lists up to 6 authors, then "et al."
        author_names = [a.get("name", "") for a in rec.get("authors", []) if a.get("name")]
        # DOI can appear either as elocationid ("doi: 10.xxxx/yyy") or in articleids
        doi = ""
        eloc = rec.get("elocationid", "") or ""
        if "10." in eloc:
            doi = eloc.replace("doi:", "").strip()
        else:
            for aid in rec.get("articleids", []) or []:
                if aid.get("idtype") == "doi":
                    doi = (aid.get("value") or "").strip()
                    break

        papers.append({
            "pmid": pid,
            "title": rec.get("title", "Untitled"),
            "journal": rec.get("fulljournalname") or rec.get("source", ""),
            "journal_abbrev": rec.get("source", "") or rec.get("fulljournalname", ""),
            "year": (rec.get("pubdate", "") or "")[:4],
            "volume": rec.get("volume", "") or "",
            "issue": rec.get("issue", "") or "",
            "pages": rec.get("pages", "") or "",
            "doi": doi,
            "author_list": author_names,
            "authors": ", ".join(author_names[:3]),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            "abstract": "",
        })
    return papers


# A disease+drug query returns *therapy* literature — clinical trials, treatment
# reviews. That is the right corpus for grounding factual claims, but it is the
# WRONG corpus for "what published nanoparticle designs are closest to mine?".
# Constraining the search to formulation/delivery vocabulary surfaces actual
# nanocarrier design papers instead.
_NANO_DESIGN_TERMS = (
    '("nanoparticle"[Title/Abstract] OR "nanoparticles"[Title/Abstract] OR '
    '"nanocarrier"[Title/Abstract] OR "nanomedicine"[Title/Abstract] OR '
    '"liposome"[Title/Abstract] OR "lipid nanoparticle"[Title/Abstract] OR '
    '"micelle"[Title/Abstract] OR "drug delivery"[Title/Abstract])'
)


def _attach_abstracts(papers):
    """
    Fetches abstracts for a list of paper dicts and attaches them in place.
    Shared by the therapy-grounding search and the synthesis-methods search: both
    need real source text, since a claim can only be grounded against text the
    model has actually seen.
    """
    if not papers:
        return
    id_list = [p["pmid"] for p in papers]
    efetch_params = {"db": "pubmed", "id": ",".join(id_list),
                     "rettype": "abstract", "retmode": "text"}
    try:
        ef_resp = _ncbi_get("efetch.fcgi", efetch_params, timeout=15)
        ef_resp.raise_for_status()
    except requests.exceptions.RequestException:
        return
    chunks = re.split(r'\n\nPMID:\s*(\d+)\n', ef_resp.text)
    abstract_map = {}
    for i in range(1, len(chunks), 2):
        abstract_map[chunks[i]] = chunks[i - 1].strip()
    for p in papers:
        block = abstract_map.get(p["pmid"], "")
        if "Abstract" in block:
            block = block.split("Abstract", 1)[1].lstrip(" \n:")
        p["abstract"] = block[:1200].strip()


# Methods papers are a THIRD, distinct corpus. Neither the therapy search
# (disease AND drug) nor the similar-designs search (disease AND drug AND nano)
# retrieves papers that describe how to actually MAKE the particle — so the model
# has had no source that could ground a synthesis step, and has been correctly
# marking nearly all of them as extrapolated. Restricting to fabrication vocabulary
# surfaces the protocol literature the synthesis section needs.
_SYNTH_METHOD_TERMS = (
    '("synthesis"[Title/Abstract] OR "preparation"[Title/Abstract] OR '
    '"fabrication"[Title/Abstract] OR "protocol"[Title/Abstract] OR '
    '"formulation"[Title/Abstract] OR "sol-gel"[Title/Abstract] OR '
    '"encapsulation"[Title/Abstract] OR "conjugation"[Title/Abstract] OR '
    '"functionalization"[Title/Abstract] OR "characterization"[Title/Abstract])'
)


def search_synthesis_methods(keywords, retmax_each=2, cap=6):
    """
    Retrieves METHOD papers for the fabrication steps this specific design needs.

    `keywords` come from the model itself, after it has committed to an architecture
    and materials (e.g. "mesoporous silica sol-gel TEOS CTAB"). Searching per-step
    matters: one blended query returns generic reviews, whereas a query per step
    returns the paper that actually describes that step.

    Reviews are excluded on the first attempt. A survey article matches the keywords
    well but describes no specific procedure, so it cannot ground a synthesis step —
    that is how an earlier run ended up citing "Approaches on Ferrofluid Synthesis"
    for a step it does not contain. If excluding reviews finds nothing, we retry
    allowing them: a weak source still beats none, which would force the step back to
    "extrapolated".

    Returns (papers_with_abstracts, queries_used).
    """
    papers, queries, seen = [], [], set()

    for kw in (keywords or [])[:6]:
        kw = (kw or "").strip()
        if not kw:
            continue

        primary = f'("{kw}") AND {_SYNTH_METHOD_TERMS} NOT review[Publication Type]'
        loose   = f'("{kw}") AND {_SYNTH_METHOD_TERMS}'

        found, used_query = [], primary
        for attempt in (primary, loose):
            cache_key = ("methods", attempt.lower(), retmax_each)
            cached = _cache_get(cache_key)
            if cached is not None:
                found, _q = cached
            else:
                esearch_params = {
                    "db": "pubmed", "term": attempt, "retmode": "json",
                    "retmax": retmax_each, "sort": "relevance",
                }
                try:
                    r = _ncbi_get("esearch.fcgi", esearch_params, timeout=10)
                    r.raise_for_status()
                    id_list = r.json().get("esearchresult", {}).get("idlist", [])
                except requests.exceptions.RequestException:
                    id_list = []
                found = _fetch_summaries(id_list) if id_list else []
                if found:
                    _attach_abstracts(found)
                _cache_set(cache_key, found, attempt)

            used_query = attempt
            if found:
                break

        queries.append(used_query)
        for p in found:
            if p["pmid"] not in seen and len(papers) < cap:
                seen.add(p["pmid"])
                papers.append(p)

    return papers, queries


def search_similar_designs(disease, drug, retmax=5):
    """
    Finds the closest PUBLISHED NANOPARTICLE DESIGNS for this target — prior art
    the researcher may want to check before committing to a formulation.

    This is deliberately a different search from the grounding search:
      grounding  = (disease) AND (drug)                      -> therapy literature
      similarity = (disease) AND (drug) AND (nano/delivery)  -> formulation literature

    Progressively loosens if the tight query is empty, since a novel disease/drug
    pairing may simply have no published nanocarrier work yet — which is itself a
    useful signal and is surfaced to the user rather than hidden.
    Returns (papers, query_used).
    """
    disease = (disease or "").strip()
    drug = (drug or "").strip()

    attempts = []
    if disease and drug:
        attempts.append(f'("{disease}") AND ("{drug}") AND {_NANO_DESIGN_TERMS}')
    if disease:
        attempts.append(f'("{disease}") AND {_NANO_DESIGN_TERMS}')
    if drug:
        attempts.append(f'("{drug}") AND {_NANO_DESIGN_TERMS}')

    for query in attempts:
        cache_key = ("similar", query.lower(), retmax)
        cached = _cache_get(cache_key)
        if cached is not None:
            papers, _q = cached
            if papers:
                return papers, query
            continue

        esearch_params = {
            "db": "pubmed", "term": query, "retmode": "json",
            "retmax": retmax, "sort": "relevance",
        }
        try:
            r = _ncbi_get("esearch.fcgi", esearch_params, timeout=10)
            r.raise_for_status()
            id_list = r.json().get("esearchresult", {}).get("idlist", [])
        except requests.exceptions.RequestException:
            return [], query

        if not id_list:
            _cache_set(cache_key, [], query)
            continue

        papers = _fetch_summaries(id_list)
        _cache_set(cache_key, papers, query)
        if papers:
            return papers, query

    return [], (attempts[0] if attempts else "")


def search_pubmed(disease, drug, retmax=5, fetch_abstracts=False):
    """
    Searches PubMed for the closest-matching papers on disease + drug payload.
    Falls back to disease alone if that pairing returns nothing.
    If fetch_abstracts=True, also pulls abstract text via efetch — needed
    when grounding the AI's design against the actual paper content, not
    just titles/metadata.
    Checks the in-memory cache first; identical (disease, drug, retmax,
    fetch_abstracts) queries within the TTL window skip NCBI entirely.
    Returns (papers: list[dict], query_used: str).
    """
    cache_key = _cache_key(disease, drug, retmax, fetch_abstracts)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    primary_terms = [t for t in [disease, drug] if t]
    query = " AND ".join(f'("{t}")' for t in primary_terms)
    if not query:
        return [], ""  # nothing meaningful to cache here

    esearch_params = {
        "db": "pubmed", "term": query, "retmode": "json",
        "retmax": retmax, "sort": "relevance",
    }

    es_resp = _ncbi_get("esearch.fcgi", esearch_params, timeout=10)
    es_resp.raise_for_status()
    id_list = es_resp.json().get("esearchresult", {}).get("idlist", [])

    # Fallback: disease alone if disease+drug pairing isn't yet co-published
    if not id_list and disease:
        query = f'("{disease}")'
        esearch_params["term"] = query
        es_resp = _ncbi_get("esearch.fcgi", esearch_params, timeout=10)
        es_resp.raise_for_status()
        id_list = es_resp.json().get("esearchresult", {}).get("idlist", [])

    if not id_list:
        _cache_set(cache_key, [], query)
        return [], query

    papers = _fetch_summaries(id_list)

    if fetch_abstracts and papers:
        efetch_params = {
            "db": "pubmed", "id": ",".join(id_list),
            "rettype": "abstract", "retmode": "text",
        }
        ef_resp = _ncbi_get("efetch.fcgi", efetch_params, timeout=15)
        ef_resp.raise_for_status()
        # efetch returns plain text records separated by blank lines + PMID footers;
        # split on the "PMID: xxxxx" markers NCBI includes at the end of each record
        raw_text = ef_resp.text
        chunks = re.split(r'\n\nPMID:\s*(\d+)\n', raw_text)
        # chunks alternates: [text_before_first_pmid, pmid1, text_after_pmid1, pmid2, ...]
        # rebuild a {pmid: text_chunk_before_it} map
        abstract_map = {}
        for i in range(1, len(chunks), 2):
            pmid = chunks[i]
            text_block = chunks[i - 1]
            abstract_map[pmid] = text_block.strip()

        for p in papers:
            block = abstract_map.get(p["pmid"], "")
            # Strip the title/author/journal header lines, keep from "Abstract" onward if present
            if "Abstract" in block:
                block = block.split("Abstract", 1)[1].lstrip(" \n:")
            # Cap length to keep prompt size reasonable
            p["abstract"] = block[:1200].strip()

    _cache_set(cache_key, papers, query)
    return papers, query


@app.route("/literature", methods=["POST"])
def literature():
    """
    Standalone endpoint kept for the UI's independent 'Related Literature'
    card — does not fetch full abstracts, just titles/metadata (faster).
    """
    body = request.get_json()
    params = body.get("params", {})
    disease = params.get("disease", "").strip()
    drug = params.get("drug", "").strip()

    try:
        papers, query = search_pubmed(disease, drug, retmax=5, fetch_abstracts=False)
        return jsonify({"ok": True, "papers": papers, "query": query})
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": f"PubMed request failed: {e}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────
# Claude call with automatic retry on malformed JSON
# ──────────────────────────────────────────────────────────────────────────

def _collect_cited_tags(design):
    """
    Returns the set of source tags (S1, M2, ...) the model actually cited anywhere
    in the design. "References" means WORKS CITED — a paper we retrieved but never
    cited does not belong there, and listing it inflates the apparent evidence base.
    """
    tags = set()

    def take(claim):
        if isinstance(claim, dict):
            src = claim.get("source")
            if isinstance(src, str) and src.strip():
                # A step may cite more than one source ("M1, M3")
                for t in re.split(r"[,\s;]+", src.strip()):
                    if t:
                        tags.add(t.upper())

    for v in (design.get("composition") or {}).values():
        take(v)
    for step in (design.get("synthesis") or []):
        take(step)
    for r in (design.get("rationale") or []):
        take(r)
    return tags


def _finalize_bibliography(design, similar_papers, cited_pool, tag_to_pmid):
    """
    Builds the FINAL numbered bibliography once every pass has run.

    Numbering has to happen last. If we number papers as we retrieve them, then drop
    the uncited ones, the surviving numbers have holes in them (6, 8, 10...). So:
      1. Prior art (Most Similar Published Designs) is listed IN FULL — it is offered
         as a reading list, not as a citation list, and the section says so.
      2. References gets ONLY papers an actual citation resolves to.
      3. Everything is then numbered sequentially, and the tag map is rebuilt so every
         superscript still resolves.

    Returns (bib, source_map).
    """
    cited_tags = _collect_cited_tags(design)
    cited_pmids = {tag_to_pmid[t] for t in cited_tags if t in tag_to_pmid}

    bib, seen = [], {}

    # 1. Prior art, in full
    for p in similar_papers:
        if p["pmid"] not in seen:
            n = len(bib) + 1
            seen[p["pmid"]] = n
            bib.append({**p, "n": n, "section": "similar", "citation": _format_vancouver(p)})

    # 2. References: cited works only
    for p in cited_pool:
        if p["pmid"] in cited_pmids and p["pmid"] not in seen:
            n = len(bib) + 1
            seen[p["pmid"]] = n
            bib.append({**p, "n": n, "section": "reference", "citation": _format_vancouver(p)})

    # 3. Rebuild the tag -> number map against the final numbering
    source_map = {}
    by_pmid = {p["pmid"]: p for p in (list(similar_papers) + list(cited_pool))}
    for tag, pmid in tag_to_pmid.items():
        if pmid in seen and pmid in by_pmid:
            p = by_pmid[pmid]
            source_map[tag] = {
                "n": seen[pmid], "pmid": pmid, "title": p["title"], "url": p["url"],
                "journal": p.get("journal", ""), "journal_abbrev": p.get("journal_abbrev", ""),
                "year": p.get("year", ""), "volume": p.get("volume", ""),
                "issue": p.get("issue", ""), "pages": p.get("pages", ""),
                "doi": p.get("doi", ""), "author_list": p.get("author_list", []),
                "authors": p.get("authors", ""), "citation": _format_vancouver(p),
            }
    return bib, source_map


def _nm(value):
    """Pulls the first number out of a thickness string like '~40 nm' or '5-8 nm'."""
    if not value:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(m.group(1)) if m else None


def _check_dimensions(design):
    """
    Reconciles the layer stack against the stated hydrodynamic diameter.

    The stack is listed innermost-outward: entry 1 is the core (a DIAMETER), and every
    subsequent entry is a shell (a RADIAL thickness, applied on both sides). So:

        total diameter = core + 2 x (sum of shell thicknesses)

    The model gets this wrong in a way that is easy to miss and trivial to check: in
    one run a 15 nm core with 40 + 5 + 5 + 8 nm shells was reported as ~105 nm, when
    the arithmetic gives 131 nm — outside the requested 80-120 nm band. A reviewer
    catches this in seconds; a junior user does not. We do not silently "fix" the
    number (that would just move the lie), we surface the discrepancy.

    Returns None if it cannot be checked, else a dict describing the reconciliation.
    """
    stack = design.get("layer_stack") or []
    if len(stack) < 2:
        return None

    core = _nm(stack[0].get("thickness") if isinstance(stack[0], dict) else None)
    shells = [_nm(s.get("thickness")) for s in stack[1:] if isinstance(s, dict)]
    if core is None or not shells or any(s is None for s in shells):
        return None

    computed = core + 2 * sum(shells)
    stated = _nm(_claim_text((design.get("composition") or {}).get("diameter")))
    if stated is None:
        return None

    # Tolerance. The figures carry "~", so some rounding slack is legitimate: each
    # layer is written to the nearest nm or so, and a shell's error doubles in the
    # diameter. But the slack must stay smaller than a real mistake.
    #
    # Calibrated against an observed miss: a design listed a ~35 nm silica shell
    # while its own protocol tracked the particle to ~80 nm after shell growth,
    # which implies ~30 nm. That 5 nm shell error showed up as 10 nm of diameter
    # (116 computed vs 106 stated) and slipped through a 12% band. 6% catches it
    # while still absorbing honest rounding — a correctly specified design
    # reconciles to within 1-2 nm, nowhere near this threshold.
    #
    # The absolute floor keeps very small carriers (dendrimers at 5-15 nm, micelles
    # at 20-50 nm) from being flagged over sub-nanometre rounding, where a
    # percentage band would be unreasonably tight.
    delta = abs(computed - stated)
    tolerance = max(3.0, 0.06 * stated)
    ok = delta <= tolerance

    shell_str = " + ".join(f"{s:g}" for s in shells)
    return {
        "ok": ok,
        "computed": computed,
        "stated": stated,
        "delta": delta,
        "tolerance": tolerance,
        "expression": f"{core:g} nm core + 2 × ({shell_str}) nm shells = {computed:g} nm",
    }


def _retrieval_notice(status):
    """
    Returns the reader-facing notice for a literature-retrieval outcome, or None
    when retrieval succeeded and nothing needs saying.

    This exists as a separate pure function so the wording is testable without
    building a PDF and parsing it back. The distinction it draws is the point:
    an ungrounded report means nothing until the reader knows whether no
    literature exists or the search simply never ran.

    Returns {"severity": "warn"|"note", "text": str} or None.
    """
    status = status or {}
    state = status.get("state")

    if state == "failed":
        detail = status.get("detail") or "cause unknown"
        return {
            "severity": "warn",
            "text": (
                f"<b>Literature retrieval failed.</b> One or more PubMed queries did not "
                f"complete ({detail}). This report is therefore ungrounded by circumstance, "
                f"not because no supporting literature exists — absence of citations below "
                f"carries no evidential weight. Re-run before drawing any conclusion from "
                f"the provenance tags."
            ),
        }

    if state == "empty":
        return {
            "severity": "note",
            "text": (
                "<b>No matching literature found.</b> The PubMed queries completed but "
                "returned no records for this disease, payload and fabrication combination. "
                "Every claim below is therefore extrapolated. This may indicate genuinely "
                "unexplored design space, or simply that the search terms were too narrow."
            ),
        }

    return None


def _claim_text(claim):
    """Pulls the plain text out of a {text, provenance, source} claim object."""
    if isinstance(claim, dict):
        return claim.get("text", "")
    return str(claim or "")


# If the model fails to emit usable search terms, fall back to the canonical
# fabrication methods for its topology. Better a generic methods paper than none:
# with no method sources at all, every synthesis step is forced to be extrapolated.
_ARCH_METHOD_TERMS = {
    "mesoporous_core_shell": ["mesoporous silica nanoparticle synthesis",
                              "mesoporous silica drug loading", "stimuli-responsive pore gating"],
    "vesicle":               ["liposome thin film hydration", "liposome extrusion drug loading",
                              "PEGylated liposome preparation"],
    "lipid_nanoparticle":    ["lipid nanoparticle microfluidic mixing",
                              "ionizable lipid nanoparticle formulation", "mRNA lipid nanoparticle"],
    "micelle":               ["polymeric micelle preparation", "block copolymer micelle drug loading"],
    "polymer_matrix":        ["PLGA nanoparticle emulsion solvent evaporation",
                              "PLGA nanoparticle drug encapsulation", "nanoprecipitation polymer nanoparticle"],
    "inorganic_core_ligand": ["gold nanoparticle synthesis citrate reduction",
                              "gold nanoparticle thiol conjugation", "nanoparticle surface functionalization"],
    "dendrimer":             ["PAMAM dendrimer synthesis", "dendrimer drug conjugation"],
}


def _fallback_method_terms(design, params):
    """Architecture-derived method queries, plus a payload-loading query."""
    arch = design.get("architecture", "mesoporous_core_shell")
    terms = list(_ARCH_METHOD_TERMS.get(arch, _ARCH_METHOD_TERMS["mesoporous_core_shell"]))
    drug = (params.get("drug") or "").strip()
    if drug:
        terms.append(f"{drug} nanoparticle encapsulation")
    return terms


def _route_line(route):
    """
    The administration route is often undetermined at the start of a particle design,
    so it is optional. When it is NOT set we must not quietly fall back to intravenous —
    that would silently constrain the design on the user's behalf and present an
    assumption as a decision. Instead we tell the model the route is open, and ask it to
    choose one and justify it, so the choice is visible and can be challenged.
    """
    route = (route or "").strip()
    if route:
        return f"- Administration route: {route}"
    return (
        "- Administration route: NOT SPECIFIED by the researcher. Do NOT default to "
        "intravenous. Choose the route best suited to this target and payload, and state "
        "and justify that choice explicitly in the rationale."
    )


def call_claude_for_json(client, prompt, max_tokens=3500, max_attempts=3):
    """
    Calls Claude with the given prompt and parses the response as JSON.
    If parsing fails (truncation, stray formatting, malformed escaping, etc.),
    automatically retries up to max_attempts times, each time appending a
    stronger correction instruction that includes the actual parse error and
    a snippet of what was returned, so the model can see exactly what went
    wrong rather than blindly repeating the same mistake.

    Raises json.JSONDecodeError on the final attempt if all retries fail,
    so the route's existing error handling still produces a clear message —
    this only adds resilience, it doesn't hide a genuinely broken response.

    Returns (parsed_json, attempts_used).
    """
    last_error = None
    last_raw = ""
    current_prompt = prompt

    for attempt in range(1, max_attempts + 1):
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": current_prompt}]
        )
        raw = message.content[0].text
        raw = raw.replace("```json", "").replace("```", "").strip()

        try:
            return json.loads(raw), attempt
        except json.JSONDecodeError as e:
            last_error = e
            last_raw = raw
            if attempt < max_attempts:
                current_prompt = f"""{prompt}

IMPORTANT: Your previous response was not valid JSON and failed to parse with this error:
{e}

Here is what you returned (truncated to first 500 chars):
{raw[:500]}

Return ONLY a single valid JSON object this time. No markdown fences, no commentary before or after, no trailing commas, and make sure every string is properly closed and every bracket is matched. If your previous response was cut off, keep this one more concise so it completes fully within the token limit."""

    raise last_error if last_error else json.JSONDecodeError("Unknown JSON error", last_raw, 0)


# ──────────────────────────────────────────────────────────────────────────
# Design generation — literature-grounded
# ──────────────────────────────────────────────────────────────────────────

@app.route("/generate", methods=["POST"])
def generate():
    body = request.get_json()
    api_key = body.get("apiKey")
    params  = body.get("params", {})

    disease = params.get("disease", "cancer")
    drug = params.get("drug", "Olaparib")

    # ── Two DISTINCT literature searches, serving two different purposes ──
    #
    #   grounding  = (disease) AND (drug)
    #       Therapy literature. This is what the model reads and cites when making
    #       factual claims. Ends up in "References".
    #
    #   similarity = (disease) AND (drug) AND (nanoparticle/delivery terms)
    #       Formulation literature — the closest PUBLISHED NANOPARTICLE DESIGNS, so
    #       the researcher can check prior art before committing to a formulation.
    #       Ends up in "Most Similar Published Designs".
    #
    # These were previously the SAME query, so the two sections showed identical
    # papers and the "similar designs" list was really just clinical trial results.
    # Retrieval status is tracked, not swallowed. An all-extrapolated report can mean
    # two completely different things — "no literature supports this design" or "the
    # literature search never completed" — and a reader cannot tell them apart from
    # the output. Conflating them is precisely the kind of silent ambiguity this tool
    # exists to remove, so failures are recorded and surfaced in the report.
    retrieval_errors = []

    lit_papers, lit_query = [], ""
    try:
        lit_papers, lit_query = search_pubmed(disease, drug, retmax=5, fetch_abstracts=True)
    except Exception as e:
        lit_papers = []
        retrieval_errors.append(f"grounding search ({type(e).__name__})")
        app.logger.warning("PubMed grounding search failed: %s", e)

    similar_papers, similar_query = [], ""
    try:
        similar_papers, similar_query = search_similar_designs(disease, drug, retmax=5)
    except Exception as e:
        similar_papers = []
        retrieval_errors.append(f"prior-art search ({type(e).__name__})")
        app.logger.warning("PubMed similar-designs search failed: %s", e)

    # ── Source tags for the prompt (numbering deferred) ──
    # Citation NUMBERS cannot be assigned yet. References must contain only works the
    # model actually CITES, and we do not know what it cited until both passes have
    # run. Numbering here and filtering later would leave holes in the sequence
    # (6, 8, 11...). So for now we only build the tag -> paper map; _finalize_bibliography()
    # assigns the numbers at the end.
    tag_to_pmid = {}    # "S1" -> pmid, later also "M1" -> pmid

    source_block = ""
    for i, p in enumerate(lit_papers, start=1):
        tag = f"S{i}"
        tag_to_pmid[tag] = p["pmid"]
        abstract_text = p["abstract"] or "(abstract unavailable)"
        source_block += f"\n[{tag}] PMID {p['pmid']} — {p['title']} ({p['year']})\n{abstract_text}\n"

    grounding_instructions = f"""
The following PubMed sources were retrieved for this disease/drug pairing. Use them as grounding where genuinely relevant:
{source_block if source_block else "(No closely matching PubMed sources were found for this disease/drug pairing — all claims should be marked as extrapolated.)"}
""" if lit_papers else "\nNo PubMed sources were retrieved for this query — mark all claims as extrapolated.\n"

    # ── Pinned architecture ──
    # If the user (or a "Load example" click) pinned a topology, the model must be
    # TOLD to use it — not merely overridden afterwards. Forcing the enum after the
    # fact would draw a polymer matrix while the composition text still described
    # mesopores and gates, which is worse than either alone.
    requested_arch = (params.get("architecture") or "").strip()
    if requested_arch in ARCHITECTURES:
        arch_directive = f"""
REQUIRED ARCHITECTURE — NON-NEGOTIABLE:
You MUST set "architecture" to exactly "{requested_arch}" ({ARCH_LABELS[requested_arch]}).
Every other field must be CONSISTENT with that topology: the composition, the layer
stack, the synthesis protocol and the rationale must all describe this carrier and no
other. Do not describe mesopores or gates unless the topology is mesoporous_core_shell.
Do not describe an aqueous lumen unless the topology is vesicle. If the requested
payload is an unusual fit for this topology, still use the topology and address the
trade-off honestly in the rationale.
"""
    else:
        requested_arch = ""
        arch_directive = ""

    prompt = f"""You are an expert nanomedicine researcher. Design an optimal nanoparticle complex for the following parameters:
{arch_directive}

- Disease target: {disease}
- Target receptor/biomarker: {params.get('receptor','EpCAM')}
- Therapeutic payload: {drug}
- Imaging modality: {params.get('imaging','T2 MRI')}
- Release trigger: {params.get('trigger','pH-responsive')}
- Target size: {params.get('size','80-120 nm')}
{_route_line(params.get('route'))}
{f"- Literature context: {params.get('lit')}" if params.get('lit') else ''}
{grounding_instructions}

CRITICAL — provenance tagging:
For EVERY field in "composition", the "rationale" sentences, and EVERY synthesis step, you must tag whether the claim is:
- "grounded": directly supported by one of the numbered sources above (cite it by tag, e.g. "S1")
- "extrapolated": your own reasoning/general nanomedicine knowledge, not directly stated in the provided sources

Be honest and conservative — only mark something "grounded" if a source above genuinely supports that specific claim (e.g. the material, the mechanism, the specific dosing approach). General nanomedicine principles not stated in these specific sources should be "extrapolated", even if true.

FORMATTING RULES (strict):
- SURFACE CHEMISTRY MUST MATCH THE SHELL MATERIAL. Anchors are not interchangeable:
    * silica (MSN) -> SILANE chemistry (APTES, silane-PEG-maleimide). DSPE-PEG is a LIPID anchor and CANNOT graft to silica — it has no silane group. Never write "DSPE-PEG ... via silane coupling"; that is a chemical impossibility.
    * lipid surfaces (LNP, liposome, micelle) -> DSPE-PEG or other lipid-anchored PEG.
    * gold / inorganic -> THIOL (e.g. thiol-PEG, cysteine, Fab' sulfhydryl).
    * PLGA / polymer -> NHS-ester or carbodiimide coupling to surface carboxyls, or PEG-PLGA block copolymer.
- THE TARGETING LIGAND MUST SIT AT THE DISTAL END OF THE PEG CHAIN, not on the particle surface underneath the PEG brush. If PEG is grafted first and the antibody is then coupled to the particle surface, the ligand is buried beneath its own stealth layer and cannot bind its receptor. Use HETEROBIFUNCTIONAL PEG (e.g. silane-PEG-maleimide) and conjugate a thiolated antibody or Fab' to the DISTAL terminus.
- Do NOT describe EDC/NHS coupling to antibody lysines or carboxyls as "site-specific" — it is RANDOM conjugation and can block the antigen-binding site. Site-specific means maleimide-thiol to a reduced hinge Fab', enzymatic ligation, or a click handle. Use the term accurately or not at all.
- LAYER STACK ARITHMETIC MUST RECONCILE WITH THE STATED DIAMETER. Entry 1 of "layer_stack" is the CORE (a diameter); every later entry is a SHELL (a radial thickness, present on BOTH sides). So: total diameter = core + 2 x (sum of shell thicknesses). A 15 nm core with 40 + 5 + 5 + 8 nm shells is 131 nm, NOT 105 nm.
    Before you answer, do this arithmetic explicitly and make all THREE of these agree:
      (a) core + 2 x (sum of shells) = "diameter".text
      (b) the requested size range contains that number
      (c) any particle size you quote in a SYNTHESIS STEP matches the running total at that stage
    (c) is the one most often missed. If a synthesis step says the particle reaches ~80 nm after shell growth, then a 20 nm core forces a 30 nm radial shell — NOT 35 nm. Reconcile the stack against your own protocol's numbers, then state the final diameter. Do not leave a layer thickness that contradicts a figure you quote elsewhere in the report.
- "synthesis_search_terms": 3-5 SHORT PubMed queries (2-6 words each) naming the fabrication METHODS this design requires — one per major synthesis step. Name the MATERIAL and the TECHNIQUE. Be SPECIFIC: name the actual technique, not the field. These retrieve protocol papers, so a vague query returns broad reviews that cannot ground a step.
    GOOD: ["mesoporous silica sol-gel TEOS CTAB", "iron oxide thermal decomposition oleate", "ZIF-8 pore gating drug release", "silane-PEG-maleimide antibody conjugation"]
    BAD (too broad, returns reviews): ["iron oxide nanoparticle applications", "nanoparticle synthesis", "biomedical nanotechnology"]
    BAD (disease-led, returns clinical papers): ["breast cancer nanoparticle treatment", "olaparib therapy"]
- "architecture" must be exactly one of the seven enum values. It describes the particle's TOPOLOGY (how its compartments are arranged), which is what gets drawn as a cross-section. Choose it from the payload and carrier chemistry:
    * lipid_nanoparticle    — nucleic acid payloads (mRNA, siRNA, saRNA, pDNA, ASO). Ionizable lipid complexed with the nucleic acid throughout a DENSE interior. NOTE: an LNP has NO aqueous lumen — do not confuse it with a liposome.
    * vesicle               — liposome, polymersome, exosome, or cell-membrane-coated carrier. A true BILAYER enclosing a HOLLOW AQUEOUS LUMEN. Use for hydrophilic small molecules encapsulated in the lumen (a hydrophobic drug would instead partition into the bilayer).
    * mesoporous_core_shell — mesoporous silica (MSN), incl. magnetic MSN. Solid core, porous shell, drug held INSIDE the mesopores, gates capping the pore mouths.
    * micelle               — amphiphilic block copolymer. A single MONOLAYER corona (not a bilayer) around a hydrophobic core in which a poorly-soluble drug is dissolved.
    * polymer_matrix        — PLGA, albumin, nanogel, solid lipid nanoparticle. Drug dispersed HOMOGENEOUSLY through a degradable matrix; no discrete compartments.
    * inorganic_core_ligand — gold nanoparticle/nanorod, quantum dot, upconversion NP. Dense inorganic core with NO interior payload; drug is conjugated to the SURFACE via a (often cleavable) linker.
    * dendrimer             — PAMAM or other hyperbranched polymer. Generational branching, payload held in interior voids and on terminal groups.
- "imaging_core": true only if a distinct inorganic imaging core (SPION, gold, quantum dot) is inset at the CENTRE of the carrier — e.g. a SPION core inside an LNP or liposome. Set false when the carrier itself provides contrast (e.g. inorganic_core_ligand) or when there is no imaging component.
- "diameter".text must be ONLY a short measurement, e.g. "~105 nm" or "95-110 nm". It is rendered as a compact label under a diagram, so a sentence will break the layout. Put any reasoning about size (EPR effect, renal clearance, etc.) in "rationale" instead, NOT in the diameter field.
- "layer_stack" must list each concentric layer from the innermost core outward. "thickness" is a short measurement only ("~30 nm"); "material" is a short noun phrase only ("mesoporous silica shell"). No sentences in either field.

Respond ONLY with a valid JSON object. No markdown, no backticks, no preamble. Structure:
{{
  "composition": {{
    "core": {{"text": "material and purpose", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    "shell": {{"text": "material and purpose", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    "payload": {{"text": "drug and loading strategy", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    "surface": {{"text": "PEG, antibody, targeting ligand details", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    "gate": {{"text": "stimuli-responsive gate mechanism", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    "diameter": {{"text": "SHORT total diameter ONLY, e.g. '~105 nm' or '95-110 nm'. NOT a sentence. NO explanation, NO layer breakdown, NO reasoning — just the number and unit.", "provenance": "grounded|extrapolated", "source": "S1 or null"}}
  }},
  "rationale": [
    {{"text": "one sentence of rationale", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    {{"text": "one sentence of rationale", "provenance": "grounded|extrapolated", "source": "S1 or null"}}
  ],
  "synthesis": [
    {{"step": "Step name", "detail": "One sentence of key procedure", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    {{"step": "Step name", "detail": "One sentence of key procedure", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    {{"step": "Step name", "detail": "One sentence of key procedure", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    {{"step": "Step name", "detail": "One sentence of key procedure", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    {{"step": "Step name", "detail": "One sentence of key procedure", "provenance": "grounded|extrapolated", "source": "S1 or null"}},
    {{"step": "Step name", "detail": "One sentence of key procedure", "provenance": "grounded|extrapolated", "source": "S1 or null"}}
  ],
  "architecture": "EXACTLY ONE of: mesoporous_core_shell | vesicle | lipid_nanoparticle | micelle | polymer_matrix | inorganic_core_ligand | dendrimer",
  "imaging_core": true,
  "synthesis_search_terms": ["3-5 short PubMed queries naming the FABRICATION METHODS this specific design needs"],
  "layer_stack": [
    {{"thickness": "~30 nm", "material": "Fe3O4 core"}},
    {{"thickness": "~40 nm", "material": "mesoporous silica shell"}},
    {{"thickness": "~5 nm", "material": "ZIF-8 gate layer"}},
    {{"thickness": "~10 nm", "material": "PEG/antibody corona"}}
  ],
  "svgColors": {{
    "core": "#hex", "shell": "#hex", "gate": "#hex", "surface": "#hex", "payload": "#hex"
  }}
}}"""

    try:
        client = get_client(api_key)
        design, attempts_used = call_claude_for_json(client, prompt, max_tokens=3500, max_attempts=3)

        # Never trust the enum straight from the model — coerce it onto a known
        # topology so the diagram can dispatch safely. Drawing an LNP as a
        # mesoporous particle is exactly the failure this guards against.
        design["architecture"] = _normalize_architecture(design.get("architecture"))
        design["imaging_core"] = bool(design.get("imaging_core", False))

        # Backstop: if a topology was pinned, it is authoritative. The prompt already
        # instructed the model, so this should be a no-op — but a model that ignored
        # the directive must not silently produce a different topology than the one
        # the user asked for.
        if requested_arch and design["architecture"] != requested_arch:
            app.logger.warning(
                "Model returned '%s' despite pinned architecture '%s'; overriding.",
                design["architecture"], requested_arch
            )
            design["architecture"] = requested_arch

        # ── PASS 2: re-ground the synthesis protocol against METHOD papers ──
        #
        # Pass 1 could only ever mark synthesis steps as extrapolated: the sources in
        # its prompt were therapy papers (disease AND drug), which say nothing about
        # how to make a particle. Now that the model has committed to an architecture
        # and materials, we can retrieve the protocol literature for those specific
        # fabrication steps and ask it to write the synthesis again — this time with
        # real methods text in front of it.
        #
        # NOTE: the goal is to supply better SOURCES, never to pressure the tagging.
        # Telling a model to "mark more claims as grounded" just teaches it to lie,
        # which would destroy the credibility this provenance system exists to create.
        method_papers, method_queries = [], []
        try:
            terms = design.get("synthesis_search_terms") or []
            if isinstance(terms, str):
                terms = [terms]
            # Fall back to architecture-derived terms if the model gave us nothing useful
            if not terms:
                terms = _fallback_method_terms(design, params)
            method_papers, method_queries = search_synthesis_methods(terms)
        except Exception as e:
            method_papers = []
            retrieval_errors.append(f"synthesis-methods search ({type(e).__name__})")
            app.logger.warning("PubMed methods search failed: %s", e)

        if method_papers:
            # Method sources get their own tag space (M1, M2, ...) so the model cannot
            # confuse a therapy paper with a protocol paper when citing a step.
            method_block = ""
            for i, p in enumerate(method_papers, start=1):
                tag = f"M{i}"
                abstract_text = p.get("abstract") or "(abstract unavailable)"
                method_block += f"\n[{tag}] PMID {p['pmid']} — {p['title']} ({p.get('year','')})\n{abstract_text}\n"

            synth_prompt = f"""You are an expert nanomedicine researcher writing the SYNTHESIS PROTOCOL for a nanoparticle design that has already been decided. Do not change the design — only write how to make it.

THE DESIGN (fixed):
- Topology: {ARCH_LABELS.get(design['architecture'], design['architecture'])}
- Core: {_claim_text(design.get('composition', {}).get('core'))}
- Shell: {_claim_text(design.get('composition', {}).get('shell'))}
- Payload: {_claim_text(design.get('composition', {}).get('payload'))}
- Surface: {_claim_text(design.get('composition', {}).get('surface'))}
- Release gate: {_claim_text(design.get('composition', {}).get('gate'))}
- Diameter: {_claim_text(design.get('composition', {}).get('diameter'))}

METHOD SOURCES retrieved from PubMed for these fabrication steps. These are real protocol papers — use them as the basis for the steps wherever they genuinely apply:
{method_block}

Write the synthesis as 5-7 ordered steps. Return ONLY JSON, no other text:
{{
  "synthesis": [
    {{"step": "Short step title", "detail": "Specific protocol detail: reagents, temperatures, times, ratios.", "provenance": "grounded", "source": "M1"}}
  ]
}}

CHEMISTRY RULES (a reviewer will catch these instantly):
- Anchor chemistry must match the surface: silica -> SILANE (APTES, silane-PEG); lipid -> DSPE-PEG; gold -> THIOL; polymer -> NHS/carbodiimide. DSPE-PEG cannot graft to silica.
- Conjugate the targeting ligand to the DISTAL end of the PEG chain via heterobifunctional PEG (e.g. silane-PEG-maleimide + thiolated Fab'). Never graft PEG first and then couple the antibody to the particle surface — that buries the ligand under its own stealth layer.
- Do not call EDC/NHS coupling "site-specific"; it is random.
- Order the steps so that each is chemically possible given the previous one.

PROVENANCE RULES — these matter more than any other instruction:
- "grounded" ONLY if the METHOD SOURCE you cite genuinely describes that procedure. Cite it by its tag (e.g. "M2").
- "extrapolated" if you are drawing on general nanomedicine knowledge rather than a source above. Set "source" to null.
- DO NOT mark a step as grounded because it seems plausible, because a similar method exists, or to make the report look better-sourced. A false grounding tag is far worse than an honest extrapolated one — a researcher will follow the citation and find it does not support the step.
- It is entirely acceptable, and expected, for some steps to remain extrapolated. Adapting a published method to new materials is extrapolation; say so.
- Where a source describes the method for a DIFFERENT material and you are adapting it, that is "extrapolated", not "grounded".
"""
            try:
                synth_result, _ = call_claude_for_json(client, synth_prompt, max_tokens=2000, max_attempts=2)
                new_steps = synth_result.get("synthesis")
                if isinstance(new_steps, list) and new_steps:
                    design["synthesis"] = new_steps
            except Exception:
                # Keep pass 1's synthesis rather than failing the whole generation
                app.logger.warning("Synthesis re-grounding pass failed; keeping pass-1 protocol.")

        # ── FINALIZE the bibliography, now that every citation is known ──
        # Only here can we tell which papers were actually cited. References is a
        # WORKS CITED list; a retrieved-but-uncited paper does not belong in it, and
        # including it inflates the apparent evidence base. Prior art is different —
        # it is offered as a reading list and is shown in full.
        for i, p in enumerate(method_papers, start=1):
            tag_to_pmid[f"M{i}"] = p["pmid"]

        cited_pool = list(lit_papers) + list(method_papers)
        bib, source_map = _finalize_bibliography(design, similar_papers, cited_pool, tag_to_pmid)

        # Dimensional reconciliation — cheap, mechanical, and the kind of arithmetic
        # slip a reviewer catches instantly but a junior user never would.
        design["dimension_check"] = _check_dimensions(design)

        # Retrieval status, so the report can distinguish "nothing was found" from
        # "the search did not run". Both produce an all-extrapolated design, but they
        # license completely different conclusions.
        total_found = len(lit_papers) + len(similar_papers) + len(method_papers)
        if retrieval_errors:
            design["retrieval_status"] = {
                "state": "failed",
                "detail": "; ".join(sorted(set(retrieval_errors))),
                "found": total_found,
            }
        elif total_found == 0:
            design["retrieval_status"] = {"state": "empty", "detail": "", "found": 0}
        else:
            design["retrieval_status"] = {"state": "ok", "detail": "", "found": total_found}

        # Drop the scratch field — it is a retrieval instruction, not part of the design
        design.pop("synthesis_search_terms", None)

        response = {
            "ok": True,
            "design": design,
            "sources": source_map,
            "bibliography": {
                # Closest published nanoparticle designs (prior art)
                "similar": [b for b in bib if b["section"] == "similar"],
                # Sources the model actually cited that are not already listed above
                "references": [b for b in bib if b["section"] == "reference"],
                "similar_query": similar_query,
                "grounding_query": lit_query,
                "method_queries": method_queries,
            },
        }
        if attempts_used > 1:
            # Surface this so the frontend/console can optionally note a retry happened —
            # useful for diagnosing how often malformed JSON occurs in practice
            response["retries_used"] = attempts_used - 1
        return jsonify(response)

    except ValueError as e:
        # Missing/empty API key. That is the caller's problem, not a server fault:
        # returning 500 here would make every keyless visit look like an outage to
        # the host's monitoring and to anyone reading the logs.
        return jsonify({"ok": False, "error": str(e)}), 400
    except anthropic.AuthenticationError:
        # Do not echo the provider's message — it can quote the submitted key.
        return jsonify({
            "ok": False,
            "error": "Anthropic rejected that API key. Check it was copied in full "
                     "and is active at console.anthropic.com."
        }), 401
    except anthropic.RateLimitError:
        return jsonify({
            "ok": False,
            "error": "Anthropic rate limit reached for this key — wait a moment and retry."
        }), 429
    except json.JSONDecodeError as e:
        return jsonify({
            "ok": False,
            "error": f"Claude returned malformed JSON after multiple attempts: {e}. "
                     f"This usually means the response was cut off or unusually structured — try again."
        }), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────
# PDF export
# ──────────────────────────────────────────────────────────────────────────

GROUNDED_COLOR = colors.HexColor("#1565C0")       # darker blue — legible on white paper
EXTRAPOLATED_COLOR = colors.HexColor("#6A1FA8")   # darker purple — legible on white paper

def _claim_to_html(claim, sources=None):
    """
    Converts a {text, provenance, source} claim object into ReportLab's
    Paragraph-flavored HTML-subset markup.

    Citations render as superscript numerals — "...tumor imaging.^1" — matching
    ICMJE/Vancouver convention used by PubMed and most biomedical journals,
    rather than the inline [S1] tag used on screen. The internal tag ("S1") is
    mapped to its reference number via the sources map.

    Provenance stays color-coded (our motif): grounded = blue, Extrapolated =
    purple. Falls back gracefully if claim is a plain string.
    """
    if not isinstance(claim, dict):
        return str(claim or "")
    text = (claim.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    provenance = claim.get("provenance")
    source = claim.get("source")
    if provenance == "grounded":
        cite = ""
        if source:
            n = (sources or {}).get(source, {}).get("n")
            marker = str(n) if n else str(source)
            cite = f'<super><font color="#1565C0" size="7">{marker}</font></super>'
        return f'<font color="#1565C0">{text}</font>{cite}'
    else:
        return f'<font color="#6A1FA8">{text}</font>'


def _recolor_svg_for_print(svg_markup):
    """
    The diagram SVG is authored for the dark navy UI: label text is near-white
    and leader lines are faint white. Handed to a white PDF page verbatim, that
    renders as white-on-white — effectively invisible.

    This swaps only the *chrome* colors (label text, leader lines, size bar) for
    dark ink suitable for paper. The nanoparticle's own fills (core/shell/gate/
    corona) are AI-chosen and already saturated, so they're left untouched.
    The on-screen SVG is not modified — this transform applies to the PDF only.
    """
    replacements = [
        # Label text: near-white -> near-black ink
        ('fill="rgba(232,237,245,0.85)"', 'fill="#1A1A1A"'),
        # Size caption under the particle: faint white -> mid gray
        ('fill="rgba(232,237,245,.5)"',   'fill="#555555"'),
        ('fill="rgba(232,237,245,0.5)"',  'fill="#555555"'),
        # Leader lines: faint white -> visible gray
        ('stroke="rgba(255,255,255,0.2)"', 'stroke="#9A9A9A"'),
        ('stroke="rgba(255,255,255,0.25)"','stroke="#9A9A9A"'),
        ('stroke="rgba(255,255,255,.3)"',  'stroke="#777777"'),
        ('stroke="rgba(255,255,255,0.3)"', 'stroke="#777777"'),
        # Leader anchor dots on the particle edge
        ('fill="rgba(255,255,255,0.45)"',  'fill="#777777"'),
        # Mesopore channel interiors: read as "open channel" against the dark UI,
        # but would vanish on white paper — give them a light gray fill instead.
        ('fill="rgba(255,255,255,0.14)"',  'fill="#F0F0F0"'),
    ]
    out = svg_markup
    for old, new in replacements:
        out = out.replace(old, new)
    return out


# Canonical forms for biomedical terms whose capitalization carries meaning.
# Python's str.title() destroys all of these ("mRNA" -> "Mrna", "EpCAM" -> "Epcam",
# "pH" -> "Ph"), which reads as sloppy in a research document — so we never use it.
_CANONICAL_TERMS = {
    "mrna": "mRNA", "sirna": "siRNA", "mirna": "miRNA", "shrna": "shRNA",
    "sgrna": "sgRNA", "grna": "gRNA", "trna": "tRNA", "rrna": "rRNA",
    "saRNA".lower(): "saRNA", "asos": "ASOs", "aso": "ASO",
    "rna": "RNA", "dna": "DNA", "pdna": "pDNA", "cdna": "cDNA",
    "crispr": "CRISPR", "cas9": "Cas9",
    "epcam": "EpCAM", "her2": "HER2", "egfr": "EGFR", "vegf": "VEGF",
    "pd-l1": "PD-L1", "pd1": "PD-1", "psma": "PSMA", "cd44": "CD44",
    "folr1": "FOLR1", "trop2": "TROP2", "muc1": "MUC1",
    "peg": "PEG", "pegylated": "PEGylated", "lnp": "LNP", "lnps": "LNPs",
    "spion": "SPION", "spions": "SPIONs", "msn": "MSN", "zif-8": "ZIF-8",
    "dspe": "DSPE", "dopc": "DOPC", "dotap": "DOTAP",
    "ph": "pH", "ros": "ROS", "mri": "MRI", "pet": "PET", "ct": "CT",
    "tnbc": "TNBC", "gbm": "GBM", "pdac": "PDAC", "nsclc": "NSCLC",
    "hcc": "HCC", "aml": "AML", "cll": "CLL", "epr": "EPR",
    "parp": "PARP", "igg": "IgG", "iv": "IV",
}


def _smart_title(text):
    """
    Title-cases a phrase WITHOUT destroying biomedical capitalization.

    str.title() is unusable here: it turns "mRNA" into "Mrna", "EpCAM" into
    "Epcam", "pH-responsive" into "Ph-Responsive". This instead:
      - maps known terms to their canonical form (mrna/MRNA -> mRNA)
      - leaves any token that already carries internal capitals alone (EpCAM,
        PEG-Lipid, Fe3O4) — the user's own casing is assumed intentional
      - capitalizes only plain lowercase words
    Hyphenated compounds are handled part by part ("pH-responsive" -> "pH-Responsive"
    is avoided; it stays "pH-responsive" because "responsive" follows a canonical term).
    """
    if not text:
        return ""

    def cap_part(part, is_first_in_token):
        if not part:
            return part
        low = part.lower()
        if low in _CANONICAL_TERMS:
            return _CANONICAL_TERMS[low]
        # Already has internal capitals (EpCAM, Fe3O4, McAb) -> the user meant it
        if any(ch.isupper() for ch in part[1:]):
            return part
        # All-caps short token is probably an acronym the user typed deliberately
        if part.isupper() and len(part) <= 5:
            return part
        return part[0].upper() + part[1:] if is_first_in_token else part

    out_tokens = []
    for token in str(text).split():
        if "-" in token:
            sub = token.split("-")
            # Capitalize the first sub-part; keep later parts lowercase unless canonical,
            # so "triple-negative" -> "Triple-negative" and "pH-responsive" stays correct.
            rebuilt = [cap_part(sub[0], True)] + [cap_part(s, False) for s in sub[1:]]
            out_tokens.append("-".join(rebuilt))
        else:
            out_tokens.append(cap_part(token, True))
    return " ".join(out_tokens)


def _report_title(disease, drug):
    """
    Builds a grammatical title regardless of which fields the user filled in.

    The old template hardcoded "for {disease} with {drug} Payload", which produced
    "...Design for with Mrna Payload" when the disease field was left blank. The
    ability to leave a field open is deliberate (it broadens the design space), so
    the title has to degrade gracefully rather than emit broken grammar.
    """
    d = _smart_title((disease or "").strip())
    p = _smart_title((drug or "").strip())
    if d and p:
        return f"AI-Generated Nanoparticle Design for {d} with {p} Payload"
    if d:
        return f"AI-Generated Nanoparticle Design for {d}"
    if p:
        return f"AI-Generated Nanoparticle Design for {p} Delivery"
    return "AI-Generated Nanoparticle Design"


def _format_vancouver(paper):
    """
    Formats a PubMed record in Vancouver (ICMJE) style — the convention PubMed
    itself displays and that biomedical journals require:

        Smith J, Lee K, Chen W, et al. Title of the article. Nat Commun.
        2024;15(1):2341. doi:10.1038/s41467-024-12345. PMID: 38472910.

    Fields are omitted gracefully when PubMed doesn't supply them, so a sparse
    record still yields a well-formed citation rather than stray punctuation.
    """
    authors = paper.get("author_list") or []
    if not authors:
        author_str = ""
    elif len(authors) <= 6:
        author_str = ", ".join(authors)
    else:
        author_str = ", ".join(authors[:6]) + ", et al"

    title = (paper.get("title") or "").rstrip(". ")
    journal = (paper.get("journal_abbrev") or paper.get("journal") or "").rstrip(". ")
    year = paper.get("year") or ""
    vol = paper.get("volume") or ""
    issue = paper.get("issue") or ""
    pages = paper.get("pages") or ""
    doi = paper.get("doi") or ""
    pmid = paper.get("pmid") or ""

    # Build "2024;15(1):2341" from whichever parts exist
    locator = year
    if vol:
        locator += f";{vol}"
        if issue:
            locator += f"({issue})"
        if pages:
            locator += f":{pages}"
    elif pages:
        locator += f":{pages}"

    parts = []
    if author_str:
        parts.append(f"{author_str}.")
    if title:
        parts.append(f"{title}.")
    if journal:
        parts.append(f"<i>{journal}</i>.")
    if locator:
        parts.append(f"{locator}.")
    if doi:
        parts.append(f"doi:{doi}.")
    if pmid:
        parts.append(f"PMID: {pmid}.")
    return " ".join(parts)


def _short_diameter(diameter_text):
    """
    The diagram caption and the report callout need a SHORT diameter label.

    The prompt now demands one, but models drift — we have seen it return a whole
    paragraph here ("Final hydrodynamic diameter estimated at 95-110 nm: Fe3O4
    core (~30 nm) + ... to leverage EPR effect and avoid rapid renal clearance."),
    which overflowed the SVG caption and turned the report heading into a wall of
    text. So never trust this field's length: pull the first measurement out of it
    and use only that for display. The full original text still shows in the
    Composition table's Diameter row, so nothing is lost.
    """
    if not diameter_text:
        return ""
    text = str(diameter_text).strip()
    # Already short enough to use as-is
    if len(text) <= 24:
        return text
    # First measurement-like token: "~105 nm", "95-110 nm", "95–110 nm", "100nm"
    m = re.search(
        r"[~≈<>]?\s*\d+(?:\.\d+)?\s*(?:[–—-]\s*\d+(?:\.\d+)?)?\s*(?:nm|µm|um)",
        text, re.IGNORECASE
    )
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()
    # No measurement found — truncate rather than blow out the layout
    return text[:24].rstrip() + "…"


def _resolve_layer_stack(design, diameter_text):
    """
    Returns a list of (thickness, material) tuples for the report's layer table.

    Prefers the structured "layer_stack" the model is now asked to emit. Falls
    back to parsing a trailing "(a + b + c)" breakdown out of the diameter string
    for older/nonconforming responses. Returns [] if neither is usable, in which
    case the report simply omits the table.
    """
    stack = design.get("layer_stack")
    if isinstance(stack, list) and stack:
        rows = []
        for item in stack:
            if isinstance(item, dict):
                thickness = str(item.get("thickness", "") or "").strip()
                material = str(item.get("material", "") or "").strip()
                if material:
                    rows.append((thickness or "—", material))
        if rows:
            return rows

    # Legacy fallback: "~105 nm total (15 nm core + ~40 nm shell + ~5 nm gate)"
    if not diameter_text:
        return []
    m = re.match(r"^.*?\((.*)\)\s*$", str(diameter_text).strip(), re.DOTALL)
    if not m:
        return []
    layers = [seg.strip() for seg in m.group(1).split("+") if seg.strip()]
    if len(layers) < 2:
        return []
    rows = []
    for layer in layers:
        lm = re.match(
            r"^([~≈<>]?\s*[\d.]+\s*(?:[–—-]\s*[\d.]+)?\s*(?:nm|µm|um))\s+(.*)$",
            layer, re.IGNORECASE
        )
        if lm:
            rows.append((lm.group(1).strip(), lm.group(2).strip()))
        else:
            rows.append(("—", layer))
    return rows


def _disclaimer_style():
    """
    Style for the cover-page footer. Defined at module scope because the page
    callback that paints it runs outside _build_pdf_report's local scope.
    """
    return ParagraphStyle(
        "NSDisclaimer", parent=getSampleStyleSheet()["Normal"],
        fontName="Times-Roman", fontSize=7.5, leading=10,
        textColor=colors.HexColor("#888888"), alignment=TA_JUSTIFY,
    )


def _build_pdf_report(design, sources, params, svg_markup, bibliography=None):
    """
    Builds a complete PDF report for a generated nanoparticle design using
    reportlab's Platypus framework. Returns a BytesIO buffer ready to send.

    Uses TWO page templates rather than one:
      Cover — a shorter frame that reserves the bottom of the page, so the
              disclaimer can be painted as a true fixed footer (pinned to the
              page bottom) instead of merely flowing after the diagram. The
              architecture section is scaled up to fill the space this leaves.
      Body  — a full-height frame for everything from Composition onward.
    A plain SimpleDocTemplate cannot do this: it has one frame and one set of
    margins for every page, so a "footer" would just float wherever the content
    happened to end.
    """
    buf = io.BytesIO()

    PAGE_W, PAGE_H = letter
    L_MARGIN = R_MARGIN = 0.7 * inch
    TOP_MARGIN = 0.65 * inch
    BODY_BOTTOM = 0.65 * inch
    COVER_BOTTOM = 1.45 * inch      # reserved strip for the fixed cover footer
    FRAME_W = PAGE_W - L_MARGIN - R_MARGIN

    doc = BaseDocTemplate(
        buf, pagesize=letter,
        topMargin=TOP_MARGIN, bottomMargin=BODY_BOTTOM,
        leftMargin=L_MARGIN, rightMargin=R_MARGIN,
        title="NanoSynth AI — Design Report",
        author="NanoSynth AI",
    )

    cover_frame = Frame(
        L_MARGIN, COVER_BOTTOM, FRAME_W, PAGE_H - TOP_MARGIN - COVER_BOTTOM,
        id="cover", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    body_frame = Frame(
        L_MARGIN, BODY_BOTTOM, FRAME_W, PAGE_H - TOP_MARGIN - BODY_BOTTOM,
        id="body", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )

    DISCLAIMER_TEXT = (
        "This design was generated by NanoSynth AI, a research tool developed by Rocco Vidaurre-Kirsch. "
        "The design itself is model-generated and is not authored, reviewed, or endorsed by the tool's developer. "
        "It is intended as a research starting point, not a validated synthesis protocol. "
        "Review all grounded claims against their cited sources and consult domain experts before laboratory use."
    )

    def _draw_cover_footer(canvas, doc_):
        """Paints the disclaimer at a FIXED position at the foot of the cover page."""
        canvas.saveState()
        para = Paragraph(DISCLAIMER_TEXT, _disclaimer_style())
        w, h = para.wrap(FRAME_W, COVER_BOTTOM)
        y = 0.55 * inch
        # Hairline rule above the footer, matching the report's motif
        canvas.setStrokeColor(colors.HexColor("#CCCCCC"))
        canvas.setLineWidth(0.5)
        canvas.line(L_MARGIN, y + h + 10, L_MARGIN + FRAME_W, y + h + 10)
        para.drawOn(canvas, L_MARGIN, y)
        canvas.restoreState()

    doc.addPageTemplates([
        PageTemplate(id="Cover", frames=[cover_frame], onPage=_draw_cover_footer),
        PageTemplate(id="Body", frames=[body_frame]),
    ])

    styles = getSampleStyleSheet()

    # ── Typography: biomedical journal convention (PubMed / ICMJE) ──
    # Serif body (Times) set justified, as journals do; sans-serif reserved for
    # the wordmark and structural labels. Our motif is retained via the teal
    # rule, the NanoSynth wordmark, and the Grounded/Extrapolated color coding.
    SERIF, SERIF_B, SERIF_I = "Times-Roman", "Times-Bold", "Times-Italic"
    SANS_B = "Helvetica-Bold"
    INK = colors.HexColor("#1A1A1A")
    MUTED = colors.HexColor("#5A5A5A")
    TEAL = colors.HexColor("#00A88C")   # motif accent, darkened for print legibility

    wordmark_style = ParagraphStyle(
        "NSWordmark", parent=styles["Normal"], fontName=SANS_B, fontSize=8,
        textColor=TEAL, spaceAfter=1, alignment=TA_LEFT,
    )
    byline_style = ParagraphStyle(
        "NSByline", parent=styles["Normal"], fontName=SERIF_I, fontSize=8,
        textColor=MUTED, spaceAfter=8, alignment=TA_LEFT,
    )
    title_style = ParagraphStyle(
        "NSTitle", parent=styles["Title"], fontName=SERIF_B, fontSize=16,
        leading=20, spaceAfter=6, textColor=INK, alignment=TA_LEFT,
    )
    subtitle_style = ParagraphStyle(
        "NSSubtitle", parent=styles["Normal"], fontName=SERIF_I, fontSize=9.5,
        leading=13, textColor=MUTED, spaceAfter=10, alignment=TA_LEFT,
    )
    section_style = ParagraphStyle(
        "NSSection", parent=styles["Heading2"], fontName=SERIF_B, fontSize=11.5,
        leading=14, spaceBefore=15, spaceAfter=7, textColor=INK,
    )
    body_style = ParagraphStyle(
        "NSBody", parent=styles["Normal"], fontName=SERIF, fontSize=9.5,
        leading=13.5, spaceAfter=6, alignment=TA_JUSTIFY, textColor=INK,
    )
    legend_style = ParagraphStyle(
        "NSLegend", parent=styles["Normal"], fontName=SERIF, fontSize=8,
        leading=11, textColor=MUTED, spaceAfter=10, alignment=TA_JUSTIFY,
    )
    step_title_style = ParagraphStyle(
        "NSStepTitle", parent=styles["Normal"], fontName=SERIF_B, fontSize=9.5,
        leading=13, textColor=INK, spaceAfter=1,
    )
    ref_style = ParagraphStyle(
        "NSRef", parent=styles["Normal"], fontName=SERIF, fontSize=8.5,
        leading=12, spaceAfter=5, textColor=INK,
    )
    # Reference list: hanging indent, as in a journal bibliography
    bib_style = ParagraphStyle(
        "NSBib", parent=styles["Normal"], fontName=SERIF, fontSize=8.5,
        leading=12, spaceAfter=5, textColor=INK,
        leftIndent=16, firstLineIndent=-16, alignment=TA_LEFT,
    )
    disclaimer_style = ParagraphStyle(
        "NSDisclaimer", parent=styles["Normal"], fontName=SERIF, fontSize=7.5,
        leading=10, textColor=colors.HexColor("#888888"), spaceBefore=8, alignment=TA_JUSTIFY,
    )

    story = []

    # ── Header: wordmark + tool attribution, then the report's own title block ──
    # The byline sits with the WORDMARK, not in the report metadata, so it reads as
    # authorship of the tool rather than of this particular generated design.
    disease = params.get("disease", "")
    drug = params.get("drug", "")
    story.append(Paragraph("NANOSYNTH AI &nbsp;·&nbsp; DESIGN REPORT", wordmark_style))
    story.append(Paragraph(
        "Tool developed by Rocco Vidaurre-Kirsch",
        byline_style
    ))
    story.append(Paragraph(_report_title(disease, drug), title_style))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%d %B %Y, %H:%M')} &nbsp;·&nbsp; "
        f"Literature grounding via PubMed (NCBI E-utilities)",
        subtitle_style
    ))
    story.append(HRFlowable(width="100%", thickness=1.0, color=TEAL))
    story.append(Spacer(1, 9))

    # ── Provenance key (applies to the whole report) ──
    story.append(Paragraph(
        '<b>Provenance key.</b> '
        '<font color="#1565C0">Blue text</font> denotes a claim grounded in a cited source, '
        'marked with a superscript reference number. '
        '<font color="#6A1FA8">Purple text</font> denotes a claim extrapolated by the model from general '
        'nanomedicine principles and not directly supported by the retrieved literature. '
        'These tags are self-reported by the model and have not been independently verified; '
        'readers should consult the cited sources before relying on any claim.',
        legend_style
    ))

    # ── Retrieval status ──
    # The wording lives in _retrieval_notice() so it can be tested directly, without
    # rendering a PDF and shelling out to an external binary to read it back.
    notice = _retrieval_notice(design.get("retrieval_status"))
    if notice:
        severe = notice["severity"] == "warn"
        story.append(Paragraph(
            notice["text"],
            ParagraphStyle(
                "NSRetrieval", parent=legend_style, fontSize=8, leading=11,
                textColor=colors.HexColor("#8A6100" if severe else "#555555"),
                borderColor=colors.HexColor("#E8B84B" if severe else "#CCCCCC"),
                borderWidth=0.6, borderPadding=6,
                backColor=colors.HexColor("#FDF6E3") if severe else None,
                spaceBefore=6, spaceAfter=4,
            )
        ))

    # ── 1. Nanoparticle architecture ──
    c = design.get("composition", {})
    diameter_text = (c.get("diameter") or {}).get("text", "") if isinstance(c.get("diameter"), dict) else c.get("diameter", "")
    total_dia = _short_diameter(diameter_text)
    stack_rows_src = _resolve_layer_stack(design, diameter_text)

    story.append(Paragraph("1. Nanoparticle Architecture", section_style))

    # Name the topology explicitly — it is a design decision the model made, and a
    # reader must be able to tell an LNP from a liposome at a glance.
    arch_key = _normalize_architecture(design.get("architecture"))
    arch_label = ARCH_LABELS.get(arch_key, arch_key)
    if design.get("imaging_core"):
        arch_label += " with inset imaging core"
    story.append(Paragraph(
        f'<b>Topology:</b> {arch_label}',
        ParagraphStyle("NSArch", parent=body_style, spaceAfter=6, alignment=TA_LEFT)
    ))

    # Total diameter as a single compact callout. _short_diameter() guarantees
    # this is a measurement and not a paragraph, even if the model over-explains.
    if total_dia:
        story.append(Paragraph(
            f'<font size="17" color="#1A1A1A"><b>{total_dia}</b></font>'
            f'<font size="9" color="#5A5A5A"> &nbsp;total hydrodynamic diameter</font>',
            ParagraphStyle("NSDia", parent=body_style, spaceBefore=6, spaceAfter=12, alignment=TA_CENTER)
        ))

    # Layer stack as a scannable table — one row per concentric layer, thickness
    # split from material so the architecture reads down a column at a glance.
    if stack_rows_src:
        stack_rows = []
        for idx, (thickness, material) in enumerate(stack_rows_src):
            stack_rows.append([
                Paragraph(f'<font color="#888888">{idx + 1}</font>', ref_style),
                Paragraph(f"<b>{thickness}</b>", ref_style),
                Paragraph(material, ref_style),
            ])
        stack_table = Table(stack_rows, colWidths=[0.3 * inch, 0.95 * inch, 5.55 * inch])
        stack_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#E4E4E4")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D6D6D6")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F7F7F7")),
        ]))
        story.append(stack_table)

        # Dimensional reconciliation. Surfaced, not silently corrected: the arithmetic
        # is checkable, and a reader deserves to see when the stated diameter and the
        # layer stack disagree rather than have one quietly overwritten.
        dim = design.get("dimension_check")
        if dim and not dim.get("ok"):
            story.append(Spacer(1, 8))
            story.append(Paragraph(
                f'<b>Dimensional check — not reconciled.</b> Summing the layer stack gives '
                f'{dim["expression"]}, against a stated {dim["stated"]:g} nm — a discrepancy of '
                f'{dim["delta"]:.0f} nm. At least one layer thickness or the stated diameter is '
                f'wrong; both should be treated as unverified until recomputed.',
                ParagraphStyle(
                    "NSDimWarn", parent=legend_style, fontSize=8, leading=11,
                    textColor=colors.HexColor("#8A6100"),
                    borderColor=colors.HexColor("#E8B84B"), borderWidth=0.6,
                    borderPadding=6, backColor=colors.HexColor("#FDF6E3"),
                )
            ))
        story.append(Spacer(1, 26))

    try:
        # Recolor label text / leader lines for a white page before rasterizing —
        # the source SVG is styled for the dark UI and would be invisible otherwise.
        print_svg = _recolor_svg_for_print(svg_markup)
        # svg2rlg returns a reportlab Drawing, which is itself a Flowable —
        # it can go straight into the story as vector art (no PNG step).
        drawing = svg2rlg(io.BytesIO(print_svg.encode("utf-8")))
        if drawing is None:
            raise ValueError("SVG could not be parsed")

        # Fit the diagram to the space ACTUALLY LEFT on the cover, not to a fixed
        # width. The layer stack varies from ~3 to ~6 rows depending on the design,
        # and a fixed width large enough to look good under a 4-row stack pushes the
        # diagram onto page 2 under a 6-row one — which breaks the cover in half.
        # Measure what has been laid out so far and give the diagram the remainder.
        used_h = sum(
            f.wrap(FRAME_W, PAGE_H)[1] + getattr(f, "getSpaceBefore", lambda: 0)()
                                       + getattr(f, "getSpaceAfter", lambda: 0)()
            for f in story
        )
        avail_h = (PAGE_H - TOP_MARGIN - COVER_BOTTOM) - used_h - 6   # 6pt breathing room
        aspect = drawing.height / drawing.width if drawing.width else 0.6

        # Prefer to fill the width, but shrink if the height would not fit.
        target_w = min(FRAME_W, avail_h / aspect if aspect else FRAME_W)
        target_w = max(target_w, 3.2 * inch)   # never shrink to illegibility

        scale = target_w / drawing.width if drawing.width else 1
        drawing.width = drawing.width * scale
        drawing.height = drawing.height * scale
        drawing.scale(scale, scale)
        drawing.hAlign = "CENTER"
        story.append(drawing)
    except Exception:
        story.append(Paragraph("(Diagram could not be rendered for this report.)", body_style))

    # The disclaimer is NOT appended here — it is painted as a fixed footer at the
    # foot of the cover page by _draw_cover_footer(), so it sits flush at the page
    # bottom regardless of how tall the layer stack happens to be.
    #
    # NextPageTemplate switches to the full-height Body frame for everything after
    # the break; the cover keeps its shorter frame so the footer never collides.
    story.append(NextPageTemplate("Body"))
    story.append(PageBreak())

    # ── Composition ──
    story.append(Paragraph("2. Composition", section_style))
    comp_rows = [
        ["Core", _claim_to_html(c.get("core"), sources)],
        ["Shell", _claim_to_html(c.get("shell"), sources)],
        ["Payload", _claim_to_html(c.get("payload"), sources)],
        ["Surface", _claim_to_html(c.get("surface"), sources)],
        ["Release Gate", _claim_to_html(c.get("gate"), sources)],
        ["Diameter", _claim_to_html(c.get("diameter"), sources)],
    ]
    comp_table_data = [[Paragraph(f"<b>{label}</b>", body_style), Paragraph(value, body_style)] for label, value in comp_rows]
    comp_table = Table(comp_table_data, colWidths=[1.3 * inch, 5.5 * inch])
    comp_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F4F6FA")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(comp_table)
    story.append(Spacer(1, 8))

    # ── Synthesis Protocol ──
    story.append(Paragraph("3. Synthesis Protocol", section_style))
    for i, step in enumerate(design.get("synthesis", []), start=1):
        detail_html = _claim_to_html({
            "text": step.get("detail", ""),
            "provenance": step.get("provenance"),
            "source": step.get("source"),
        }, sources)
        # KeepTogether prevents a step's heading from being stranded at the bottom
        # of one page while its detail text flows onto the next.
        story.append(KeepTogether([
            Paragraph(f"{i}. {step.get('step', '')}", step_title_style),
            Paragraph(detail_html, body_style),
        ]))
    story.append(Spacer(1, 8))

    # ── 4. Most Similar Published Designs (prior art) ──
    # Functionally distinct from References: these are the closest PUBLISHED
    # NANOPARTICLE DESIGNS for this target, surfaced so the researcher can check
    # existing formulations before committing to this one. They are numbered in the
    # same global sequence as the references, and are NOT repeated there.
    similar = (bibliography or {}).get("similar", [])
    references = (bibliography or {}).get("references", [])

    if similar:
        story.append(Paragraph("4. Most Similar Published Designs", section_style))
        story.append(Paragraph(
            "Published nanoparticle formulations most closely matching this target and payload. "
            "Provided as prior art for comparison; entries cited in the text above carry their "
            "superscript number here and are not repeated under References.",
            legend_style
        ))
        for src in sorted(similar, key=lambda s: s.get("n", 0)):
            story.append(Paragraph(
                f'{src.get("n", "")}. {src.get("citation") or src.get("title", "")} '
                f'<link href="{src.get("url", "")}" color="#1565C0">{src.get("url", "")}</link>',
                bib_style
            ))
        story.append(Spacer(1, 8))

    # ── 5. Design Rationale ──
    story.append(Paragraph("5. Design Rationale", section_style))
    rationale = design.get("rationale", [])
    if isinstance(rationale, list):
        for r in rationale:
            story.append(Paragraph(_claim_to_html(r, sources), body_style))
    else:
        story.append(Paragraph(str(rationale), body_style))

    # ── 6. References (always opens the final page) ──
    # Only sources the model actually cited that are NOT already listed under
    # Most Similar Published Designs — no paper appears twice.
    #
    # "References" (not "Bibliography") is deliberate: in ICMJE/Vancouver style —
    # which this report follows — References means works ACTUALLY CITED, resolvable
    # by their superscript number. A bibliography implies broader background reading
    # that may not be cited, which would overstate what this tool did.
    #
    # The PageBreak gives references a dedicated closing page, as journals do, so the
    # heading is never stranded mid-page beneath the rationale.
    if references or similar:
        story.append(PageBreak())
        story.append(Paragraph("6. References", section_style))

    if references:
        story.append(Paragraph(
            "Sources consulted by the model when grounding the claims above.",
            legend_style
        ))
        for src in sorted(references, key=lambda s: s.get("n", 0)):
            story.append(Paragraph(
                f'{src.get("n", "")}. {src.get("citation") or src.get("title", "")} '
                f'<link href="{src.get("url", "")}" color="#1565C0">{src.get("url", "")}</link>',
                bib_style
            ))
        story.append(Spacer(1, 6))
    elif similar:
        # Every cited source already appears above — say so rather than showing an
        # empty References heading, which would look like a bug.
        story.append(Paragraph(
            "All sources cited in this report are listed under "
            "<i>Most Similar Published Designs</i> above and are not repeated here.",
            legend_style
        ))

    # NOTE: the disclaimer is no longer repeated here — it now sits on the cover
    # page, directly under the diagram, so the reader meets it before the design.

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/export-pdf", methods=["POST"])
def export_pdf():
    """
    Builds a downloadable PDF report from a previously generated design.
    Expects the same {design, sources, params} the frontend already holds in
    memory from the /generate response, plus the SVG markup of the diagram
    so it can be rasterized and embedded (reportlab has no native SVG support).
    """
    body = request.get_json()
    design = body.get("design", {})
    sources = body.get("sources", {})
    params = body.get("params", {})
    svg_markup = body.get("svg", "")
    bibliography = body.get("bibliography", {})

    try:
        pdf_buf = _build_pdf_report(design, sources, params, svg_markup, bibliography)
        # Name the file from whichever fields are actually filled in — either may be
        # left blank on purpose to broaden the design space.
        slug_parts = [
            re.sub(r'[^a-zA-Z0-9]+', '_', (params.get(k) or "")).strip('_').lower()
            for k in ("disease", "drug")
        ]
        slug = "_".join(p for p in slug_parts if p) or "design"
        filename = f"nanosynth_{slug}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({"ok": False, "error": f"PDF export failed: {e}"}), 500


@app.route("/health")
def health():
    """Liveness probe for the host. Deliberately reveals nothing about config."""
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    # Debug mode is OFF unless explicitly requested. The Werkzeug debugger will
    # execute arbitrary code supplied by anyone who can trigger a traceback, so
    # a publicly reachable instance running debug=True is a remote code execution
    # hole, not just a noisy log. Opt in locally with NANOSYNTH_DEBUG=1.
    debug = os.environ.get("NANOSYNTH_DEBUG", "").lower() in ("1", "true", "yes")

    # Hosts assign the port via $PORT. Bind 127.0.0.1 by default so a local run
    # is not exposed to the rest of the network; set HOST=0.0.0.0 to share it.
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")

    print("\n  NanoSynth AI — Research Server")
    print("  Created by: Rocco Vidaurre-Kirsch")
    print(f"  Open http://localhost:{port} in your browser")
    if debug:
        print("  ⚠  DEBUG MODE — do not expose this instance publicly")
    print()
    app.run(debug=debug, host=host, port=port)
