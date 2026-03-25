(function () {
  "use strict";

  var SERVICE_COLORS = {
    fisioterapia: "#22c55e",
    fala: "#6366f1",
    pilates: "#06b6d4"
  };

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function startOfWeekMonday(date) {
    var d = new Date(date);
    var day = d.getDay(); // 0=sun, 1=mon
    var diff = day === 0 ? -6 : 1 - day;
    d.setDate(d.getDate() + diff);
    d.setHours(0, 0, 0, 0);
    return d;
  }

  function withTime(baseDate, dayOffset, hour, minute) {
    var d = new Date(baseDate);
    d.setDate(d.getDate() + dayOffset);
    d.setHours(hour, minute || 0, 0, 0);
    return d;
  }

  function isoLocal(date) {
    return (
      date.getFullYear() +
      "-" + pad2(date.getMonth() + 1) +
      "-" + pad2(date.getDate()) +
      "T" + pad2(date.getHours()) +
      ":" + pad2(date.getMinutes()) +
      ":00"
    );
  }

  function timeLabel(date) {
    return new Intl.DateTimeFormat("pt-PT", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(date);
  }

  function buildNormalEvents(weekStart) {
    var data = [
      { day: 0, hour: 9, minute: 0, durationMin: 60, service: "Fisioterapia", client: "Rogi Gifo", key: "fisioterapia" },
      { day: 1, hour: 9, minute: 0, durationMin: 60, service: "Fisioterapia", client: "Rogi Gifo", key: "fisioterapia" },
      { day: 1, hour: 12, minute: 0, durationMin: 60, service: "Fisioterapia", client: "Joao", key: "fisioterapia" },
      { day: 3, hour: 16, minute: 0, durationMin: 60, service: "Terapia da fala", client: "Joao", key: "fala" },
      { day: 4, hour: 18, minute: 0, durationMin: 60, service: "Pilates", client: "Clinico (0/10)", key: "pilates" }
    ];

    return data.map(function (item, index) {
      var start = withTime(weekStart, item.day, item.hour, item.minute);
      var end = new Date(start);
      end.setMinutes(end.getMinutes() + item.durationMin);
      var color = SERVICE_COLORS[item.key] || "#64748b";

      return {
        id: "event-" + index,
        start: isoLocal(start),
        end: isoLocal(end),
        title: item.service + " - " + item.client,
        backgroundColor: color,
        borderColor: color,
        textColor: "#ffffff",
        extendedProps: {
          service: item.service,
          client: item.client
        }
      };
    });
  }

  function buildAvailabilityEvents(weekStart) {
    var events = [];
    var day;
    for (day = 0; day < 6; day += 1) {
      var morningStart = withTime(weekStart, day, 8, 0);
      var morningEnd = withTime(weekStart, day, 13, 0);
      var afternoonStart = withTime(weekStart, day, 14, 0);
      var afternoonEnd = withTime(weekStart, day, 21, 0);

      events.push({
        id: "avail-m-" + day,
        start: isoLocal(morningStart),
        end: isoLocal(morningEnd),
        display: "background",
        backgroundColor: "rgba(34, 197, 94, 0.16)"
      });
      events.push({
        id: "avail-a-" + day,
        start: isoLocal(afternoonStart),
        end: isoLocal(afternoonEnd),
        display: "background",
        backgroundColor: "rgba(14, 165, 233, 0.14)"
      });
    }
    return events;
  }

  function updateAvailabilityButtonState(showAvailability) {
    var btn = document.querySelector(".fc-showAvailability-button");
    if (!btn) {
      return;
    }
    btn.textContent = showAvailability ? "Mostrar marcações" : "Mostrar disponibilidades";
    btn.classList.toggle("btn-primary", showAvailability);
    btn.classList.toggle("btn-outline-secondary", !showAvailability);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var calendarEl = document.getElementById("fc-calendar");
    if (!calendarEl || !window.FullCalendar) {
      return;
    }

    if (typeof window.TEST_MODE_SHOW_AVAILABILITY !== "boolean") {
      window.TEST_MODE_SHOW_AVAILABILITY = false;
    }

    var showAvailability = window.TEST_MODE_SHOW_AVAILABILITY;
    var currentWeekStart = startOfWeekMonday(new Date());
    var calendar;

    function renderSources() {
      calendar.removeAllEventSources();
      if (showAvailability) {
        calendar.addEventSource(buildAvailabilityEvents(currentWeekStart));
      } else {
        calendar.addEventSource(buildNormalEvents(currentWeekStart));
      }
      updateAvailabilityButtonState(showAvailability);
    }

    calendar = new FullCalendar.Calendar(calendarEl, {
      themeSystem: "bootstrap4",
      locale: "pt",
      firstDay: 1,
      initialView: "timeGridWeek",
      nowIndicator: true,
      allDaySlot: false,
      slotMinTime: "08:00:00",
      slotMaxTime: "21:00:00",
      slotDuration: "00:30:00",
      slotMinHeight: 42,
      stickyHeaderDates: true,
      expandRows: true,
      height: "100%",
      headerToolbar: {
        left: "prev,next today",
        center: "title",
        right: "showAvailability,timeGridDay,timeGridWeek,dayGridMonth"
      },
      customButtons: {
        showAvailability: {
          text: "Mostrar disponibilidades",
          click: function () {
            showAvailability = !showAvailability;
            window.TEST_MODE_SHOW_AVAILABILITY = showAvailability;
            renderSources();
          }
        }
      },
      buttonText: {
        today: "Hoje",
        day: "Dia",
        week: "Semana",
        month: "Mês"
      },
      slotLabelFormat: {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false
      },
      eventTimeFormat: {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false
      },
      datesSet: function (info) {
        currentWeekStart = startOfWeekMonday(info.start);
        renderSources();
      },
      eventContent: function (arg) {
        if (arg.event.display === "background") {
          return null;
        }
        var service = arg.event.extendedProps.service || "Serviço";
        var client = arg.event.extendedProps.client || "Cliente";
        var content = document.createElement("div");
        content.className = "fc-test-event";
        content.textContent = timeLabel(arg.event.start) + " " + service + " - " + client;
        return { domNodes: [content] };
      }
    });

    calendar.render();
    renderSources();
  });
})();
