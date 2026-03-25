(() => {
  function initToasts() {
    const container = document.querySelector("[data-toast-container]");
    const alerts = document.querySelector(".bo-alert-spacer");
    if (!container) return;
    if (alerts) alerts.style.display = "none";
    const toastEls = container.querySelectorAll(".toast");
    toastEls.forEach((el) => {
      const isError = el.classList.contains("text-bg-danger");
      const toast = new bootstrap.Toast(el, {
        autohide: !isError,
        delay: isError ? 8000 : 5000,
      });
      toast.show();
    });
  }

  function initConfirmModal() {
    const modalEl = document.getElementById("boConfirmModal");
    if (!modalEl) return;
    const modal = new bootstrap.Modal(modalEl);
    const acceptBtn = modalEl.querySelector("[data-confirm-accept]");
    let pendingAction = null;

    document.addEventListener("click", (event) => {
      const target = event.target.closest("[data-confirm]");
      if (!target) return;
      event.preventDefault();
      const text = target.getAttribute("data-confirm") || "Tens a certeza que queres continuar?";
      modalEl.querySelector(".bo-confirm-text").textContent = text;
      pendingAction = target;
      modal.show();
    });

    acceptBtn.addEventListener("click", () => {
      if (!pendingAction) return;
      if (pendingAction.tagName === "A") {
        window.location.href = pendingAction.getAttribute("href");
      } else if (pendingAction.form) {
        pendingAction.form.submit();
      } else {
        pendingAction.click();
      }
      modal.hide();
    });
  }

  function initSelectAll() {
    document.querySelectorAll("[data-select-all]").forEach((toggle) => {
      const table = toggle.closest("table");
      if (!table) return;
      const checkboxes = table.querySelectorAll("tbody [data-select-row]");
      const countEls = document.querySelectorAll("[data-selected-count]");

      function updateCount() {
        const count = Array.from(checkboxes).filter((cb) => cb.checked).length;
        countEls.forEach((el) => {
          el.textContent = String(count);
        });
        if (checkboxes.length) {
          toggle.checked = count === checkboxes.length;
          toggle.indeterminate = count > 0 && count < checkboxes.length;
        }
      }

      toggle.addEventListener("change", () => {
        checkboxes.forEach((cb) => {
          cb.checked = toggle.checked;
        });
        updateCount();
      });

      checkboxes.forEach((cb) => cb.addEventListener("change", updateCount));
      updateCount();
    });
  }

  function initAutoSubmitFilters() {
    document.querySelectorAll("form[data-auto-submit='1']").forEach((form) => {
      form.querySelectorAll("select[data-auto-submit]").forEach((select) => {
        select.addEventListener("change", () => {
          const pageInput = form.querySelector("input[name='page']");
          if (pageInput) pageInput.remove();
          form.submit();
        });
      });
    });
  }

  function initFormLoading() {
    document.querySelectorAll("form").forEach((form) => {
      form.addEventListener("submit", () => {
        const submits = form.querySelectorAll("[type='submit']");
        submits.forEach((btn) => {
          btn.disabled = true;
          if (!btn.dataset.originalText) {
            btn.dataset.originalText = btn.textContent;
          }
          if (btn.classList.contains("btn-primary")) {
            btn.innerHTML = "<span class=\"spinner-border spinner-border-sm me-2\" role=\"status\" aria-hidden=\"true\"></span>A processar…";
          }
        });
      });
    });
  }

  function initQuickCreate() {
    const modal = document.getElementById("boQuickCreateModal");
    if (!modal) return;

    const clientSearch = modal.querySelector("#boClientSearch");
    const clientResults = modal.querySelector("#boClientResults");
    const clientId = modal.querySelector("#boClientId");
    const clientLabel = modal.querySelector("#boClientLabel");
    const serviceSelect = modal.querySelector("#boServiceSelect");
    const professionalSelect = modal.querySelector("#boProfessionalSelect");
    const dateInput = modal.querySelector("#boDateInput");
    const timeSelect = modal.querySelector("#boTimeSelect");

    function clearResults() {
      if (clientResults) clientResults.innerHTML = "";
    }

    function clearClientSelection() {
      if (clientId) clientId.value = "";
      if (clientLabel) clientLabel.value = "";
    }

    function setClient(result) {
      if (clientId) clientId.value = result.id || "";
      if (clientLabel) clientLabel.value = result.label || "";
      if (clientSearch) clientSearch.value = result.label || "";
      clearResults();
    }

    function renderClientResults(results) {
      if (!clientResults) return;
      clientResults.innerHTML = "";
      if (!results.length) {
        const empty = document.createElement("div");
        empty.className = "list-group-item text-muted";
        empty.textContent = "Sem resultados.";
        clientResults.appendChild(empty);
        return;
      }
      results.forEach((r) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "list-group-item list-group-item-action";
        button.textContent = `${r.label}${r.nif ? " · " + r.nif : ""}${r.phone ? " · " + r.phone : ""}`;
        if (!r.has_user) {
          button.classList.add("disabled");
          button.textContent += " (sem login)";
        }
        button.addEventListener("click", () => {
          if (!r.has_user) return;
          setClient(r);
        });
        clientResults.appendChild(button);
      });
    }

    const debouncedClientSearch = (window.AppUtils && window.AppUtils.debounce
      ? window.AppUtils.debounce
      : (fn, wait) => {
          let timer = null;
          const wrapped = () => {
            if (timer) clearTimeout(timer);
            timer = setTimeout(fn, wait);
          };
          wrapped.cancel = () => {
            if (timer) clearTimeout(timer);
          };
          return wrapped;
        })(() => {
      const q = clientSearch ? clientSearch.value.trim() : "";
      if (q.length < 2) {
        clearResults();
        return;
      }
      fetch(`/backoffice/api/clients/search/?q=${encodeURIComponent(q)}`)
        .then((res) => res.json())
        .then((data) => {
          renderClientResults(data.results || []);
        })
        .catch(() => clearResults());
    }, 250);

    if (clientSearch) {
      clientSearch.addEventListener("input", () => {
        const q = clientSearch.value.trim();
        if (!clientLabel || q !== clientLabel.value) {
          clearClientSelection();
        }
        if (q.length < 2) {
          debouncedClientSearch.cancel();
          clearResults();
          return;
        }
        debouncedClientSearch();
      });
    }

    function setProfessionalOptions(results) {
      if (!professionalSelect) return;
      professionalSelect.innerHTML = "<option value=\"\">Selecionar</option>";
      results.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.label;
        professionalSelect.appendChild(opt);
      });
    }

    function loadProfessionals() {
      if (!serviceSelect || !serviceSelect.value) {
        setProfessionalOptions([]);
        return;
      }
      fetch(`/backoffice/api/professionals/by-service/?service_id=${encodeURIComponent(serviceSelect.value)}`)
        .then((res) => res.json())
        .then((data) => {
          setProfessionalOptions(data.results || []);
          loadSlots();
        })
        .catch(() => setProfessionalOptions([]));
    }

    function setSlots(slots) {
      if (!timeSelect) return;
      timeSelect.innerHTML = "<option value=\"\">Selecionar</option>";
      slots.forEach((slot) => {
        const opt = document.createElement("option");
        opt.value = slot;
        opt.textContent = slot;
        timeSelect.appendChild(opt);
      });
    }

    function loadSlots() {
      if (!serviceSelect || !professionalSelect || !dateInput || !timeSelect) return;
      const serviceId = serviceSelect.value;
      const profId = professionalSelect.value;
      const date = dateInput.value;
      if (!serviceId || !profId || !date) {
        setSlots([]);
        return;
      }
      fetch(`/backoffice/api/slots/?service_id=${encodeURIComponent(serviceId)}&professional_id=${encodeURIComponent(profId)}&date=${encodeURIComponent(date)}`)
        .then((res) => res.json())
        .then((data) => {
          setSlots(data.slots || []);
        })
        .catch(() => setSlots([]));
    }

    if (serviceSelect) {
      serviceSelect.addEventListener("change", loadProfessionals);
    }
    if (professionalSelect) {
      professionalSelect.addEventListener("change", loadSlots);
    }
    if (dateInput) {
      dateInput.addEventListener("change", loadSlots);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (window.bootstrap) {
      initToasts();
      initConfirmModal();
    }
    initSelectAll();
    initFormLoading();
    initAutoSubmitFilters();
    initQuickCreate();
  });
})();
