/**
 * Structural invariant tests for the seven nanoparticle topologies.
 *
 * These assert CHEMISTRY, not styling. A failure here means the figure would
 * misrepresent the design — e.g. drawing a lipid nanoparticle with the hollow
 * lumen of a liposome, which is the single most common structural error in
 * nanomedicine figures.
 *
 * Run:  node tests/test_render.js
 *
 * The frontend is one self-contained HTML file, so the script is extracted and
 * evaluated against a minimal DOM shim. A syntax check is NOT enough — an
 * earlier regression deleted a `const` block and still parsed cleanly; only
 * executing the render path caught it.
 */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const js = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

// ── Minimal DOM shim ──────────────────────────────────────────────────────
const store = {};
const mkTags = labels => {
  const btns = labels.map(t => {
    const b = { textContent: t, _c: new Set() };
    b.classList = {
      toggle: (c, on) => (on ? b._c.add(c) : b._c.delete(c)),
      contains: c => b._c.has(c), add: c => b._c.add(c), remove: c => b._c.delete(c),
    };
    return b;
  });
  return { _btns: btns, querySelectorAll: () => btns };
};
const opts = a => a.map(t => ({ text: t, value: t, selected: false }));
const mkEl = id => ({
  id, innerHTML: '', value: '', _cls: new Set(),
  classList: {
    add(c) { store[id]._cls.add(c); }, remove(c) { store[id]._cls.delete(c); },
    contains(c) { return store[id]._cls.has(c); },
  },
  querySelector: () => ({ innerHTML: '' }),
});
store['disease'] = { value: '' };
store['receptor'] = { value: '' };
store['drug'] = { value: '' };
store['sizeRange']  = { options: opts(['50–100 nm', '80–120 nm', '120–200 nm']), value: '' };
store['adminRoute'] = { options: opts(['Not Specified', 'Intravenous (IV)', 'Intratumoral', 'Oral', 'Inhaled']), value: '' };
store['archChoice'] = { value: '', options: opts(['']) };
store['imagingTags'] = mkTags(['T2 MRI', 'PET', 'Fluorescence', 'CT', 'Photoacoustic']);
store['triggerTags'] = mkTags(['pH-responsive', 'ROS', 'Temperature', 'Enzyme', 'Light']);

global.document = {
  getElementById: id => store[id] || (store[id] = mkEl(id)),
  querySelectorAll: () => [], createElement: () => ({ click() {}, remove() {} }),
  addEventListener() {}, body: { appendChild() {} },
};
global.window = {};
global.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });

// `eval` hoists function declarations but NOT const bindings, so re-export the
// consts the assertions need. Harness limitation, not a code issue.
eval(js + '\n;global.ARCH_RENDERERS=ARCH_RENDERERS;global.ARCH_REFERENCE=ARCH_REFERENCE;'
        + 'global.REF_COLORS=REF_COLORS;global.GEO=GEO;');

// ── Harness ───────────────────────────────────────────────────────────────
let failures = 0;
const check = (name, pass) => {
  if (!pass) failures++;
  console.log(`  ${pass ? 'PASS' : 'FAIL'}  ${name}`);
};
const section = t => console.log(`\n── ${t} ──`);

const sc = { core: '#8B5A2B', shell: '#C8CDD4', gate: '#E8B84B', surface: '#2F80ED', payload: '#E5533D' };
const params = { disease: 'TNBC', drug: 'mRNA', receptor: 'EpCAM', imaging: 'T2 MRI', trigger: 'pH-responsive' };
const ARCHS = ['mesoporous_core_shell', 'vesicle', 'lipid_nanoparticle', 'micelle',
               'polymer_matrix', 'inorganic_core_ligand', 'dendrimer'];

