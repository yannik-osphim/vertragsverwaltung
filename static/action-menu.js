(function () {
  const selector = "details.row-action-menu";

  function syncWrap(menu) {
    const wrap = menu.closest(".table-wrap");
    if (!wrap) {
      return;
    }
    wrap.classList.toggle("action-menu-open", Boolean(wrap.querySelector(selector + "[open]")));
  }

  function closeMenu(menu) {
    menu.open = false;
    syncWrap(menu);
  }

  function closeOtherMenus(activeMenu) {
    document.querySelectorAll(selector + "[open]").forEach((menu) => {
      if (menu !== activeMenu) {
        closeMenu(menu);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(selector).forEach((menu) => {
      menu.addEventListener("toggle", () => {
        if (menu.open) {
          closeOtherMenus(menu);
        }
        syncWrap(menu);
      });
    });
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest(selector)) {
      return;
    }
    closeOtherMenus(null);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeOtherMenus(null);
    }
  });
})();
