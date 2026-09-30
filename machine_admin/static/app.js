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

  var jobList = document.querySelector("[data-job-list]");
  if (jobList) {
    var listTimer; var listClosed = false; var listBusy = false; var listPointerDown = false;
    var listStatus = document.querySelector("[data-job-list-refresh-status]");
    async function refreshJobList() {
      if (listClosed || listBusy) return;
      window.clearTimeout(listTimer);
      listBusy = true;
      var controller = new AbortController();
      var timeout = window.setTimeout(function () { controller.abort(); }, 15000);
      try {
        var response = await fetch("/admin/jobs", {credentials:"same-origin", cache:"no-store", signal:controller.signal});
        if (response.status === 401 || (response.redirected && new URL(response.url).pathname === "/login")) {
          listClosed = true; throw new Error("Sua sessão expirou. Entre novamente para acompanhar.");
        }
        if (!response.ok) throw new Error("Não foi possível atualizar agora. Tentaremos novamente.");
        var page = new DOMParser().parseFromString(await response.text(), "text/html");
        var updated = page.querySelector("[data-job-list]");
        if (!updated) throw new Error("Não foi possível atualizar a lista. Atualize a página.");
        if (listClosed || listPointerDown) return;
        jobList.replaceChildren(...Array.from(updated.childNodes).map(function (node) { return document.importNode(node, true); }));
        applyFilters("jobs");
        listStatus.textContent = "Atualizado às " + new Date().toLocaleTimeString("pt-BR") + ". Próxima atualização em 5 segundos.";
      } catch (error) { listStatus.textContent = error.name === "AbortError" ? "Atualização lenta. Tentando novamente…" : error.message; }
      finally { window.clearTimeout(timeout); listBusy = false; if (!listClosed) listTimer = window.setTimeout(refreshJobList, 5000); }
    }
    document.querySelector("[data-job-list-refresh]").addEventListener("click", refreshJobList);
    jobList.addEventListener("pointerdown", function () { listPointerDown = true; });
    window.addEventListener("pointerup", function () { listPointerDown = false; });
    window.addEventListener("pointercancel", function () { listPointerDown = false; });
    jobList.addEventListener("submit", function (event) {
      if (event.defaultPrevented || !event.target.matches("[data-job-list-pause]")) return;
      listClosed = true; window.clearTimeout(listTimer);
      event.target.querySelector("button").disabled = true;
      listStatus.textContent = "Solicitando pausa. Aguarde a confirmação…";
    });
    window.addEventListener("pagehide", function () { listClosed = true; window.clearTimeout(listTimer); });
    refreshJobList();
  }
})();

