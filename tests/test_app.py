"""
Backend invariant tests. No network, no API key required.

Run:  python tests/test_app.py

Covers the mechanical guarantees the report's credibility rests on:
architecture normalisation, dimensional reconciliation, cited-only references,
and NCBI rate limiting.
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402

failures = []


def check(name, passed):
    if not passed:
        failures.append(name)
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")


def section(t):
    print(f"\n── {t} ──")


# ── Architecture normalisation ────────────────────────────────────────────
# The model drifts on enums. Each of these must land on a canonical topology
# rather than silently defaulting — drawing an LNP as a mesoporous particle is
# exactly the failure this guards against.
section("architecture normalisation")
for a in app.ARCHITECTURES:
    check(f"canonical '{a}' passes through", app._normalize_architecture(a) == a)

for raw, want in [
    ("LNP", "lipid_nanoparticle"), ("lipid nanoparticle", "lipid_nanoparticle"),
    ("Lipid-Nanoparticle", "lipid_nanoparticle"), ("ionizable lipid", "lipid_nanoparticle"),
    ("liposome", "vesicle"), ("polymersome", "vesicle"), ("exosome", "vesicle"),
    ("MSN", "mesoporous_core_shell"), ("mesoporous silica", "mesoporous_core_shell"),
    ("polymeric micelle", "micelle"), ("block copolymer", "micelle"),
    ("PLGA", "polymer_matrix"), ("nanogel", "polymer_matrix"), ("SLN", "polymer_matrix"),
    ("gold", "inorganic_core_ligand"), ("quantum dot", "inorganic_core_ligand"),
    ("PAMAM", "dendrimer"), ("hyperbranched", "dendrimer"),
]:
    check(f"'{raw}' -> {want}", app._normalize_architecture(raw) == want)

check("unknown input falls back safely",
      app._normalize_architecture("nonsense") in app.ARCHITECTURES)
check("None falls back safely", app._normalize_architecture(None) in app.ARCHITECTURES)


# ── Dimensional reconciliation ────────────────────────────────────────────
# layer_stack[0] is the core (a DIAMETER); each later entry is a radial shell
# thickness present on BOTH sides. So: total = core + 2 x sum(shells).
section("dimensional reconciliation")

inconsistent = {
    "layer_stack": [
        {"thickness": "~15 nm"}, {"thickness": "~40 nm"},
        {"thickness": "~5 nm"}, {"thickness": "~5 nm"}, {"thickness": "~8 nm"},
    ],
    "composition": {"diameter": {"text": "~105 nm"}},
}
r = app._check_dimensions(inconsistent)
check("flags a stack that does not sum to the stated diameter", r and not r["ok"])
check("computes 15 + 2x(40+5+5+8) = 131 nm", r and abs(r["computed"] - 131) < 0.01)

consistent = {
    "layer_stack": [
        {"thickness": "~15 nm"}, {"thickness": "~30 nm"},
        {"thickness": "~5 nm"}, {"thickness": "~5 nm"},
    ],
    "composition": {"diameter": {"text": "~95 nm"}},
}
check("stays silent on a consistent design", app._check_dimensions(consistent)["ok"])

# Tolerance calibration. A shell listed 5 nm too thick shows up as 10 nm of
# diameter — small enough to look like rounding, large enough to be a real
# error. An earlier 12% band let exactly this through on a live run.
def _dim(core, shells, stated):
    return app._check_dimensions({
        "layer_stack": [{"thickness": f"~{core} nm"}]
                       + [{"thickness": f"~{s} nm"} for s in shells],
        "composition": {"diameter": {"text": f"~{stated} nm"}},
    })

check("catches a 5 nm shell error (116 computed vs 106 stated)",
      not _dim(20, [35, 5, 8], 106)["ok"])
check("silent once that shell is corrected to 30 nm",
      _dim(20, [30, 5, 8], 106)["ok"])
check("silent on 1 nm rounding drift",
      _dim(20, [30, 5, 8], 105)["ok"])
check("silent on a small micelle within the absolute floor",
      _dim(10, [8, 4], 35)["ok"])
check("silent on a small dendrimer",
      _dim(4, [3, 2], 14)["ok"])
check("reports the discrepancy size for the reader",
      abs(_dim(20, [35, 5, 8], 106)["delta"] - 10.0) < 0.01)

# Must degrade, never raise, on malformed model output.
for bad, label in [
    ({}, "empty design"),
    ({"layer_stack": []}, "empty stack"),
    ({"layer_stack": [{"thickness": "~15 nm"}]}, "single layer"),
    ({"layer_stack": [{"thickness": "thick"}, {"thickness": "thin"}],
      "composition": {"diameter": {"text": "big"}}}, "no numbers"),
    ({"layer_stack": ["not a dict", "x"],
      "composition": {"diameter": {"text": "~100 nm"}}}, "malformed entries"),
]:
    try:
        app._check_dimensions(bad)
        check(f"handles {label} without raising", True)
    except Exception as e:  # noqa: BLE001
        check(f"handles {label} without raising — raised {e}", False)


# ── Retrieval status ──────────────────────────────────────────────────────
# An all-extrapolated report can mean "no literature supports this" or "the
# search never ran". Those license opposite conclusions and must not look alike.
section("retrieval status is distinguishable")

import re  # noqa: E402
import subprocess, tempfile  # noqa: E402


def _report_text(status):
    E = lambda t: {"text": t, "provenance": "extrapolated", "source": None}  # noqa: E731
    d = {
        "architecture": "mesoporous_core_shell", "imaging_core": False,
        "composition": {k: E("...") for k in
                        ["core", "shell", "payload", "surface", "gate"]}
                       | {"diameter": E("~96 nm")},
        "layer_stack": [{"thickness": "~20 nm", "material": "core"},
                        {"thickness": "~30 nm", "material": "shell"},
                        {"thickness": "~3 nm", "material": "gate"},
                        {"thickness": "~5 nm", "material": "corona"}],
        "rationale": [E("...")],
        "synthesis": [{"step": "s", "detail": "d",
                       "provenance": "extrapolated", "source": None}],
        "retrieval_status": status,
    }
    d["dimension_check"] = app._check_dimensions(d)
    svg = ('<svg width="640" height="380" viewBox="0 0 640 380" '
           'xmlns="http://www.w3.org/2000/svg"><circle cx="320" cy="185" r="60" '
           'fill="#8B5A2B"/></svg>')
    buf = app._build_pdf_report(
        d, {}, {"disease": "x", "drug": "y", "size": "80-120 nm", "route": ""},
        svg, {"similar": [], "references": [], "similar_query": "()"})
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(buf.getvalue())
        path = f.name
    raw = subprocess.run(["pdftotext", path, "-"],
                         capture_output=True, text=True).stdout
    # Collapse whitespace: PDF line wrapping would otherwise split the phrases
    # being matched, making these assertions fail on layout rather than content.
    return re.sub(r"\s+", " ", raw)


_failed = _report_text({"state": "failed",
                        "detail": "grounding search (ConnectionError)", "found": 0})
check("a failed search is reported as a failure",
      "Literature retrieval failed" in _failed)
check("failure says ungrounded by circumstance, not by absence",
      "ungrounded by circumstance" in _failed)
check("failure warns that missing citations prove nothing",
      "carries no evidential weight" in _failed)

_empty = _report_text({"state": "empty", "detail": "", "found": 0})
check("a genuinely empty search is NOT reported as a failure",
      "No matching literature found" in _empty
      and "Literature retrieval failed" not in _empty)

_ok = _report_text({"state": "ok", "detail": "", "found": 8})
check("a successful search adds no retrieval notice",
      "Literature retrieval failed" not in _ok
      and "No matching literature found" not in _ok)


# ── Bibliography: References means WORKS CITED ────────────────────────────
section("bibliography — cited works only")


def mk(pmid, title):
    return {
        "pmid": pmid, "title": title, "journal_abbrev": "J Test", "year": "2023",
        "volume": "1", "issue": "1", "pages": "1-9", "doi": "",
        "author_list": ["Doe A"], "authors": "Doe A",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "abstract": "...",
    }


similar = [mk("100", "Prior art A"), mk("200", "Prior art B")]
therapy = [mk("300", "Therapy paper"), mk("301", "Uncited therapy paper")]
methods = [mk("400", "Method paper"), mk("401", "Uncited method paper")]
tag_to_pmid = {"S1": "300", "S2": "301", "M1": "400", "M2": "401"}

design = {
    "composition": {"core": {"text": "..", "source": "M1"}},
    "synthesis": [{"step": "a", "detail": "..", "source": "M1"},
                  {"step": "b", "detail": "..", "source": None}],
    "rationale": [{"text": "..", "source": "S1"}],
}

bib, smap = app._finalize_bibliography(design, similar, therapy + methods, tag_to_pmid)
refs = [b for b in bib if b["section"] == "reference"]
sims = [b for b in bib if b["section"] == "similar"]

check("prior art listed in full (it is a reading list, not a citation list)", len(sims) == 2)
check("uncited papers excluded from References",
      {b["pmid"] for b in refs} == {"300", "400"})
check("numbering is contiguous with no holes",
      [b["n"] for b in bib] == list(range(1, len(bib) + 1)))
check("every cited tag resolves to a listed entry",
      all(any(b["n"] == v["n"] for b in bib) for v in smap.values()))

cited = app._collect_cited_tags(design)
check("cited tags collected correctly", cited == {"M1", "S1"})
check("multi-source citations split",
      app._collect_cited_tags({"synthesis": [{"source": "M1, M3"}]}) == {"M1", "M3"})

# A paper found by two different searches must appear ONCE, reusing its number.
dup_design = {"synthesis": [{"source": "S1"}, {"source": "M1"}]}
dup_bib, dup_map = app._finalize_bibliography(
    dup_design, [], [mk("500", "Shared"), mk("500", "Shared")], {"S1": "500", "M1": "500"})
check("a paper found by two searches is listed once", len(dup_bib) == 1)
check("both tags point at that one entry", dup_map["S1"]["n"] == dup_map["M1"]["n"])


# ── NCBI rate limiting ────────────────────────────────────────────────────
# NCBI throttles BY IP, so on a shared deployment every visitor competes for
# the same budget. The limiter must hold under concurrency, not just serially.
section("NCBI rate limiting")

calls = []


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"esearchresult": {"idlist": []}}

    text = ""


app.requests.get = lambda url, params=None, timeout=None, **kw: (
    calls.append(time.monotonic()), _Resp())[1]


def worst_window(ts, window=1.0):
    ts = sorted(ts)
    return max((sum(1 for u in ts[i:] if u - t < window) for i, t in enumerate(ts)),
               default=0)


calls.clear()
for i in range(11):   # one generation's worth of queries
    app._ncbi_get("esearch.fcgi", {"term": f"q{i}"})
check(f"serial burst stays <= 3/sec (worst window: {worst_window(calls)})",
      worst_window(calls) <= 3)

calls.clear()
threads = [threading.Thread(target=lambda: app._ncbi_get("esearch.fcgi", {"term": "x"}))
           for _ in range(16)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(f"concurrent burst stays <= 3/sec (worst window: {worst_window(calls)})",
      worst_window(calls) <= 3)


# ── Misc robustness ───────────────────────────────────────────────────────
section("misc")
check("unspecified admin route does NOT silently become IV",
      "Do NOT default to intravenous" in app._route_line(""))
check("specified admin route passes through verbatim",
      app._route_line("Intratumoral") == "- Administration route: Intratumoral")
check("debug is off unless explicitly enabled",
      os.environ.get("NANOSYNTH_DEBUG", "").lower() not in ("1", "true", "yes"))


print("\n" + ("═══ ALL BACKEND TESTS PASSED ═══" if not failures
              else f"═══ {len(failures)} FAILURE(S) ═══\n  " + "\n  ".join(failures)))
sys.exit(0 if not failures else 1)
