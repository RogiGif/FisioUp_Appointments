(function () {
  const dataEl = document.getElementById("calendar-data");
  if (!dataEl || !window.tui || !window.tui.Calendar) {
    return;
  }
  const data = JSON.parse(dataEl.textContent);
  const isClientMode = !!data.clientMode;
  const hasAvailabilityFeed = !!data.availabilityEventsUrl;
  const filtersEnabled = data.filtersEnabled !== false;
  const clientProfileId = data.clientProfileId ? String(data.clientProfileId) : "";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function resolveSlotLabels(schedule) {
    const raw = schedule && schedule.raw ? schedule.raw : {};
    const isBlocked = raw.type === "blocked";
    const isHoliday = raw.type === "holiday";
    const isGroup = raw.type === "group";

    if (isBlocked) {
      return { primary: "Bloqueado", secondary: "" };
    }

    if (isHoliday) {
      return { primary: raw.holiday_name || "Feriado nacional", secondary: "" };
    }

    if (isGroup) {
      return { primary: schedule.title || "Turma", secondary: "" };
    }

    if (isClientMode) {
      return { primary: raw.service_name || schedule.title || "", secondary: "" };
    }

    const clientName = raw.client_name || "";
    if (clientName) {
      return { primary: clientName, secondary: raw.service_name || "" };
    }

    return { primary: schedule.title || "", secondary: "" };
  }
  const calendars = (data.services || []).map((s) => ({
    id: String(s.id),
    name: s.name,
    color: "#ffffff",
    bgColor: s.color,
    dragBgColor: s.color,
    borderColor: s.color,
  }));

  const calendar = new tui.Calendar("#tui-calendar-init", {
    defaultView: "week",
    useCreationPopup: false,
    useDetailPopup: false,
    taskView: false,
    milestoneView: false,
    hiddenDays: [0],
    timeZone: "local",
    scheduleView: ["time"],
    calendars,
    week: {
      taskView: false,
      milestoneView: false,
      startDayOfWeek: 1,
      daynames: ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"],
      showNowIndicator: true,
      hourStart: 8,
      hourEnd: 20,
    },
    month: {
      taskView: false,
      milestoneView: false,
      startDayOfWeek: 1,
      daynames: ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"],
    },
    template: {
      timegridDisplayPrimaryTime: function (time) {
        return moment(time).format("HH[h]");
      },
      timegridDisplayTime: function (time) {
        return moment(time).format("HH[h]");
      },
      time: function (schedule) {
        const startMoment = moment(schedule.start.toUTCString());
        const start = startMoment.format("HH:mm");
        const isPast = startMoment.isBefore(moment());
        const isBlocked = schedule.raw && schedule.raw.type === "blocked";
        const isHoliday = schedule.raw && schedule.raw.type === "holiday";
        const textClass = (isBlocked || isHoliday)
          ? "schedule-text-blocked"
          : (isPast ? "schedule-text-past" : "schedule-text-future");
        const labels = resolveSlotLabels(schedule);
        const primaryTitle = escapeHtml(labels.primary);
        const secondaryTitle = labels.secondary
          ? `<span class="schedule-meta">${escapeHtml(labels.secondary)}</span>`
          : "";
        const lockIcon = (isBlocked || isHoliday)
          ? '<i class="feather-lock schedule-lock-icon" aria-hidden="true"></i>'
          : "";
        return (
          `<span class="schedule-text ${textClass}">` +
            `<span class="schedule-time">${escapeHtml(start)}</span>` +
            `${lockIcon}` +
            `<span class="schedule-title">${primaryTitle}</span>` +
            `${secondaryTitle}` +
          `</span>`
        );
      },
    },
  });

  if (typeof calendar.setTheme === "function") {
    try {
      calendar.setTheme({
        "week.timegridOneHour.height": "56px",
        "week.timegridHalfHour.height": "28px",
      });
    } catch (_err) {
      // keep default theme if setTheme is unavailable or incompatible
    }
  }

  window.__proCalendarInstance = calendar;

  if (data.baseDate) {
    calendar.setDate(new Date(`${data.baseDate}T00:00:00`));
  }

  const allServiceIds = new Set((data.services || []).map((s) => String(s.id)));
  let allEvents = data.events || [];
  let availabilityEvents = [];
  let showAvailability = false;
  let activeServiceIds = new Set(allServiceIds);
  let activeProfessionalIds = new Set((data.professionals || []).map((p) => String(p.id)));
  let availabilityToggleBtn = null;

  function keepWeekHeaderVisible() {
    const daynameContainer = document.querySelector("#tui-calendar-init .tui-full-calendar-dayname-container");
    if (!daynameContainer) {
      return;
    }
    if (daynameContainer.scrollTop !== 0) {
      daynameContainer.scrollTop = 0;
    }
  }

  function syncCalendarLayoutAfterRender() {
    requestAnimationFrame(() => {
      keepWeekHeaderVisible();
      if (typeof window.__setCalendarHeight === "function") {
        window.__setCalendarHeight();
      }
    });
  }

  function renderEvents() {
    if (!isClientMode && !activeProfessionalIds.size) {
      calendar.clear();
      calendar.render(true);
      syncCalendarLayoutAfterRender();
      return;
    }
    const filtered = allEvents.filter((ev) => {
      const isBlocked = ev.raw && ev.raw.type === "blocked";
      const isHoliday = ev.raw && ev.raw.type === "holiday";
      const serviceId = ev.raw && ev.raw.service_id != null ? String(ev.raw.service_id) : "0";
      const professionalId = ev.raw && ev.raw.professional_id != null ? String(ev.raw.professional_id) : "";
      if (isHoliday) {
        return true;
      }
      if (isBlocked) {
        if (isClientMode) {
          return false;
        }
        return !activeProfessionalIds.size || activeProfessionalIds.has(professionalId);
      }
      const serviceOk = activeServiceIds.size ? activeServiceIds.has(serviceId) : false;
      return serviceOk && (isClientMode || activeProfessionalIds.has(professionalId));
    });
    calendar.clear();
    calendar.createSchedules(filtered);
    calendar.render(true);
    syncCalendarLayoutAfterRender();
  }

  function renderAvailabilityEvents() {
    if (!activeServiceIds.size) {
      calendar.clear();
      calendar.render(true);
      syncCalendarLayoutAfterRender();
      return;
    }
    const filtered = availabilityEvents.filter((ev) => {
      const isHoliday = ev.raw && ev.raw.type === "holiday";
      if (isHoliday) {
        return true;
      }
      const serviceId = ev.raw && ev.raw.service_id != null ? String(ev.raw.service_id) : "";
      return activeServiceIds.has(serviceId);
    });
    calendar.clear();
    calendar.createSchedules(filtered);
    calendar.render(true);
    syncCalendarLayoutAfterRender();
  }

  function updateCalendarTypeName() {
    const view = calendar.getViewName();
    const nameEl = document.getElementById("calendarTypeName");
    const iconEl = document.getElementById("calendarTypeIcon");
    if (!nameEl || !iconEl) return;
    if (view === "day") {
      nameEl.textContent = "Diário";
      iconEl.className = "feather-list calendar-icon fs-12 me-1";
    } else if (view === "month") {
      nameEl.textContent = "Mensal";
      iconEl.className = "feather-grid calendar-icon fs-12 me-1";
    } else {
      nameEl.textContent = "Semanal";
      iconEl.className = "feather-umbrella calendar-icon fs-12 me-1";
    }
  }

  function updateRenderRange() {
    const rangeEl = document.getElementById("renderRange");
    if (!rangeEl) return;
    const view = calendar.getViewName();
    if (view === "day") {
      rangeEl.textContent = moment(calendar.getDate()).format("DD/MM/YYYY");
      return;
    }
    const start = moment(calendar.getDateRangeStart().getTime()).format("DD/MM/YYYY");
    const end = moment(calendar.getDateRangeEnd().getTime()).format("DD/MM/YYYY");
    rangeEl.textContent = `${start} ~ ${end}`;
  }

  function fetchEvents() {
    if ((isClientMode && !activeServiceIds.size) || (!isClientMode && !activeProfessionalIds.size)) {
      calendar.clear();
      calendar.render(true);
      syncCalendarLayoutAfterRender();
      return;
    }
    const start = moment(calendar.getDateRangeStart().getTime()).format("YYYY-MM-DD");
    const end = moment(calendar.getDateRangeEnd().getTime()).format("YYYY-MM-DD");
    const params = new URLSearchParams();
    params.set("start", start);
    params.set("end", end);
    const shouldFilterServices = activeServiceIds.size > 0 && activeServiceIds.size < allServiceIds.size;
    if (shouldFilterServices) {
      Array.from(activeServiceIds).forEach((id) => params.append("service_id", id));
    }
    if (!isClientMode && activeProfessionalIds.size === 1) {
      params.set("professional_id", Array.from(activeProfessionalIds)[0]);
    }
    fetch(`${data.eventsUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        if (showAvailability && hasAvailabilityFeed) {
          return;
        }
        allEvents = payload.events || [];
        renderEvents();
        if (quickModalEl && quickModalEl.classList.contains("show")) {
          updateQuickBlockButton();
        }
      })
      .catch(() => {
        if (showAvailability && hasAvailabilityFeed) {
          return;
        }
        renderEvents();
      });
  }

  function fetchAvailabilityEvents() {
    if (!hasAvailabilityFeed || !data.availabilityEventsUrl) {
      return;
    }
    if (!activeServiceIds.size) {
      calendar.clear();
      calendar.render(true);
      syncCalendarLayoutAfterRender();
      return;
    }
    const start = moment(calendar.getDateRangeStart().getTime()).format("YYYY-MM-DD");
    const end = moment(calendar.getDateRangeEnd().getTime()).format("YYYY-MM-DD");
    const params = new URLSearchParams();
    params.set("start", start);
    params.set("end", end);
    const shouldFilterServices = activeServiceIds.size > 0 && activeServiceIds.size < allServiceIds.size;
    if (shouldFilterServices) {
      Array.from(activeServiceIds).forEach((id) => params.append("service_id", id));
    }
    fetch(`${data.availabilityEventsUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        if (!showAvailability) {
          return;
        }
        availabilityEvents = payload.events || [];
        renderAvailabilityEvents();
      })
      .catch(() => {
        if (!showAvailability) {
          return;
        }
        renderAvailabilityEvents();
      });
  }

  function refreshCurrentView() {
    if (showAvailability && hasAvailabilityFeed) {
      fetchAvailabilityEvents();
    } else {
      fetchEvents();
    }
  }

  function updateAvailabilityToggleLabel() {
    if (!availabilityToggleBtn) return;
    const label = availabilityToggleBtn.querySelector("span");
    if (label) {
      label.textContent = showAvailability ? "Mostrar marcações" : "Mostrar disponibilidades";
    }
  }

  const quickModalEl = document.getElementById("quickBookingModal");
  const quickModal = quickModalEl && window.bootstrap ? new window.bootstrap.Modal(quickModalEl) : null;
  const quickModalTitle = document.getElementById("quick-booking-modal-title");
  const rescheduleModalEl = document.getElementById("rescheduleBookingModal");
  const rescheduleModal = rescheduleModalEl && window.bootstrap ? new window.bootstrap.Modal(rescheduleModalEl) : null;
  const rescheduleCurrentLabel = document.getElementById("reschedule-current-label");
  const rescheduleClientInput = document.getElementById("reschedule-client-input");
  const rescheduleServiceSelect = document.getElementById("reschedule-service-select");
  const rescheduleProfessionalSelect = document.getElementById("reschedule-professional-select");
  const rescheduleDateInput = document.getElementById("reschedule-date-input");
  const rescheduleTimeSelect = document.getElementById("reschedule-time-select");
  const rescheduleSendClientEmail = document.getElementById("reschedule-send-client-email");
  const rescheduleError = document.getElementById("reschedule-error");
  const rescheduleSaveBtn = document.getElementById("reschedule-save-btn");
  const eventModalEl = document.getElementById("eventOptionsModal");
  const eventModal = eventModalEl && window.bootstrap ? new window.bootstrap.Modal(eventModalEl) : null;
  const eventOptionTitle = document.getElementById("event-option-title");
  const eventOptionTime = document.getElementById("event-option-time");
  const eventDetailBtn = document.getElementById("event-detail-btn");
  const eventConfirmBtn = document.getElementById("event-confirm-btn");
  const eventNewBtn = document.getElementById("event-new-btn");
  const quickClientInput = document.getElementById("quick-client-search");
  const quickClientId = document.getElementById("quick-client-id");
  const quickClientUserId = document.getElementById("quick-client-user-id");
  const quickClientResults = document.getElementById("quick-client-results");
  const quickCreateClientBtn = document.getElementById("quick-create-client-btn");
  const quickServiceSelect = document.getElementById("quick-service-select");
  const quickDateGroup = document.getElementById("quick-date-group");
  const quickDateInput = document.getElementById("quick-date-input");
  const quickProfessionalSelect = document.getElementById("quick-professional-select");
  const quickTimeSelect = document.getElementById("quick-time-select");
  const quickSeriesBtn = document.getElementById("quick-series-btn");
  const quickCreateBtn = document.getElementById("quick-create-btn");
  const quickBlockBtn = document.getElementById("quick-block-btn");
  const quickSendClientEmail = document.getElementById("quick-send-client-email");
  const quickError = document.getElementById("quick-error");
  const quickSlotLabel = document.getElementById("quick-slot-label");
  let quickSlotMoment = null;
  let quickWindowStartMoment = null;
  let quickRescheduleId = "";
  let quickRescheduleContext = null;
  let quickRescheduleServiceLabel = "";
  let quickRescheduleProfessionalLabel = "";
  let activeRescheduleContext = null;
  const QUICK_WINDOW_MINUTES = 60;

  function getCookie(name) {
    const match = document.cookie.match(new RegExp(`(^| )${name}=([^;]+)`));
    return match ? decodeURIComponent(match[2]) : "";
  }

  function setQuickError(message) {
    if (!quickError) return;
    if (!message) {
      quickError.classList.add("d-none");
      quickError.textContent = "";
      return;
    }
    quickError.textContent = message;
    quickError.classList.remove("d-none");
  }

  function setRescheduleError(message) {
    if (!rescheduleError) return;
    if (!message) {
      rescheduleError.classList.add("d-none");
      rescheduleError.textContent = "";
      return;
    }
    rescheduleError.textContent = message;
    rescheduleError.classList.remove("d-none");
  }

  function appendCalendarContextParams(params) {
    const urlParams = new URLSearchParams(window.location.search);
    ["week", "status", "q"].forEach((key) => {
      const value = (urlParams.get(key) || "").trim();
      if (value) {
        params.set(key, value);
      }
    });
  }

  function parseQuickModalRestoreContext() {
    const urlParams = new URLSearchParams(window.location.search);
    const quickOpen = (urlParams.get("quick_open") || "").trim() === "1";
    const rescheduleId = (urlParams.get("reschedule_id") || "").trim();
    if (!quickOpen && !rescheduleId) {
      return null;
    }
    const dateValue = (urlParams.get("date") || "").trim();
    if (!dateValue && !rescheduleId) {
      return null;
    }
    const timeValue = (urlParams.get("time") || "").trim() || "";
    let startDate = null;
    if (dateValue) {
      const fallbackTime = timeValue || "09:00";
      const startMoment = moment(`${dateValue} ${fallbackTime}`, "YYYY-MM-DD HH:mm", true);
      if (!startMoment.isValid()) {
        return null;
      }
      startDate = startMoment.toDate();
    }
    if (!startDate && !rescheduleId) {
      return null;
    }
    return {
      startDate,
      preferredTime: timeValue,
      preferredServiceId: (urlParams.get("service_id") || "").trim(),
      preferredServiceLabel: (urlParams.get("service_label") || "").trim(),
      preferredProfessionalId: (urlParams.get("professional_id") || "").trim(),
      preferredProfessionalLabel: (urlParams.get("professional_label") || "").trim(),
      clientProfileId: (urlParams.get("quick_client_profile_id") || "").trim(),
      clientUserId: (urlParams.get("quick_client_user_id") || "").trim(),
      clientLabel: (urlParams.get("quick_client_label") || "").trim(),
      rescheduleId,
    };
  }

  function clearQuickModalRestoreParams() {
    const url = new URL(window.location.href);
    [
      "quick_open",
      "return_to",
      "date",
      "time",
      "service_id",
      "service_label",
      "professional_id",
      "professional_label",
      "reschedule_id",
      "quick_client_profile_id",
      "quick_client_user_id",
      "quick_client_label",
    ].forEach((key) => url.searchParams.delete(key));
    const nextUrl = url.pathname + (url.search ? url.search : "") + url.hash;
    window.history.replaceState({}, document.title, nextUrl);
  }

  function buildNoAvailabilityMessage(rawMessage) {
    const message = String(rawMessage || "").toLowerCase();
    if (
      message.includes("feriado")
      || message.includes("não atende")
      || message.includes("sem horários")
      || message.includes("data no passado")
    ) {
      return "Nesse dia não tem marcações.";
    }
    return rawMessage || "Nesse dia não tem marcações.";
  }

  function formatDateForLabel(dateStr) {
    const parsed = moment(dateStr, "YYYY-MM-DD", true);
    if (!parsed.isValid()) {
      return dateStr || "—";
    }
    return parsed.format("DD/MM/YYYY");
  }

  function ensureSelectOption(selectEl, value, label) {
    if (!selectEl || !value) return;
    const normalized = String(value);
    const exists = selectEl.querySelector(`option[value="${normalized}"]`);
    if (exists) return;
    const opt = document.createElement("option");
    opt.value = normalized;
    opt.textContent = label || normalized;
    selectEl.appendChild(opt);
  }

  function populateRescheduleProfessionals(professionals, selectedId, fallbackLabel) {
    if (!rescheduleProfessionalSelect) return;
    rescheduleProfessionalSelect.innerHTML = '<option value="">— escolher —</option>';
    (professionals || []).forEach((prof) => {
      const opt = document.createElement("option");
      opt.value = String(prof.id);
      opt.textContent = prof.label;
      rescheduleProfessionalSelect.appendChild(opt);
    });
    if (selectedId) {
      ensureSelectOption(rescheduleProfessionalSelect, selectedId, fallbackLabel || "Profissional atual");
      rescheduleProfessionalSelect.value = String(selectedId);
    }
  }

  function populateRescheduleTimes(slots, preferredTime) {
    if (!rescheduleTimeSelect) return;
    const uniqueSlots = Array.from(new Set((slots || []).filter(Boolean)));
    rescheduleTimeSelect.innerHTML = '<option value="">— escolher —</option>';
    uniqueSlots.forEach((slot) => {
      const opt = document.createElement("option");
      opt.value = slot;
      opt.textContent = slot;
      rescheduleTimeSelect.appendChild(opt);
    });
    if (!uniqueSlots.length) {
      return;
    }
    if (preferredTime && uniqueSlots.includes(preferredTime)) {
      rescheduleTimeSelect.value = preferredTime;
      return;
    }
    rescheduleTimeSelect.value = uniqueSlots[0];
  }

  function loadRescheduleTimeOptions(preferredTime) {
    if (!activeRescheduleContext || !rescheduleTimeSelect || !data.slotsApiUrl) {
      return;
    }
    const serviceId = String(activeRescheduleContext.serviceId || "");
    const professionalId = String(activeRescheduleContext.professionalId || "");
    const date = String(activeRescheduleContext.date || "");
    const rescheduleId = String(activeRescheduleContext.rescheduleId || "");
    if (!serviceId || !professionalId || !date || !rescheduleId) {
      populateRescheduleTimes([], "");
      setRescheduleError("Dados incompletos para reagendar.");
      return;
    }
    const params = new URLSearchParams();
    params.set("service_id", serviceId);
    params.set("professional_id", professionalId);
    params.set("date", date);
    params.set("reschedule_id", rescheduleId);
    fetch(`${data.slotsApiUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        const slots = payload && Array.isArray(payload.slots) ? payload.slots : [];
        if (!payload.ok || !slots.length) {
          populateRescheduleTimes([], "");
          setRescheduleError(buildNoAvailabilityMessage(payload && payload.message));
          return;
        }
        populateRescheduleTimes(slots, preferredTime || activeRescheduleContext.time || "");
        activeRescheduleContext.time = rescheduleTimeSelect ? (rescheduleTimeSelect.value || "") : "";
        setRescheduleError("");
      })
      .catch(() => {
        populateRescheduleTimes([], "");
        setRescheduleError("Erro ao carregar horários.");
      });
  }

  function loadRescheduleProfessionals(preferredProfessionalId, preferredTime) {
    if (!activeRescheduleContext || !data.professionalsByServiceUrl) {
      return;
    }
    const serviceId = String(activeRescheduleContext.serviceId || "");
    if (!serviceId) {
      setRescheduleError("Serviço inválido para reagendamento.");
      return;
    }
    fetch(`${data.professionalsByServiceUrl}?service_id=${encodeURIComponent(serviceId)}`)
      .then((res) => res.json())
      .then((payload) => {
        const results = payload && Array.isArray(payload.results) ? payload.results : [];
        const selectedId = String(preferredProfessionalId || activeRescheduleContext.professionalId || "");
        if (selectedId && !results.some((p) => String(p.id) === selectedId)) {
          results.push({
            id: selectedId,
            label: activeRescheduleContext.professionalLabel || "Profissional atual",
          });
        }
        populateRescheduleProfessionals(results, selectedId, activeRescheduleContext.professionalLabel || "");
        if (!results.length) {
          setRescheduleError("Não há profissionais para este serviço.");
          populateRescheduleTimes([], "");
          return;
        }
        if (rescheduleProfessionalSelect) {
          activeRescheduleContext.professionalId = rescheduleProfessionalSelect.value || selectedId;
        }
        setRescheduleError("");
        loadRescheduleTimeOptions(preferredTime || activeRescheduleContext.time || "");
      })
      .catch(() => {
        setRescheduleError("Erro ao carregar profissionais.");
      });
  }

  function showRescheduleModalWithContext(context) {
    if (!rescheduleModal) return;
    activeRescheduleContext = { ...context };
    if (rescheduleCurrentLabel) {
      const dateLabel = formatDateForLabel(activeRescheduleContext.date);
      const timeLabel = activeRescheduleContext.currentTime || activeRescheduleContext.time || "—";
      rescheduleCurrentLabel.textContent = `Marcação atual: ${dateLabel} ${timeLabel}`;
    }
    if (rescheduleClientInput) {
      rescheduleClientInput.value = activeRescheduleContext.clientLabel || "";
    }
    if (rescheduleServiceSelect) {
      ensureSelectOption(
        rescheduleServiceSelect,
        activeRescheduleContext.serviceId,
        activeRescheduleContext.serviceLabel || "Serviço atual"
      );
      rescheduleServiceSelect.value = String(activeRescheduleContext.serviceId || "");
    }
    if (rescheduleDateInput) {
      rescheduleDateInput.value = activeRescheduleContext.date || "";
      const todayStr = moment().format("YYYY-MM-DD");
      rescheduleDateInput.min = todayStr;
    }
    if (rescheduleSendClientEmail) {
      rescheduleSendClientEmail.checked = true;
    }
    populateRescheduleTimes([], "");
    setRescheduleError("");
    loadRescheduleProfessionals(activeRescheduleContext.professionalId, activeRescheduleContext.time);
    rescheduleModal.show();
  }

  function openRescheduleModal(prefill) {
    if (!rescheduleModal) return;
    const fallback = prefill || {};
    const fallbackDate = fallback.startDate
      ? moment(fallback.startDate).format("YYYY-MM-DD")
      : "";
    const fallbackTime = (fallback.preferredTime || "").trim();
    const baseContext = {
      rescheduleId: (fallback.rescheduleId || "").trim(),
      clientProfileId: (fallback.clientProfileId || "").trim(),
      clientUserId: (fallback.clientUserId || "").trim(),
      clientLabel: (fallback.clientLabel || "").trim(),
      serviceId: (fallback.preferredServiceId || "").trim(),
      serviceLabel: (fallback.preferredServiceLabel || "").trim(),
      professionalId: (fallback.preferredProfessionalId || "").trim(),
      professionalLabel: (fallback.preferredProfessionalLabel || "").trim(),
      date: fallbackDate,
      time: fallbackTime,
      currentTime: fallbackTime,
    };
    if (!baseContext.rescheduleId) {
      setRescheduleError("Reagendamento inválido.");
      return;
    }
    const contextUrl = data.rescheduleContextUrl || "";
    if (!contextUrl) {
      showRescheduleModalWithContext(baseContext);
      return;
    }
    const params = new URLSearchParams();
    params.set("reschedule_id", baseContext.rescheduleId);
    fetch(`${contextUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        if (!payload.ok || !payload.appointment) {
          showRescheduleModalWithContext(baseContext);
          setRescheduleError(payload.message || "Não foi possível carregar a marcação.");
          return;
        }
        const appt = payload.appointment;
        showRescheduleModalWithContext({
          ...baseContext,
          clientProfileId: String(appt.client_profile_id || baseContext.clientProfileId || ""),
          clientUserId: String(appt.client_user_id || baseContext.clientUserId || ""),
          clientLabel: appt.client_label || baseContext.clientLabel || "",
          serviceId: String(appt.service_id || baseContext.serviceId || ""),
          serviceLabel: appt.service_name || baseContext.serviceLabel || "",
          professionalId: String(appt.professional_id || baseContext.professionalId || ""),
          professionalLabel: appt.professional_name || baseContext.professionalLabel || "",
          date: appt.date || baseContext.date || "",
          time: appt.time || baseContext.time || "",
          currentTime: appt.time || baseContext.currentTime || "",
        });
      })
      .catch(() => {
        showRescheduleModalWithContext(baseContext);
        setRescheduleError("Erro ao carregar dados da marcação.");
      });
  }

  function getQuickSelectedTime() {
    if (quickTimeSelect && quickTimeSelect.value) {
      return quickTimeSelect.value;
    }
    return quickSlotMoment ? quickSlotMoment.format("HH:mm") : "";
  }

  function handleCreateClientFromQuickModal() {
    if (!quickSlotMoment) {
      setQuickError("Seleciona um horário.");
      return;
    }
    const params = new URLSearchParams();
    params.set("date", quickSlotMoment.format("YYYY-MM-DD"));
    const selectedTime = getQuickSelectedTime();
    if (selectedTime) {
      params.set("time", selectedTime);
    }
    if (quickServiceSelect && quickServiceSelect.value) {
      params.set("service_id", quickServiceSelect.value);
    }
    if (quickProfessionalSelect && quickProfessionalSelect.value) {
      params.set("professional_id", quickProfessionalSelect.value);
    }
    params.set("return_to", "calendar_quick_modal");
    appendCalendarContextParams(params);
    const createClientUrl = data.createClientUrl || "/prof/utentes/novo/";
    window.location.href = createClientUrl + (params.toString() ? `?${params.toString()}` : "");
  }

  function handleSeriesBookingFromQuickModal() {
    if (isClientMode) {
      return;
    }
    if (!quickSlotMoment) {
      setQuickError("Seleciona um horário.");
      return;
    }
    const clientProfileIdSelected = quickClientId ? (quickClientId.value || "") : "";
    if (!clientProfileIdSelected) {
      setQuickError("Seleciona um cliente.");
      return;
    }
    const params = new URLSearchParams();
    params.set("mode", "serie");
    params.set("client_profile_id", clientProfileIdSelected);
    if (quickClientUserId && quickClientUserId.value) {
      params.set("client_id", quickClientUserId.value);
    }
    if (quickServiceSelect && quickServiceSelect.value) {
      params.set("service_id", quickServiceSelect.value);
    }
    if (quickProfessionalSelect && quickProfessionalSelect.value) {
      params.set("professional_id", quickProfessionalSelect.value);
    }
    params.set("start_date", quickSlotMoment.format("YYYY-MM-DD"));
    const selectedTime = getQuickSelectedTime();
    if (selectedTime) {
      params.set("time", selectedTime);
    }
    appendCalendarContextParams(params);
    const bookingUrl = data.bookingUrl || "/prof/marcar/";
    window.location.href = `${bookingUrl}?${params.toString()}`;
  }

  function updateQuickSlotLabel() {
    if (!quickSlotLabel || !quickSlotMoment) return;
    quickSlotLabel.textContent = quickSlotMoment.format("DD/MM/YYYY HH:mm");
  }

  function setQuickDateInputValue() {
    if (!quickDateInput || !quickSlotMoment) return;
    quickDateInput.value = quickSlotMoment.format("YYYY-MM-DD");
  }

  function setQuickSlotDate(dateStr) {
    if (!quickSlotMoment || !dateStr) return false;
    const parsedDate = moment(dateStr, "YYYY-MM-DD", true);
    if (!parsedDate.isValid()) return false;
    quickSlotMoment = quickSlotMoment
      .clone()
      .year(parsedDate.year())
      .month(parsedDate.month())
      .date(parsedDate.date());
    if (quickWindowStartMoment) {
      quickWindowStartMoment = quickWindowStartMoment
        .clone()
        .year(parsedDate.year())
        .month(parsedDate.month())
        .date(parsedDate.date());
    }
    updateQuickSlotLabel();
    updateQuickBlockButton();
    return true;
  }

  function setQuickModalMode(isReschedule) {
    const inRescheduleMode = !!isReschedule;
    if (quickModalTitle) {
      quickModalTitle.textContent = inRescheduleMode ? "Reagendar marcação" : "Marcação rápida";
    }
    if (quickCreateBtn) {
      quickCreateBtn.textContent = inRescheduleMode ? "Guardar reagendamento" : "Criar marcação";
    }
    if (quickSeriesBtn) {
      quickSeriesBtn.classList.toggle("d-none", inRescheduleMode);
    }
    if (quickBlockBtn) {
      quickBlockBtn.classList.toggle("d-none", inRescheduleMode);
    }
    if (quickCreateClientBtn) {
      quickCreateClientBtn.classList.toggle("d-none", inRescheduleMode);
    }
    if (quickClientInput) {
      quickClientInput.disabled = inRescheduleMode;
    }
    if (quickServiceSelect) {
      quickServiceSelect.disabled = inRescheduleMode;
    }
    if (quickDateGroup) {
      quickDateGroup.classList.toggle("d-none", !inRescheduleMode);
    }
    if (quickDateInput) {
      quickDateInput.disabled = !inRescheduleMode;
    }
    if (quickProfessionalSelect) {
      const defaultDisabled = !isClientMode && !!(data && data.currentProfessionalId) && !((data && data.canConfirmAll) || false);
      quickProfessionalSelect.disabled = inRescheduleMode ? false : defaultDisabled;
    }
  }

  function findBlockedEvent(slotMoment, professionalId) {
    if (!slotMoment) return null;
    const slotKey = slotMoment.format("YYYY-MM-DD HH:mm");
    const profKey = professionalId ? String(professionalId) : "";
    return (allEvents || []).find((ev) => {
      if (!ev || !ev.raw || ev.raw.type !== "blocked") return false;
      if (profKey) {
        const evProf = ev.raw.professional_id != null ? String(ev.raw.professional_id) : "";
        if (evProf !== profKey) return false;
      }
      const evStart = moment(ev.start).format("YYYY-MM-DD HH:mm");
      return evStart === slotKey;
    }) || null;
  }

  function updateQuickBlockButton() {
    if (!quickBlockBtn || !quickSlotMoment) return;
    const profId = quickProfessionalSelect
      ? (quickProfessionalSelect.value || "")
      : (data && data.currentProfessionalId ? String(data.currentProfessionalId) : "");
    const isBlocked = !!findBlockedEvent(quickSlotMoment, profId);
    if (isBlocked) {
      quickBlockBtn.innerHTML = '<i class="feather-unlock me-2"></i>Desbloquear horário';
      quickBlockBtn.classList.remove("btn-danger", "btn-outline-danger");
      quickBlockBtn.classList.add("btn-success");
    } else {
      quickBlockBtn.innerHTML = '<i class="feather-lock me-2"></i>Bloquear horário';
      quickBlockBtn.classList.add("btn-danger");
      quickBlockBtn.classList.remove("btn-success", "btn-outline-danger");
    }
  }

  function updateProfessionalOptions(professionals, selectedId, options) {
    const config = options || {};
    const forceCurrentProfessional = config.forceCurrentProfessional !== false;
    if (!quickProfessionalSelect) return;
    quickProfessionalSelect.innerHTML = '<option value="">— escolher —</option>';
    (professionals || []).forEach((prof) => {
      const opt = document.createElement("option");
      opt.value = prof.id;
      opt.textContent = prof.label;
      quickProfessionalSelect.appendChild(opt);
    });
    if (forceCurrentProfessional && !isClientMode && data && data.currentProfessionalId) {
      const currentId = String(data.currentProfessionalId);
      const exists = quickProfessionalSelect.querySelector(`option[value="${currentId}"]`);
      if (!exists) {
        const opt = document.createElement("option");
        opt.value = currentId;
        opt.textContent = data.currentProfessionalName || "Profissional";
        quickProfessionalSelect.appendChild(opt);
      }
    }
    if (selectedId && quickProfessionalSelect.querySelector(`option[value="${selectedId}"]`)) {
      quickProfessionalSelect.value = selectedId;
    } else if (quickProfessionalSelect.options.length === 2) {
      quickProfessionalSelect.selectedIndex = 1;
    } else {
      quickProfessionalSelect.value = "";
    }
  }

  function updateServiceOptions(services, selectedId) {
    if (!quickServiceSelect) return;
    quickServiceSelect.innerHTML = '<option value="">— escolher —</option>';
    (services || []).forEach((service) => {
      const opt = document.createElement("option");
      opt.value = service.id;
      opt.textContent = service.name;
      quickServiceSelect.appendChild(opt);
    });

    if (selectedId && quickServiceSelect.querySelector(`option[value="${selectedId}"]`)) {
      quickServiceSelect.value = selectedId;
      return;
    }
    if (activeServiceIds.size === 1) {
      const onlyService = Array.from(activeServiceIds)[0];
      if (quickServiceSelect.querySelector(`option[value="${onlyService}"]`)) {
        quickServiceSelect.value = onlyService;
        return;
      }
    }
    quickServiceSelect.value = "";
  }

  function ensureQuickServiceOption(serviceId, serviceLabel) {
    if (!quickServiceSelect || !serviceId) return;
    const normalizedId = String(serviceId);
    const exists = quickServiceSelect.querySelector(`option[value="${normalizedId}"]`);
    if (exists) {
      return;
    }
    const opt = document.createElement("option");
    opt.value = normalizedId;
    opt.textContent = serviceLabel || "Serviço atual";
    quickServiceSelect.appendChild(opt);
  }

  function ensureQuickProfessionalOption(professionalId, professionalLabel) {
    if (!quickProfessionalSelect || !professionalId) return;
    const normalizedId = String(professionalId);
    const exists = quickProfessionalSelect.querySelector(`option[value="${normalizedId}"]`);
    if (exists) {
      return;
    }
    const opt = document.createElement("option");
    opt.value = normalizedId;
    opt.textContent = professionalLabel || "Profissional atual";
    quickProfessionalSelect.appendChild(opt);
  }

  function setQuickSlotTime(timeStr) {
    if (!quickSlotMoment || !timeStr) return;
    const [hRaw, mRaw] = (timeStr || "").split(":");
    const h = parseInt(hRaw || "0", 10);
    const m = parseInt(mRaw || "0", 10);
    if (Number.isNaN(h) || Number.isNaN(m)) return;
    quickSlotMoment = quickSlotMoment.clone().hour(h).minute(m).second(0).millisecond(0);
    updateQuickSlotLabel();
    updateQuickBlockButton();
  }

  function syncQuickTimeSelect(slots, preferredTime) {
    if (!quickTimeSelect) return;
    quickTimeSelect.innerHTML = '<option value="">— escolher —</option>';
    const normalizedSlots = Array.from(new Set((slots || []).filter(Boolean)));
    normalizedSlots.forEach((slot) => {
      const opt = document.createElement("option");
      opt.value = slot;
      opt.textContent = slot;
      quickTimeSelect.appendChild(opt);
    });
    if (!normalizedSlots.length) {
      return;
    }

    let selectedTime = "";
    if (preferredTime && normalizedSlots.includes(preferredTime)) {
      selectedTime = preferredTime;
    } else if (quickSlotMoment) {
      const targetMinutes = parseTimeToMinutes(quickSlotMoment.format("HH:mm"));
      const sameOrAfter = normalizedSlots.find((slot) => parseTimeToMinutes(slot) >= targetMinutes);
      selectedTime = sameOrAfter || normalizedSlots[0];
    } else {
      selectedTime = normalizedSlots[0];
    }

    quickTimeSelect.value = selectedTime;
    setQuickSlotTime(selectedTime);
  }

  function filterSlotsToQuickWindow(slots) {
    if (quickRescheduleId) {
      return slots || [];
    }
    if (!quickWindowStartMoment) {
      return slots || [];
    }
    const windowStart = quickWindowStartMoment.hours() * 60 + quickWindowStartMoment.minutes();
    const windowEnd = windowStart + QUICK_WINDOW_MINUTES;
    return (slots || []).filter((slot) => {
      const slotMinutes = parseTimeToMinutes(slot);
      return slotMinutes >= windowStart && slotMinutes < windowEnd;
    });
  }

  function buildAvailabilityParams(serviceId) {
    if (!quickWindowStartMoment) return null;
    const params = new URLSearchParams();
    params.set("date", quickWindowStartMoment.format("YYYY-MM-DD"));
    params.set("time", quickWindowStartMoment.format("HH:mm"));
    params.set("window_minutes", String(QUICK_WINDOW_MINUTES));
    if (serviceId) {
      params.set("service_id", serviceId);
    }
    if (quickRescheduleId) {
      params.set("reschedule_id", quickRescheduleId);
    }
    return params;
  }

  function loadSlotOptionsForCurrentSelection(preferredTime) {
    if (!quickSlotMoment || !quickTimeSelect) return;
    const serviceId = quickServiceSelect ? quickServiceSelect.value : "";
    let professionalId = quickProfessionalSelect ? quickProfessionalSelect.value : "";
    if (!professionalId && !quickRescheduleId && data && data.currentProfessionalId) {
      professionalId = String(data.currentProfessionalId);
    }
    if (!serviceId || !professionalId || !data.slotsApiUrl) {
      syncQuickTimeSelect([], "");
      return;
    }

    const params = new URLSearchParams();
    params.set("service_id", serviceId);
    params.set("professional_id", professionalId);
    params.set("date", quickSlotMoment.format("YYYY-MM-DD"));
    if (quickRescheduleId) {
      params.set("reschedule_id", quickRescheduleId);
    }

    fetch(`${data.slotsApiUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        const slots = payload && Array.isArray(payload.slots) ? payload.slots : [];
        const filteredSlots = filterSlotsToQuickWindow(slots);
        if (!payload.ok || !filteredSlots.length) {
          syncQuickTimeSelect([], "");
          setQuickError(payload.message || "Sem horários disponíveis para este período.");
          return;
        }
        syncQuickTimeSelect(filteredSlots, preferredTime || quickSlotMoment.format("HH:mm"));
        setQuickError("");
      })
      .catch(() => {
        syncQuickTimeSelect([], "");
        setQuickError("Erro ao carregar horários disponíveis.");
      });
  }

  function loadServicesForQuickWindow(preferredServiceId, preferredTime, preferredProfessionalId) {
    if (!quickServiceSelect) return;
    if (!data.availabilityOptionsUrl || !quickWindowStartMoment) {
      const fallbackServiceId = preferredServiceId || "";
      if (fallbackServiceId && quickServiceSelect.querySelector(`option[value="${fallbackServiceId}"]`)) {
        quickServiceSelect.value = fallbackServiceId;
      } else if (activeServiceIds.size === 1) {
        quickServiceSelect.value = Array.from(activeServiceIds)[0];
      } else {
        quickServiceSelect.value = "";
      }
      if (quickServiceSelect.value) {
        loadProfessionalsForService(quickServiceSelect.value, preferredTime, preferredProfessionalId);
      } else {
        updateProfessionalOptions([], "", { forceCurrentProfessional: false });
        syncQuickTimeSelect([], "");
      }
      return;
    }

    const params = buildAvailabilityParams("");
    fetch(`${data.availabilityOptionsUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        const services = payload && Array.isArray(payload.services) ? payload.services : [];
        const visibleServices = services.filter((service) => activeServiceIds.has(String(service.id)));
        updateServiceOptions(visibleServices, preferredServiceId || "");

        if (!visibleServices.length) {
          updateProfessionalOptions([], "", { forceCurrentProfessional: false });
          syncQuickTimeSelect([], "");
          setQuickError("Não há serviços disponíveis para este período.");
          return;
        }

        const selectedService = quickServiceSelect.value || "";
        if (!selectedService) {
          updateProfessionalOptions([], "", { forceCurrentProfessional: false });
          syncQuickTimeSelect([], "");
          setQuickError("");
          return;
        }

        setQuickError("");
        loadProfessionalsForService(selectedService, preferredTime, preferredProfessionalId);
      })
      .catch(() => {
        setQuickError("Erro ao carregar serviços disponíveis.");
      });
  }

  function parseTimeToMinutes(timeStr) {
    const parts = (timeStr || "").split(":");
    const h = parseInt(parts[0] || "0", 10);
    const m = parseInt(parts[1] || "0", 10);
    return (h * 60) + m;
  }

  function adjustQuickSlotIfNeeded() {
    return;
  }

  function getHolidayNameForMoment(dateMoment) {
    if (!dateMoment) {
      return "";
    }
    const dayKey = moment(dateMoment).format("YYYY-MM-DD");
    const merged = []
      .concat(Array.isArray(allEvents) ? allEvents : [])
      .concat(Array.isArray(availabilityEvents) ? availabilityEvents : []);
    const holidayEvent = merged.find((ev) => {
      if (!ev || !ev.raw || ev.raw.type !== "holiday") {
        return false;
      }
      return moment(ev.start).format("YYYY-MM-DD") === dayKey;
    });
    if (!holidayEvent) {
      return "";
    }
    return (holidayEvent.raw && holidayEvent.raw.holiday_name) || "Feriado nacional";
  }

  function openQuickModal(start, isAllDay, options) {
    if (!quickModal) {
      return;
    }
    const explicitPrefill = options || {};
    const prefill = { ...explicitPrefill };
    if (!prefill.rescheduleId && quickRescheduleContext && quickRescheduleContext.rescheduleId) {
      prefill.rescheduleId = quickRescheduleContext.rescheduleId;
      prefill.clientProfileId = quickRescheduleContext.clientProfileId || prefill.clientProfileId || "";
      prefill.clientUserId = quickRescheduleContext.clientUserId || prefill.clientUserId || "";
      prefill.clientLabel = quickRescheduleContext.clientLabel || prefill.clientLabel || "";
      prefill.preferredServiceId = quickRescheduleContext.preferredServiceId || prefill.preferredServiceId || "";
      prefill.preferredServiceLabel = quickRescheduleContext.preferredServiceLabel || prefill.preferredServiceLabel || "";
      prefill.preferredProfessionalId = quickRescheduleContext.preferredProfessionalId || prefill.preferredProfessionalId || "";
      prefill.preferredProfessionalLabel = quickRescheduleContext.preferredProfessionalLabel || prefill.preferredProfessionalLabel || "";
    }
    if (prefill.rescheduleId) {
      openRescheduleModal(prefill);
      return;
    }
    const tz = (data && data.timezone) ? data.timezone : "Europe/Lisbon";
    const now = moment.tz ? moment.tz(tz) : moment();
    const rawStart = start && typeof start.toDate === "function" ? start.toDate() : start;
    let startMoment = moment(rawStart).local();

    if (isAllDay) {
      const clickedDate = startMoment.clone().startOf("day");
      const today = now.clone().startOf("day");
      if (clickedDate.isSame(today)) {
        startMoment = now.clone().add(1, "hour").startOf("hour");
      } else {
        startMoment = clickedDate.clone().hour(9).minute(0);
      }
    }

    if (startMoment.isBefore(now)) {
      window.alert("Não podes marcar no passado.");
      return;
    }
    const holidayName = getHolidayNameForMoment(startMoment);
    if (holidayName) {
      window.alert(`Não é possível marcar em feriado nacional (${holidayName}).`);
      return;
    }

    quickSlotMoment = startMoment.clone();
    quickWindowStartMoment = startMoment.clone();
    quickRescheduleId = (prefill.rescheduleId || "").trim();
    quickRescheduleServiceLabel = (prefill.preferredServiceLabel || "").trim();
    quickRescheduleProfessionalLabel = (prefill.preferredProfessionalLabel || "").trim();
    setQuickModalMode(!!quickRescheduleId);
    setQuickDateInputValue();
    updateQuickSlotLabel();
    if (quickClientInput) {
      quickClientInput.value = prefill.clientLabel || "";
    }
    if (quickClientId) {
      quickClientId.value = isClientMode ? clientProfileId : (prefill.clientProfileId || "");
    }
    if (quickClientUserId) {
      quickClientUserId.value = isClientMode ? (quickClientUserId.value || "") : (prefill.clientUserId || "");
    }
    if (quickClientResults) {
      quickClientResults.classList.add("d-none");
    }
    if (quickSendClientEmail) {
      quickSendClientEmail.checked = true;
    }
    setQuickError("");

    const preferredServiceId = prefill.preferredServiceId
      || (activeServiceIds.size === 1 ? Array.from(activeServiceIds)[0] : "");
    let preferredProfessionalId = prefill.preferredProfessionalId
      || (activeProfessionalIds.size === 1 ? Array.from(activeProfessionalIds)[0] : "");
    if (!preferredProfessionalId && !quickRescheduleId && data.currentProfessionalId) {
      preferredProfessionalId = String(data.currentProfessionalId);
    }
    const preferredTime = prefill.preferredTime || quickSlotMoment.format("HH:mm");

    if (quickServiceSelect) quickServiceSelect.value = "";
    if (quickProfessionalSelect) quickProfessionalSelect.value = "";
    syncQuickTimeSelect([], "");
    if (quickRescheduleId) {
      const fixedServiceId = preferredServiceId || (quickServiceSelect ? quickServiceSelect.value : "");
      ensureQuickServiceOption(fixedServiceId, quickRescheduleServiceLabel);
      if (quickServiceSelect && fixedServiceId && quickServiceSelect.querySelector(`option[value="${fixedServiceId}"]`)) {
        quickServiceSelect.value = fixedServiceId;
      }
      if (!quickServiceSelect || !quickServiceSelect.value) {
        setQuickError("Serviço inválido para reagendamento.");
      } else {
        ensureQuickProfessionalOption(preferredProfessionalId, quickRescheduleProfessionalLabel);
        if (quickProfessionalSelect && preferredProfessionalId) {
          quickProfessionalSelect.value = String(preferredProfessionalId);
        }
        if (preferredTime) {
          syncQuickTimeSelect([preferredTime], preferredTime);
        }
        loadProfessionalsForService(
          quickServiceSelect.value,
          preferredTime,
          preferredProfessionalId,
          { rescheduleMode: true }
        );
      }
    } else {
      loadServicesForQuickWindow(preferredServiceId, preferredTime, preferredProfessionalId);
    }
    updateQuickBlockButton();
    quickModal.show();
  }

  function maybeRestoreQuickModalFromUrl() {
    if (isClientMode) {
      return;
    }
    const restoreContext = parseQuickModalRestoreContext();
    if (!restoreContext) {
      return;
    }
    clearQuickModalRestoreParams();
    window.setTimeout(() => {
      if (restoreContext.rescheduleId) {
        openRescheduleModal(restoreContext);
        return;
      }
      openQuickModal(restoreContext.startDate, false, restoreContext);
    }, 120);
  }

  function buildAppointmentDetailUrl(appointmentId) {
    const params = new URLSearchParams();
    const urlParams = new URLSearchParams(window.location.search);
    const weekParam = urlParams.get("week");
    if (weekParam) {
      params.set("week", weekParam);
    }
    const baseUrl = (typeof data !== "undefined" && data.appointmentDetailUrlBase)
      ? data.appointmentDetailUrlBase
      : "/prof/calendario/marcacao/";
    if (isClientMode) {
      return baseUrl;
    }
    const qs = params.toString();
    return `${baseUrl}${appointmentId}/` + (qs ? `?${qs}` : "");
  }

  function buildAppointmentConfirmUrl(appointmentId) {
    const baseUrl = (typeof data !== "undefined" && data.appointmentConfirmUrlBase)
      ? data.appointmentConfirmUrlBase
      : "/prof/calendario/marcacao/";
    return `${baseUrl}${appointmentId}/confirmar/`;
  }

  function buildGroupSessionDetailUrl(sessionId) {
    const params = new URLSearchParams();
    const urlParams = new URLSearchParams(window.location.search);
    const weekParam = urlParams.get("week");
    if (weekParam) {
      params.set("week", weekParam);
    }
    const baseUrl = (typeof data !== "undefined" && data.groupSessionDetailUrlBase)
      ? data.groupSessionDetailUrlBase
      : "/backoffice/turmas/";
    const qs = params.toString();
    return `${baseUrl}${sessionId}/` + (qs ? `?${qs}` : "");
  }

  function openEventOptionsModal(schedule) {
    if (!eventModal || !schedule) return false;
    const startDate = schedule.start && typeof schedule.start.toDate === "function"
      ? schedule.start.toDate()
      : schedule.start;
    const startLabel = startDate ? moment(startDate).format("DD/MM/YYYY HH:mm") : "—";
    if (eventOptionTime) {
      eventOptionTime.textContent = startLabel;
    }
    if (eventOptionTitle) {
      eventOptionTitle.textContent = schedule.title || "Marcação";
    }
    const statusEl = document.getElementById("event-option-status");
    const statusBadge = document.getElementById("event-option-status-badge");
    const paymentEl = document.getElementById("event-option-payment");
    const isBlocked = schedule.raw && schedule.raw.type === "blocked";
    if (statusEl) {
      statusEl.classList.add("d-none");
    }
    if (statusBadge) {
      statusBadge.className = "badge";
      statusBadge.textContent = "—";
      statusBadge.style.backgroundColor = "";
      statusBadge.style.color = "";
    }
    if (paymentEl) {
      paymentEl.classList.add("d-none");
    }
    const isHoliday = schedule.raw && schedule.raw.type === "holiday";
    if (isBlocked || isHoliday) {
      if (eventOptionTitle) {
        eventOptionTitle.textContent = isHoliday ? "Feriado nacional" : "Horário bloqueado";
      }
      if (statusEl && statusBadge) {
        statusBadge.textContent = isHoliday
          ? ((schedule.raw && schedule.raw.holiday_name) || "Feriado nacional")
          : "Bloqueado";
        statusBadge.style.backgroundColor = "#64748b";
        statusBadge.style.color = "#ffffff";
        statusEl.classList.remove("d-none");
      }
      if (eventDetailBtn) {
        eventDetailBtn.classList.add("d-none");
      }
      if (eventConfirmBtn) {
        eventConfirmBtn.classList.add("d-none");
        eventConfirmBtn.onclick = null;
      }
      if (eventNewBtn) {
        if (isHoliday) {
          eventNewBtn.classList.add("d-none");
          eventNewBtn.onclick = null;
        } else {
          eventNewBtn.classList.remove("d-none");
          eventNewBtn.textContent = "Desbloquear";
          eventNewBtn.onclick = () => {
            const blockUrl = data.blockSlotUrl || "/prof/calendario/bloquear/";
            const params = new URLSearchParams();
            const profId = schedule.raw && schedule.raw.professional_id ? String(schedule.raw.professional_id) : "";
            if (profId) {
              params.set("professional_id", profId);
            }
            params.set("date", moment(startDate).format("YYYY-MM-DD"));
            params.set("time", moment(startDate).format("HH:mm"));
            const urlParams = new URLSearchParams(window.location.search);
            const weekParam = urlParams.get("week");
            if (weekParam) {
              params.set("week", weekParam);
            }
            fetch(blockUrl, {
              method: "POST",
              headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": getCookie("csrftoken"),
              },
              body: params.toString(),
            })
              .then((res) => {
                if (!res.ok) {
                  throw new Error("Erro ao desbloquear.");
                }
                if (eventModal) {
                  eventModal.hide();
                }
                refreshCurrentView();
              })
              .catch(() => {
                if (eventNewBtn) {
                  eventNewBtn.textContent = "Erro ao desbloquear";
                }
              });
          };
        }
      }
      eventModal.show();
      return true;
    }
    if (schedule.raw && statusEl && statusBadge) {
      const statusLabel = schedule.raw.status || "—";
      const statusRaw = schedule.raw.status_raw || "";
      statusBadge.textContent = statusLabel;
      // cores: pending=amarelo, awaiting_validation=roxo, scheduled=verde, completed=azul logo, no_show/cancelled=vermelho
      let bg = "#6c757d";
      let fg = "#ffffff";
      if (statusRaw === "pending_confirmation") bg = "#f0ad4e";
      if (statusRaw === "awaiting_validation") bg = "#8b5cf6";
      if (statusRaw === "scheduled") bg = "#28a745";
      if (statusRaw === "completed") bg = "#00B6DF";
      if (statusRaw === "no_show") bg = "#ef4444";
      if (statusRaw === "cancelled") bg = "#dc3545";
      statusBadge.style.backgroundColor = bg;
      statusBadge.style.color = fg;
      statusEl.classList.remove("d-none");
    }
    if (isClientMode && schedule.raw && paymentEl) {
      if (schedule.raw.status_raw === "completed") {
        const isPaid = !!schedule.raw.is_paid;
        const priceVal = schedule.raw.final_price;
        const priceLabel = typeof priceVal === "number"
          ? `${priceVal.toFixed(2)} €`
          : priceVal ? `${priceVal} €` : "—";
        paymentEl.textContent = `Pago: ${isPaid ? "Sim" : "Não"} · Preço: ${priceLabel}`;
        paymentEl.classList.remove("d-none");
      }
    }
    if (eventDetailBtn) {
      eventDetailBtn.classList.remove("d-none");
      if (schedule.raw && schedule.raw.type === "group") {
        eventDetailBtn.textContent = "Ver sessão";
        eventDetailBtn.setAttribute("href", buildGroupSessionDetailUrl(schedule.raw.session_id));
      } else {
        eventDetailBtn.textContent = "Ver/editar marcação";
        eventDetailBtn.setAttribute("href", buildAppointmentDetailUrl(schedule.id));
      }
    }
    if (eventConfirmBtn) {
      const isGroup = schedule.raw && schedule.raw.type === "group";
      const isPending = schedule.raw && schedule.raw.status_raw === "pending_confirmation";
      const canConfirmAll = !!(data && data.canConfirmAll);
      const currentProfId = data && data.currentProfessionalId ? String(data.currentProfessionalId) : "";
      const eventProfId = schedule.raw && schedule.raw.professional_id ? String(schedule.raw.professional_id) : "";
      const canConfirmThis = canConfirmAll || (currentProfId && eventProfId && currentProfId === eventProfId);

      if (!isGroup && isPending && !isClientMode && canConfirmThis) {
        eventConfirmBtn.classList.remove("d-none");
        eventConfirmBtn.onclick = () => {
          const url = buildAppointmentConfirmUrl(schedule.id);
          fetch(url, {
            method: "POST",
            headers: {
              "X-CSRFToken": getCookie("csrftoken"),
            },
          })
            .then((res) => {
              if (!res.ok) {
                throw new Error("Erro ao confirmar.");
              }
              if (eventModal) {
                eventModal.hide();
              }
              refreshCurrentView();
            })
            .catch(() => {
              if (eventConfirmBtn) {
                eventConfirmBtn.textContent = "Erro ao confirmar";
              }
            });
        };
      } else {
        eventConfirmBtn.classList.add("d-none");
        eventConfirmBtn.onclick = null;
        eventConfirmBtn.textContent = "Confirmar";
      }
    }
    if (eventNewBtn) {
      eventNewBtn.classList.remove("d-none");
      eventNewBtn.textContent = "Marcar outra neste horário";
      eventNewBtn.onclick = () => {
        eventModal.hide();
        openQuickModal(schedule.start, false);
      };
    }
    eventModal.show();
    return true;
  }

  function loadProfessionalsForService(serviceId, preferredTime, preferredProfessionalId, options) {
    if (!quickProfessionalSelect) return;
    const config = options || {};
    const isRescheduleMode = !!config.rescheduleMode;
    if (!serviceId) {
      updateProfessionalOptions([], "", { forceCurrentProfessional: false });
      loadSlotOptionsForCurrentSelection(preferredTime);
      return;
    }
    const selectedBefore = preferredProfessionalId
      || quickProfessionalSelect.value
      || (data.currentProfessionalId ? String(data.currentProfessionalId) : "");

    if (isRescheduleMode && data.professionalsByServiceUrl) {
      fetch(`${data.professionalsByServiceUrl}?service_id=${encodeURIComponent(serviceId)}`)
        .then((res) => res.json())
        .then((payload) => {
          const results = payload.results || [];
          const normalizedSelectedBefore = selectedBefore ? String(selectedBefore) : "";
          if (
            normalizedSelectedBefore
            && !results.some((p) => String(p.id) === normalizedSelectedBefore)
          ) {
            results.push({
              id: normalizedSelectedBefore,
              label: quickRescheduleProfessionalLabel || "Profissional atual",
            });
          }
          updateProfessionalOptions(results, normalizedSelectedBefore, { forceCurrentProfessional: false });
          if (!results.length) {
            setQuickError("Não há profissionais para este serviço.");
          } else {
            setQuickError("");
          }
          loadSlotOptionsForCurrentSelection(preferredTime);
        })
        .catch(() => {
          setQuickError("Erro ao carregar profissionais.");
          loadSlotOptionsForCurrentSelection(preferredTime);
        });
      return;
    }

    if (data.availabilityOptionsUrl && quickWindowStartMoment) {
      const params = buildAvailabilityParams(serviceId);
      fetch(`${data.availabilityOptionsUrl}?${params.toString()}`)
        .then((res) => res.json())
        .then((payload) => {
          const professionals = payload && Array.isArray(payload.professionals) ? payload.professionals : [];
          updateProfessionalOptions(professionals, selectedBefore, { forceCurrentProfessional: false });
          if (!professionals.length) {
            setQuickError("Não há profissionais disponíveis para este horário.");
          } else {
            setQuickError("");
          }
          loadSlotOptionsForCurrentSelection(preferredTime);
        })
        .catch(() => {
          setQuickError("Erro ao carregar profissionais.");
          loadSlotOptionsForCurrentSelection(preferredTime);
        });
      return;
    }

    const url = data.professionalsByServiceUrl;
    if (!url) {
      loadSlotOptionsForCurrentSelection(preferredTime);
      return;
    }
    fetch(`${url}?service_id=${encodeURIComponent(serviceId)}`)
      .then((res) => res.json())
      .then((payload) => {
        const results = payload.results || [];
        updateProfessionalOptions(results, selectedBefore);
        if (quickProfessionalSelect && !quickProfessionalSelect.value && activeProfessionalIds.size === 1) {
          quickProfessionalSelect.value = Array.from(activeProfessionalIds)[0];
        }
        if (!results.length && (!data.currentProfessionalId || !quickProfessionalSelect.value)) {
          setQuickError("Não há profissionais para este serviço.");
        } else {
          setQuickError("");
        }
        loadSlotOptionsForCurrentSelection(preferredTime);
      })
      .catch(() => {
        setQuickError("Erro ao carregar profissionais.");
        loadSlotOptionsForCurrentSelection(preferredTime);
      });
  }

  function handleClientSearch() {
    if (!quickClientInput || !quickClientResults) return;
    const term = (quickClientInput.value || "").trim();
    if (term.length < 2) {
      quickClientResults.classList.add("d-none");
      quickClientResults.innerHTML = "";
      return;
    }
    const url = data.clientsSearchUrl;
    if (!url) return;
    fetch(`${url}?q=${encodeURIComponent(term)}`)
      .then((res) => res.json())
      .then((payload) => {
        const results = payload.results || [];
        quickClientResults.innerHTML = "";
        if (!results.length) {
          quickClientResults.classList.add("d-none");
          return;
        }
        results.forEach((c) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "list-group-item list-group-item-action";
          btn.textContent = c.label;
          btn.dataset.clientId = c.id;
          btn.dataset.userId = c.user_id || "";
          btn.addEventListener("click", () => {
            quickClientInput.value = c.label;
            if (quickClientId) quickClientId.value = c.id;
            if (quickClientUserId) quickClientUserId.value = c.user_id || "";
            quickClientResults.classList.add("d-none");
          });
          quickClientResults.appendChild(btn);
        });
        quickClientResults.classList.remove("d-none");
      });
  }

  const debouncedClientSearch = window.AppUtils.debounce(handleClientSearch, 250);

  if (quickClientInput) {
    quickClientInput.addEventListener("input", () => {
      if (quickClientId) {
        quickClientId.value = "";
      }
      if (quickClientUserId) {
        quickClientUserId.value = "";
      }
      debouncedClientSearch();
    });
    quickClientInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      debouncedClientSearch.cancel();
      handleClientSearch();
    });
    quickClientInput.addEventListener("blur", () => {
      window.setTimeout(() => {
        if (quickClientResults) quickClientResults.classList.add("d-none");
      }, 200);
    });
  }

  if (quickServiceSelect) {
    quickServiceSelect.addEventListener("change", () => {
      const preferred = quickSlotMoment ? quickSlotMoment.format("HH:mm") : "";
      loadProfessionalsForService(
        quickServiceSelect.value,
        preferred,
        "",
        { rescheduleMode: !!quickRescheduleId }
      );
    });
  }

  if (rescheduleProfessionalSelect) {
    rescheduleProfessionalSelect.addEventListener("change", () => {
      if (!activeRescheduleContext) return;
      activeRescheduleContext.professionalId = (rescheduleProfessionalSelect.value || "").trim();
      activeRescheduleContext.time = "";
      loadRescheduleTimeOptions("");
    });
  }

  if (rescheduleDateInput) {
    rescheduleDateInput.addEventListener("change", () => {
      if (!activeRescheduleContext) return;
      const nextDate = (rescheduleDateInput.value || "").trim();
      if (!nextDate) {
        setRescheduleError("Seleciona uma data.");
        return;
      }
      const nextMoment = moment(nextDate, "YYYY-MM-DD", true);
      if (!nextMoment.isValid()) {
        setRescheduleError("Data inválida.");
        return;
      }
      const today = moment().startOf("day");
      if (nextMoment.isBefore(today)) {
        setRescheduleError("Não podes marcar no passado.");
        return;
      }
      activeRescheduleContext.date = nextDate;
      activeRescheduleContext.time = "";
      loadRescheduleTimeOptions("");
    });
  }

  if (rescheduleTimeSelect) {
    rescheduleTimeSelect.addEventListener("change", () => {
      if (!activeRescheduleContext) return;
      activeRescheduleContext.time = (rescheduleTimeSelect.value || "").trim();
    });
  }

  if (rescheduleModalEl) {
    rescheduleModalEl.addEventListener("hidden.bs.modal", () => {
      activeRescheduleContext = null;
      setRescheduleError("");
      if (rescheduleTimeSelect) {
        rescheduleTimeSelect.innerHTML = '<option value="">— escolher —</option>';
      }
      if (rescheduleProfessionalSelect) {
        rescheduleProfessionalSelect.innerHTML = '<option value="">— escolher —</option>';
      }
    });
  }

  if (rescheduleSaveBtn) {
    rescheduleSaveBtn.addEventListener("click", (event) => {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      if (!activeRescheduleContext) {
        setRescheduleError("Reagendamento inválido.");
        return;
      }
      const serviceId = String(activeRescheduleContext.serviceId || "");
      const professionalId = rescheduleProfessionalSelect ? (rescheduleProfessionalSelect.value || "").trim() : "";
      const date = rescheduleDateInput ? (rescheduleDateInput.value || "").trim() : "";
      const time = rescheduleTimeSelect ? (rescheduleTimeSelect.value || "").trim() : "";
      const rescheduleId = String(activeRescheduleContext.rescheduleId || "");
      if (!serviceId || !professionalId || !date || !time || !rescheduleId) {
        setRescheduleError("Preenche data, hora e profissional para reagendar.");
        return;
      }
      const params = new URLSearchParams();
      params.set("date", date);
      params.set("time", time);
      params.set("service_id", serviceId);
      params.set("professional_id", professionalId);
      params.set("reschedule_id", rescheduleId);
      params.set("send_client_email", rescheduleSendClientEmail && rescheduleSendClientEmail.checked ? "1" : "0");
      const url = data.quickCreateUrl || "/prof/calendario/quick-create/";
      fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: params.toString(),
      })
        .then((res) => res.json())
        .then((payload) => {
          if (!payload.ok) {
            setRescheduleError(payload.message || "Erro ao guardar reagendamento.");
            return;
          }
          if (rescheduleModal) {
            rescheduleModal.hide();
          }
          refreshCurrentView();
        })
        .catch(() => {
          setRescheduleError("Erro ao guardar reagendamento.");
        });
    });
  }

  if (quickDateInput) {
    quickDateInput.addEventListener("change", () => {
      if (!quickRescheduleId || !quickSlotMoment) {
        return;
      }
      const nextDate = (quickDateInput.value || "").trim();
      if (!nextDate) {
        return;
      }
      const currentTime = getQuickSelectedTime() || quickSlotMoment.format("HH:mm");
      const nextMoment = moment(`${nextDate} ${currentTime}`, "YYYY-MM-DD HH:mm", true);
      if (!nextMoment.isValid()) {
        setQuickError("Data inválida.");
        setQuickDateInputValue();
        return;
      }
      const tz = (data && data.timezone) ? data.timezone : "Europe/Lisbon";
      const now = moment.tz ? moment.tz(tz) : moment();
      if (nextMoment.isBefore(now)) {
        setQuickError("Não podes marcar no passado.");
        setQuickDateInputValue();
        return;
      }
      const holidayName = getHolidayNameForMoment(nextMoment);
      if (holidayName) {
        setQuickError(`Não é possível marcar em feriado nacional (${holidayName}).`);
        setQuickDateInputValue();
        return;
      }
      if (!setQuickSlotDate(nextDate)) {
        setQuickError("Data inválida.");
        setQuickDateInputValue();
        return;
      }
      setQuickError("");
      loadSlotOptionsForCurrentSelection(currentTime);
    });
  }

  if (quickCreateClientBtn) {
    quickCreateClientBtn.addEventListener("click", (event) => {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      handleCreateClientFromQuickModal();
    });
  }

  if (quickSeriesBtn) {
    quickSeriesBtn.addEventListener("click", (event) => {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      handleSeriesBookingFromQuickModal();
    });
  }

  if (quickProfessionalSelect) {
    quickProfessionalSelect.addEventListener("change", () => {
      setQuickError("");
      const preferred = quickSlotMoment ? quickSlotMoment.format("HH:mm") : "";
      loadSlotOptionsForCurrentSelection(preferred);
      updateQuickBlockButton();
    });
  }

  if (quickTimeSelect) {
    quickTimeSelect.addEventListener("change", () => {
      setQuickSlotTime(quickTimeSelect.value || "");
    });
  }

  if (quickModalEl) {
    quickModalEl.addEventListener("hidden.bs.modal", () => {
      quickRescheduleId = "";
      quickRescheduleContext = null;
      quickRescheduleServiceLabel = "";
      quickRescheduleProfessionalLabel = "";
      setQuickModalMode(false);
      if (quickDateInput) {
        quickDateInput.value = "";
      }
      setQuickError("");
    });
  }

  if (quickCreateBtn) {
    quickCreateBtn.addEventListener("click", (event) => {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      if (!quickSlotMoment) {
        setQuickError("Seleciona um horário.");
        return;
      }
      const clientId = isClientMode ? (clientProfileId || (quickClientId ? quickClientId.value : "")) : (quickClientId ? quickClientId.value : "");
      if (!clientId && !isClientMode && !quickRescheduleId) {
        setQuickError("Seleciona um cliente.");
        return;
      }
      if (isClientMode && !clientId) {
        setQuickError("Cliente inválido.");
        return;
      }
      const serviceId = quickServiceSelect ? quickServiceSelect.value : "";
      if (!serviceId) {
        setQuickError("Seleciona um serviço.");
        return;
      }
      const professionalId = quickProfessionalSelect ? quickProfessionalSelect.value : "";
      if (!professionalId) {
        setQuickError("Seleciona um profissional.");
        return;
      }
      const selectedTime = quickTimeSelect ? quickTimeSelect.value : "";
      if (!selectedTime) {
        setQuickError("Seleciona um horário.");
        return;
      }

      const params = new URLSearchParams();
      if (clientId) {
        params.set("client_profile_id", clientId);
      }
      params.set("date", quickSlotMoment.format("YYYY-MM-DD"));
      params.set("time", selectedTime);
      params.set("service_id", serviceId);
      params.set("professional_id", professionalId);
      params.set("send_client_email", quickSendClientEmail && quickSendClientEmail.checked ? "1" : "0");
      if (quickRescheduleId) {
        params.set("reschedule_id", quickRescheduleId);
      }

      const url = data.quickCreateUrl || "/prof/calendario/quick-create/";
      fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: params.toString(),
      })
        .then((res) => res.json())
        .then((payload) => {
          if (!payload.ok) {
            setQuickError(payload.message || "Erro ao criar marcação.");
            return;
          }
          if (quickModal) {
            quickModal.hide();
          }
          if (quickRescheduleId) {
            quickRescheduleId = "";
            quickRescheduleContext = null;
            setQuickModalMode(false);
          }
          refreshCurrentView();
        })
        .catch(() => {
          setQuickError("Erro ao criar marcação.");
        });
    });
  }

  if (quickBlockBtn) {
    quickBlockBtn.addEventListener("click", (event) => {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      if (!quickSlotMoment) {
        setQuickError("Seleciona um horário.");
        return;
      }
      let professionalId = quickProfessionalSelect ? quickProfessionalSelect.value : "";
      if (!professionalId && data && data.currentProfessionalId) {
        professionalId = String(data.currentProfessionalId);
      }
      if (!professionalId) {
        setQuickError("Seleciona um profissional.");
        return;
      }
      const blockUrl = data.blockSlotUrl || "/prof/calendario/bloquear/";
      const params = new URLSearchParams();
      params.set("professional_id", professionalId);
      params.set("date", quickSlotMoment.format("YYYY-MM-DD"));
      params.set("time", quickSlotMoment.format("HH:mm"));
      const urlParams = new URLSearchParams(window.location.search);
      const weekParam = urlParams.get("week");
      if (weekParam) {
        params.set("week", weekParam);
      }

      fetch(blockUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: params.toString(),
      })
        .then((res) => {
          if (!res.ok) {
            return res.text().then((txt) => {
              throw new Error(txt || "Erro ao bloquear.");
            });
          }
          refreshCurrentView();
          updateQuickBlockButton();
        })
        .catch((err) => {
          setQuickError(err.message || "Erro ao bloquear.");
        });
    });
  }

  function handleNav(action) {
    if (action === "move-prev") {
      calendar.prev();
    } else if (action === "move-next") {
      calendar.next();
    } else if (action === "move-today") {
      calendar.today();
    }
    updateCalendarTypeName();
    updateRenderRange();
    refreshCurrentView();
  }

  function getCalendarVerticalScroller(target) {
    const root = document.getElementById("tui-calendar-init");
    if (!root) {
      return null;
    }
    const selectors = [
      ".tui-full-calendar-timegrid-container",
      ".tui-full-calendar-weekday-grid",
      ".tui-full-calendar-right",
    ];
    if (target && typeof target.closest === "function") {
      for (let i = 0; i < selectors.length; i += 1) {
        const closestScroller = target.closest(selectors[i]);
        if (closestScroller) {
          return closestScroller;
        }
      }
    }
    for (let i = 0; i < selectors.length; i += 1) {
      const scroller = root.querySelector(selectors[i]);
      if (scroller) {
        return scroller;
      }
    }
    return null;
  }

  function bindCalendarWheelScroll() {
    const root = document.getElementById("tui-calendar-init");
    if (!root || root.dataset.wheelBound === "1") {
      return;
    }
    root.dataset.wheelBound = "1";
    root.addEventListener("wheel", (event) => {
      if (event.defaultPrevented) {
        return;
      }
      const scroller = getCalendarVerticalScroller(event.target);
      if (!scroller) {
        return;
      }
      const deltaY = Number(event.deltaY || 0);
      if (!deltaY) {
        return;
      }
      const maxScrollTop = scroller.scrollHeight - scroller.clientHeight;
      if (maxScrollTop <= 0) {
        return;
      }
      const nextScrollTop = Math.max(0, Math.min(maxScrollTop, scroller.scrollTop + deltaY));
      if (nextScrollTop !== scroller.scrollTop) {
        scroller.scrollTop = nextScrollTop;
        event.preventDefault();
      }
    }, { passive: false });
  }

  function bindWeekHeaderLock() {
    const root = document.getElementById("tui-calendar-init");
    if (!root || root.dataset.weekHeaderLockBound === "1") {
      return;
    }
    root.dataset.weekHeaderLockBound = "1";
    root.addEventListener("scroll", (event) => {
      const target = event.target;
      if (!target || !target.classList) {
        return;
      }
      if (
        target.classList.contains("tui-full-calendar-timegrid-container") ||
        target.classList.contains("tui-full-calendar-weekday-grid")
      ) {
        keepWeekHeaderVisible();
      }
    }, true);
  }

  document.querySelectorAll(".menu-navi button").forEach((btn) => {
    btn.addEventListener("click", () => handleNav(btn.getAttribute("data-action")));
  });

  document.querySelectorAll(".dropdown-menu [data-action]").forEach((item) => {
    item.addEventListener("click", () => {
      const action = item.getAttribute("data-action");
      if (action === "toggle-daily") {
        calendar.changeView("day", true);
      } else if (action === "toggle-monthly") {
        calendar.changeView("month", true);
      } else if (action === "toggle-weekly") {
        calendar.changeView("week", true);
      } else {
        return;
      }
      updateCalendarTypeName();
      updateRenderRange();
      refreshCurrentView();
    });
  });

  document.querySelectorAll("#calendarList input[type='checkbox']").forEach((input) => {
    const label = input.closest("label");
    const serviceId = String(input.value);
    if (input.checked && label) label.classList.add("is-checked");
    input.addEventListener("change", () => {
      if (input.checked) {
        activeServiceIds.add(serviceId);
        if (label) label.classList.add("is-checked");
      } else {
        activeServiceIds.delete(serviceId);
        if (label) label.classList.remove("is-checked");
      }
      refreshCurrentView();
    });
  });

  if (!isClientMode && filtersEnabled) {
    document.querySelectorAll(".schedule-item[data-professional-id]").forEach((item) => {
      item.addEventListener("click", () => {
        const id = item.getAttribute("data-professional-id");
        if (!id) return;
        if (activeProfessionalIds.has(id)) {
          activeProfessionalIds.delete(id);
          item.classList.add("opacity-50");
        } else {
          activeProfessionalIds.add(id);
          item.classList.remove("opacity-50");
        }
        refreshCurrentView();
      });
    });
  }

  calendar.on("beforeCreateSchedule", (event) => {
    const holidayName = getHolidayNameForMoment(event.start);
    if (holidayName) {
      window.alert(`Não é possível marcar em feriado nacional (${holidayName}).`);
      if (event.guide && typeof event.guide.clearGuideElement === "function") {
        event.guide.clearGuideElement();
      }
      return;
    }
    const view = calendar.getViewName();
    const isAllDay = event.isAllDay || view === "month";
    openQuickModal(event.start, isAllDay);
    if (event.guide && typeof event.guide.clearGuideElement === "function") {
      event.guide.clearGuideElement();
    }
  });

  calendar.on("clickSchedule", (event) => {
    if (!event || !event.schedule) return;
    const schedule = event.schedule;
    if (schedule.raw && schedule.raw.type === "holiday") {
      openEventOptionsModal(schedule);
      return;
    }
    if (schedule.raw && schedule.raw.type === "availability") {
      if (!isClientMode) {
        const prefill = {
          preferredServiceId: schedule.raw && schedule.raw.service_id != null
            ? String(schedule.raw.service_id)
            : "",
        };
        const profIds = Array.isArray(schedule.raw.professional_ids) ? schedule.raw.professional_ids : [];
        if (profIds.length === 1) {
          prefill.preferredProfessionalId = String(profIds[0]);
        }
        openQuickModal(schedule.start, false, prefill);
        return;
      }
      const baseUrl = (typeof data !== "undefined" && data.bookingUrl)
        ? data.bookingUrl
        : "/marcar/";
      const url = new URL(baseUrl, window.location.origin);
      const startDate = schedule.start && typeof schedule.start.toDate === "function"
        ? schedule.start.toDate()
        : schedule.start;
      if (startDate) {
        const startMoment = moment(startDate);
        url.searchParams.set("date", startMoment.format("YYYY-MM-DD"));
        url.searchParams.set("time", startMoment.format("HH:mm"));
      }
      if (schedule.raw.service_id != null) {
        url.searchParams.set("service_id", String(schedule.raw.service_id));
      }
      const profIds = Array.isArray(schedule.raw.professional_ids) ? schedule.raw.professional_ids : [];
      if (profIds.length === 1) {
        url.searchParams.set("professional_id", String(profIds[0]));
      }
      window.location.href = url.toString();
      return;
    }
    if (!schedule.id) return;
    if (!openEventOptionsModal(schedule)) {
      window.location.href = buildAppointmentDetailUrl(schedule.id);
    }
  });

  calendar.on("beforeUpdateSchedule", (event) => {
    if (!event || !event.schedule || !event.schedule.id) return;
    window.location.href = buildAppointmentDetailUrl(event.schedule.id);
  });

  updateCalendarTypeName();
  updateRenderRange();
  renderEvents();
  syncCalendarLayoutAfterRender();
  maybeRestoreQuickModalFromUrl();

  availabilityToggleBtn = document.getElementById("availability-toggle-btn");
  if (hasAvailabilityFeed && availabilityToggleBtn) {
    updateAvailabilityToggleLabel();
    availabilityToggleBtn.addEventListener("click", () => {
      showAvailability = !showAvailability;
      updateAvailabilityToggleLabel();
      refreshCurrentView();
    });
  }
})();
