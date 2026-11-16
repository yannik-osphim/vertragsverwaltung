(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var form = document.querySelector(".time-booking-form");
    if (!form) return;

    var contract = form.elements.contract_id;
    var workItem = form.elements.work_item_key;
    var amount = form.elements.hours;
    var start = form.elements.start_date;
    var end = form.elements.end_date;
    var dayHours = Number(form.dataset.workdayHours);
    var inDays = form.dataset.workUnit === "day";
    var detail = form.querySelector("[data-work-item-detail]");
    var templates = new Map();
    form.querySelectorAll("template[data-work-item]").forEach(function (template) {
      templates.set(template.dataset.workItem, template);
    });
    var previousStart = start.value;
    var previousItem;
    var numberFormat = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

    function updateWorkItem() {
      var template = templates.get(workItem.value);
      var valid = template && template.dataset.contractId === contract.value;
      form.querySelector("[data-booking-empty]").hidden = Boolean(valid);
      form.querySelector("[data-booking-submit]").disabled = !valid;
      var selection = valid ? workItem.value : "";
      if (selection === previousItem) return;
      previousItem = selection;
      detail.replaceChildren();
      if (valid) {
        detail.appendChild(template.content.cloneNode(true));
      } else {
        var empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "Keine Leistung ausgew\u00e4hlt.";
        detail.appendChild(empty);
      }
    }

    function updateAmount() {
      var raw = amount.value.trim();
      var value = Number(raw.replace(",", "."));
      var valid = /^(?:\d+(?:[.,]\d{0,2})?|[.,]\d{1,2})$/.test(raw) && Number.isFinite(value) && value > 0;
      amount.setCustomValidity(raw && !valid ? "Bitte einen positiven Aufwand mit maximal zwei Nachkommastellen angeben." : "");
      var equivalent = inDays ? value * dayHours : value / dayHours;
      form.querySelector("[data-work-equivalent]").textContent = valid
        ? numberFormat.format(equivalent) + (inDays ? " Stunden" : " Arbeitstage") : "\u2013";
      form.querySelector("[data-work-total]").textContent = valid
        ? numberFormat.format(value) + (inDays ? " AT" : " h") : "\u2013";
    }

    function validateDates() {
      end.min = start.value;
      end.setCustomValidity(start.value && end.value && end.value < start.value
        ? "Das Enddatum darf nicht vor dem Startdatum liegen." : "");
    }

    contract.addEventListener("change", updateWorkItem);
    workItem.addEventListener("change", updateWorkItem);
    amount.addEventListener("input", updateAmount);
    start.addEventListener("change", function () {
      if (!end.value || end.value === previousStart || end.value < start.value) end.value = start.value;
      previousStart = start.value;
      validateDates();
    });
    end.addEventListener("change", validateDates);
    form.addEventListener("reset", function () {
      window.setTimeout(function () { updateWorkItem(); updateAmount(); validateDates(); }, 0);
    });
    updateWorkItem();
    updateAmount();
    validateDates();
  });
})();