section('all seven topologies render');
for (const a of ARCHS) {
  let svg = '', err = null;
  try { svg = buildSVG({ diameter: '~100 nm' }, sc, params, a, false); } catch (e) { err = e.message; }
  const labels = (svg.match(/<text /g) || []).length - 1;   // minus the size caption
  check(`${a} renders (${err || labels + ' labels'})`,
        !err && svg.startsWith('<svg') && svg.includes('</svg>') && labels >= 5);
}

section('vesicle vs LNP — the distinction most often gotten wrong');
// A liposome has a bilayer around a HOLLOW AQUEOUS LUMEN. An LNP has a dense
// ionizable-lipid interior and NO lumen. If someone "simplifies" the LNP
// renderer into the vesicle one, these fail rather than shipping a figure
// that lies about the science.
const LUMEN = 'rgba(255,255,255,0.14)';
const ves = buildSVG({ diameter: '~100 nm' }, sc, params, 'vesicle', false);
const lnp = buildSVG({ diameter: '~100 nm' }, sc, params, 'lipid_nanoparticle', false);
check('vesicle HAS a hollow aqueous lumen', ves.includes(LUMEN));
check('LNP has NO lumen (dense interior)', !lnp.includes(LUMEN));

section('labels adapt per topology');
const msn = buildSVG({ diameter: '~100 nm' }, sc, params, 'mesoporous_core_shell', false);
check('LNP does NOT say "Mesoporous"', !lnp.includes('Mesoporous'));
check('LNP does NOT say "in Pores"', !lnp.includes('in Pores'));
check('LNP says "Ionizable Lipid"', lnp.includes('Ionizable Lipid'));
check('MSN DOES say "Mesoporous Shell"', msn.includes('Mesoporous Shell'));
check('MSN DOES say "in Pores"', msn.includes('in Pores'));
check('mRNA casing preserved (not "Mrna")', lnp.includes('mRNA') && !lnp.includes('Mrna'));

section('imaging_core is a modifier, not an eighth topology');
for (const a of ['vesicle', 'lipid_nanoparticle', 'micelle', 'polymer_matrix', 'dendrimer']) {
  const off = buildSVG({ diameter: '~100 nm' }, sc, params, a, false);
  const on  = buildSVG({ diameter: '~100 nm' }, sc, params, a, true);
  check(`${a} accepts an inset imaging core`, on.length > off.length && on.includes(sc.core));
}

section('unknown architecture falls back safely');
let fellBack = false;
try { fellBack = buildSVG({ diameter: '~100 nm' }, sc, params, 'nonsense_arch', false).startsWith('<svg'); }
catch (e) { fellBack = false; }
check('unknown key renders a default rather than throwing', fellBack);

section('reference sheet stays in sync with the renderers');
const refKeys = ARCH_REFERENCE.map(e => e.key);
const rendKeys = Object.keys(ARCH_RENDERERS);
// If an eighth topology is added and the guide is not updated, this fails —
// rather than silently shipping an incomplete reference for unsupervised users.
check(`all ${rendKeys.length} renderers documented, none orphaned`,
      rendKeys.every(k => refKeys.includes(k)) && refKeys.every(k => rendKeys.includes(k)));
for (const e of ARCH_REFERENCE) {
  const rows = ['when', 'size', 'triggers', 'examples', 'watch'];
  const missing = rows.filter(r => !e[r] || !String(e[r]).trim());
  check(`${e.key} card complete${missing.length ? ' — missing: ' + missing : ''}`,
        missing.length === 0 && e.prefill && e.prefill.drug);
}

section('icons are true cross-sections drawn by the real renderers');
for (const e of ARCH_REFERENCE) {
  const svg = archIconSVG(e);
  check(`${e.key} icon renders, label-free`,
        svg.startsWith('<svg') && svg.includes('</svg>') && !svg.includes('<text'));
}

section('"Load example" pins the topology it advertises');
// Without pinning, the model re-infers the architecture from the parameters and
// can pick a different one — which made the "Polymer Matrix" example generate
// a gated mesoporous particle.
renderArchRef();
ARCH_REFERENCE.forEach((e, i) => {
  loadArchExample(i);
  check(`${e.key} example pins archChoice=${e.key}`, store['archChoice'].value === e.key);
});
check('polymer_matrix example loads NO trigger (matrix release is erosion-controlled)',
      ARCH_REFERENCE.find(e => e.key === 'polymer_matrix').prefill.trigger.length === 0);

