(function () {
  function isDeleteAction(form) {
    var action = form.getAttribute("action") || "";
    if (!action) {
      return false;
    }
    try {
      var url = new URL(action, window.location.origin);
      return /\/delete\/?$/.test(url.pathname);
    } catch (error) {
      return /\/delete\/?$/.test(action.split("?")[0]);
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !isDeleteAction(form)) {
      return;
    }
    var message = form.dataset.confirmMessage || "Soll dieser Eintrag wirklich geloescht werden?";
    if (!window.confirm(message)) {
      event.preventDefault();
    }
  });
})();
