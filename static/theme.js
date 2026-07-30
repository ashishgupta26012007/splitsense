(function () {
  const toggle = document.getElementById("theme-toggle");
  const saved = localStorage.getItem("splitsense-theme");

  if (saved === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
    if (toggle) toggle.textContent = "☀️";
  }

  if (toggle) {
    toggle.addEventListener("click", () => {
      const isDark = document.documentElement.getAttribute("data-theme") === "dark";
      if (isDark) {
        document.documentElement.removeAttribute("data-theme");
        toggle.textContent = "🌙";
        localStorage.setItem("splitsense-theme", "light");
      } else {
        document.documentElement.setAttribute("data-theme", "dark");
        toggle.textContent = "☀️";
        localStorage.setItem("splitsense-theme", "dark");
      }
    });
  }
})();