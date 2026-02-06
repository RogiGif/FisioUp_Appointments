(function () {
  const dataEl = document.getElementById("calendar-data");
  if (!dataEl || !window.tui || !window.tui.Calendar) {
    return;
  }
  const data = JSON.parse(dataEl.textContent);
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
    useDetailPopup: true,
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
      time: function (schedule) {
        const startMoment = moment(schedule.start.toUTCString());
        const start = startMoment.format("HH:mm");
        const isPast = startMoment.isBefore(moment());
        const textClass = isPast ? "schedule-text-past" : "schedule-text-future";
        return `<span class="schedule-text ${textClass}"><strong>${start}</strong> ${schedule.title}</span>`;
      },
    },
  });

  if (data.baseDate) {
    calendar.setDate(new Date(`${data.baseDate}T00:00:00`));
  }

  let allEvents = data.events || [];
  let activeServiceIds = new Set();
  let activeProfessionalIds = new Set((data.professionals || []).map((p) => String(p.id)));

  function renderEvents() {
    if (!activeProfessionalIds.size) {
      calendar.clear();
      calendar.render(true);
      return;
    }
    const filtered = allEvents.filter((ev) => {
      const serviceId = ev.raw && ev.raw.service_id != null ? String(ev.raw.service_id) : "0";
      const professionalId = ev.raw && ev.raw.professional_id != null ? String(ev.raw.professional_id) : "";
      return activeServiceIds.has(serviceId) && activeProfessionalIds.has(professionalId);
    });
    calendar.clear();
    calendar.createSchedules(filtered);
    calendar.render(true);
  }

  function updateCalendarTypeName() {
    const view = calendar.getViewName();
    const nameEl = document.getElementById("calendarTypeName");
    const iconEl = document.getElementById("calendarTypeIcon");
    if (!nameEl || !iconEl) return;
    if (view === "day") {
      nameEl.textContent = "Dia";
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
    if (!activeServiceIds.size || !activeProfessionalIds.size) {
      calendar.clear();
      calendar.render(true);
      return;
    }
    const start = moment(calendar.getDateRangeStart().getTime()).format("YYYY-MM-DD");
    const end = moment(calendar.getDateRangeEnd().getTime()).format("YYYY-MM-DD");
    const params = new URLSearchParams();
    params.set("start", start);
    params.set("end", end);
    Array.from(activeServiceIds).forEach((id) => params.append("service_id", id));
    if (activeProfessionalIds.size === 1) {
      params.set("professional_id", Array.from(activeProfessionalIds)[0]);
    }
    fetch(`${data.eventsUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((payload) => {
        allEvents = payload.events || [];
        renderEvents();
      })
      .catch(() => {
        renderEvents();
    });
  }

  function redirectToBooking(start, isAllDay) {
    const now = moment();
    const rawStart = start && typeof start.toDate === "function" ? start.toDate() : start;
    let startMoment = moment(rawStart);

    // Se for clique "all day", definir hora default válida
    if (isAllDay) {
      const clickedDate = startMoment.clone().startOf("day");
      const today = now.clone().startOf("day");

      if (clickedDate.isSame(today)) {
        // Hoje → próxima hora cheia
        startMoment = now.clone().add(1, "hour").startOf("hour");
      } else {
        // Outro dia → começa às 09:00
        startMoment = clickedDate.clone().hour(9).minute(0);
      }
    }

    // Bloquear passado (hora e dia)
    if (startMoment.isBefore(now)) {
      window.alert("Não podes marcar no passado.");
      return;
    }

    // Redirecionar para marcação com date + time
    const params = new URLSearchParams();
    params.set("date", startMoment.format("YYYY-MM-DD"));
    params.set("time", startMoment.format("HH:mm"));

    // Se existir filtro ativo de serviço único, manter no URL
    if (typeof activeServiceIds !== "undefined" && activeServiceIds.size === 1) {
      params.set("service_id", [...activeServiceIds][0]);
    }

    // Se existir filtro ativo de profissional único, manter no URL
    if (typeof activeProfessionalIds !== "undefined" && activeProfessionalIds.size === 1) {
      params.set("professional_id", [...activeProfessionalIds][0]);
    }

    const urlParams = new URLSearchParams(window.location.search);
    const weekParam = urlParams.get("week");
    const statusParam = urlParams.get("status");
    const qParam = urlParams.get("q");
    if (weekParam) {
      params.set("week", weekParam);
    } else {
      params.set("week", startMoment.clone().startOf("isoWeek").format("YYYY-MM-DD"));
    }
    if (statusParam) {
      params.set("status", statusParam);
    }
    if (qParam) {
      params.set("q", qParam);
    }

    // manter url base vinda do template, se existir
    const baseUrl = (typeof data !== "undefined" && data.bookingUrl) ? data.bookingUrl : "/prof/marcar/";
    window.location.href = `${baseUrl}?${params.toString()}`;
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
    fetchEvents();
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
      fetchEvents();
    });
  });

  document.querySelectorAll("#calendarList input[type='checkbox']").forEach((input) => {
    const label = input.closest("label");
    const serviceId = String(input.value);
    if (input.checked) {
      activeServiceIds.add(serviceId);
      if (label) label.classList.add("is-checked");
    }
    input.addEventListener("change", () => {
      if (input.checked) {
        activeServiceIds.add(serviceId);
        if (label) label.classList.add("is-checked");
      } else {
        activeServiceIds.delete(serviceId);
        if (label) label.classList.remove("is-checked");
      }
      fetchEvents();
    });
  });

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
      fetchEvents();
    });
  });

  calendar.on("beforeCreateSchedule", (event) => {
    const view = calendar.getViewName();
    const isAllDay = event.isAllDay || view === "month";
    redirectToBooking(event.start, isAllDay);
    if (event.guide && typeof event.guide.clearGuideElement === "function") {
      event.guide.clearGuideElement();
    }
  });

  updateCalendarTypeName();
  updateRenderRange();
  renderEvents();
})();
