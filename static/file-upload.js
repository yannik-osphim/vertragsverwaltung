(function () {
  function acceptLabel(value) {
    if (!value) {
      return "Datei";
    }
    return value
      .split(",")
      .map(function (part) {
        return part.trim().replace("application/", "").replace("image/", "").replace(".", "").toUpperCase();
      })
      .filter(Boolean)
      .slice(0, 4)
      .join(", ");
  }

  function singleFileList(files) {
    if (!files || files.length <= 1 || typeof DataTransfer === "undefined") {
      return files;
    }
    var transfer = new DataTransfer();
    transfer.items.add(files[0]);
    return transfer.files;
  }

  function enhanceFileInput(input) {
    if (input.dataset.fileDropEnhanced === "true") {
      return;
    }
    input.dataset.fileDropEnhanced = "true";
    input.classList.add("file-input-native");

    var zone = document.createElement("div");
    zone.className = "file-dropzone";
    zone.setAttribute("role", "button");
    zone.setAttribute("tabindex", "0");
    zone.setAttribute("aria-label", "Datei auswaehlen");

    var title = document.createElement("strong");
    var meta = document.createElement("span");
    zone.appendChild(title);
    zone.appendChild(meta);

    function updateLabel() {
      var files = Array.prototype.slice.call(input.files || []);
      if (files.length) {
        title.textContent = files.length === 1 ? files[0].name : files.length + " Dateien ausgewaehlt";
        meta.textContent = "Bereit zum Upload";
        zone.classList.add("has-file");
      } else {
        title.textContent = "Datei hier ablegen oder auswaehlen";
        meta.textContent = acceptLabel(input.getAttribute("accept"));
        zone.classList.remove("has-file");
      }
    }

    function openPicker(event) {
      event.preventDefault();
      event.stopPropagation();
      input.click();
    }

    function setFiles(files) {
      if (!files || !files.length) {
        return;
      }
      try {
        input.files = input.multiple ? files : singleFileList(files);
      } catch (error) {
        return;
      }
      input.dispatchEvent(new Event("change", { bubbles: true }));
      updateLabel();
    }

    input.insertAdjacentElement("afterend", zone);
    updateLabel();

    input.addEventListener("change", updateLabel);
    zone.addEventListener("click", openPicker);
    zone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        openPicker(event);
      }
    });
    zone.addEventListener("dragenter", function (event) {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragover", function (event) {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragleave", function (event) {
      if (!zone.contains(event.relatedTarget)) {
        zone.classList.remove("is-dragging");
      }
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      setFiles(event.dataTransfer ? event.dataTransfer.files : null);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.slice.call(document.querySelectorAll("input[type='file']")).forEach(enhanceFileInput);
  });
})();
