(function () {
  var storageKey = "osphim-theme";
  var root = document.documentElement;

  function preferredTheme() {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function currentTheme() {
    return root.dataset.theme || preferredTheme();
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.textContent = theme === "dark" ? "Hell" : "Dunkel";
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
  }

  try {
    applyTheme(localStorage.getItem(storageKey) || currentTheme());
  } catch (error) {
    applyTheme(currentTheme());
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-toggle]");
    if (!button) {
      return;
    }
    var nextTheme = currentTheme() === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(storageKey, nextTheme);
    } catch (error) {
    }
    applyTheme(nextTheme);
  });

  if (window.matchMedia) {
    var mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    mediaQuery.addEventListener("change", function (event) {
      try {
        if (localStorage.getItem(storageKey)) {
          return;
        }
      } catch (error) {
      }
      applyTheme(event.matches ? "dark" : "light");
    });
  }
})();
