// The - and + buttons beside a number input. Each steps its input by the
// input's own step, or fills an empty input with its data-start, and fires
// "input", so the htmx previews that listen for typing refresh the same way.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-step]");
  if (!button) return;
  const input = document.getElementById(button.getAttribute("aria-controls"));
  if (input.value === "" && input.dataset.start) input.value = input.dataset.start;
  else if (button.dataset.step === "up") input.stepUp();
  else input.stepDown();
  input.dispatchEvent(new Event("input", { bubbles: true }));
});

// A checkbox with data-disables turns off that input and its - and +
// buttons while it is ticked, so the form sends no value for it.
document.addEventListener("change", (event) => {
  const box = event.target.closest("input[data-disables]");
  if (!box) return;
  const id = box.dataset.disables;
  for (const el of document.querySelectorAll(`#${id}, button[aria-controls="${id}"]`)) {
    el.disabled = box.checked;
  }
});
