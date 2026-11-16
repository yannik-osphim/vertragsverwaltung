(function () {
  function icon(name) {
    var node = document.createElement("span");
    node.className = "table-icon icon-" + name;
    node.setAttribute("aria-hidden", "true");
    return node;
  }

  function field(labelText, control) {
    var label = document.createElement("label");
    var text = document.createElement("span");
    text.textContent = labelText;
    label.append(text, control);
    return label;
  }

  function select(options, label) {
    var control = document.createElement("select");
    control.setAttribute("aria-label", label);
    control.dataset.searchableEnhanced = "true";
    options.forEach(function (option) { control.add(new Option(option[1], option[0])); });
    return control;
  }

  function placePanel(panel, trigger) {
    var anchor = trigger.getBoundingClientRect();
    var width = Math.min(panel.offsetWidth, window.innerWidth - 24);
    var height = panel.offsetHeight;
    var top = anchor.bottom + 8;
    if (top + height > window.innerHeight - 12 && anchor.top - height - 8 >= 12) {
      top = anchor.top - height - 8;
    }
    panel.style.left = Math.max(12, Math.min(anchor.right - width, window.innerWidth - width - 12)) + "px";
    panel.style.top = Math.max(12, Math.min(top, window.innerHeight - height - 12)) + "px";
  }

  window.createCompactTableTools = function (ctx) {
    var state = ctx.state;
    var menus = [];
    var criteria = [];
    ctx.wrapper.classList.add("compact-table-wrap");
    ctx.table.classList.add("compact-data-table");
    var scroll = document.createElement("div");
    scroll.className = "compact-table-scroll";
    ctx.wrapper.appendChild(scroll);
    scroll.appendChild(ctx.table);
    ctx.tools.classList.add("compact-table-tools");
    ctx.actions.className = "compact-table-actions";
    var searchLabel = ctx.table.dataset.searchLabel || "Tabelle durchsuchen";
    ctx.filter.placeholder = searchLabel;
    ctx.filter.setAttribute("aria-label", searchLabel);
    var search = document.createElement("div");
    search.className = "compact-table-search";
    search.append(icon("search"), ctx.filter);
    ctx.tools.prepend(search);
    ctx.tools.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && event.target.tagName === "INPUT") event.preventDefault();
    });

    function closeMenus(restoreFocus) {
      menus.forEach(function (menu) {
        if (menu.panel.hidden) return;
        menu.panel.hidden = true;
        menu.trigger.setAttribute("aria-expanded", "false");
        if (restoreFocus) menu.trigger.focus();
      });
    }

    function menu(name, iconName) {
      var trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "compact-icon-button";
      trigger.setAttribute("aria-label", name);
      trigger.setAttribute("aria-expanded", "false");
      trigger.setAttribute("aria-haspopup", "dialog");
      trigger.title = name;
      trigger.appendChild(icon(iconName));
      var panel = document.createElement("div");
      panel.className = "compact-table-panel";
      panel.id = "table-panel-" + ctx.tableIndex + "-" + menus.length;
      panel.hidden = true;
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-label", name);
      trigger.setAttribute("aria-controls", panel.id);
      var heading = document.createElement("strong");
      heading.className = "compact-panel-title";
      heading.textContent = name;
      panel.appendChild(heading);
      panel.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && event.target.tagName === "INPUT") event.preventDefault();
      });
      ctx.actions.appendChild(trigger);
      ctx.wrapper.appendChild(panel);
      var item = { trigger: trigger, panel: panel };
      menus.push(item);
      trigger.addEventListener("click", function () {
        var open = panel.hidden;
        closeMenus(false);
        ctx.table.querySelectorAll(".row-action-menu[open]").forEach(function (rowMenu) { rowMenu.open = false; });
        if (!open) return;
        panel.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        placePanel(panel, trigger);
        var control = panel.querySelector("input, select, button");
        if (control) control.focus({ preventScroll: true });
      });
      return item;
    }

    var filters = menu("Filter", "filter");
    var sorting = menu("Sortierung", "sort");
    var actions = menu("Tabellenaktionen", "more");
    var filterBadge = document.createElement("span");
    filterBadge.className = "compact-filter-badge";
    filterBadge.hidden = true;
    filters.trigger.appendChild(filterBadge);

    function update() { state.page = 1; ctx.render(); }

    var columns = ctx.headers.flatMap(function (header, index) {
      return header.dataset.sortable === "false" ? [] : [[String(index), header.textContent.trim()]];
    });
    var searchScope = select([["-1", "Alle Spalten"]].concat(columns), "Suchbereich");
    filters.panel.appendChild(field("Suchbereich", searchScope));
    searchScope.addEventListener("change", function () { state.searchIndex = Number(searchScope.value); update(); });

    function addFacet(label, valueFor) {
      var values = Array.from(new Set(ctx.rows.map(valueFor).filter(function (value) { return value && value !== "-"; })))
        .sort(function (a, b) { return a.localeCompare(b, "de", { numeric: true }); });
      if (!values.length) return;
      var control = select([["", "Alle"]].concat(values.map(function (value) { return [value, value]; })), label);
      criteria.push({ control: control, valueFor: valueFor });
      filters.panel.appendChild(field(label, control));
      control.addEventListener("change", update);
    }
    ctx.headers.forEach(function (header, index) {
      var label = header.textContent.trim();
      if (/^(status|typ|art|rolle|abrechnung|bonus-freigabe)$/i.test(label)) {
        addFacet(label, function (row) { return ctx.cellText(row, index); });
      }
    });
    if (ctx.rows.some(function (row) { return row.dataset.kind; })) {
      addFacet("Leistungsart", function (row) { return row.dataset.kind || ""; });
    }
    if (ctx.rows.some(function (row) { return row.dataset.bookingSource; })) {
      addFacet("Herkunft", function (row) { return row.dataset.bookingSource || ""; });
    }

    var dateColumns = ctx.headers.flatMap(function (header, index) {
      var label = header.textContent.trim();
      return /datum|zeitraum|start|ende|beginn|laufzeit|faellig|aktualisiert|erstellt|quittiert/i.test(label)
        && ctx.rows.some(function (row) { return ctx.dateRange(ctx.cellText(row, index)); }) ? [[String(index), label]] : [];
    });
    var hasDates = ctx.rows.some(function (row) { return row.dataset.start; }) || dateColumns.length > 0;
    var dateColumn = select(dateColumns, "Datumsfeld");
    if (dateColumns.length > 1) filters.panel.appendChild(field("Datumsfeld", dateColumn));
    dateColumn.addEventListener("change", update);
    var dates = document.createElement("div");
    dates.className = "compact-filter-dates";
    var dateInputs = {};
    [["from", "Von"], ["to", "Bis"]].forEach(function (item) {
      var control = document.createElement("input");
      control.type = "date";
      control.setAttribute("aria-label", item[1]);
      dateInputs[item[0]] = control;
      dates.appendChild(field(item[1], control));
      control.addEventListener("change", function () {
        dateInputs.to.min = dateInputs.from.value;
        dateInputs.from.max = dateInputs.to.value;
        update();
      });
    });
    if (hasDates) filters.panel.appendChild(dates);
    function resetFilters() {
      criteria.forEach(function (item) { item.control.value = ""; });
      Object.values(dateInputs).forEach(function (control) { control.value = ""; });
      dateInputs.to.min = "";
      dateInputs.from.max = "";
      dateColumn.selectedIndex = 0;
      state.searchIndex = -1;
      searchScope.value = "-1";
    }
    function command(panel, label, handler) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "compact-panel-command";
      button.append(icon("reset"), document.createTextNode(label));
      button.addEventListener("click", handler);
      panel.appendChild(button);
    }
    command(filters.panel, "Filter zuruecksetzen", function () { resetFilters(); update(); });

    var sortColumn = select([["-1", "Standardreihenfolge"]].concat(columns), "Sortieren nach");
    var sortDirection = select([["desc", "Absteigend"], ["asc", "Aufsteigend"]], "Reihenfolge");
    sorting.panel.append(field("Sortieren nach", sortColumn), field("Reihenfolge", sortDirection));
    function changeSort() {
      state.sortIndex = Number(sortColumn.value);
      state.sortDirection = sortDirection.value;
      update();
    }
    sortColumn.addEventListener("change", changeSort);
    sortDirection.addEventListener("change", changeSort);

    // Reuse the existing table engine for aggregation and pagination.
    actions.panel.appendChild(ctx.aggregation);
    actions.panel.appendChild(field("Eintraege pro Seite", ctx.pageSize));
    command(actions.panel, "Ansicht zuruecksetzen", function () {
      resetFilters();
      ctx.filter.value = "";
      state.query = "";
      state.sortIndex = ctx.defaultSortIndex;
      state.sortDirection = ctx.defaultSortDirection;
      state.aggregateEnabled = false;
      state.aggregateMethod = "sum";
      ctx.aggregateToggle.checked = false;
      ctx.aggregateMethod.value = "sum";
      state.pageSize = 10;
      ctx.pageSize.value = "10";
      update();
      closeMenus(true);
    });
    ctx.actions.appendChild(ctx.pagination);
    [[ctx.prev, "prev", "Vorherige Seite"], [ctx.next, "next", "Naechste Seite"]].forEach(function (item) {
      item[0].className = "compact-icon-button";
      item[0].textContent = "";
      item[0].title = item[2];
      item[0].setAttribute("aria-label", item[2]);
      item[0].appendChild(icon(item[1]));
    });
    ctx.pagination.prepend(ctx.pageInfo);
    ctx.pageInfo.className = "compact-page-info";
    ctx.pageInfo.setAttribute("role", "status");
    ctx.pageInfo.setAttribute("aria-live", "polite");

    var emptyRow = ctx.table.tBodies[0].querySelector(".empty")?.parentElement;
    if (!emptyRow) {
      emptyRow = ctx.table.tBodies[0].insertRow();
      var cell = emptyRow.insertCell();
      cell.colSpan = ctx.headers.length;
      cell.className = "empty";
      cell.textContent = ctx.table.dataset.emptyLabel || "Keine Eintraege gefunden.";
    }

    document.addEventListener("click", function (event) {
      if (!menus.some(function (item) { return item.panel.contains(event.target) || item.trigger.contains(event.target); })) closeMenus(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeMenus(true);
    });
    document.addEventListener("focusin", function (event) {
      if (!menus.some(function (item) { return item.panel.contains(event.target) || item.trigger.contains(event.target); })) closeMenus(false);
    });
    function closeFloatingPanels(event) {
      if (event && event.target instanceof Node && menus.some(function (item) { return item.panel.contains(event.target); })) return;
      closeMenus(false);
      ctx.table.querySelectorAll(".row-action-menu[open]").forEach(function (item) { item.open = false; });
    }
    window.addEventListener("resize", closeFloatingPanels);
    window.addEventListener("scroll", closeFloatingPanels, true);
    ctx.table.querySelectorAll(".row-action-menu").forEach(function (item) {
      var trigger = item.querySelector("summary");
      trigger.replaceChildren(icon("more"));
      if (!trigger.getAttribute("aria-label")) trigger.setAttribute("aria-label", "Aktionen");
      trigger.title = trigger.getAttribute("aria-label");
      item.addEventListener("toggle", function () {
        if (!item.open) return;
        closeMenus(false);
        placePanel(item.querySelector(".row-action-list"), item.querySelector("summary"));
      });
    });

    return {
      matches: function (row) {
        if (!criteria.every(function (item) { return !item.control.value || item.valueFor(row) === item.control.value; })) return false;
        if (!dateInputs.from.value && !dateInputs.to.value) return true;
        var period = row.dataset.start ? { start: row.dataset.start, end: row.dataset.end || row.dataset.start }
          : ctx.dateRange(ctx.cellText(row, Number(dateColumn.value)));
        return Boolean(period)
          && (!dateInputs.from.value || period.end >= dateInputs.from.value)
          && (!dateInputs.to.value || period.start <= dateInputs.to.value);
      },
      sync: function (total, start, end, pages) {
        var count = criteria.filter(function (item) { return Boolean(item.control.value); }).length
          + Object.values(dateInputs).filter(function (control) { return Boolean(control.value); }).length
          + (state.searchIndex < 0 ? 0 : 1);
        filterBadge.textContent = count;
        filterBadge.hidden = count === 0;
        filters.trigger.classList.toggle("is-active", count > 0);
        filters.trigger.title = count ? "Filter (" + count + " aktiv)" : "Filter";
        actions.trigger.classList.toggle("is-active", state.aggregateEnabled);
        sortColumn.value = String(state.sortIndex);
        sortDirection.value = state.sortDirection;
        sortDirection.disabled = state.sortIndex < 0;
        sorting.trigger.classList.toggle("is-active", state.sortIndex !== ctx.defaultSortIndex || state.sortDirection !== ctx.defaultSortDirection);
        ctx.pageInfo.textContent = (total ? start + 1 : 0) + "\u2013" + end + " / " + total;
        ctx.pageInfo.setAttribute("aria-label", total + " Eintraege, Seite " + state.page + " von " + pages);
        ctx.pageInfo.title = "Seite " + state.page + " von " + pages;
        emptyRow.style.display = total ? "none" : "";
        ctx.table.querySelectorAll(".row-action-menu[open]").forEach(function (item) { item.open = false; });
      }
    };
  };
})();
