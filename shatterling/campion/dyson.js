/* clm-mem "Dyson build" — a tiny design language for progress states.
 *
 * Metaphor: the logo is a Dyson shell under construction around a star.
 *   grey    #9aa3ad / #6e7a89  = unfinished structure (track, in-flight panels)
 *   purple  #7c3aed             = built shell
 *   lilac   #c084fc             = lit core / energy released
 *
 * States:
 *   indeterminate  -> animated loader (/dyson.svg): grey panels orbit a tilted belt
 *                     around the purple shell; SMIL, animates inside <img>.
 *   determinate    -> grey track ring + purple arc growing clockwise from 12 o'clock
 *                     (fraction built), core grows brighter with p in [0,1].
 *   complete       -> end pose (/dyson-done.svg): all panels fused into a closed
 *                     shell, grey seams mark the panel joints, core fully lit.
 */
"use strict";
(function () {
  const PUR = "#7c3aed", HI = "#c084fc", GRY = "#9aa3ad", TRK = "#2d3540";
  const NS = "http://www.w3.org/2000/svg";

  function spinner(size) {                       // indeterminate loader
    const i = document.createElement("img");
    i.src = "/dyson.svg"; i.width = size; i.height = size;
    i.className = "dyson-spin"; i.alt = "";
    return i;
  }

  function done(size) {                           // end position (static pose)
    const i = document.createElement("img");
    i.src = "/dyson-done.svg"; i.width = size; i.height = size;
    i.className = "dyson-done"; i.alt = "";
    return i;
  }

  /* determinate: fraction-built ring. p in [0,1]. */
  function progress(size, p) {
    p = Math.max(0, Math.min(1, p || 0));
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("width", size); s.setAttribute("height", size);
    s.setAttribute("class", "dyson-prog");
    const g = document.createElementNS(NS, "g");
    g.setAttribute("transform", "translate(12,12)");
    const mk = (n, at) => { const e = document.createElementNS(NS, n);
      for (const k in at) e.setAttribute(k, at[k]); return e; };
    // unbuilt track
    g.appendChild(mk("circle", { r: 8.6, fill: "none", stroke: TRK, "stroke-width": 3 }));
    // built arc, clockwise from 12 o'clock
    const C = 2 * Math.PI * 8.6;
    if (p > 0) {
      g.appendChild(mk("circle", {
        r: 8.6, fill: "none", stroke: PUR, "stroke-width": 3,
        "stroke-dasharray": `${(C * p).toFixed(3)} ${C.toFixed(3)}`,
        transform: "rotate(-90)" }));
    }
    // core grows brighter with progress
    g.appendChild(mk("circle", { r: (1 + 4.5 * p).toFixed(2), fill: HI }));
    s.appendChild(g);
    return s;
  }

  /* swap helper: replace a spinner with the done pose (+flash), then optional cb */
  function finish(node, size, cb) {
    const d = done(size || node.width || 24);
    d.classList.add("flash");
    node.replaceWith ? node.replaceWith(d) : node.parentNode.replaceChild(d, node);
    if (cb) setTimeout(cb, 500);
    return d;
  }

  window.dyson = { spinner, progress, done, finish };
})();
