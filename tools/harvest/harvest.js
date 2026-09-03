// HARVEST — paste into DevTools console on chess.com/analysis with Game Review open.
// 1. Right-click the review panel → Inspect → right-click the element → Copy → Copy selector
// 2. Set ROOT to that selector (or leave "body" and it grabs everything — big file)
// 3. Paste this whole file, press Enter. A JSON file downloads. Send it to me.
(() => {
  const ROOT = ".sidebar-tab-content-component.sidebar-view-content";                       // <-- change to the review panel selector
  const PROPS = ["display","position","flexDirection","alignItems","justifyContent","gap","gridTemplateColumns",
    "width","height","minWidth","maxWidth","paddingTop","paddingRight","paddingBottom","paddingLeft",
    "marginTop","marginRight","marginBottom","marginLeft","color","backgroundColor","backgroundImage",
    "borderTop","borderRight","borderBottom","borderLeft","borderRadius","boxShadow","opacity",
    "fontFamily","fontSize","fontWeight","lineHeight","letterSpacing","textTransform","textAlign",
    "cursor","overflow","zIndex","transition"];
  const MAX = 4000; let n = 0;
  const walk = el => {
    if (n++ > MAX || el.nodeType !== 1) return null;
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    const st = {}; for (const p of PROPS) st[p] = cs[p];
    const txt = [...el.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent.trim()).filter(Boolean).join(" ");
    const node = { tag: el.tagName.toLowerCase(), cls: el.className?.baseVal ?? el.className ?? "", id: el.id,
      rect: [r.x|0, r.y|0, r.width|0, r.height|0], st, txt: txt.slice(0,120) };
    if (el.tagName === "IMG") node.src = el.currentSrc;
    if (el.tagName === "svg") node.svg = el.outerHTML.slice(0, 5000);
    if (el.tagName === "USE") node.href = el.getAttribute("href") || el.getAttribute("xlink:href");
    node.ch = [...el.children].map(walk).filter(Boolean);
    return node;
  };
  const root = document.querySelector(ROOT);
  const tree = walk(root);
  // fonts + stylesheet asset urls
  const fonts = [], urls = new Set();
  for (const ss of document.styleSheets) { try { for (const rule of ss.cssRules) {
    if (rule instanceof CSSFontFaceRule) fonts.push(rule.cssText);
    const m = rule.cssText.match(/url\(([^)]+)\)/g); if (m) m.forEach(u => urls.add(u));
  } } catch (e) {} }
  const out = { url: location.href, viewport: [innerWidth, innerHeight], dpr: devicePixelRatio, root: ROOT, tree, fonts, urls: [...urls].slice(0, 500) };
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out)], { type: "application/json" }));
  a.download = `harvest_${Date.now()}.json`; a.click();
  console.log(`harvested ${n} nodes, ${fonts.length} @font-face rules, ${urls.size} asset urls`);
})();
