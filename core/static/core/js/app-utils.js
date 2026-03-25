(function () {
  if (window.AppUtils && typeof window.AppUtils.debounce === "function") {
    return;
  }

  function debounce(fn, wait) {
    let timer = null;
    let lastArgs = null;
    let lastThis = null;

    function clear() {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    }

    function invoke() {
      timer = null;
      const args = lastArgs;
      const context = lastThis;
      lastArgs = null;
      lastThis = null;
      return fn.apply(context, args || []);
    }

    function debounced() {
      lastArgs = arguments;
      lastThis = this;
      clear();
      timer = window.setTimeout(invoke, wait);
    }

    debounced.cancel = function () {
      clear();
      lastArgs = null;
      lastThis = null;
    };

    debounced.flush = function () {
      if (!timer) {
        return undefined;
      }
      clear();
      return invoke();
    };

    return debounced;
  }

  window.AppUtils = Object.assign({}, window.AppUtils || {}, {
    debounce: debounce,
  });
})();
