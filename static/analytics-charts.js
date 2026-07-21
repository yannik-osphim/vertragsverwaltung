(function () {
  var dataElement = document.getElementById("analytics-chart-data");
  if (!dataElement || !window.Chart) {
    return;
  }

  var data;
  try {
    data = JSON.parse(dataElement.textContent || "{}");
  } catch (error) {
    return;
  }

  var rootStyle = getComputedStyle(document.documentElement);
  function css(name, fallback) {
    return (rootStyle.getPropertyValue(name) || fallback).trim();
  }

  var colors = {
    ink: css("--ink", "#05062f"),
    muted: css("--muted", "#60657f"),
    line: css("--line", "#d9dff0"),
    primary: css("--primary", "#4430c7"),
    accent: css("--accent", "#06aee8"),
    warm: css("--accent-warm", "#f04467"),
    green: css("--green", "#0d8d73"),
    amber: css("--amber", "#b56a00"),
    teal: css("--teal", "#087f99"),
  };
  var seriesPalette = [
    colors.primary,
    colors.accent,
    colors.warm,
    colors.green,
    colors.amber,
    colors.teal,
    "#7c5cff",
    "#00b393",
    "#ff8a4c",
    "#7d89b0",
  ];

  var currencyFormatter = new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  });
  var exactCurrencyFormatter = new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: "EUR",
  });
  var numberFormatter = new Intl.NumberFormat("de-DE", {
    maximumFractionDigits: 2,
  });

  function money(cents, exact) {
    var value = Number(cents || 0) / 100;
    return (exact ? exactCurrencyFormatter : currencyFormatter).format(value);
  }

  function canvas(name) {
    return document.querySelector('[data-analytics-chart="' + name + '"]');
  }

  function baseOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "index",
        intersect: false,
      },
      plugins: {
        legend: {
          labels: {
            color: colors.ink,
            usePointStyle: true,
            boxWidth: 9,
            boxHeight: 9,
          },
        },
        tooltip: {
          backgroundColor: "rgba(5, 6, 47, 0.92)",
          titleColor: "#ffffff",
          bodyColor: "#ffffff",
          borderColor: colors.line,
          borderWidth: 1,
        },
      },
      scales: {
        x: {
          ticks: { color: colors.muted },
          grid: { color: "transparent" },
        },
      },
    };
  }

  function currencyScale(position) {
    return {
      position: position || "left",
      ticks: {
        color: colors.muted,
        callback: function (value) {
          return money(value);
        },
      },
      grid: {
        color: colors.line,
        drawOnChartArea: position !== "right",
      },
    };
  }

  function numberScale(position, suffix) {
    return {
      position: position || "left",
      beginAtZero: true,
      ticks: {
        color: colors.muted,
        callback: function (value) {
          return numberFormatter.format(value) + (suffix || "");
        },
      },
      grid: {
        color: colors.line,
        drawOnChartArea: position !== "right",
      },
    };
  }

  function lineDataset(label, values, color, yAxisID) {
    return {
      type: "line",
      label: label,
      data: values || [],
      borderColor: color,
      backgroundColor: color,
      yAxisID: yAxisID || "y",
      tension: 0.32,
      pointRadius: 3,
      pointHoverRadius: 5,
      borderWidth: 2,
    };
  }

  function renderRevenueTrend() {
    var target = canvas("revenue-trend");
    if (!target) {
      return;
    }
    var options = baseOptions();
    options.scales.y = currencyScale("left");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + money(context.parsed.y, true);
      },
    };
    new Chart(target, {
      type: "line",
      data: {
        labels: data.labels || [],
        datasets: [
          Object.assign(lineDataset(data.revenueTrend.label, data.revenueTrend.values, colors.primary), {
            fill: true,
            backgroundColor: "rgba(68, 48, 199, 0.12)",
          }),
        ],
      },
      options: options,
    });
  }

  function renderRevenueComponents() {
    var target = canvas("revenue-components");
    if (!target) {
      return;
    }
    var options = baseOptions();
    options.scales.x.stacked = true;
    options.scales.y = currencyScale("left");
    options.scales.y.stacked = true;
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + money(context.parsed.y, true);
      },
    };
    new Chart(target, {
      type: "bar",
      data: {
        labels: data.labels || [],
        datasets: [
          {
            label: "Lizenzen",
            data: data.revenueComponents.license || [],
            backgroundColor: "rgba(68, 48, 199, 0.82)",
            stack: "revenue",
          },
          {
            label: "Dienstleistungen",
            data: data.revenueComponents.service || [],
            backgroundColor: "rgba(6, 174, 232, 0.78)",
            stack: "revenue",
          },
          {
            label: "Pauschalen",
            data: data.revenueComponents.flatFee || [],
            backgroundColor: "rgba(13, 141, 115, 0.78)",
            stack: "revenue",
          },
          {
            label: "Variable Kostensaetze",
            data: data.revenueComponents.variableCost || [],
            backgroundColor: "rgba(240, 68, 103, 0.78)",
            stack: "revenue",
          },
        ],
      },
      options: options,
    });
  }

  function renderYearComparison() {
    var target = canvas("year-comparison");
    if (!target) {
      return;
    }
    var comparison = data.yearComparison || {};
    var options = baseOptions();
    options.scales.yRevenue = currencyScale("left");
    options.scales.yDiff = currencyScale("right");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + money(context.parsed.y, true);
      },
    };
    new Chart(target, {
      type: "bar",
      data: {
        labels: data.shortLabels || data.labels || [],
        datasets: [
          lineDataset("Umsatz " + comparison.previousYear, comparison.previous || [], colors.muted, "yRevenue"),
          lineDataset("Umsatz " + comparison.currentYear, comparison.current || [], colors.primary, "yRevenue"),
          {
            type: "bar",
            label: "Differenz",
            data: comparison.difference || [],
            yAxisID: "yDiff",
            backgroundColor: function (context) {
              return (context.raw || 0) >= 0 ? "rgba(13, 141, 115, 0.72)" : "rgba(240, 68, 103, 0.72)";
            },
            borderRadius: 6,
          },
        ],
      },
      options: options,
    });
  }

  function renderRecurringRevenue() {
    var target = canvas("recurring-revenue");
    if (!target) {
      return;
    }
    var recurring = data.recurringRevenue || {};
    var options = baseOptions();
    options.scales.y = currencyScale("left");
    options.scales.y1 = currencyScale("right");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + money(context.parsed.y, true);
      },
    };
    new Chart(target, {
      type: "line",
      data: {
        labels: data.labels || [],
        datasets: [
          lineDataset("MRR", recurring.mrr || [], colors.accent, "y"),
          lineDataset("ARR", recurring.arr || [], colors.primary, "y1"),
        ],
      },
      options: options,
    });
  }

  function renderActiveLicenses() {
    var target = canvas("active-licenses");
    if (!target) {
      return;
    }
    var licenses = data.activeLicenses || {};
    var options = baseOptions();
    options.scales.yCount = numberScale("left", "");
    options.scales.yAverage = currencyScale("right");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        if (context.dataset.yAxisID === "yAverage") {
          return context.dataset.label + ": " + money(context.parsed.y, true);
        }
        return context.dataset.label + ": " + numberFormatter.format(context.parsed.y);
      },
    };
    new Chart(target, {
      type: "bar",
      data: {
        labels: data.labels || [],
        datasets: [
          lineDataset("Lizenzanzahl", licenses.count || [], colors.primary, "yCount"),
          {
            type: "bar",
            label: "Durchschnittlicher Lizenzumsatz",
            data: licenses.averageRevenue || [],
            yAxisID: "yAverage",
            backgroundColor: "rgba(6, 174, 232, 0.72)",
            borderRadius: 6,
          },
        ],
      },
      options: options,
    });
  }

  function renderLicenseTypes() {
    var target = canvas("license-types");
    if (!target) {
      return;
    }
    var licenseTypes = data.licenseTypes || {};
    var options = baseOptions();
    options.scales.y = numberScale("left", "");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + numberFormatter.format(context.parsed.y);
      },
    };
    new Chart(target, {
      type: "line",
      data: {
        labels: data.labels || [],
        datasets: (licenseTypes.series || []).map(function (series, index) {
          return lineDataset(
            series.label,
            series.values || [],
            seriesPalette[index % seriesPalette.length],
            "y"
          );
        }),
      },
      options: options,
    });
  }

  function renderInvoiceActivity() {
    var target = canvas("invoice-activity");
    if (!target) {
      return;
    }
    var invoices = data.invoiceActivity || {};
    var options = baseOptions();
    options.scales.yCount = numberScale("left", "");
    options.scales.yAverage = currencyScale("right");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        if (context.dataset.yAxisID === "yAverage") {
          return context.dataset.label + ": " + money(context.parsed.y, true);
        }
        return context.dataset.label + ": " + numberFormatter.format(context.parsed.y);
      },
    };
    new Chart(target, {
      type: "bar",
      data: {
        labels: data.labels || [],
        datasets: [
          lineDataset("Rechnungen", invoices.count || [], colors.primary, "yCount"),
          {
            type: "bar",
            label: "Durchschnittliche Rechnungssumme",
            data: invoices.averageAmount || [],
            yAxisID: "yAverage",
            backgroundColor: "rgba(13, 141, 115, 0.72)",
            borderRadius: 6,
          },
        ],
      },
      options: options,
    });
  }

  function renderDiscounts() {
    var target = canvas("discounts");
    if (!target) {
      return;
    }
    var discounts = data.discounts || {};
    var options = baseOptions();
    options.scales.y = currencyScale("left");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + money(context.parsed.y, true);
      },
    };
    new Chart(target, {
      type: "line",
      data: {
        labels: data.labels || [],
        datasets: [
          Object.assign(lineDataset("Rabatte", discounts.values || [], colors.warm, "y"), {
            fill: true,
            backgroundColor: "rgba(240, 68, 103, 0.12)",
          }),
        ],
      },
      options: options,
    });
  }

  function renderBookedHours() {
    var target = canvas("booked-hours");
    if (!target) {
      return;
    }
    var options = baseOptions();
    options.scales.y = numberScale("left", " h");
    options.plugins.tooltip.callbacks = {
      label: function (context) {
        return context.dataset.label + ": " + numberFormatter.format(context.parsed.y) + " h";
      },
    };
    new Chart(target, {
      type: "line",
      data: {
        labels: data.labels || [],
        datasets: [
          Object.assign(lineDataset("Gebuchte Stunden", (data.bookedHours || {}).hours || [], colors.warm, "y"), {
            fill: true,
            backgroundColor: "rgba(240, 68, 103, 0.12)",
          }),
        ],
      },
      options: options,
    });
  }

  Chart.defaults.color = colors.ink;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  renderRevenueTrend();
  renderRevenueComponents();
  renderYearComparison();
  renderRecurringRevenue();
  renderActiveLicenses();
  renderLicenseTypes();
  renderInvoiceActivity();
  renderDiscounts();
  renderBookedHours();
})();
