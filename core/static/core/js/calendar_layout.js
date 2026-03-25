(function () {
  const main = document.querySelector("main.nxl-container");
  if (main) {
    main.classList.add("apps-container", "apps-calendar");
  }

  const content = document.querySelector(".nxl-content");
  if (content) {
    content.classList.add("without-header", "nxl-full-content");
  }

  let resizeTimer = null;
  let wheelGuardBound = false;

  function applyTuiScrollLayout(calendarRoot) {
    if (!calendarRoot) {
      return;
    }

    const right = calendarRoot.querySelector(".tui-full-calendar-right");
    if (right) {
      right.style.display = "flex";
      right.style.flexDirection = "column";
      right.style.minHeight = "0";
      right.style.height = "100%";
      right.style.overflowY = "hidden";
    }

    const dayHeader = calendarRoot.querySelector(".tui-full-calendar-dayname-container");
    if (dayHeader) {
      dayHeader.style.flex = "0 0 auto";
      dayHeader.style.position = "sticky";
      dayHeader.style.top = "0";
      dayHeader.style.zIndex = "5";
      dayHeader.style.background = "#fff";
      dayHeader.style.overflowY = "hidden";
    }

    const timeGrid = calendarRoot.querySelector(".tui-full-calendar-timegrid-container");
    if (timeGrid) {
      timeGrid.style.flex = "1 1 auto";
      timeGrid.style.minHeight = "0";
      timeGrid.style.height = "auto";
      timeGrid.style.overflowY = "auto";
      timeGrid.style.overflowX = "hidden";
      timeGrid.style.webkitOverflowScrolling = "touch";
    }
  }

  function applyScrollFallback(contentBody, calendarRoot) {
    if (!contentBody || !calendarRoot) {
      return;
    }

    const timeGrid = calendarRoot.querySelector(".tui-full-calendar-timegrid-container");
    if (!timeGrid) {
      contentBody.style.overflowY = "auto";
      return;
    }

    contentBody.style.overflowY = "hidden";
    contentBody.style.overflowX = "hidden";
    contentBody.style.overscrollBehavior = "none";
    timeGrid.style.overflowY = "auto";
  }

  function bindCalendarWheelGuard(contentBody, calendarRoot) {
    if (!contentBody || !calendarRoot || wheelGuardBound) {
      return;
    }
    wheelGuardBound = true;

    contentBody.addEventListener("wheel", (event) => {
      if (!calendarRoot.contains(event.target)) {
        return;
      }

      const timeGrid = calendarRoot.querySelector(".tui-full-calendar-timegrid-container");
      if (!timeGrid) {
        return;
      }

      const maxScrollTop = timeGrid.scrollHeight - timeGrid.clientHeight;
      if (maxScrollTop <= 0) {
        return;
      }

      const deltaY = Number(event.deltaY || 0);
      if (!deltaY) {
        return;
      }

      const nextScrollTop = Math.max(0, Math.min(maxScrollTop, timeGrid.scrollTop + deltaY));
      if (nextScrollTop !== timeGrid.scrollTop) {
        timeGrid.scrollTop = nextScrollTop;
      }

      event.preventDefault();
    }, { passive: false });
  }

  function setCalendarHeight() {
    const contentArea = document.querySelector(".apps-calendar .content-area.calendar-content-area")
      || document.querySelector(".content-area.calendar-content-area");
    const contentHeader = document.querySelector(".apps-calendar .content-area.calendar-content-area .content-area-header")
      || document.querySelector(".content-area.calendar-content-area .content-area-header");
    const contentBody = document.querySelector(".apps-calendar .calendar-content-body")
      || document.querySelector(".calendar-content-body");
    const calendarRoot = document.getElementById("tui-calendar-init");
    if (!contentArea || !contentBody || !calendarRoot) {
      return;
    }

    const bottomGap = 8;
    const areaTop = contentArea.getBoundingClientRect().top;
    const areaAvailable = Math.max(380, Math.floor(window.innerHeight - areaTop - bottomGap));
    const headerHeight = contentHeader ? Math.ceil(contentHeader.getBoundingClientRect().height) : 0;
    const bodyAvailable = Math.max(320, areaAvailable - headerHeight);

    contentArea.style.height = `${areaAvailable}px`;
    document.documentElement.style.setProperty("--calendar-panel-height", `${bodyAvailable}px`);
    contentBody.style.height = `${bodyAvailable}px`;
    calendarRoot.style.height = "100%";
    calendarRoot.style.minHeight = "0";
    applyTuiScrollLayout(calendarRoot);
    bindCalendarWheelGuard(contentBody, calendarRoot);

    if (window.__proCalendarInstance && typeof window.__proCalendarInstance.render === "function") {
      window.__proCalendarInstance.render(true);
      requestAnimationFrame(() => {
        applyTuiScrollLayout(calendarRoot);
        applyScrollFallback(contentBody, calendarRoot);
      });
      return;
    }

    applyScrollFallback(contentBody, calendarRoot);
  }

  function scheduleSetCalendarHeight() {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(setCalendarHeight, 40);
  }

  window.setCalendarHeight = setCalendarHeight;
  window.__setCalendarHeight = setCalendarHeight;

  requestAnimationFrame(setCalendarHeight);
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
})();
