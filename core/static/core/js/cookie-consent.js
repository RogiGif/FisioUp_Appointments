(function () {
    const banner = document.getElementById("cookie-consent-banner");
    const modalEl = document.getElementById("cookiePreferencesModal");
    if (!banner || !modalEl) return;

    const analyticsToggle = document.getElementById("cookieAnalytics");
    const marketingToggle = document.getElementById("cookieMarketing");

    const getCookie = (name) => {
        const match = document.cookie.match(new RegExp("(^|; )" + name + "=([^;]*)"));
        return match ? decodeURIComponent(match[2]) : "";
    };

    const setCookie = (name, value, days) => {
        const expires = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toUTCString();
        let cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax`;
        if (window.location.protocol === "https:") {
            cookie += "; Secure";
        }
        document.cookie = cookie;
    };

    const parseConsent = (raw) => {
        if (!raw) return null;
        try {
            const parsed = JSON.parse(raw);
            if (!parsed || typeof parsed !== "object") return null;
            return {
                necessary: true,
                analytics: !!parsed.analytics,
                marketing: !!parsed.marketing,
                ts: parsed.ts || new Date().toISOString(),
            };
        } catch (err) {
            return null;
        }
    };

    const showBanner = () => {
        banner.style.display = "block";
    };

    const hideBanner = () => {
        banner.style.display = "none";
    };

    const ensureModal = () => {
        if (window.bootstrap && window.bootstrap.Modal) {
            return window.bootstrap.Modal.getOrCreateInstance(modalEl);
        }
        return null;
    };

    const openModal = () => {
        const modal = ensureModal();
        if (modal) {
            modal.show();
            return;
        }
        modalEl.classList.add("show");
        modalEl.style.display = "block";
        modalEl.removeAttribute("aria-hidden");
    };

    const closeModal = () => {
        const modal = ensureModal();
        if (modal) {
            modal.hide();
            return;
        }
        modalEl.classList.remove("show");
        modalEl.style.display = "none";
        modalEl.setAttribute("aria-hidden", "true");
    };

    const applyConsent = (consent) => {
        if (analyticsToggle) analyticsToggle.checked = !!consent.analytics;
        if (marketingToggle) marketingToggle.checked = !!consent.marketing;
        if (consent.analytics) loadAnalyticsScripts();
        if (consent.marketing) loadMarketingScripts();
    };

    const saveConsent = (consent) => {
        setCookie("cookie_consent", JSON.stringify(consent), 180);
        applyConsent(consent);
        hideBanner();
        window.dispatchEvent(new CustomEvent("cookie-consent-updated", { detail: consent }));
    };

    const loadAnalyticsScripts = () => {
        // TODO: adicionar scripts analiticos (ex: Google Analytics) quando permitido.
    };

    const loadMarketingScripts = () => {
        // TODO: adicionar scripts de marketing (ex: Meta Pixel) quando permitido.
    };

    const acceptAllBtn = document.getElementById("cookieAcceptAll");
    const rejectBtn = document.getElementById("cookieReject");
    const saveBtn = document.getElementById("cookieSavePreferences");
    const openPrefBtn = document.getElementById("cookieOpenPreferences");

    acceptAllBtn?.addEventListener("click", () => {
        saveConsent({ necessary: true, analytics: true, marketing: true, ts: new Date().toISOString() });
    });

    rejectBtn?.addEventListener("click", () => {
        saveConsent({ necessary: true, analytics: false, marketing: false, ts: new Date().toISOString() });
    });

    saveBtn?.addEventListener("click", () => {
        saveConsent({
            necessary: true,
            analytics: !!analyticsToggle?.checked,
            marketing: !!marketingToggle?.checked,
            ts: new Date().toISOString(),
        });
        closeModal();
    });

    openPrefBtn?.addEventListener("click", () => {
        openModal();
    });

    document.querySelectorAll("[data-cookie-preferences]").forEach((el) => {
        el.addEventListener("click", (event) => {
            event.preventDefault();
            openModal();
        });
    });

    const existing = parseConsent(getCookie("cookie_consent"));
    if (existing) {
        applyConsent(existing);
        hideBanner();
    } else {
        showBanner();
    }
})();
