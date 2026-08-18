(function () {
  "use strict";

  function normalise(value) {
    return (value || "")
      .toString()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .trim();
  }

  function applyFilters(target) {
    var rows = Array.from(document.querySelectorAll('[data-filter-row="' + target + '"]'));
    var controls = Array.from(document.querySelectorAll('[data-filter-target="' + target + '"]'));
    var visible = 0;

    rows.forEach(function (row) {
      var matches = controls.every(function (control) {
        var query = normalise(control.value);
        if (!query) return true;
        var field = control.dataset.filterField || "text";
        return normalise(row.dataset[field]).indexOf(query) !== -1;
      });
      row.hidden = !matches;
      if (matches) visible += 1;
    });

    document.querySelectorAll('[data-filter-count="' + target + '"]').forEach(function (node) {
      node.textContent = visible + (visible === 1 ? " resultado" : " resultados");
    });
    document.querySelectorAll('[data-filter-empty="' + target + '"]').forEach(function (node) {
      node.hidden = rows.length === 0 || visible !== 0;
    });
    document.querySelectorAll('[data-filter-group="' + target + '"]').forEach(function (group) {
      var groupRows = Array.from(group.querySelectorAll('[data-filter-row="' + target + '"]'));
      group.hidden = groupRows.length > 0 && groupRows.every(function (row) { return row.hidden; });
    });
  }

  document.querySelectorAll("[data-filter-target]").forEach(function (control) {
    ["input", "change"].forEach(function (eventName) {
      control.addEventListener(eventName, function () {
        applyFilters(control.dataset.filterTarget);
      });
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-reveal-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.dataset.revealTarget);
      if (!input) return;
      var reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.setAttribute("aria-pressed", reveal ? "true" : "false");
      button.textContent = reveal ? "Ocultar" : "Mostrar";
    });
  });

  var parameters = new URLSearchParams(window.location.search);
  if (parameters.has("job")) {
    var jobFilter = document.querySelector('[data-url-filter="job"]');
    if (jobFilter) {
      jobFilter.value = "#" + parameters.get("job");
      applyFilters(jobFilter.dataset.filterTarget);
    }
  }

  document.querySelectorAll("[data-filter-target]").forEach(function (control) {
    applyFilters(control.dataset.filterTarget);
  });
})();
