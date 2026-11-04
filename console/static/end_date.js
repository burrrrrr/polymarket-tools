// The end date override picker: each day button holds its end time, and the
// custom box counts days from the page's data-now. Either fills the hidden
// end_date_override input and fires "input", so the htmx previews refresh
// as they do for typing. Listed clears it.
function chooseEnd(picker, value, chosen) {
  const input = document.getElementById(picker.dataset.endPicker);
  input.value = value;
  for (const button of picker.querySelectorAll("button[data-end]")) {
    button.classList.toggle("on", button === chosen);
  }
  const summary = document.getElementById(`${input.id}-summary`);
  summary.textContent = value
    ? `ends ${new Date(value).toISOString().slice(0, 16).replace("T", " ")} UTC`
    : "the listed end date";
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-end-picker] button[data-end]");
  if (!button) return;
  const picker = button.closest("[data-end-picker]");
  picker.querySelector("[data-end-days]").value = "";
  chooseEnd(picker, button.dataset.end, button);
});

document.addEventListener("input", (event) => {
  const box = event.target.closest("[data-end-picker] input[data-end-days]");
  if (!box) return;
  const picker = box.closest("[data-end-picker]");
  const days = Number(box.value);
  if (Number.isInteger(days) && days > 0) {
    const at = new Date(Date.parse(picker.dataset.now) + days * 86400000);
    chooseEnd(picker, at.toISOString(), null);
  } else {
    chooseEnd(picker, "", picker.querySelector('button[data-end=""]'));
  }
});
