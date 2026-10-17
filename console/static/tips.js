// One floating tooltip for every .tip button. It lives on <body>, so the
// overflow of a scrolling table never clips it, and the listeners sit on
// document, so tips in regions htmx swaps in work without rebinding.
(() => {
  const GUTTER = 16;
  const GAP = 8;
  let texts = {};
  fetch("/glossary.json").then((r) => r.json()).then((t) => { texts = t; });
  const box = document.createElement("div");
  box.id = "tooltip";
  box.className = "tooltip";
  box.setAttribute("role", "tooltip");
  box.hidden = true;
  document.body.append(box);
  let active = null;

  function place(trigger) {
    const r = trigger.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    box.style.left = "0px";
    box.style.top = "0px";
    const w = box.offsetWidth;
    const h = box.offsetHeight;
    let top = r.bottom + GAP;
    if (top + h > vh - GAP && r.top - GAP - h >= GAP) top = r.top - GAP - h;
    const left = Math.max(GUTTER, Math.min(r.left + r.width / 2 - w / 2, vw - GUTTER - w));
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  }

  function show(trigger) {
    if (active && active !== trigger) active.removeAttribute("aria-describedby");
    active = trigger;
    box.textContent = texts[trigger.dataset.tip] ?? "";
    box.hidden = false;
    trigger.setAttribute("aria-describedby", box.id);
    place(trigger);
  }

  function hide() {
    if (!active) return;
    active.removeAttribute("aria-describedby");
    active = null;
    box.hidden = true;
  }

  const tipOf = (e) => (e.target instanceof Element ? e.target.closest(".tip") : null);

  document.addEventListener("mouseover", (e) => { const t = tipOf(e); if (t) show(t); });
  document.addEventListener("mouseout", (e) => { if (active && tipOf(e) === active) hide(); });
  document.addEventListener("focusin", (e) => { const t = tipOf(e); if (t) show(t); });
  document.addEventListener("focusout", (e) => { if (active && tipOf(e) === active) hide(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  document.addEventListener("htmx:afterSwap", () => { if (active && !active.isConnected) hide(); });
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