section('the traps survive');
const body = store['archModalBody'].innerHTML;
check('LNP: "no aqueous lumen"', body.includes('no aqueous lumen'));
check('matrix: "Release triggers do not apply"', body.includes('Release triggers do not apply'));
check('inorganic: "surface only"', body.includes('surface only'));
check('MSN: "No MSN drug is FDA-approved"', body.includes('No MSN drug is FDA-approved'));
check('micelle: size warning', body.includes('do not ask for 100+ nm'));

// readJSON is async, so these run last and the summary waits on them.
(async () => {
  section('readJSON turns non-JSON failures into actionable messages');
  // A bare resp.json() on an HTML body throws "Unexpected token '<'", which tells a
  // visitor nothing. On free hosting that spins down after idle, an HTML cold-start
  // or gateway page is the most likely non-JSON response, so it needs to say so.
  const resp = (status, body) => ({ status, text: async () => body });
  const caught = async (status, body) => {
    try { return { value: await readJSON(resp(status, body)) }; }
    catch (e) { return { error: e }; }
  };

  const okCase = await caught(200, '{"ok":true}');
  check('valid JSON parses unchanged', okCase.value && okCase.value.ok === true);

  const errCase = await caught(400, '{"ok":false,"error":"No API key provided."}');
  check('a JSON error body is returned, not thrown',
        errCase.value && errCase.value.error.includes('No API key'));

  const html = (await caught(200, '<!DOCTYPE html><html>Deploying</html>')).error;
  check('HTML body does NOT surface "Unexpected token"',
        html instanceof Error && !html.message.includes('Unexpected token'));
  check('HTML body says a page came back instead of a result',
        html.message.includes('web page instead of a result'));
  check('HTML body tells the user to wait and retry',
        html.message.toLowerCase().includes('try again'));

  for (const status of [502, 503, 504]) {
    const e = (await caught(status, '<html>Bad Gateway</html>')).error;
    check(`HTTP ${status} explains the spin-down / cold start`,
          e.message.includes('starting up') && e.message.includes(String(status)));
  }

  const garbage = (await caught(500, 'upstream connect error')).error;
  check('non-HTML garbage is quoted back verbatim',
        garbage.message.includes('upstream connect error') && garbage.message.includes('500'));

  section('inline source tags are resolved, not printed');
  // The model sometimes writes "[S5]" in prose rather than in the source field.
  // Left alone it renders literally, which reads as leaked debug output.
  const srcMap = { S4: { n: 6, url: 'u', title: 't' } };
  const resolved = claimSpan(
    { text: 'ZUP1-driven repair [S4] drives resistance.',
      provenance: 'grounded', source: 'S4' }, srcMap);
  check('a literal [S4] never reaches the page', !resolved.includes('[S4]'));
  check('it becomes a clickable superscript', resolved.includes('<sup><a class="cite-link"'));

  const dangling = claimSpan(
    { text: 'Toxicity in systemic regimens [S9] is a liability.',
      provenance: 'extrapolated', source: null }, srcMap);
  check('an unresolvable tag is removed', !dangling.includes('[S9]'));
  check('no space is left before punctuation', dangling.includes('regimens is'));

  const plain = claimSpan(
    { text: 'Size range [80-120 nm] is unaffected.',
      provenance: 'extrapolated', source: null }, srcMap);
  check('ordinary bracketed prose is untouched', plain.includes('[80-120 nm]'));

  console.log('\n' + (failures === 0
    ? '═══ ALL RENDER TESTS PASSED ═══'
    : `═══ ${failures} FAILURE(S) ═══`));
  process.exit(failures === 0 ? 0 : 1);
})();