(function () {
  "use strict";
  var labels = {queued: "Na fila", running: "Em execução", pausing: "Pausando", paused: "Pausado", cancelling: "Interrompendo", blocked: "Bloqueado", completed: "Concluído", completed_with_errors: "Concluído com alertas", failed: "Com falhas", cancelled: "Interrompido", found: "Encontrado", not_found: "Não encontrado", retryable_error: "Erro recuperável", permanent_error: "Erro permanente", credential_error: "Erro de acesso", portal_unavailable: "Portal indisponível", integration_unavailable: "Integração indisponível", active: "Disponível", cooldown: "Em espera", invalid: "Acesso inválido", disabled: "Desativado", pending: "Pendente", leased: "Em consulta"};
  function el(tag, text, className) { var node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
  function date(value, timezone) { if (!value) return "—"; var parsed = new Date(value); if (Number.isNaN(parsed.getTime())) return value; return parsed.toLocaleString("pt-BR", timezone ? {timeZone: timezone} : undefined); }
  document.querySelectorAll("[data-local-time]").forEach(function (node) { node.textContent = date(node.dataset.localTime, node.dataset.timezone); });

  document.querySelectorAll("[data-account-selector]").forEach(function (selector) {
    var dataset = selector.querySelector("[data-dataset-select]");
    var limit = selector.querySelector("[data-parallel-limit]");
    var form = selector.closest("form");
    var submit = form.querySelector("[data-selection-submit]");
    var previousMunicipality = null;
    function update() {
      var option = dataset.options[dataset.selectedIndex];
      var municipality = option ? option.dataset.municipality : null;
      var available = 0; var chosen = 0;
      selector.querySelectorAll("[data-account-option]").forEach(function (row) {
        var checkbox = row.querySelector("input");
        var matching = municipality && row.dataset.municipality === municipality;
        row.hidden = !matching;
        checkbox.disabled = !matching || checkbox.dataset.usable !== "true";
        if (!matching) checkbox.checked = false;
        if (matching && !checkbox.disabled) {
          available += 1;
          if (municipality !== previousMunicipality) checkbox.checked = true;
          if (checkbox.checked) chosen += 1;
        }
      });
      previousMunicipality = municipality;
      var maximum = Math.min(chosen, Number(option && option.dataset.limit || 1));
      limit.max = String(Math.max(1, maximum));
      limit.value = String(Math.max(1, Math.min(Number(limit.value) || 1, maximum || 1)));
      selector.querySelector("[data-no-accounts]").textContent = !municipality ? "Escolha a base para ver os acessos correspondentes." : (!available ? "Nenhum acesso utilizável. Cadastre ou corrija um acesso deste convênio." : "");
      selector.querySelector("[data-selection-summary]").textContent = municipality ? chosen + " acesso(s) selecionado(s) · até " + (maximum ? limit.value : 0) + " simultâneo(s) · " + (option.dataset.count || 0) + " registros na mesma fila." : "Selecione a base e pelo menos um acesso.";
      selector.querySelector("[data-capacity-note]").hidden = !municipality;
      selector.querySelector("[data-capacity-message]").textContent = "O convênio permite no máximo " + Number(option && option.dataset.limit || 1) + " acesso(s) simultâneo(s).";
      submit.disabled = !municipality || chosen < 1;
    }
    selector.addEventListener("change", update);
    selector.addEventListener("input", function (event) { if (event.target === limit) update(); });
    update();
    form.addEventListener("submit", function () { submit.disabled = true; submit.textContent = "Salvando…"; });
  });
  document.querySelectorAll("[data-cron-preset]").forEach(function (preset) {
    var expression = preset.closest("form").querySelector("[data-cron-expression]");
    preset.addEventListener("change", function () { if (preset.value) expression.value = preset.value; else expression.focus(); });
    expression.addEventListener("input", function () { preset.value = ""; });
  });

  var monitor = document.querySelector("[data-job-monitor]");
  if (!monitor) return;
  var jobId = monitor.dataset.jobId;
  var base = "/admin/consultations/" + jobId;
  var closed = false; var timer; var resultCursor = 0; var resultBusy = false; var resultLoaded = false; var lastResultRefresh = 0;
  async function json(url) {
    var controller = new AbortController();
    var timeout = window.setTimeout(function () { controller.abort(); }, 15000);
    try {
      var response = await fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}, cache: "no-store", signal: controller.signal});
      if (!response.ok) throw new Error(response.status === 401 ? "Sua sessão expirou. Entre novamente para acompanhar." : "Não foi possível atualizar agora (HTTP " + response.status + ").");
      return await response.json();
    } finally { window.clearTimeout(timeout); }
  }
  function updateSummary(summary) {
    var status = monitor.querySelector("[data-job-status]"); status.replaceChildren(el("span", labels[summary.status] || summary.status, "status " + summary.status.replaceAll("_", "-")));
    monitor.querySelectorAll("[data-job-metric]").forEach(function (node) { node.textContent = summary[node.dataset.jobMetric] || 0; });
    var processed = summary.completed + summary.failed;
    monitor.querySelector("[data-job-progress]").style.width = (summary.total ? Math.min(100, processed * 100 / summary.total) : 0) + "%";
    monitor.querySelector("[data-job-progress-label]").textContent = processed + " de " + summary.total + " processados";
    var execution = summary.execution || {};
    var capacity = summary.capacity || {};
    var capacityNote = monitor.querySelector("[data-job-capacity]");
    if (capacityNote) capacityNote.textContent = "Solicitado: " + summary.max_parallel_accounts + " simultâneo(s). Disponível agora: até " + (capacity.effective_limit || 0) + ". Limite do convênio: " + (capacity.agreement_limit || 0) + ".";
    monitor.querySelector("[data-job-reason-title]").textContent = execution.executable ? (summary.status === "running" ? "Em execução" : "Pronta para iniciar") : "Estado da execução";
    monitor.querySelector("[data-job-reason-text]").textContent = execution.reason || labels[summary.status] || summary.status;
    var draining = monitor.querySelector("[data-job-draining]");
    if (draining) {
      draining.hidden = !["pausing", "cancelling"].includes(summary.status);
      var openAccounts = (summary.accounts || []).filter(function (account) { return account.in_use && account.current_job_id === Number(jobId); });
      draining.textContent = "Pedido recebido. " + (openAccounts.length ? "Encerrando " + openAccounts.length + " sessão(ões) do portal com segurança." : "Aguardando a confirmação final do encerramento.") + " Os resultados já obtidos serão preservados. Não é necessário clicar novamente.";
    }
    monitor.querySelectorAll("[data-job-action]").forEach(function (node) { node.hidden = !node.dataset.states.split(",").includes(summary.status); });
    var accounts = monitor.querySelector("[data-account-rows]"); accounts.replaceChildren();
    (summary.accounts || []).forEach(function (account) {
      var row = el("tr");
      var state = account.current_job_id === Number(jobId) ? "Em uso nesta consulta" : (account.in_use ? (account.current_job_id ? "Em uso na consulta #" + account.current_job_id : "Testando acesso") : (account.available ? "Disponível" : labels[account.status] || account.status));
      row.append(el("td", account.label), el("td", state), el("td", account.completed), el("td", account.last_error || "—", "message-cell")); accounts.append(row);
    });
    if (!summary.accounts || !summary.accounts.length) { var empty = el("tr"); var cell = el("td", "Nenhum acesso vinculado à consulta."); cell.colSpan = 4; empty.append(cell); accounts.append(empty); }
    var exports = monitor.querySelector("[data-export-rows]");
    if (exports) {
      exports.replaceChildren();
      (summary.exports || []).forEach(function (artifact) {
        var row = el("tr"); var filename = el("td", artifact.filename);
        if (artifact.partial) filename.append(el("small", "Resultado parcial", "cell-note"));
        if (artifact.error_message) filename.append(el("small", artifact.error_message, "cell-note failure-count"));
        var action = el("td");
        if (artifact.download_url && exports.dataset.canDownload === "true") { var link = el("a", "Baixar", "button small"); link.href = artifact.download_url; action.append(link); }
        row.append(filename, el("td", {queued:"Na fila",building:"Preparando",ready:"Pronto",failed:"Falhou"}[artifact.status] || artifact.status), el("td", artifact.row_count), el("td", date(artifact.created_at)), action); exports.append(row);
      });
      if (!(summary.exports || []).length) { var noExports = el("tr"); var emptyExports = el("td", "Nenhum arquivo solicitado. Selecione o formato e clique em Preparar arquivo."); emptyExports.colSpan = 5; noExports.append(emptyExports); exports.append(noExports); }
    }
    var events = monitor.querySelector("[data-event-list]"); events.replaceChildren();
    (summary.events || []).forEach(function (event) { var row = el("li"); row.append(el("time", date(event.at)), el("span", event.message)); events.append(row); });
    if (!summary.events || !summary.events.length) events.append(el("li", "Aguardando o primeiro evento."));
    monitor.querySelector("[data-refresh-status]").textContent = "Atualizado às " + new Date().toLocaleTimeString("pt-BR") + ". Próxima atualização em 5 segundos.";
  }
  async function loadResults(reset) {
    if (resultBusy) return;
    resultBusy = true;
    var status = monitor.querySelector("[data-results-status]"); var list = monitor.querySelector("[data-result-list]");
    status.textContent = "Atualizando resultados…";
    try {
      var outcome = monitor.querySelector("[data-result-outcome]").value;
      var data = await json(base + "/results?limit=25&after_id=" + (reset ? 0 : resultCursor) + (outcome ? "&outcome=" + encodeURIComponent(outcome) : ""));
      var items = data.items || data.results || [];
      if (reset) list.replaceChildren();
      items.forEach(function (item) {
        var result = item.result || item.result_data || item.data || {};
        var cpf = item.cpf || (result.requested && result.requested.cpf) || (result.confirmed && result.confirmed.cpf) || (item.cpf_last4 ? "***" + item.cpf_last4 : "Registro " + (item.item_id || item.id));
        var details = el("details", undefined, "result-item");
        var heading = el("summary"); heading.append(el("strong", cpf), el("span", labels[item.outcome || item.status] || item.outcome || item.status || "Resposta"));
        details.append(heading);
        if (item.error_message) details.append(el("p", item.error_message, "inline-warning"));
        details.append(el("pre", JSON.stringify(item, null, 2))); list.append(details);
      });
      if (!list.children.length) list.append(el("p", "Nenhum retorno para este filtro. A consulta pode estar aguardando login ou o primeiro registro.", "muted"));
      resultCursor = data.next_after_id || data.next_cursor || 0;
      monitor.querySelector("[data-results-next]").hidden = !resultCursor;
      status.textContent = items.length + " retorno(s) nesta página.";
      resultLoaded = true; lastResultRefresh = Date.now();
    } catch (error) { status.textContent = error.name === "AbortError" ? "A atualização demorou mais que o esperado. Tente novamente." : error.message; }
    finally { resultBusy = false; }
  }
  async function poll() {
    if (closed) return;
    try { updateSummary(await json(base + "/status")); if (!resultLoaded || (Date.now() - lastResultRefresh > 30000 && !monitor.querySelector(".result-item[open]"))) await loadResults(true); }
    catch (error) { monitor.querySelector("[data-refresh-status]").textContent = error.name === "AbortError" ? "Atualização lenta. Tentando novamente…" : error.message; }
    finally { if (!closed) timer = window.setTimeout(poll, 5000); }
  }
  monitor.querySelector("[data-results-refresh]").addEventListener("click", function () { loadResults(true); });
  monitor.querySelector("[data-result-outcome]").addEventListener("change", function () { loadResults(true); });
  monitor.querySelector("[data-results-next]").addEventListener("click", function () { loadResults(false); });
  monitor.querySelectorAll("form[data-job-action]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      monitor.querySelectorAll("form[data-job-action] button").forEach(function (button) { button.disabled = true; });
      monitor.querySelector("[data-refresh-status]").textContent = "Enviando pedido. Aguarde a confirmação…";
    });
  });
  window.addEventListener("pagehide", function () { closed = true; window.clearTimeout(timer); });
  poll();
})();

