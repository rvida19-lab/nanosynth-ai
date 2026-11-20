# NanoSynth AI

**A literature-grounded nanoparticle design tool.** Give it a disease target, a
payload, and a few constraints; it returns a labeled cross-section diagram, a
composition table, a step-by-step synthesis protocol, and a journal-formatted
PDF — with every claim tagged as either **Grounded** in a cited PubMed source or
**Extrapolated** from general principles.

> **Research prototype.** Designs are model-generated and have not been
> experimentally validated. Read [Limitations](#limitations) before relying on
> any output — it is the most important section in this README.

**[Live demo](https://nanosynth-ai.onrender.com)** — bring your own Anthropic API key
git add README.md
git commit -m "Add live demo link"
git push
---

## What makes it different

Most LLM wrappers produce a confident paragraph and leave you to figure out what
is real. NanoSynth is built around the opposite premise: **make the seams
visible.**

**Provenance on every claim.** Each composition field, synthesis step, and
rationale sentence is tagged Grounded (blue, with a superscript citation) or
Extrapolated (purple). The tags are self-reported by the model — see
[Limitations](#limitations) — but they tell you where to aim your scepticism.

**Three distinct PubMed searches, not one.** Each answers a different question,
because one query cannot:

| Search | Query shape | Feeds |
|---|---|---|
| Therapy | `(disease) AND (drug)` | **References** — grounds factual claims |
| Prior art | `(disease) AND (drug) AND (nanocarrier terms)` | **Most Similar Published Designs** |
| Methods | `(fabrication technique) AND (synthesis terms) NOT review` | **Synthesis protocol** grounding |

The methods search exists because a disease+drug query returns clinical trials,
which cannot ground *"thermal decomposition of iron(III) oleate at 320 °C."*
Without it, every synthesis step was honestly — and uselessly — marked
Extrapolated. The model names its own fabrication steps, then those get searched.

**Seven topologies, not one picture.** The diagram is a 2D cross-section, so what
must differ between designs is the *arrangement of compartments*. A liposome has
a bilayer around a hollow lumen; a lipid nanoparticle has no lumen at all.
Conflating them is a scientific error, and the renderer asserts against it.

**Mechanical self-checks.** The layer stack is reconciled against the stated
diameter (`core + 2 × Σ shells`) and the mismatch is *reported*, not silently
corrected. References lists only works actually cited.

---

## Run it locally

Recommended for any real use: your API key never leaves your machine.

```bash
git clone https://github.com/rvida19-lab/nanosynth-ai.git
cd nanosynth-ai
pip install -r requirements.txt

cp .env.example .env     # then add your Anthropic API key
python app.py            # → http://localhost:5000
```

You need an [Anthropic API key](https://console.anthropic.com/settings/keys).
You can also paste it into the UI instead of using `.env`.

An [NCBI API key](https://account.ncbi.nlm.nih.gov/settings/) is optional but
recommended — it raises the PubMed limit from 3 to 10 requests/sec.

### Deploy your own

```bash
gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT app:app
```

`render.yaml` is included for one-click [Render](https://render.com) deploys.
Three settings are deliberate, not defaults:

- **`--workers 1`** — the NCBI rate limiter and PubMed cache are process-local.
  Multiple workers would each keep their own, doubling the request rate against
  NCBI's per-IP limit.
- **`--threads 8`** — requests are almost entirely network wait, so threads give
  real concurrency here.
- **`--timeout 300`** — a generation makes two Claude calls plus up to ~11 PubMed
  queries and can run 60–120s. Gunicorn's 30s default would kill it mid-request.

Never set `NANOSYNTH_DEBUG=1` on a reachable instance: the Werkzeug debugger
allows arbitrary code execution by anyone who can trigger a traceback.

The hosted demo is **bring-your-own-key** — the server holds no Anthropic
credentials, so no one can bill generations to the maintainer.

---

## Limitations

Read this before using any output. These are not hypothetical; each was found in
real generated reports.

**Designs are unvalidated.** Nothing here has been synthesised or tested. Output
is a structured starting point for an expert to critique, not a protocol to
follow.

**Provenance tags are self-reported.** The model decides what counts as
Grounded. A tag means *the model asserts this source supports the claim* — not
that anyone verified it does. Always open the citation.

**Chemistry errors occur and are not always obvious.** Observed in real runs:

- a lipid anchor (DSPE-PEG) described as grafting to a silica surface, which is
  chemically impossible — DSPE has no silane group;
- a conjugation order that buries the targeting antibody *beneath* its own PEG
  stealth layer, where it cannot reach its receptor;
- random EDC/NHS coupling described as "site-specific";
- a layer stack that did not sum to its own stated diameter.

Prompt-level guards now target each of these, and the dimensional mismatch is
checked mechanically. **Guards reduce the error rate; they do not eliminate it.**

**This is the core structural problem:** the output is fluent, well-formatted,
and plausibly cited, so a domain expert catches these in seconds while a novice
catches none of them. The people qualified to trust the output are the ones who
least need it. The provenance system narrows that gap; it does not close it.

**Scope.** AI use in drug *discovery* sits outside the FDA's January 2025
guidance on AI supporting regulatory decision-making, and the January 2026
FDA–EMA Guiding Principles name generative models in molecular design as future
work. Nothing here is regulatory-grade evidence.

**Best used as** an ideation and teaching tool — exploring why an LNP is not a
liposome, or generating a structured draft for an expert to tear apart.

---

## How it works

```
Request → PubMed (therapy + prior art, with abstracts)
        → Claude pass 1: architecture, composition, rationale
                         + names its own fabrication steps
        → PubMed (methods search, per fabrication step)
        → Claude pass 2: synthesis protocol, re-grounded on real protocol papers
        → Bibliography finalised (numbering assigned last, cited works only)
        → SVG cross-section + journal-formatted PDF
```

Two passes exist because the architecture must be chosen before anyone can know
which fabrication methods to search for.

## File structure

```
app.py              Flask backend, prompts, PubMed retrieval, PDF generation
static/index.html   Entire frontend — HTML, CSS, JS in one file
render.yaml         Render deployment blueprint
.env.example        Configuration template
```

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | No* | Server-side key. Omit on public deploys. |
| `NCBI_API_KEY` | No | Raises PubMed limit 3 → 10 req/sec |
| `PORT` | No | Listen port (default 5000) |
| `HOST` | No | Bind address (default `127.0.0.1`) |
| `NANOSYNTH_DEBUG` | No | Local debugging only — never in production |

\* Either set it or paste a key into the UI.

---

## UI conventions (do not regress these)

- **The diagram encodes real structure — do not rearrange it for looks.**
  `buildSVG()` dispatches on `design.architecture` to one of SEVEN topologies.
  The figure is a 2D cross-section, so what differs between designs is the
  arrangement of compartments, not the chemistry. Each renderer declares its own
  labels, because "Mesoporous Shell" is meaningless on a lipid nanoparticle.

  | key | what the cross-section MUST show |
  |---|---|
  | `mesoporous_core_shell` | Solid core; shell pierced by mesopores; drug **inside the pores**; gates **capping pore mouths** |
  | `vesicle` | True **bilayer** (head groups on both faces) enclosing a **hollow aqueous lumen**; hydrophilic drug in the lumen |
  | `lipid_nanoparticle` | **NO lumen.** Nucleic acid complexed with ionizable lipid through a **dense** interior; single surface monolayer |
  | `micelle` | Single **monolayer** corona (not a bilayer); drug **dissolved** in the hydrophobic core |
  | `polymer_matrix` | Drug dispersed **homogeneously**; **no discrete compartments** — that absence is the defining feature |
  | `inorganic_core_ligand` | Dense core with **NO interior payload**; drug conjugated to the **surface** via a linker |
  | `dendrimer` | Generational **branching**; payload in interior voids **and** on terminal groups |

  **`vesicle` vs `lipid_nanoparticle` is the distinction most often gotten wrong.**
  A liposome has a bilayer around a hollow lumen. An LNP has no lumen at all.
  Conflating them is a scientific error, and the render tests assert against it.

- `imaging_core` is an orthogonal **boolean modifier**, not an eighth architecture —
  a SPION can be inset in an LNP, vesicle, micelle, matrix, or dendrimer without
  changing that carrier's topology. Do not expand this into new enum values.
- The model's `architecture` value is coerced through `_normalize_architecture()`
  in `app.py`, which maps synonyms ("LNP", "liposome", "PLGA", "PAMAM") onto the
  canonical keys. Never trust the raw enum.
- **"Grounded" and "Extrapolated" are always capitalized** in every user-facing
  string — the sidebar disclaimer, the legend above the output cards, and the
  PDF provenance key. If you rewrite any of those files, carry it forward.
- Provenance colors: Grounded = blue, Extrapolated = purple. Claim text is
  marked with a colored **underline only** — no background highlight fill.
- The PDF follows biomedical journal convention: serif body, justified,
  numbered sections, superscript numeric citations, Vancouver/ICMJE references.
  Teal rule, wordmark, and provenance colors are the motif.
- **References** (grounding sources, last section) and **Most Similar Published
  Designs** (prior-art formulations) are two DIFFERENT PubMed searches. They must
  not be collapsed back into one query. One global numbering spans both; a paper
  is never listed twice.

## Report section order

1. Nanoparticle Architecture (topology, layer stack, diagram) — cover page
2. Composition
3. Synthesis Protocol
4. Most Similar Published Designs (prior art)
5. Design Rationale
6. References — always opens the final page

## Contributing

The most valuable contribution is **domain correction**. If a generated design
contains a chemistry error, open an issue with the parameters that produced it
and what is wrong — those become prompt guards or mechanical checks, which is
how the four errors listed under Limitations were fixed.

Also useful: additional topologies (see the conventions table above), better
method-search strategies, and test cases that assert structural invariants.

## License

MIT — see [LICENSE](LICENSE).

## Citation

```bibtex
@software{vidaurrekirsch_nanosynth_2026,
  author = {Vidaurre-Kirsch, Rocco},
  title  = {NanoSynth AI: A literature-grounded nanoparticle design tool},
  year   = {2026},
  url    = {https://github.com/rvida19-lab/nanosynth-ai}
}
```

---

Built by **Rocco Vidaurre-Kirsch**. Designs are model-generated and are not
authored, reviewed, or endorsed by the maintainer.
