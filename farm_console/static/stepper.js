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
