(function () {
  function normalize(value) {
    return (value || "").toString().trim().toLowerCase();
  }

  function debounce(callback, wait) {
    var timeoutId = null;
    return function () {
      var args = arguments;
      window.clearTimeout(timeoutId);
      timeoutId = window.setTimeout(function () {
        callback.apply(null, args);
      }, wait);
    };
  }

  function cellText(row, index) {
    var cell = row.children[index];
    if (!cell) {
      return "";
    }
    var controls = Array.prototype.slice.call(cell.querySelectorAll("input:not([type='hidden']):not([type='checkbox']):not([type='radio']), select, textarea"));
    if (controls.length === 0) {
      return cell.textContent.trim();
    }
    return controls.map(function (control) {
      if (control.tagName === "SELECT") {
        var selected = control.options[control.selectedIndex];
        return selected ? selected.textContent.trim() : control.value;
      }
      return control.value;
    }).join(" ").trim();
  }

  function aggregateCellText(row, index) {
    var cell = row.children[index];
    if (!cell) {
      return "";
    }
    if (cell.dataset.aggregateValue !== undefined) {
      var suffix = cell.dataset.aggregateSuffix || "";
      return cell.dataset.aggregateValue + (suffix ? " " + suffix : "");
    }
    return cellText(row, index);
  }

  function compareRows(index, direction) {
    return function (a, b) {
      var leftCell = a.children[index];
      var rightCell = b.children[index];
      var left = leftCell && leftCell.dataset.sortValue !== undefined ? leftCell.dataset.sortValue : cellText(a, index);
      var right = rightCell && rightCell.dataset.sortValue !== undefined ? rightCell.dataset.sortValue : cellText(b, index);
      var leftDate = dateRange(left);
      var rightDate = dateRange(right);
      var leftNumber = parseNumericToken(left);
      var rightNumber = parseNumericToken(right);
      var result;
      if (leftCell && leftCell.dataset.sortType === "number") {
        result = Number(left) - Number(right);
      } else if (leftDate && rightDate) {
        result = leftDate.start.localeCompare(rightDate.start);
      } else if (leftNumber && rightNumber && leftNumber.suffix === rightNumber.suffix) {
        result = leftNumber.value - rightNumber.value;
      } else {
        result = left.localeCompare(right, "de", { numeric: true, sensitivity: "base" });
      }
      return direction === "desc" ? -result : result;
    };
  }

  function parseNumericToken(text) {
    var value = (text || "").toString().replace(/\u00a0/g, " ").trim();
    if (!value || value === "-") {
      return null;
    }

    if (/^\d{1,2}\.\d{1,2}\.\d{2,4}$/.test(value) || /^\d{4}-\d{1,2}-\d{1,2}$/.test(value)) {
      return null;
    }
    if (/\d{1,2}\.\d{1,2}\.\d{2,4}\s*[-/]/.test(value)) {
      return null;
    }

    var matches = value.match(/[+-]?(?:\d{1,3}(?:[.\s']\d{3})+|\d+)(?:[,.]\d+)?/g);
    if (!matches || matches.length !== 1) {
      return null;
    }

    var token = matches[0];
    var tokenIndex = value.indexOf(token);
    var prefix = value.slice(0, tokenIndex).trim();
    var suffix = value.slice(tokenIndex + token.length).trim();
    if (prefix && !/^[€$£+-]+$/.test(prefix)) {
      return null;
    }
    if (suffix && /[-/]\s*\d/.test(suffix)) {
      return null;
    }

    var normalized = token.replace(/[\s']/g, "");
    if (normalized.indexOf(",") >= 0) {
      normalized = normalized.replace(/\./g, "").replace(",", ".");
    } else if (/^\d{1,3}(?:\.\d{3})+$/.test(normalized)) {
      normalized = normalized.replace(/\./g, "");
    }

    var number = Number(normalized);
    if (!Number.isFinite(number)) {
      return null;
    }
    return { value: number, suffix: suffix };
  }

  function normalizeHeader(text) {
    return normalize(text).replace(/\s+/g, " ");
  }

  function dateRange(text) {
    var value = (text || "").trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return { start: value, end: value };
    var matches = value.match(/\b\d{2}\.\d{2}\.\d{4}\b/g);
    if (!matches || !/^\d{2}\.\d{2}\.\d{4}/.test(value)) return null;
    function iso(date) { return date.split(".").reverse().join("-"); }
    return { start: iso(matches[0]), end: iso(matches[matches.length - 1]) };
  }

  function shouldAggregateColumn(headerText) {
    var header = normalizeHeader(headerText);
    return !/(nummer|datev|status|datum|start|ende|zeitraum|vertrag|unternehmen|name|rolle|benutzer|email|aktion|auswahl|freigabe)/.test(header);
  }

  function detectNumericColumns(table, rows) {
    var headers = Array.prototype.slice.call(table.tHead ? table.tHead.rows[0].cells : []);
    return headers.map(function (header, index) {
      var numeric = [];
      var suffixes = [];
      rows.forEach(function (row) {
        var text = aggregateCellText(row, index);
        var parsed = parseNumericToken(text);
        if (parsed !== null) {
          numeric.push(parsed.value);
          if (parsed.suffix) {
            suffixes.push(parsed.suffix);
          }
        }
      });
      var populatedCells = rows.filter(function (row) {
        var text = aggregateCellText(row, index);
        return text && text !== "-";
      }).length;
      if (!shouldAggregateColumn(header.textContent) || numeric.length === 0 || numeric.length !== populatedCells) {
        return null;
      }
      var suffix = "";
      if (suffixes.length > 0 && suffixes.every(function (item) { return item === suffixes[0]; })) {
        suffix = suffixes[0];
      }
      return { index: index, suffix: suffix };
    }).filter(Boolean);
  }

  function formatAggregate(value, suffix) {
    var rounded = Math.round((value + Number.EPSILON) * 100) / 100;
    var options = Number.isInteger(rounded)
      ? { maximumFractionDigits: 0 }
      : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
    return rounded.toLocaleString("de-DE", options) + (suffix ? " " + suffix : "");
  }

  function aggregateValues(values, method) {
    if (method === "count") {
      return values.length;
    }
    if (values.length === 0) {
      return null;
    }
    if (method === "min") {
      return Math.min.apply(null, values);
    }
    if (method === "max") {
      return Math.max.apply(null, values);
    }
    return values.reduce(function (sum, value) {
      return sum + value;
    }, 0);
  }

  function initTable(table, tableIndex) {
    var compact = Boolean(window.createCompactTableTools);
    var compactControls = null;
    var tbody = table.tBodies[0];
    if (!tbody) {
      return;
    }
    if (!table.tHead || !table.tHead.rows[0]) {
      return;
    }

    var rows = Array.prototype.slice.call(tbody.rows).filter(function (row) {
      return !row.querySelector(".empty");
    });
    if (rows.length === 0 && !compact) {
      return;
    }

    var wrapper = table.closest(".table-wrap");
    var hasTools = wrapper && Array.prototype.some.call(wrapper.children, function (child) {
      return child.classList && child.classList.contains("table-tools");
    });
    if (!wrapper || hasTools) {
      return;
    }

    var defaultSortIndex = table.dataset.defaultSort !== undefined ? Number(table.dataset.defaultSort) : -1;
    var defaultSortDirection = table.dataset.defaultDirection || "asc";
    var state = {
      query: "",
      searchIndex: -1,
      page: 1,
      pageSize: 10,
      sortIndex: defaultSortIndex,
      sortDirection: defaultSortDirection,
      aggregateEnabled: false,
      aggregateMethod: "sum"
    };
    var numericColumns = detectNumericColumns(table, rows);

    var tools = document.createElement("div");
    tools.className = "table-tools";

    var filter = document.createElement("input");
    filter.className = "table-filter";
    filter.type = "search";
    filter.placeholder = "Tabelle filtern";
    filter.setAttribute("aria-label", "Tabelle filtern");
    tools.appendChild(filter);

    var actions = document.createElement("div");
    actions.className = "table-actions";

    var aggregation = document.createElement("div");
    aggregation.className = "table-aggregation";

    var aggregateLabel = document.createElement("label");
    var aggregateToggle = document.createElement("input");
    aggregateToggle.type = "checkbox";
    aggregateToggle.disabled = numericColumns.length === 0;
    aggregateLabel.appendChild(aggregateToggle);
    aggregateLabel.appendChild(document.createTextNode("Aggregation"));

    var aggregateMethod = document.createElement("select");
    aggregateMethod.setAttribute("aria-label", "Aggregationsmethode");
    [
      ["sum", "Summe"],
      ["min", "Min"],
      ["max", "Max"],
      ["count", "Anzahl"]
    ].forEach(function (option) {
      var node = document.createElement("option");
      node.value = option[0];
      node.textContent = option[1];
      aggregateMethod.appendChild(node);
    });
    aggregateMethod.disabled = numericColumns.length === 0;

    aggregation.appendChild(aggregateLabel);
    aggregation.appendChild(aggregateMethod);
    actions.appendChild(aggregation);

    var pagination = document.createElement("div");
    pagination.className = "table-pagination";

    var pageSize = document.createElement("select");
    pageSize.setAttribute("aria-label", "Zeilen pro Seite");
    [
      ["10", "10"],
      ["25", "25"],
      ["50", "50"],
      ["0", "Alle"]
    ].forEach(function (option) {
      var node = document.createElement("option");
      node.value = option[0];
      node.textContent = option[1];
      pageSize.appendChild(node);
    });

    var prev = document.createElement("button");
    prev.className = "button subtle";
    prev.type = "button";
    prev.textContent = "Zurueck";

    var pageInfo = document.createElement("span");

    var next = document.createElement("button");
    next.className = "button subtle";
    next.type = "button";
    next.textContent = "Weiter";

    pagination.appendChild(pageSize);
    pagination.appendChild(prev);
    pagination.appendChild(pageInfo);
    pagination.appendChild(next);
    actions.appendChild(pagination);
    tools.appendChild(actions);
    wrapper.insertBefore(tools, table);

    var aggregateFoot = table.tFoot || table.createTFoot();
    var aggregateRow = document.createElement("tr");
    aggregateRow.className = "aggregate-row";
    var headerCells = Array.prototype.slice.call(table.tHead ? table.tHead.rows[0].cells : []);
    headerCells.forEach(function (header, index) {
      var name = normalizeHeader(header.textContent);
      if (/^(aktion(en)?|auswahl|freigabe)$/.test(name) || header.querySelector("input[type='checkbox']")) {
        header.dataset.sortable = "false";
        header.classList.add(name.indexOf("aktion") === 0 ? "table-action-heading" : "table-selection-heading");
      }
      if (rows.some(function (row) { return row.children[index] && row.children[index].querySelector("input[type='checkbox']"); })) {
        header.classList.add("table-selection-heading");
      }
      aggregateRow.appendChild(document.createElement("td"));
    });
    numericColumns.forEach(function (column) {
      headerCells[column.index].classList.add("numeric");
      rows.forEach(function (row) { if (row.children[column.index]) row.children[column.index].classList.add("numeric"); });
    });
    aggregateFoot.appendChild(aggregateRow);

    if (compact) {
      compactControls = window.createCompactTableTools({
        table: table, wrapper: wrapper, tools: tools, filter: filter, actions: actions,
        aggregation: aggregation, aggregateToggle: aggregateToggle, aggregateMethod: aggregateMethod,
        pagination: pagination, pageSize: pageSize, prev: prev, next: next, pageInfo: pageInfo,
        state: state, rows: rows, headers: headerCells, render: render, tableIndex: tableIndex,
        cellText: cellText, dateRange: dateRange, defaultSortIndex: defaultSortIndex, defaultSortDirection: defaultSortDirection
      });
    }

    headerCells.forEach(function (header, index) {
      if (header.dataset.sortable === "false") {
        return;
      }
      header.classList.add("sortable-heading");
      header.tabIndex = 0;
      header.addEventListener("click", function () {
        if (state.sortIndex === index) {
          state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
        } else {
          state.sortIndex = index;
          state.sortDirection = "asc";
        }
        state.page = 1;
        render();
      });
      header.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          header.click();
        }
      });
    });

    function filteredRows() {
      var query = normalize(state.query);
      var current = rows.filter(function (row) {
        var rowText = Array.prototype.slice.call(row.children).map(function (_, index) {
          return (state.searchIndex < 0 || state.searchIndex === index) && headerCells[index]?.dataset.sortable !== "false" ? cellText(row, index) : "";
        }).join(" ");
        var matchesSearch = compact
          ? query.split(/\s+/).every(function (term) { return normalize(rowText).indexOf(term) >= 0; })
          : normalize(rowText).indexOf(query) >= 0;
        if (!matchesSearch || (compactControls && !compactControls.matches(row))) {
          return false;
        }
        return true;
      });
      if (state.sortIndex >= 0) {
        current.sort(compareRows(state.sortIndex, state.sortDirection));
      }
      return current;
    }

    function updateAggregation(current) {
      aggregateRow.style.display = state.aggregateEnabled ? "" : "none";
      if (!state.aggregateEnabled) {
        return;
      }

      var cells = Array.prototype.slice.call(aggregateRow.children);
      cells.forEach(function (cell, index) {
        cell.textContent = index === 0 ? "Aggregation (" + aggregateMethod.options[aggregateMethod.selectedIndex].text + ")" : "";
        cell.className = index === 0 ? "aggregate-label" : "";
      });

      numericColumns.forEach(function (column) {
        var values = current.map(function (row) {
          var parsed = parseNumericToken(aggregateCellText(row, column.index));
          return parsed ? parsed.value : null;
        }).filter(function (value) {
          return value !== null;
        });
        var aggregate = aggregateValues(values, state.aggregateMethod);
        var cell = cells[column.index];
        if (!cell) {
          return;
        }
        cell.className = "aggregate-value";
        cell.textContent = aggregate === null
          ? "-"
          : formatAggregate(aggregate, state.aggregateMethod === "count" ? "" : column.suffix);
      });
    }

    function render() {
      var current = filteredRows();
      var total = current.length;
      var size = state.pageSize || total || 1;
      var pageCount = Math.max(1, Math.ceil(total / size));
      state.page = Math.min(Math.max(1, state.page), pageCount);
      var start = state.pageSize === 0 ? 0 : (state.page - 1) * size;
      var end = state.pageSize === 0 ? total : start + size;
      var visible = new Set(current.slice(start, end));

      current.forEach(function (row) {
        row.style.display = visible.has(row) ? "" : "none";
        tbody.appendChild(row);
      });
      rows.filter(function (row) {
        return current.indexOf(row) === -1;
      }).forEach(function (row) {
        row.style.display = "none";
        tbody.appendChild(row);
      });

      prev.disabled = state.page <= 1;
      next.disabled = state.page >= pageCount;
      pageInfo.textContent = total + " Eintraege - Seite " + state.page + "/" + pageCount;
      if (compactControls) {
        compactControls.sync(total, start, Math.min(end, total), pageCount);
        headerCells.forEach(function (header, index) {
          if (header.dataset.sortable !== "false") {
            header.setAttribute("aria-sort", state.sortIndex === index
              ? (state.sortDirection === "asc" ? "ascending" : "descending") : "none");
          }
        });
      }
      updateAggregation(current);
    }

    filter.addEventListener("input", function () {
      state.query = filter.value;
      state.page = 1;
      render();
    });

    pageSize.addEventListener("change", function () {
      state.pageSize = parseInt(pageSize.value, 10);
      state.page = 1;
      render();
    });

    prev.addEventListener("click", function () {
      state.page -= 1;
      render();
    });

    next.addEventListener("click", function () {
      state.page += 1;
      render();
    });

    aggregateToggle.addEventListener("change", function () {
      state.aggregateEnabled = aggregateToggle.checked;
      render();
    });

    aggregateMethod.addEventListener("change", function () {
      state.aggregateMethod = aggregateMethod.value;
      render();
    });

    function refreshLiveAggregation() {
      // Invoice forms recalculate their amounts on the same bubbling event.
      queueMicrotask(function () { updateAggregation(filteredRows()); });
    }
    table.addEventListener("input", refreshLiveAggregation);
    table.addEventListener("change", refreshLiveAggregation);

    table.dataset.enhancedTable = String(tableIndex);
    render();
  }

  function initSearchableSelect(select) {
    var filterSourceName = select.dataset.filterContractSelect || "";
    var shouldSearch = select.options.length > 10 || select.dataset.searchable === "true";
    if (select.dataset.searchableEnhanced || select.multiple || (!shouldSearch && !filterSourceName)) {
      return;
    }

    var allOptions = Array.prototype.slice.call(select.options).map(function (option) {
      var dataset = {};
      Object.keys(option.dataset).forEach(function (key) {
        dataset[key] = option.dataset[key];
      });
      return {
        value: option.value,
        text: option.textContent,
        disabled: option.disabled,
        selected: option.selected,
        contractId: option.dataset.contractId || "",
        dataset: dataset
      };
    });
    var selectedValue = select.value || (allOptions[0] ? allOptions[0].value : "");

    var wrapper = document.createElement("div");
    wrapper.className = "searchable-select";
    var display = document.createElement("button");
    display.type = "button";
    display.className = "select-display";
    display.setAttribute("aria-haspopup", "listbox");
    display.setAttribute("aria-expanded", "false");
    if (select.dataset.label) {
      display.setAttribute("aria-label", select.dataset.label);
    }

    var menu = document.createElement("div");
    menu.className = "select-menu";
    menu.hidden = true;

    var search = document.createElement("input");
    search.type = "search";
    search.className = "select-search";
    search.placeholder = "Auswahl suchen";
    search.setAttribute("aria-label", "Auswahl suchen");

    var list = document.createElement("div");
    list.className = "select-options";
    list.setAttribute("role", "listbox");

    menu.appendChild(search);
    menu.appendChild(list);
    wrapper.appendChild(display);
    wrapper.appendChild(menu);
    select.parentNode.insertBefore(wrapper, select.nextSibling);
    select.classList.add("native-select-hidden");

    function contractSelect() {
      if (!filterSourceName || !select.form) {
        return null;
      }
      return select.form.querySelector('[name="' + filterSourceName + '"]');
    }

    function optionMatchesContract(optionData) {
      var source = contractSelect();
      if (!source || !source.value || !optionData.contractId) {
        return true;
      }
      return optionData.contractId === source.value;
    }

    function selectedOptionData() {
      return allOptions.find(function (option) {
        return option.value === selectedValue && optionMatchesContract(option);
      });
    }

    function allowedOptions() {
      return allOptions.filter(optionMatchesContract);
    }

    function appendNativeOption(optionData) {
      var option = document.createElement("option");
      option.value = optionData.value;
      option.textContent = optionData.text;
      option.disabled = optionData.disabled;
      if (optionData.dataset) {
        Object.keys(optionData.dataset).forEach(function (key) {
          option.dataset[key] = optionData.dataset[key];
        });
      }
      select.appendChild(option);
    }

    function syncNativeOptions(options) {
      select.innerHTML = "";
      if (options.length === 0) {
        var emptyLabel = select.dataset.emptyLabel || "Keine Treffer";
        appendNativeOption({ value: "", text: emptyLabel, disabled: true });
        selectedValue = "";
        select.value = "";
        display.textContent = emptyLabel;
        display.disabled = true;
        return;
      }
      display.disabled = select.disabled;
      options.forEach(appendNativeOption);
      if (!options.some(function (option) { return option.value === selectedValue; })) {
        selectedValue = options[0].value;
      }
      select.value = selectedValue;
      var selected = selectedOptionData();
      display.textContent = selected ? selected.text : options[0].text;
    }

    function closeMenu() {
      menu.hidden = true;
      display.setAttribute("aria-expanded", "false");
      wrapper.classList.remove("open");
    }

    function openMenu() {
      menu.hidden = false;
      display.setAttribute("aria-expanded", "true");
      wrapper.classList.add("open");
      renderOptions();
      search.focus();
      search.select();
    }

    function chooseOption(optionData) {
      if (optionData.disabled) {
        return;
      }
      selectedValue = optionData.value;
      syncNativeOptions(allowedOptions());
      select.dispatchEvent(new Event("change", { bubbles: true }));
      closeMenu();
    }

    function renderOptions() {
      var query = normalize(search.value);
      var options = allowedOptions();
      syncNativeOptions(options);
      var selectedOption = selectedOptionData();
      var matches = options.filter(function (option) {
        return !query || normalize(option.text + " " + option.value + " " + (option.dataset.optionDescription || "")).indexOf(query) !== -1;
      });
      var visible = [];

      if (selectedOption && matches.indexOf(selectedOption) === -1) {
        visible.push(selectedOption);
      }

      matches.forEach(function (option) {
        if (visible.length >= 10) {
          return;
        }
        if (!visible.some(function (item) { return item.value === option.value; })) {
          visible.push(option);
        }
      });

      list.innerHTML = "";
      if (visible.length === 0) {
        var empty = document.createElement("div");
        empty.className = "select-empty";
        empty.textContent = "Keine Treffer";
        list.appendChild(empty);
        return;
      }

      visible.forEach(function (optionData) {
        var optionButton = document.createElement("button");
        optionButton.type = "button";
        optionButton.className = "select-option";
        optionButton.textContent = optionData.text;
        if (optionData.dataset.optionDescription) {
          var description = document.createElement("small");
          description.className = "select-option-description";
          description.textContent = optionData.dataset.optionDescription;
          optionButton.appendChild(description);
        }
        optionButton.setAttribute("role", "option");
        optionButton.setAttribute("aria-selected", optionData.value === selectedValue ? "true" : "false");
        optionButton.disabled = optionData.disabled;
        optionButton.addEventListener("click", function () {
          chooseOption(optionData);
        });
        list.appendChild(optionButton);
      });
    }

    var debouncedRenderOptions = debounce(renderOptions, 180);
    search.addEventListener("input", debouncedRenderOptions);
    display.addEventListener("click", function () {
      if (menu.hidden) {
        openMenu();
      } else {
        closeMenu();
      }
    });
    search.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeMenu();
        display.focus();
      }
    });
    document.addEventListener("click", function (event) {
      if (!wrapper.contains(event.target) && event.target !== select) {
        closeMenu();
      }
    });
    select.addEventListener("change", function () {
      selectedValue = select.value;
      syncNativeOptions(allowedOptions());
      renderOptions();
    });
    var source = contractSelect();
    if (source) {
      source.addEventListener("change", function () {
        search.value = "";
        syncNativeOptions(allowedOptions());
        renderOptions();
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    select.dataset.searchableEnhanced = "true";
    syncNativeOptions(allowedOptions());
    renderOptions();
  }

  function initSearchableSelects() {
    Array.prototype.slice.call(document.querySelectorAll("select")).forEach(initSearchableSelect);
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll(".table-wrap table")).forEach(initTable);
    initSearchableSelects();
  });
})();
