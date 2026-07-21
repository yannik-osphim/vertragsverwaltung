(function () {
  const selector = "details.row-action-menu";

  function syncWrap(menu) {
    const wrap = menu.closest(".table-wrap");
    if (!wrap) {
      return;
    }
    wrap.classList.toggle("action-menu-open", Boolean(wrap.querySelector(selector + "[open]")));
  }

  function positionMenu(menu) {
    const trigger = menu.querySelector(".row-action-trigger");
    const list = menu.querySelector(".row-action-list");
    if (!trigger || !list) {
      return;
    }
    if (!menu.open) {
      list.style.removeProperty("--row-action-left");
      list.style.removeProperty("--row-action-top");
      return;
    }
    const triggerRect = trigger.getBoundingClientRect();
    const listRect = list.getBoundingClientRect();
    const margin = 8;
    const left = Math.min(
      window.innerWidth - listRect.width - margin,
      Math.max(margin, triggerRect.right - listRect.width)
    );
    const top = Math.min(
      window.innerHeight - listRect.height - margin,
      Math.max(margin, triggerRect.bottom + 6)
    );
    list.style.setProperty("--row-action-left", left + "px");
    list.style.setProperty("--row-action-top", top + "px");
  }

  function positionOpenMenus() {
    document.querySelectorAll(selector + "[open]").forEach(positionMenu);
  }

  function closeMenu(menu) {
    menu.open = false;
    positionMenu(menu);
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
          positionMenu(menu);
        }
        syncWrap(menu);
      });
    });
  });

  window.addEventListener("resize", positionOpenMenus);
  window.addEventListener("scroll", positionOpenMenus, true);

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