(function () {
  "use strict";
  var labels = {queued:"Na fila", running:"Testando login", cancelling:"Cancelando teste", cancelled:"Teste cancelado", success:"Login confirmado", failed:"Login não confirmado"};
  function time(value) { return value ? new Date(value).toLocaleString("pt-BR") : ""; }
  function node(tag, text) { var result = document.createElement(tag); result.textContent = text; return result; }
  document.querySelectorAll("[data-access-check-monitor]").forEach(function (panel) {
    var credentialId = panel.dataset.credentialId;
    var timer; var closed = false; var submitting = false;
    var start = panel.querySelector("[data-access-check-start-button]");
    var cancelForm = panel.querySelector("[data-access-check-cancel]");
    var refresh = panel.querySelector("[data-access-check-refresh]");
    function update(data) {
      var latest = data.latest;
      panel.querySelector("[data-access-check-state]").textContent = latest ? (labels[latest.status] || latest.status) : "Nenhum teste solicitado";
      panel.querySelector("[data-access-check-message]").textContent = latest ? (latest.message || "") : "O teste verifica somente o login, sem consultar a base.";
      var stamps = [];
      if (latest) { if (latest.created_at) stamps.push("Solicitado: " + time(latest.created_at)); if (latest.started_at) stamps.push("Iniciado: " + time(latest.started_at)); if (latest.finished_at) stamps.push("Finalizado: " + time(latest.finished_at)); }
      panel.querySelector("[data-access-check-time]").textContent = stamps.join(" · ");
      var code = panel.querySelector("[data-access-check-code]");
      code.hidden = !latest || !latest.error_code;
      code.textContent = latest && latest.error_code ? "Código do diagnóstico: " + latest.error_code : "";
      var blocking = panel.querySelector("[data-access-check-blocking-job]");
      blocking.hidden = !latest || !latest.blocking_job_id;
      if (latest && latest.blocking_job_id) { blocking.href = "/admin/consultations/" + latest.blocking_job_id; blocking.textContent = "Abrir consulta #" + latest.blocking_job_id; }
      start.disabled = submitting || !data.can_test;
      cancelForm.hidden = !latest || !latest.can_cancel;
      if (latest) cancelForm.action = "/admin/credentials/" + credentialId + "/tests/" + latest.id + "/cancel";
      var reason = data.reason || "Testa login; pode consumir um captcha, sem consultar a base.";
      if (latest && latest.session_closing) reason = "Encerrando a sessão do portal com segurança. Aguarde a liberação do acesso; não é necessário clicar novamente.";
      if (data.retry_after_seconds > 0 && !(latest && ["queued", "running", "cancelling"].includes(latest.status))) reason += " Novo teste em aproximadamente " + Math.ceil(data.retry_after_seconds / 60) + " minuto(s).";
      panel.querySelector("[data-access-check-reason]").textContent = reason;
      var history = panel.querySelector("[data-access-check-history]");
      if (history) {
        history.replaceChildren();
        (data.checks || []).forEach(function (check) { var entry = node("li", ""); entry.append(node("strong", labels[check.status] || check.status), node("time", time(check.created_at)), node("span", check.message || "")); history.append(entry); });
        if (!history.children.length) history.append(node("li", "Nenhum teste solicitado."));
      }
      refresh.textContent = "Atualizado às " + new Date().toLocaleTimeString("pt-BR") + ". Atualização automática a cada 5 segundos.";
    }
    async function poll() {
      if (closed || submitting) return;
      var controller = new AbortController();
      var timeout = window.setTimeout(function () { controller.abort(); }, 15000);
      try {
        var response = await fetch("/admin/credentials/" + credentialId + "/test-status", {credentials:"same-origin", cache:"no-store", headers:{Accept:"application/json"}, signal:controller.signal});
        if ([401, 403].includes(response.status) || (response.redirected && new URL(response.url).pathname === "/login")) { closed = true; throw new Error("Entre novamente com um usuário administrador para acompanhar o teste."); }
        if (!response.ok) throw new Error("Não foi possível atualizar o teste agora. Tentaremos novamente.");
        update(await response.json());
      } catch (error) { refresh.textContent = error.name === "AbortError" ? "Atualização lenta. Tentando novamente…" : error.message; }
      finally { window.clearTimeout(timeout); if (!closed && !submitting) timer = window.setTimeout(poll, 5000); }
    }
    panel.querySelectorAll("form").forEach(function (form) { form.addEventListener("submit", function (event) { if (event.defaultPrevented) return; submitting = true; window.clearTimeout(timer); panel.querySelectorAll("button").forEach(function (button) { button.disabled = true; }); refresh.textContent = "Enviando pedido. Aguarde a confirmação…"; }); });
    window.addEventListener("pagehide", function () { closed = true; window.clearTimeout(timer); });
    poll();
  });
})();
