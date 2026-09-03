// ASSERT — run in DevTools console on the CLONE page.
// Usage: paste the harvest JSON into the variable below (or load it: fetch('harvest_desktop.json').then(r=>r.json()).then(h=>assertStyles(h)))
// Matches clone nodes to harvest nodes by data-h attribute (set in the clone: <div data-h="panel.acc.num">) — OR by identical tag+text path if absent.
// Prints a table of mismatches: selector, property, expected, actual.

function assertStyles(harvest, opts = {}) {
  const TOL = opts.tolerancePx ?? 1;          // px tolerance for spacing/size
  const PROPS = opts.props ?? Object.keys(harvest.tree.st);
  const norm = v => (v ?? "").toString().trim().replace(/\s+/g, " ");
  const px = v => { const m = /^(-?[\d.]+)px$/.exec(v); return m ? parseFloat(m[1]) : null; };
  const rgb = v => { const m = /rgba?\(([^)]+)\)/.exec(v); return m ? m[1].split(",").map(Number) : null; };
  const same = (p, e, a) => {
    e = norm(e); a = norm(a); if (e === a) return true;
    const pe = px(e), pa = px(a); if (pe !== null && pa !== null) return Math.abs(pe - pa) <= TOL;
    const ce = rgb(e), ca = rgb(a); if (ce && ca) return ce.every((x, i) => Math.abs(x - (ca[i] ?? 1)) <= 2);
    if (p === "fontFamily") return e.split(",")[0].replace(/"/g, "") === a.split(",")[0].replace(/"/g, "");
    return false;
  };
  const fails = []; let checked = 0;
  // index clone nodes by data-h path
  const byPath = {}; document.querySelectorAll("[data-h]").forEach(el => byPath[el.dataset.h] = el);
  const walk = (h, path) => {
    const key = h.h || path;                       // harvest nodes can carry an "h" path if you annotate them
    const el = byPath[key];
    if (el) {
      const cs = getComputedStyle(el);
      for (const p of PROPS) { checked++; if (!same(p, h.st[p], cs[p])) fails.push({ node: key, prop: p, expected: norm(h.st[p]), actual: norm(cs[p]) }); }
      const r = el.getBoundingClientRect();
      ["width","height"].forEach((k, i) => { const e = h.rect[2 + i], a = r[k]; if (Math.abs(e - a) > TOL) fails.push({ node: key, prop: "rect." + k, expected: e, actual: Math.round(a) }); });
    }
    (h.ch || []).forEach((c, i) => walk(c, path + "/" + i));
  };
  walk(harvest.tree, "0");
  console.log(`checked ${checked} assertions on ${Object.keys(byPath).length} mapped nodes — ${fails.length} failures`);
  if (fails.length) console.table(fails);
  return fails;
}
