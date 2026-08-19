(function () {
  "use strict";

  const dataEl = document.getElementById("calendar-data");
  const calendarRoot = document.getElementById("tui-calendar-init");
  if (!dataEl || !calendarRoot || !window.FullCalendar) {
    return;
  }
  if (!window.moment) {
    return;
  }

  if (window.moment) {
    window.moment.locale("pt");
  }

  const data = JSON.parse(dataEl.textContent || "{}");
  const isClientMode = !!data.clientMode;
  const hasAvailabilityFeed = !!data.availabilityEventsUrl;
  const filtersEnabled = data.filtersEnabled !== false;
  const clientProfileId = data.clientProfileId ? String(data.clientProfileId) : "";

  const main = document.querySelector("main.nxl-container");
  if (main) {
    main.classList.add("apps-container", "apps-calendar", "apps-calendar-fullcalendar");
  }
  const content = document.querySelector(".nxl-content");
  if (content) {
    content.classList.add("without-header", "nxl-full-content", "apps-calendar-fullcalendar");
  }
  const mainContent = document.querySelector(".main-content.apps-calendar");
  if (mainContent) {
    mainContent.classList.add("apps-calendar-fullcalendar");
  }

  const serviceColorMap = new Map();
  (data.services || []).forEach((service) => {
    serviceColorMap.set(String(service.id), service.color);
  });

  const allServiceIds = new Set((data.services || []).map((service) => String(service.id)));
  let allEvents = Array.isArray(data.events) ? data.events.slice() : [];
  let availabilityEvents = [];
  let showAvailability = false;
  let showAllEvents = true;
  let activeServiceIds = new Set(allServiceIds);
  let activeProfessionalIds = new Set((data.professionals || []).map((professional) => String(professional.id)));
  const manuallyHiddenProfessionalIds = new Set();
  let availabilityToggleBtn = null;
  let toggleAllEventsBtn = null;
  let calendar = null;
  let resizeTimer = null;
  const professionalServiceMap = new Map(
    (data.professionals || []).map((professional) => [
      String(professional.id),
      new Set((professional.service_ids || []).map((serviceId) => String(serviceId))),
    ])
  );

  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return match ? decodeURIComponent(match[2]) : "";
  }

  function parseTimeToMinutes(timeStr) {
    const parts = (timeStr || "").split(":");
    const h = parseInt(parts[0] || "0", 10);
    const m = parseInt(parts[1] || "0", 10);
    return (h * 60) + m;
  }

  function toApiDate(dateValue) {
    return window.moment(dateValue).format("YYYY-MM-DD");
  }

  function toApiTime(dateValue) {
    return window.moment(dateValue).format("HH:mm");
  }

  function getViewRange() {
    const startDate = calendar.view.activeStart || calendar.view.currentStart || new Date();
    const endExclusive = calendar.view.activeEnd || calendar.view.currentEnd || new Date();
    const endDate = window.moment(endExclusive).subtract(1, "day").toDate();
    return {
      start: toApiDate(startDate),
      end: toApiDate(endDate),
    };
  }

  function getServiceColor(serviceId) {
    if (serviceColorMap.has(serviceId)) {
      return serviceColorMap.get(serviceId);
    }
    return "#5485e4";
  }

  function extractServiceName(title) {
    const text = String(title || "").trim();
    if (!text) {
      return "Serviço";
    }
    const sep = text.indexOf(" - ");
    if (sep === -1) {
      return text;
    }
    return text.slice(0, sep).trim();
  }

  function extractProfessionalName(bodyHtml) {
    const text = String(bodyHtml || "");
    const match = text.match(/T[eé]cnico:\s*([^<\n]+)/i);
    if (!match || !match[1]) {
      return "";
    }
    return match[1].trim();
  }

  function extractClientName(title) {
    const text = String(title || "").trim();
    if (!text) {
      return "";
    }
    const sep = text.indexOf(" - ");
    if (sep === -1) {
      return "";
    }
    return text.slice(sep + 3).trim();
  }

  function getProfessionalInitials(name) {
    const text = String(name || "").trim();
    if (!text) {
      return "";
    }
    const parts = text.split(/\s+/).filter(Boolean);
    if (!parts.length) {
      return "";
    }
    if (parts.length === 1) {
      const cleaned = parts[0].replace(/[^A-Za-zÀ-ÖØ-öø-ÿ]/g, "");
      return cleaned.slice(0, 2).toUpperCase();
    }
    const firstInitial = (parts[0] || "").charAt(0).toUpperCase();
    const lastInitial = (parts[parts.length - 1] || "").charAt(0).toUpperCase();
    return (firstInitial + lastInitial).trim();
  }

  function buildEventTooltip(serviceName, professionalName, clientName, partnerName) {
    const lines = [];
    if (serviceName) {
      lines.push("Serviço: " + serviceName);
    }
    if (professionalName) {
      lines.push("Profissional: " + professionalName);
    }
    if (clientName) {
      lines.push("Cliente: " + clientName);
    }
    if (partnerName) {
      lines.push("Parceria: " + partnerName);
    }
    return lines.join("\n");
  }

  function adjustEventSubtitleVisibility(eventEl) {
    if (!eventEl) {
      return;
    }
    const subtitle = eventEl.querySelector(".fc-event-line-subtitle");
    if (!subtitle) {
      return;
    }
    subtitle.style.display = "";
    const contentEl = eventEl.querySelector(".fc-event-content-main");
    if (!contentEl) {
      return;
    }
    const clipEl =
      eventEl.querySelector(".fc-event-main") ||
      eventEl.querySelector(".fc-event-main-frame") ||
      eventEl;
    if (!clipEl) {
      return;
    }
    if (contentEl.scrollHeight > (clipEl.clientHeight + 1)) {
      subtitle.style.display = "none";
    }
  }

  function parseColorToRgba(color, alpha) {
    const value = String(color || "").trim();
    if (!value) {
      return "rgba(84, 133, 228, " + alpha + ")";
    }
    if (value.startsWith("rgba(") || value.startsWith("rgb(")) {
      return value;
    }
    const hex = value.replace("#", "");
    const normalized = hex.length === 3
      ? hex.split("").map((ch) => ch + ch).join("")
      : hex;
    if (!/^[0-9a-fA-F]{6}$/.test(normalized)) {
      return value;
    }
    const r = parseInt(normalized.slice(0, 2), 16);
    const g = parseInt(normalized.slice(2, 4), 16);
    const b = parseInt(normalized.slice(4, 6), 16);
    return "rgba(" + r + ", " + g + ", " + b + ", " + alpha + ")";
  }

  function mapEventToFullCalendar(eventItem) {
    const raw = eventItem.raw || {};
    const serviceId = raw.service_id != null ? String(raw.service_id) : String(eventItem.calendarId || "");
    const bgColor = eventItem.bgColor || getServiceColor(serviceId);
    const borderColor = eventItem.borderColor || bgColor;
    const textColor = eventItem.color || "#ffffff";
    const type = raw.type || "appointment";
    const classNames = [];
    if (type === "availability") {
      classNames.push("fc-event-availability");
    } else if (type === "blocked" || type === "holiday") {
      classNames.push("fc-event-blocked");
    } else {
      classNames.push("fc-event-booking");
    }

    return {
      id: String(eventItem.id),
      title: eventItem.title || "",
      start: eventItem.start,
      end: eventItem.end,
      allDay: false,
      editable: false,
      backgroundColor: bgColor,
      borderColor: borderColor,
      textColor: textColor,
      classNames: classNames,
      extendedProps: {
        raw: raw,
        body: eventItem.body || "",
        sourceEvent: eventItem,
      },
    };
  }

  function setCalendarHeight() {
    const contentArea = document.querySelector(".apps-calendar .content-area.calendar-content-area");
    const contentHeader = document.querySelector(".apps-calendar .content-area.calendar-content-area .content-area-header");
    const contentBody = document.querySelector(".apps-calendar .calendar-content-body");
    if (!contentArea || !contentBody || !calendarRoot) {
      return;
    }

    const bottomGap = 8;
    const areaTop = contentArea.getBoundingClientRect().top;
    const areaAvailable = Math.max(380, Math.floor(window.innerHeight - areaTop - bottomGap));
    const headerHeight = contentHeader ? Math.ceil(contentHeader.getBoundingClientRect().height) : 0;
    const bodyAvailable = Math.max(320, areaAvailable - headerHeight);

    contentArea.style.height = areaAvailable + "px";
    contentBody.style.height = bodyAvailable + "px";
    calendarRoot.style.height = "100%";
    calendarRoot.style.minHeight = "0";

    if (calendar) {
      calendar.updateSize();
    }
  }

  function scheduleSetCalendarHeight() {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(setCalendarHeight, 50);
  }

  function updateCalendarTypeName() {
    const nameEl = document.getElementById("calendarTypeName");
    const iconEl = document.getElementById("calendarTypeIcon");
    if (!nameEl || !iconEl || !calendar) {
      return;
    }
    const viewName = calendar.view.type;
    if (viewName === "timeGridDay") {
      nameEl.textContent = "Diário";
      iconEl.className = "feather-list calendar-icon fs-12 me-1";
      return;
    }
    if (viewName === "dayGridMonth") {
      nameEl.textContent = "Mensal";
      iconEl.className = "feather-grid calendar-icon fs-12 me-1";
      return;
    }
    nameEl.textContent = "Semanal";
    iconEl.className = "feather-umbrella calendar-icon fs-12 me-1";
  }

  function updateRenderRange() {
    const rangeEl = document.getElementById("renderRange");
    if (!rangeEl || !calendar) {
      return;
    }
    const viewName = calendar.view.type;
    if (viewName === "timeGridDay") {
      rangeEl.textContent = window.moment(calendar.getDate()).format("DD/MM/YYYY");
      return;
    }
    if (viewName === "dayGridMonth") {
      rangeEl.textContent = window.moment(calendar.getDate()).format("MMMM YYYY");
      return;
    }
    const start = window.moment(calendar.view.currentStart).format("DD/MM/YYYY");
    const end = window.moment(calendar.view.currentEnd).subtract(1, "day").format("DD/MM/YYYY");
    rangeEl.textContent = start + " ~ " + end;
  }

  function renderFromPayload(payloadEvents) {
    if (!showAllEvents) {
      clearCalendarEvents();
      return;
    }
    const mapped = (payloadEvents || []).map(mapEventToFullCalendar);
    calendar.removeAllEvents();
    mapped.forEach((item) => {
      calendar.addEvent(item);
    });
    scheduleSetCalendarHeight();
  }

  function clearCalendarEvents() {
    calendar.removeAllEvents();
    scheduleSetCalendarHeight();
  }

  function refreshEventsForCurrentView() {
    if (!calendar) {
      return Promise.resolve();
    }

    if (!showAllEvents) {
      clearCalendarEvents();
      return Promise.resolve();
    }

    if (!activeServiceIds.size) {
      clearCalendarEvents();
      return Promise.resolve();
    }
    if (!isClientMode && !activeProfessionalIds.size) {
      clearCalendarEvents();
      return Promise.resolve();
    }

    const range = getViewRange();
    const params = new URLSearchParams();
    params.set("start", range.start);
    params.set("end", range.end);

    if (activeServiceIds.size > 0 && activeServiceIds.size < allServiceIds.size) {
      Array.from(activeServiceIds).forEach((serviceId) => params.append("service_id", serviceId));
    }
    if (!isClientMode) {
      const totalProfessionals = Array.isArray(data.professionals) ? data.professionals.length : 0;
      if (
        totalProfessionals > 0 &&
        activeProfessionalIds.size > 0 &&
        activeProfessionalIds.size < totalProfessionals
      ) {
        Array.from(activeProfessionalIds).forEach((professionalId) => {
          params.append("professional_id", professionalId);
        });
      }
    }

    const url = showAvailability && hasAvailabilityFeed ? data.availabilityEventsUrl : data.eventsUrl;
    if (!url) {
      clearCalendarEvents();
      return Promise.resolve();
    }

    return fetch(url + "?" + params.toString())
      .then((res) => res.json())
      .then((payload) => {
        const events = Array.isArray(payload.events) ? payload.events : [];
        if (showAvailability && hasAvailabilityFeed) {
          availabilityEvents = events;
          renderFromPayload(availabilityEvents);
          return;
        }
        allEvents = events;
        renderFromPayload(allEvents);
        if (quickModalEl && quickModalEl.classList.contains("show")) {
          updateQuickBlockButton();
        }
      })
      .catch(() => {
        if (showAvailability && hasAvailabilityFeed) {
          renderFromPayload(availabilityEvents);
          return;
        }
        renderFromPayload(allEvents);
      });
  }

  function updateAvailabilityToggleLabel() {
    if (!availabilityToggleBtn) {
      return;
    }
    const label = availabilityToggleBtn.querySelector("span");
    if (label) {
      label.textContent = showAvailability ? "Mostrar marcações" : "Mostrar disponibilidades";
    }
  }

  function updateShowAllToggleLabel() {
    if (!toggleAllEventsBtn) {
      return;
    }
    const label = toggleAllEventsBtn.querySelector("span");
    if (label) {
      label.textContent = showAllEvents ? "Esconder tudo" : "Mostrar tudo";
    }
    toggleAllEventsBtn.classList.toggle("btn-primary", !showAllEvents);
    toggleAllEventsBtn.classList.toggle("btn-outline-primary", showAllEvents);
  }

  function syncServiceCheckboxes() {
    document.querySelectorAll("#calendarList input[type='checkbox']").forEach((input) => {
      const serviceId = String(input.value);
      const checked = activeServiceIds.has(serviceId);
      input.checked = checked;
      const label = input.closest("label");
      if (label) {
        label.classList.toggle("is-checked", checked);
      }
    });
  }

  function professionalMatchesActiveServices(professionalId) {
    const serviceIds = professionalServiceMap.get(String(professionalId)) || new Set();
    if (!activeServiceIds.size) {
      return false;
    }
    for (const serviceId of serviceIds) {
      if (activeServiceIds.has(serviceId)) {
        return true;
      }
    }
    return false;
  }

  function syncProfessionalFiltersVisibility() {
    if (isClientMode || !filtersEnabled) {
      return;
    }
    document.querySelectorAll(".schedule-item[data-professional-id]").forEach((item) => {
      const professionalId = String(item.getAttribute("data-professional-id") || "");
      if (!professionalId) {
        return;
      }
      const shouldShow = professionalMatchesActiveServices(professionalId);
      item.classList.toggle("d-none", !shouldShow);
      if (!shouldShow) {
        activeProfessionalIds.delete(professionalId);
        item.classList.remove("opacity-50");
        return;
      }
      if (manuallyHiddenProfessionalIds.has(professionalId)) {
        activeProfessionalIds.delete(professionalId);
        item.classList.add("opacity-50");
      } else {
        activeProfessionalIds.add(professionalId);
        item.classList.remove("opacity-50");
      }
    });
  }

  function handleNav(action) {
    if (!calendar) {
      return;
    }
    if (action === "move-prev") {
      calendar.prev();
    } else if (action === "move-next") {
      calendar.next();
    } else if (action === "move-today") {
      calendar.today();
    } else {
      return;
    }
    updateCalendarTypeName();
    updateRenderRange();
    refreshEventsForCurrentView();
  }

  const quickModalEl = document.getElementById("quickBookingModal");
  const quickModal = quickModalEl && window.bootstrap ? new window.bootstrap.Modal(quickModalEl) : null;
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
  const eventOptionMeta = document.getElementById("event-option-meta");
  const eventOptionProfessionalLine = document.getElementById("event-option-professional-line");
  const eventOptionPartner = document.getElementById("event-option-partner");
  const eventDetailBtn = document.getElementById("event-detail-btn");
  const eventConfirmBtn = document.getElementById("event-confirm-btn");
  const eventNewBtn = document.getElementById("event-new-btn");
  const quickClientInput = document.getElementById("quick-client-search");
  const quickClientId = document.getElementById("quick-client-id");
  const quickClientUserId = document.getElementById("quick-client-user-id");
  const quickClientResults = document.getElementById("quick-client-results");
  const quickCreateClientBtn = document.getElementById("quick-create-client-btn");
  const quickServiceSelect = document.getElementById("quick-service-select");
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
  let activeRescheduleContext = null;
  const QUICK_WINDOW_MINUTES = 60;

  function setQuickMessage(message, tone) {
    if (!quickError) {
      return;
    }
    if (!message) {
      quickError.classList.add("d-none");
      quickError.textContent = "";
      return;
    }
    quickError.classList.remove("alert-danger", "alert-success", "alert-warning", "d-none");
    quickError.classList.add(
      tone === "success" ? "alert-success" : tone === "warning" ? "alert-warning" : "alert-danger"
    );
    quickError.textContent = message;
  }

  function setQuickError(message) {
    setQuickMessage(message, "error");
  }

  function setQuickSuccess(message) {
    setQuickMessage(message, "success");
  }

  function showCalendarFlash(message, tone) {
    if (!message) {
      return;
    }
    const existing = document.getElementById("calendar-floating-flash");
    if (existing) {
      existing.remove();
    }
    const alert = document.createElement("div");
    alert.id = "calendar-floating-flash";
    alert.className = `alert ${tone === "success" ? "alert-success" : "alert-danger"}`;
    alert.textContent = message;
    alert.style.position = "fixed";
    alert.style.top = "92px";
    alert.style.right = "24px";
    alert.style.zIndex = "2000";
    alert.style.minWidth = "320px";
    alert.style.maxWidth = "520px";
    alert.style.boxShadow = "0 12px 28px rgba(15, 23, 42, 0.18)";
    document.body.appendChild(alert);
    window.setTimeout(() => {
      if (alert.parentNode) {
        alert.remove();
      }
    }, 3200);
  }

  function setRescheduleError(message) {
    if (!rescheduleError) {
      return;
    }
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
      const startMoment = window.moment(dateValue + " " + fallbackTime, "YYYY-MM-DD HH:mm", true);
      if (!startMoment.isValid()) {
        return null;
      }
      startDate = startMoment.toDate();
    }
    if (!startDate && !rescheduleId) {
      return null;
    }
    return {
      startDate: startDate,
      preferredTime: timeValue,
      preferredServiceId: (urlParams.get("service_id") || "").trim(),
      preferredServiceLabel: (urlParams.get("service_label") || "").trim(),
      preferredProfessionalId: (urlParams.get("professional_id") || "").trim(),
      preferredProfessionalLabel: (urlParams.get("professional_label") || "").trim(),
      clientProfileId: (urlParams.get("quick_client_profile_id") || "").trim(),
      clientUserId: (urlParams.get("quick_client_user_id") || "").trim(),
      clientLabel: (urlParams.get("quick_client_label") || "").trim(),
      rescheduleId: rescheduleId,
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

  function getQuickSelectedTime() {
    if (quickTimeSelect && quickTimeSelect.value) {
      return quickTimeSelect.value;
    }
    return quickSlotMoment ? quickSlotMoment.format("HH:mm") : "";
  }

  function buildNoAvailabilityMessage(rawMessage) {
    const message = String(rawMessage || "").toLowerCase();
    if (
      message.includes("feriado") ||
      message.includes("não atende") ||
      message.includes("sem horários") ||
      message.includes("data no passado")
    ) {
      return "Nesse dia não tem marcações.";
    }
    return rawMessage || "Nesse dia não tem marcações.";
  }

  function formatDateForLabel(dateStr) {
    const parsed = window.moment(dateStr, "YYYY-MM-DD", true);
    if (!parsed.isValid()) {
      return dateStr || "—";
    }
    return parsed.format("DD/MM/YYYY");
  }

  function ensureSelectOption(selectEl, value, label) {
    if (!selectEl || !value) {
      return;
    }
    const normalized = String(value);
    if (selectEl.querySelector('option[value="' + normalized + '"]')) {
      return;
    }
    const option = document.createElement("option");
    option.value = normalized;
    option.textContent = label || normalized;
    selectEl.appendChild(option);
  }

  function populateRescheduleProfessionals(professionals, selectedId, fallbackLabel) {
    if (!rescheduleProfessionalSelect) {
      return;
    }
    rescheduleProfessionalSelect.innerHTML = '<option value="">— escolher —</option>';
    (professionals || []).forEach((professional) => {
      const option = document.createElement("option");
      option.value = String(professional.id);
      option.textContent = professional.label;
      rescheduleProfessionalSelect.appendChild(option);
    });
    if (selectedId) {
      ensureSelectOption(rescheduleProfessionalSelect, selectedId, fallbackLabel || "Profissional atual");
      rescheduleProfessionalSelect.value = String(selectedId);
    }
  }

  function populateRescheduleTimes(slots, preferredTime) {
    if (!rescheduleTimeSelect) {
      return;
    }
    const uniqueSlots = Array.from(new Set((slots || []).filter(Boolean)));
    rescheduleTimeSelect.innerHTML = '<option value="">— escolher —</option>';
    uniqueSlots.forEach((slot) => {
      const option = document.createElement("option");
      option.value = slot;
      option.textContent = slot;
      rescheduleTimeSelect.appendChild(option);
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
    if (!activeRescheduleContext || !data.slotsApiUrl) {
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
    fetch(data.slotsApiUrl + "?" + params.toString())
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
    fetch(data.professionalsByServiceUrl + "?service_id=" + encodeURIComponent(serviceId))
      .then((res) => res.json())
      .then((payload) => {
        const results = payload && Array.isArray(payload.results) ? payload.results : [];
        const selectedId = String(preferredProfessionalId || activeRescheduleContext.professionalId || "");
        if (selectedId && !results.some((professional) => String(professional.id) === selectedId)) {
          results.push({
            id: selectedId,
            label: activeRescheduleContext.professionalLabel || "Profissional atual",
          });
        }
        populateRescheduleProfessionals(results, selectedId, activeRescheduleContext.professionalLabel || "");
        if (!results.length) {
          populateRescheduleTimes([], "");
          setRescheduleError("Não há profissionais para este serviço.");
          return;
        }
        activeRescheduleContext.professionalId = rescheduleProfessionalSelect ? (rescheduleProfessionalSelect.value || selectedId) : selectedId;
        setRescheduleError("");
        loadRescheduleTimeOptions(preferredTime || activeRescheduleContext.time || "");
      })
      .catch(() => {
        setRescheduleError("Erro ao carregar profissionais.");
      });
  }

  function showRescheduleModalWithContext(context) {
    if (!rescheduleModal) {
      return;
    }
    activeRescheduleContext = Object.assign({}, context);
    if (rescheduleCurrentLabel) {
      const dateLabel = formatDateForLabel(activeRescheduleContext.date);
      const timeLabel = activeRescheduleContext.currentTime || activeRescheduleContext.time || "—";
      rescheduleCurrentLabel.textContent = "Marcação atual: " + dateLabel + " " + timeLabel;
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
      rescheduleDateInput.min = window.moment().format("YYYY-MM-DD");
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
    if (!rescheduleModal) {
      return;
    }
    const fallback = prefill || {};
    const fallbackDate = fallback.startDate ? toApiDate(fallback.startDate) : "";
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
    fetch(contextUrl + "?" + params.toString())
      .then((res) => res.json())
      .then((payload) => {
        if (!payload.ok || !payload.appointment) {
          showRescheduleModalWithContext(baseContext);
          setRescheduleError((payload && payload.message) || "Não foi possível carregar a marcação.");
          return;
        }
        const appt = payload.appointment;
        showRescheduleModalWithContext({
          rescheduleId: String(appt.id || baseContext.rescheduleId || ""),
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
    window.location.href = createClientUrl + (params.toString() ? "?" + params.toString() : "");
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
    window.location.href = bookingUrl + "?" + params.toString();
  }

  function updateQuickSlotLabel() {
    if (!quickSlotLabel || !quickSlotMoment) {
      return;
    }
    quickSlotLabel.textContent = quickSlotMoment.format("DD/MM/YYYY HH:mm");
  }

  function findBlockedEventInCollection(collection, slotMoment, professionalId) {
    if (!slotMoment) {
      return null;
    }
    const slotKey = slotMoment.format("YYYY-MM-DD HH:mm");
    const profKey = professionalId ? String(professionalId) : "";
    return (
      (collection || []).find((eventItem) => {
        if (!eventItem || !eventItem.raw || eventItem.raw.type !== "blocked") {
          return false;
        }
        if (profKey) {
          const eventProf = eventItem.raw.professional_id != null ? String(eventItem.raw.professional_id) : "";
          if (eventProf !== profKey) {
            return false;
          }
        }
        return window.moment(eventItem.start).format("YYYY-MM-DD HH:mm") === slotKey;
      }) || null
    );
  }

  function findBlockedEvent(slotMoment, professionalId) {
    return (
      findBlockedEventInCollection(allEvents, slotMoment, professionalId) ||
      findBlockedEventInCollection(availabilityEvents, slotMoment, professionalId)
    );
  }

  function isHolidayDate(dateValue) {
    const dayKey = window.moment(dateValue).format("YYYY-MM-DD");
    return (allEvents || []).some((eventItem) => {
      if (!eventItem || !eventItem.raw || eventItem.raw.type !== "holiday") {
        return false;
      }
      return window.moment(eventItem.start).format("YYYY-MM-DD") === dayKey;
    });
  }

  function updateQuickBlockButton() {
    if (!quickBlockBtn || !quickSlotMoment) {
      return;
    }
    const professionalId = quickProfessionalSelect
      ? quickProfessionalSelect.value || ""
      : data.currentProfessionalId
        ? String(data.currentProfessionalId)
        : "";
    const isBlocked = !!findBlockedEvent(quickSlotMoment, professionalId);
    if (isBlocked) {
      quickBlockBtn.innerHTML = '<i class="feather-unlock me-2"></i>Desbloquear horário';
      quickBlockBtn.classList.remove("btn-danger", "btn-outline-danger");
      quickBlockBtn.classList.add("btn-success");
      return;
    }
    quickBlockBtn.innerHTML = '<i class="feather-lock me-2"></i>Bloquear horário';
    quickBlockBtn.classList.add("btn-danger");
    quickBlockBtn.classList.remove("btn-success", "btn-outline-danger");
  }

  function updateProfessionalOptions(professionals, selectedId, options) {
    if (!quickProfessionalSelect) {
      return;
    }
    const config = options || {};
    const forceCurrentProfessional = config.forceCurrentProfessional !== false;
    quickProfessionalSelect.innerHTML = '<option value="">— escolher —</option>';
    (professionals || []).forEach((professional) => {
      const option = document.createElement("option");
      option.value = professional.id;
      option.textContent = professional.label;
      quickProfessionalSelect.appendChild(option);
    });
    if (forceCurrentProfessional && !isClientMode && data.currentProfessionalId) {
      const currentId = String(data.currentProfessionalId);
      const exists = quickProfessionalSelect.querySelector('option[value="' + currentId + '"]');
      if (!exists) {
        const option = document.createElement("option");
        option.value = currentId;
        option.textContent = data.currentProfessionalName || "Profissional";
        quickProfessionalSelect.appendChild(option);
      }
    }
    if (selectedId && quickProfessionalSelect.querySelector('option[value="' + selectedId + '"]')) {
      quickProfessionalSelect.value = selectedId;
    } else if (quickProfessionalSelect.options.length === 2) {
      quickProfessionalSelect.selectedIndex = 1;
    } else {
      quickProfessionalSelect.value = "";
    }
  }

  function updateServiceOptions(services, selectedId) {
    if (!quickServiceSelect) {
      return;
    }
    quickServiceSelect.innerHTML = '<option value="">— escolher —</option>';
    (services || []).forEach((service) => {
      const option = document.createElement("option");
      option.value = service.id;
      option.textContent = service.name;
      quickServiceSelect.appendChild(option);
    });

    if (selectedId && quickServiceSelect.querySelector('option[value="' + selectedId + '"]')) {
      quickServiceSelect.value = selectedId;
      return;
    }
    if (activeServiceIds.size === 1) {
      const onlyService = Array.from(activeServiceIds)[0];
      if (quickServiceSelect.querySelector('option[value="' + onlyService + '"]')) {
        quickServiceSelect.value = onlyService;
        return;
      }
    }
    quickServiceSelect.value = "";
  }

  function setQuickSlotTime(timeStr) {
    if (!quickSlotMoment || !timeStr) {
      return;
    }
    const parts = (timeStr || "").split(":");
    const h = parseInt(parts[0] || "0", 10);
    const m = parseInt(parts[1] || "0", 10);
    if (Number.isNaN(h) || Number.isNaN(m)) {
      return;
    }
    quickSlotMoment = quickSlotMoment.clone().hour(h).minute(m).second(0).millisecond(0);
    updateQuickSlotLabel();
    updateQuickBlockButton();
  }

  function syncQuickTimeSelect(slots, preferredTime) {
    if (!quickTimeSelect) {
      return;
    }
    quickTimeSelect.innerHTML = '<option value="">— escolher —</option>';
    const normalizedSlots = Array.from(new Set((slots || []).filter(Boolean)));
    normalizedSlots.forEach((slot) => {
      const option = document.createElement("option");
      option.value = slot;
      option.textContent = slot;
      quickTimeSelect.appendChild(option);
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
    if (!quickWindowStartMoment) {
      return null;
    }
    const params = new URLSearchParams();
    params.set("date", quickWindowStartMoment.format("YYYY-MM-DD"));
    params.set("time", quickWindowStartMoment.format("HH:mm"));
    params.set("window_minutes", String(QUICK_WINDOW_MINUTES));
    if (serviceId) {
      params.set("service_id", serviceId);
    }
    return params;
  }

  function loadSlotOptionsForCurrentSelection(preferredTime) {
    if (!quickSlotMoment || !quickTimeSelect) {
      return;
    }
    const serviceId = quickServiceSelect ? quickServiceSelect.value : "";
    let professionalId = quickProfessionalSelect ? quickProfessionalSelect.value : "";
    if (!professionalId && data.currentProfessionalId) {
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

    fetch(data.slotsApiUrl + "?" + params.toString())
      .then((res) => res.json())
      .then((payload) => {
        const slots = payload && Array.isArray(payload.slots) ? payload.slots : [];
        const filteredSlots = filterSlotsToQuickWindow(slots);
        if (!payload.ok || !filteredSlots.length) {
          syncQuickTimeSelect([], "");
          setQuickError((payload && payload.message) || "Sem horários disponíveis para este período.");
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

  function loadProfessionalsForService(serviceId, preferredTime, preferredProfessionalId) {
    if (!quickProfessionalSelect) {
      return;
    }
    if (!serviceId) {
      updateProfessionalOptions([], "", { forceCurrentProfessional: false });
      loadSlotOptionsForCurrentSelection(preferredTime);
      return;
    }
    const selectedBefore =
      preferredProfessionalId ||
      quickProfessionalSelect.value ||
      (data.currentProfessionalId ? String(data.currentProfessionalId) : "");

    if (data.availabilityOptionsUrl && quickWindowStartMoment) {
      const params = buildAvailabilityParams(serviceId);
      fetch(data.availabilityOptionsUrl + "?" + params.toString())
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
    fetch(url + "?service_id=" + encodeURIComponent(serviceId))
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

  function loadServicesForQuickWindow(preferredServiceId, preferredTime, preferredProfessionalId) {
    if (!quickServiceSelect) {
      return;
    }
    if (!data.availabilityOptionsUrl || !quickWindowStartMoment) {
      const fallbackServiceId = preferredServiceId || "";
      if (fallbackServiceId && quickServiceSelect.querySelector('option[value="' + fallbackServiceId + '"]')) {
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
    fetch(data.availabilityOptionsUrl + "?" + params.toString())
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

  function openQuickModal(startDate, isAllDay, options) {
    if (!quickModal) {
      return;
    }
    const prefill = options || {};
    if (prefill.rescheduleId) {
      openRescheduleModal(prefill);
      return;
    }
    const timezoneName = data.timezone || "Europe/Lisbon";
    const now = window.moment.tz ? window.moment.tz(timezoneName) : window.moment();
    let startMoment = window.moment(startDate).local();

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
    if (isHolidayDate(startMoment)) {
      window.alert("Não é possível marcar em feriado nacional.");
      return;
    }

    quickSlotMoment = startMoment.clone();
    quickWindowStartMoment = startMoment.clone();
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
    if (!preferredProfessionalId && data.currentProfessionalId) {
      preferredProfessionalId = String(data.currentProfessionalId);
    }
    const preferredTime = prefill.preferredTime || quickSlotMoment.format("HH:mm");

    if (quickServiceSelect) {
      quickServiceSelect.value = "";
    }
    if (quickProfessionalSelect) {
      quickProfessionalSelect.value = "";
    }
    syncQuickTimeSelect([], "");
    loadServicesForQuickWindow(preferredServiceId, preferredTime, preferredProfessionalId);
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
    const baseUrl = data.appointmentDetailUrlBase || "/prof/calendario/marcacao/";
    if (isClientMode) {
      return baseUrl;
    }
    const qs = params.toString();
    return baseUrl + appointmentId + "/" + (qs ? "?" + qs : "");
  }

  function buildAppointmentConfirmUrl(appointmentId) {
    const baseUrl = data.appointmentConfirmUrlBase || "/prof/calendario/marcacao/";
    return baseUrl + appointmentId + "/confirmar/";
  }

  function buildGroupSessionDetailUrl(sessionId) {
    const params = new URLSearchParams();
    const urlParams = new URLSearchParams(window.location.search);
    const weekParam = urlParams.get("week");
    if (weekParam) {
      params.set("week", weekParam);
    }
    const baseUrl = data.groupSessionDetailUrlBase || "/backoffice/turmas/";
    const qs = params.toString();
    return baseUrl + sessionId + "/" + (qs ? "?" + qs : "");
  }

  function scheduleFromEvent(fcEvent) {
    return {
      id: fcEvent.id,
      title: fcEvent.title,
      start: fcEvent.start,
      end: fcEvent.end,
      body: (fcEvent.extendedProps && fcEvent.extendedProps.body) || "",
      raw: (fcEvent.extendedProps && fcEvent.extendedProps.raw) || {},
    };
  }

  function openEventOptionsModal(schedule) {
    if (!eventModal || !schedule) {
      return false;
    }
    const startDate = schedule.start || null;
    const startLabel = startDate ? window.moment(startDate).format("DD/MM/YYYY HH:mm") : "—";

    if (eventOptionTime) {
      eventOptionTime.textContent = startLabel;
    }
    if (eventOptionTitle) {
      eventOptionTitle.textContent = schedule.title || "Marcação";
    }

    const statusEl = document.getElementById("event-option-status");
    const statusBadge = document.getElementById("event-option-status-badge");
    const paymentEl = document.getElementById("event-option-payment");
    const eventType = (schedule.raw && schedule.raw.type) || "";
    const isBlocked = eventType === "blocked";
    const isHoliday = eventType === "holiday";

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
    if (eventOptionMeta) {
      eventOptionMeta.classList.add("d-none");
    }
    if (eventOptionProfessionalLine) {
      eventOptionProfessionalLine.textContent = "—";
    }
    if (eventOptionPartner) {
      eventOptionPartner.textContent = "—";
    }

    if (isBlocked || isHoliday) {
      if (eventOptionTitle) {
        eventOptionTitle.textContent = isHoliday ? "Feriado nacional" : "Horário bloqueado";
      }
      if (statusEl && statusBadge) {
        statusBadge.textContent = isHoliday
          ? (schedule.raw && schedule.raw.holiday_name ? schedule.raw.holiday_name : "Feriado")
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
            params.set("date", toApiDate(startDate));
            params.set("time", toApiTime(startDate));
            const weekParam = new URLSearchParams(window.location.search).get("week");
            if (weekParam) {
              params.set("week", weekParam);
            }
            fetch(blockUrl, {
              method: "POST",
              headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": getCookie("csrftoken"),
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
              },
              body: params.toString(),
            })
              .then((res) => res.json().then((payload) => ({ ok: res.ok, payload })))
              .then(({ ok, payload }) => {
                if (!ok || !payload.ok) {
                  throw new Error((payload && payload.message) || "Erro ao desbloquear.");
                }
                return refreshEventsForCurrentView().then(() => {
                  const blockedStillExists = !!findBlockedEvent(startDate, profId);
                  if (payload.blocked || blockedStillExists) {
                    throw new Error("O bloqueio não desapareceu do calendário.");
                  }
                  eventModal.hide();
                  showCalendarFlash(payload.message || "Bloqueio removido com sucesso.", "success");
                });
              })
              .catch((error) => {
                if (eventNewBtn) {
                  eventNewBtn.textContent = error && error.message ? error.message : "Erro ao desbloquear";
                }
              });
          };
        }
      }
      eventModal.show();
      return true;
    }

    const bodyHtml = schedule.body || "";
    const professionalName = (schedule.raw && schedule.raw.professional_name) || extractProfessionalName(bodyHtml);
    const partnerName = (schedule.raw && schedule.raw.partner_name) || "";

    if (eventOptionProfessionalLine) {
      eventOptionProfessionalLine.textContent = professionalName || "—";
    }
    if (eventOptionPartner) {
      eventOptionPartner.textContent = partnerName || "—";
    }
    if (eventOptionMeta) {
      eventOptionMeta.classList.remove("d-none");
    }

    if (schedule.raw && statusEl && statusBadge) {
      const statusLabel = schedule.raw.status || "—";
      const statusRaw = schedule.raw.status_raw || "";
      statusBadge.textContent = statusLabel;
      let bg = "#6c757d";
      if (statusRaw === "pending_confirmation") {
        bg = "#f0ad4e";
      } else if (statusRaw === "awaiting_validation") {
        bg = "#8b5cf6";
      } else if (statusRaw === "scheduled") {
        bg = "#28a745";
      } else if (statusRaw === "no_show") {
        bg = "#ef4444";
      } else if (statusRaw === "completed") {
        bg = "#00B6DF";
      } else if (statusRaw === "cancelled") {
        bg = "#dc3545";
      }
      statusBadge.style.backgroundColor = bg;
      statusBadge.style.color = "#ffffff";
      statusEl.classList.remove("d-none");
    }

    if (isClientMode && schedule.raw && paymentEl) {
      if (schedule.raw.status_raw === "completed") {
        const isPaid = !!schedule.raw.is_paid;
        const priceVal = schedule.raw.final_price;
        const priceLabel = typeof priceVal === "number" ? priceVal.toFixed(2) + " €" : priceVal ? priceVal + " €" : "—";
        paymentEl.textContent = "Pago: " + (isPaid ? "Sim" : "Não") + " · Preço: " + priceLabel;
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
      const canConfirmAll = !!data.canConfirmAll;
      const currentProfId = data.currentProfessionalId ? String(data.currentProfessionalId) : "";
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
              eventModal.hide();
              refreshEventsForCurrentView();
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

  function handleClientSearch() {
    if (!quickClientInput || !quickClientResults) {
      return;
    }
    const term = (quickClientInput.value || "").trim();
    if (term.length < 2) {
      quickClientResults.classList.add("d-none");
      quickClientResults.innerHTML = "";
      return;
    }
    if (!data.clientsSearchUrl) {
      return;
    }
    fetch(data.clientsSearchUrl + "?q=" + encodeURIComponent(term))
      .then((res) => res.json())
      .then((payload) => {
        const results = payload.results || [];
        quickClientResults.innerHTML = "";
        if (!results.length) {
          quickClientResults.classList.add("d-none");
          return;
        }
        results.forEach((clientItem) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "list-group-item list-group-item-action";
          button.textContent = clientItem.label;
          button.dataset.clientId = clientItem.id;
          button.dataset.userId = clientItem.user_id || "";
          button.addEventListener("click", () => {
            quickClientInput.value = clientItem.label;
            if (quickClientId) {
              quickClientId.value = clientItem.id;
            }
            if (quickClientUserId) {
              quickClientUserId.value = clientItem.user_id || "";
            }
            quickClientResults.classList.add("d-none");
          });
          quickClientResults.appendChild(button);
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
      if (event.key !== "Enter") {
        return;
      }
      event.preventDefault();
      debouncedClientSearch.cancel();
      handleClientSearch();
    });
    quickClientInput.addEventListener("blur", () => {
      window.setTimeout(() => {
        if (quickClientResults) {
          quickClientResults.classList.add("d-none");
        }
      }, 200);
    });
  }

  if (quickServiceSelect) {
    quickServiceSelect.addEventListener("change", () => {
      const preferred = quickSlotMoment ? quickSlotMoment.format("HH:mm") : "";
      loadProfessionalsForService(quickServiceSelect.value, preferred, "");
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

  if (rescheduleProfessionalSelect) {
    rescheduleProfessionalSelect.addEventListener("change", () => {
      if (!activeRescheduleContext) {
        return;
      }
      activeRescheduleContext.professionalId = (rescheduleProfessionalSelect.value || "").trim();
      activeRescheduleContext.time = "";
      loadRescheduleTimeOptions("");
    });
  }

  if (rescheduleDateInput) {
    rescheduleDateInput.addEventListener("change", () => {
      if (!activeRescheduleContext) {
        return;
      }
      const nextDate = (rescheduleDateInput.value || "").trim();
      if (!nextDate) {
        setRescheduleError("Seleciona uma data.");
        return;
      }
      const nextMoment = window.moment(nextDate, "YYYY-MM-DD", true);
      if (!nextMoment.isValid()) {
        setRescheduleError("Data inválida.");
        return;
      }
      if (nextMoment.isBefore(window.moment().startOf("day"))) {
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
      if (!activeRescheduleContext) {
        return;
      }
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
          refreshEventsForCurrentView();
        })
        .catch(() => {
          setRescheduleError("Erro ao guardar reagendamento.");
        });
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

      const clientId = isClientMode
        ? clientProfileId || (quickClientId ? quickClientId.value : "")
        : quickClientId
          ? quickClientId.value
          : "";
      if (!clientId && !isClientMode) {
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
          if (payload.message) {
            setQuickSuccess(payload.message);
            showCalendarFlash(payload.message, "success");
          }
          if (quickModal) {
            quickModal.hide();
          }
          refreshEventsForCurrentView();
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
      const selectedTime = getQuickSelectedTime();
      if (!selectedTime) {
        setQuickError("Seleciona um horário.");
        return;
      }
      let professionalId = quickProfessionalSelect ? quickProfessionalSelect.value : "";
      if (!professionalId && data.currentProfessionalId) {
        professionalId = String(data.currentProfessionalId);
      }
      if (!professionalId) {
        setQuickError("Seleciona um profissional.");
        return;
      }
      setQuickError("");
      quickBlockBtn.disabled = true;
      const originalLabel = quickBlockBtn.innerHTML;
      quickBlockBtn.innerHTML = '<i class="feather-loader me-2"></i>A processar...';
      const blockUrl = data.blockSlotUrl || "/prof/calendario/bloquear/";
      const params = new URLSearchParams();
      params.set("professional_id", professionalId);
      params.set("date", quickSlotMoment.format("YYYY-MM-DD"));
      params.set("time", selectedTime);
      const weekParam = new URLSearchParams(window.location.search).get("week");
      if (weekParam) {
        params.set("week", weekParam);
      }

      fetch(blockUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
          "X-Requested-With": "XMLHttpRequest",
          "Accept": "application/json",
        },
        body: params.toString(),
      })
        .then((res) => res.json().then((payload) => ({ ok: res.ok, payload })))
        .then(({ ok, payload }) => {
          if (!ok || !payload.ok) {
            throw new Error((payload && payload.message) || "Erro ao bloquear.");
          }
          setQuickSlotTime(selectedTime);
          return refreshEventsForCurrentView().then(() => {
            const blockedVisible = !!findBlockedEvent(quickSlotMoment, professionalId);
            if (payload.blocked && !blockedVisible) {
              throw new Error("O horário foi bloqueado no sistema, mas não apareceu no calendário. Atualiza a página.");
            }
            if (quickModal) {
              quickModal.hide();
            }
            setQuickSuccess(payload.message || "Horário bloqueado com sucesso.");
            showCalendarFlash(payload.message || "Horário bloqueado com sucesso.", "success");
          });
        })
        .catch((err) => {
          setQuickError(err.message || "Erro ao bloquear.");
        })
        .finally(() => {
          quickBlockBtn.disabled = false;
          quickBlockBtn.innerHTML = originalLabel;
        });
    });
  }

  document.querySelectorAll(".menu-navi button").forEach((button) => {
    button.addEventListener("click", () => {
      handleNav(button.getAttribute("data-action"));
    });
  });

  document.querySelectorAll(".dropdown-menu [data-action]").forEach((item) => {
    item.addEventListener("click", () => {
      const action = item.getAttribute("data-action");
      if (action === "toggle-daily") {
        calendar.changeView("timeGridDay");
      } else if (action === "toggle-monthly") {
        calendar.changeView("dayGridMonth");
      } else if (action === "toggle-weekly") {
        calendar.changeView("timeGridWeek");
      } else {
        return;
      }
      updateCalendarTypeName();
      updateRenderRange();
      refreshEventsForCurrentView();
    });
  });

  document.querySelectorAll("#calendarList input[type='checkbox']").forEach((input) => {
    const serviceId = String(input.value);
    const label = input.closest("label");
    if (input.checked && label) {
      label.classList.add("is-checked");
    }
    input.addEventListener("change", () => {
      showAllEvents = true;
      updateShowAllToggleLabel();
      if (input.checked) {
        activeServiceIds.add(serviceId);
        if (label) {
          label.classList.add("is-checked");
        }
      } else {
        activeServiceIds.delete(serviceId);
        if (label) {
          label.classList.remove("is-checked");
        }
      }
      syncProfessionalFiltersVisibility();
      refreshEventsForCurrentView();
    });
  });

  if (!isClientMode && filtersEnabled) {
    document.querySelectorAll(".schedule-item[data-professional-id]").forEach((item) => {
      item.addEventListener("click", () => {
        const professionalId = item.getAttribute("data-professional-id");
        if (!professionalId) {
          return;
        }
        if (item.classList.contains("d-none")) {
          return;
        }
        if (activeProfessionalIds.has(professionalId)) {
          activeProfessionalIds.delete(professionalId);
          manuallyHiddenProfessionalIds.add(professionalId);
          item.classList.add("opacity-50");
        } else {
          activeProfessionalIds.add(professionalId);
          manuallyHiddenProfessionalIds.delete(professionalId);
          item.classList.remove("opacity-50");
        }
        refreshEventsForCurrentView();
      });
    });
  }

  calendar = new FullCalendar.Calendar(calendarRoot, {
    themeSystem: "bootstrap4",
    locale: "pt",
    firstDay: 1,
    initialDate: data.baseDate || undefined,
    initialView: "timeGridWeek",
    nowIndicator: true,
    allDaySlot: false,
    slotMinTime: "08:00:00",
    slotMaxTime: "21:00:00",
    slotDuration: "00:30:00",
    scrollTime: "08:00:00",
    stickyHeaderDates: true,
    expandRows: true,
    height: "100%",
    headerToolbar: false,
    dayMaxEventRows: true,
    eventTimeFormat: {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    },
    slotLabelFormat: {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    },
    dateClick: (info) => {
      if (isHolidayDate(info.date)) {
        return;
      }
      const isAllDay = !!info.allDay || calendar.view.type === "dayGridMonth";
      openQuickModal(info.date, isAllDay);
    },
    eventClick: (info) => {
      if (!info || !info.event) {
        return;
      }
      const eventData = scheduleFromEvent(info.event);
      if (eventData.raw && eventData.raw.type === "availability") {
        if (!isClientMode) {
          const prefill = {};
          if (eventData.raw.service_id != null) {
            prefill.preferredServiceId = String(eventData.raw.service_id);
          }
          if (eventData.start) {
            prefill.preferredTime = toApiTime(eventData.start);
          }
          const professionalIds = Array.isArray(eventData.raw.professional_ids) ? eventData.raw.professional_ids : [];
          const preferredProfessionalId = eventData.raw.professional_id != null
            ? String(eventData.raw.professional_id)
            : (professionalIds.length === 1 ? String(professionalIds[0]) : "");
          if (preferredProfessionalId) {
            prefill.preferredProfessionalId = preferredProfessionalId;
          }
          openQuickModal(eventData.start || info.event.start, false, prefill);
          return;
        }
        const baseUrl = data.bookingUrl || "/marcar/";
        const url = new URL(baseUrl, window.location.origin);
        if (eventData.start) {
          url.searchParams.set("date", toApiDate(eventData.start));
          url.searchParams.set("time", toApiTime(eventData.start));
        }
        if (eventData.raw.service_id != null) {
          url.searchParams.set("service_id", String(eventData.raw.service_id));
        }
        const professionalIds = Array.isArray(eventData.raw.professional_ids) ? eventData.raw.professional_ids : [];
        if (professionalIds.length === 1) {
          url.searchParams.set("professional_id", String(professionalIds[0]));
        }
        window.location.href = url.toString();
        return;
      }
      if (!eventData.id) {
        return;
      }
      if (!openEventOptionsModal(eventData)) {
        window.location.href = buildAppointmentDetailUrl(eventData.id);
      }
    },
    eventContent: (arg) => {
      const raw = (arg.event.extendedProps && arg.event.extendedProps.raw) || {};
      const isBlocked = raw.type === "blocked";
      const isHoliday = raw.type === "holiday";
      const isPast = arg.event.start ? window.moment(arg.event.start).isBefore(window.moment()) : false;
      const startLabel = arg.event.start ? window.moment(arg.event.start).format("HH:mm") : "";
      const content = document.createElement("div");
      content.className = "fc-event-content-main";

      if (isBlocked || isHoliday) {
        content.classList.add("fc-event-content-blocked");
        content.textContent = isHoliday ? "Feriado" : "Bloqueado";
      } else if (raw.type === "availability") {
        const timeLine = document.createElement("div");
        timeLine.className = "fc-event-line-time";
        timeLine.textContent = startLabel;
        const titleLine = document.createElement("div");
        titleLine.className = "fc-event-line-title";
        titleLine.textContent = arg.event.title || "Disponível";
        content.appendChild(timeLine);
        content.appendChild(titleLine);
      } else {
        content.classList.add(isPast ? "fc-event-content-past" : "fc-event-content-future");
        const body = (arg.event.extendedProps && arg.event.extendedProps.body) || "";
        const serviceName = raw.service_name || extractServiceName(arg.event.title);
        const professionalName = raw.professional_name || extractProfessionalName(body);
        const clientName = raw.client_name || extractClientName(arg.event.title);
        const professionalInitials = getProfessionalInitials(professionalName);

        const firstLine = document.createElement("div");
        firstLine.className = "fc-event-line-title";
        firstLine.textContent = [startLabel, clientName].filter(Boolean).join(" - ") || (arg.event.title || "");

        const secondLine = document.createElement("div");
        secondLine.className = "fc-event-line-subtitle";
        secondLine.textContent = [serviceName, professionalInitials || professionalName].filter(Boolean).join(" • ");

        content.appendChild(firstLine);
        if (secondLine.textContent) {
          content.appendChild(secondLine);
        }
      }
      return { domNodes: [content] };
    },
    eventDidMount: (info) => {
      const raw = (info.event.extendedProps && info.event.extendedProps.raw) || {};
      if (raw.type === "availability" || raw.type === "blocked" || raw.type === "holiday") {
        if (raw.type === "availability") {
          info.el.setAttribute("title", info.event.title || "Disponível");
        } else if (raw.type === "holiday") {
          info.el.setAttribute("title", raw.holiday_name || "Feriado nacional");
        } else {
          info.el.setAttribute("title", "Horário bloqueado");
        }
        return;
      }
      const serviceId = raw.service_id != null ? String(raw.service_id) : "";
      const accentColor = info.event.backgroundColor || getServiceColor(serviceId);
      const borderColor = parseColorToRgba(accentColor, 0.35);
      info.el.style.backgroundColor = "#ffffff";
      info.el.style.border = "1px solid " + borderColor;
      info.el.style.borderLeft = "4px solid " + accentColor;
      info.el.style.color = "#334155";
      info.el.style.boxShadow = "none";

      const body = (info.event.extendedProps && info.event.extendedProps.body) || "";
      const serviceName = raw.service_name || extractServiceName(info.event.title);
      const professionalName = raw.professional_name || extractProfessionalName(body);
      const clientName = raw.client_name || extractClientName(info.event.title);
      const partnerName = raw.partner_name || "";
      const tooltipText = buildEventTooltip(serviceName, professionalName, clientName, partnerName);
      if (tooltipText) {
        info.el.setAttribute("title", tooltipText);
      }
      window.requestAnimationFrame(() => {
        adjustEventSubtitleVisibility(info.el);
      });
    },
    datesSet: () => {
      updateCalendarTypeName();
      updateRenderRange();
      refreshEventsForCurrentView();
    },
  });

  calendar.render();
  renderFromPayload(allEvents);
  updateCalendarTypeName();
  updateRenderRange();
  syncProfessionalFiltersVisibility();
  maybeRestoreQuickModalFromUrl();

  availabilityToggleBtn = document.getElementById("availability-toggle-btn");
  if (hasAvailabilityFeed && availabilityToggleBtn) {
    updateAvailabilityToggleLabel();
    availabilityToggleBtn.addEventListener("click", () => {
      showAvailability = !showAvailability;
      updateAvailabilityToggleLabel();
      refreshEventsForCurrentView();
    });
  }

  toggleAllEventsBtn = document.getElementById("toggle-all-events-btn");
  if (toggleAllEventsBtn) {
    updateShowAllToggleLabel();
    toggleAllEventsBtn.addEventListener("click", () => {
      showAllEvents = !showAllEvents;
      if (!showAllEvents) {
        activeServiceIds.clear();
      } else {
        activeServiceIds = new Set(allServiceIds);
      }
      syncServiceCheckboxes();
      syncProfessionalFiltersVisibility();
      updateShowAllToggleLabel();
      refreshEventsForCurrentView();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    window.setTimeout(setCalendarHeight, 120);
  });
  window.addEventListener("load", scheduleSetCalendarHeight);
  window.addEventListener("resize", scheduleSetCalendarHeight);
  window.addEventListener("orientationchange", scheduleSetCalendarHeight);
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-action]")) {
      return;
    }
    window.setTimeout(setCalendarHeight, 30);
  });

  window.__fullCalendarTest = calendar;
})();
