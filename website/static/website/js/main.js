document.addEventListener("DOMContentLoaded", function () {
    var toggler = document.querySelector(".website-navbar .navbar-toggler");
    var targetSelector = toggler ? toggler.getAttribute("data-target") : null;
    var nav = targetSelector ? document.querySelector(targetSelector) : null;

    if (toggler && nav) {
        toggler.addEventListener("click", function () {
            var isOpen = nav.classList.toggle("show");
            toggler.setAttribute("aria-expanded", String(isOpen));
        });

        var links = nav.querySelectorAll("a.nav-link, .btn-brand");
        links.forEach(function (link) {
            link.addEventListener("click", function () {
                if (window.innerWidth < 992) {
                    nav.classList.remove("show");
                    toggler.setAttribute("aria-expanded", "false");
                }
            });
        });
    }
});
