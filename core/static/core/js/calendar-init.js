(function () {
  const configEl = document.getElementById("calendar-config");
  if (!configEl || !window.tui || !window.tui.Calendar) {
    return;
  }
  const config = JSON.parse(configEl.textContent);
  const services = config.services || [];

  const calendars = services.map((s) => ({
    id: String(s.id),
    name: s.name,
    color: "#ffffff",
    bgColor: s.color,
    dragBgColor: s.color,
    borderColor: s.color,
  }));

  const calendar = new tui.Calendar("#tui-calendar-init", {
    defaultView: config.defaultView === "day" ? "day" : config.defaultView === "month" ? "month" : "week",
    useCreationPopup: false,
    useDetailPopup: true,
    calendars,
    template: {
      time: function (schedule) {
        const start = moment(schedule.start.toUTCString()).format("HH:mm");
        return `<strong>${start}</strong> ${schedule.title}`;
      },
    },
  });

  if (config.baseDate) {
    calendar.setDate(new Date(`${config.baseDate}T00:00:00`));
  }

  function selectedServiceIds() {
    const inputs = Array.from(document.querySelectorAll("#calendarList input[type='checkbox']"));
    const checked = inputs.filter((i) => i.checked).map((i) => i.value);
    return checked;
  }

  function selectedProfessionalId() {
    const input = document.querySelector("#professionalList input[name='professional_id']:checked");
    return input ? input.value : "";
  }

  function isViewAll() {
    const viewAllEl = document.getElementById("viewAllSchedules");
    return viewAllEl ? viewAllEl.checked : false;
  }

  function updateCalendarTypeName() {
    const view = calendar.getViewName();
    const nameEl = document.getElementById("calendarTypeName");
    const iconEl = document.getElementById("calendarTypeIcon");
    if (!nameEl || !iconEl) return;
    if (view === "day") {
      nameEl.textContent = "Dia";
      iconEl.className = "feather-list calendar-icon fs-12 me-1";
    } else if (view === "week") {
      nameEl.textContent = "Semanal";
      iconEl.className = "feather-umbrella calendar-icon fs-12 me-1";
    } else {
      nameEl.textContent = "Mensal";
      iconEl.className = "feather-grid calendar-icon fs-12 me-1";
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

  function updateUrl() {
    const params = new URLSearchParams(window.location.search);
    const view = calendar.getViewName();
    params.set("view", view === "day" ? "day" : view === "month" ? "month" : "week");
    if (view === "day") {
      params.set("date", moment(calendar.getDate()).format("YYYY-MM-DD"));
      params.delete("week");
      params.delete("month");
    } else if (view === "month") {
      params.set("month", moment(calendar.getDate()).format("YYYY-MM"));
      params.delete("week");
      params.delete("date");
    } else {
      params.set("week", moment(calendar.getDateRangeStart().getTime()).format("YYYY-MM-DD"));
      params.delete("date");
      params.delete("month");
    }

    params.delete("service_id");
    selectedServiceIds().forEach((id) => params.append("service_id", id));

    const profId = selectedProfessionalId();
    if (profId) {
      params.set("professional_id", profId);
    } else {
      params.delete("professional_id");
    }

    if (isViewAll()) {
      params.set("view_all", "1");
    } else {
      params.delete("view_all");
    }

    if (config.status) {
      params.set("status", config.status);
    }
    if (config.query) {
      params.set("q", config.query);
    }

    const newUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, "", newUrl);
  }

  function fetchEvents() {
    const start = moment(calendar.getDateRangeStart().getTime()).format("YYYY-MM-DD");
    const end = moment(calendar.getDateRangeEnd().getTime()).format("YYYY-MM-DD");
    const params = new URLSearchParams();
    params.set("start", start);
    params.set("end", end);
    selectedServiceIds().forEach((id) => params.append("service_id", id));
    const profId = selectedProfessionalId();
    if (profId) {
      params.set("professional_id", profId);
    }
    if (isViewAll()) {
      params.set("view_all", "1");
    }
    if (config.status) {
      params.set("status", config.status);
    }
    if (config.query) {
      params.set("q", config.query);
    }

    fetch(`${config.eventsUrl}?${params.toString()}`)
      .then((res) => res.json())
      .then((data) => {
        calendar.clear();
        calendar.createSchedules(data.events || []);
        calendar.render(true);
      })
      .catch(() => {
        calendar.clear();
      });
  }

  function handleNav(e) {
    const action = e.target.getAttribute("data-action");
    if (!action) return;
    if (action === "move-prev") {
      calendar.prev();
    } else if (action === "move-next") {
      calendar.next();
    } else if (action === "move-today") {
      calendar.today();
    }
    updateCalendarTypeName();
    updateRenderRange();
    updateUrl();
    fetchEvents();
  }

  function handleViewChange(action) {
    if (action === "toggle-daily") {
      calendar.changeView("day", true);
    } else if (action === "toggle-weekly") {
      calendar.changeView("week", true);
    } else if (action === "toggle-monthly") {
      calendar.changeView("month", true);
    }
    updateCalendarTypeName();
    updateRenderRange();
    updateUrl();
    fetchEvents();
  }

  document.querySelectorAll(".menu-navi button").forEach((btn) => {
    btn.addEventListener("click", handleNav);
  });

  document.querySelectorAll(".dropdown-menu [data-action]").forEach((item) => {
    item.addEventListener("click", () => handleViewChange(item.getAttribute("data-action")));
  });

  document.querySelectorAll("#calendarList input[type='checkbox']").forEach((input) => {
    input.addEventListener("change", () => {
      updateUrl();
      fetchEvents();
    });
  });

  const viewAllEl = document.getElementById("viewAllSchedules");
  if (viewAllEl) {
    viewAllEl.addEventListener("change", () => {
      const radios = document.querySelectorAll("#professionalList input[name='professional_id']");
      if (viewAllEl.checked) {
        radios.forEach((r) => {
          r.checked = r.value === "";
        });
      }
      updateUrl();
      fetchEvents();
    });
  }

  document.querySelectorAll("#professionalList input[name='professional_id']").forEach((input) => {
    input.addEventListener("change", () => {
      if (viewAllEl) {
        viewAllEl.checked = input.value === "";
      }
      updateUrl();
      fetchEvents();
    });
  });

  updateCalendarTypeName();
  updateRenderRange();
  updateUrl();
  fetchEvents();
})();
