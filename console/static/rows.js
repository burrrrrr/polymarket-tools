// The expand buttons of the Live markets table. A row is open while its
// detail row exists, so the button adds one at once and then loads it: a
// refresh that lands meanwhile sends that row as open and fills it in.
document.addEventListener("click", (e) => {
  const button = e.target instanceof Element ? e.target.closest("button.expand") : null;
  if (!button) return;
  const row = button.closest("tr");
  const cid = row.dataset.conditionId;
  const shown = document.getElementById(`detail-${cid}`);
  const open = !shown;
  button.setAttribute("aria-expanded", String(open));
  button.setAttribute("aria-label", button.getAttribute("aria-label").replace(/^\w+/, open ? "Collapse" : "Expand"));
  if (shown) {
    shown.remove();
    return;
  }
  const detail = document.createElement("tr");
  detail.id = `detail-${cid}`;
  detail.className = "detail";
  detail.dataset.conditionId = cid;
  detail.innerHTML = `<td colspan="${row.cells.length}" class="muted">Loading…</td>`;
  row.after(detail);
  htmx.ajax("GET", `/partials/markets/${cid}/card`, { target: detail, swap: "outerHTML" });
});
