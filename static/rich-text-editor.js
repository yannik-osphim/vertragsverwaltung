(function () {
  var selector = "textarea[data-rich-text], textarea[name='notes'], textarea[name='description']";

  function hasRichMarkup(value) {
    return /<\/?(p|br|strong|b|em|i|u|ul|ol|li|a|div)\b/i.test(value || "");
  }

  function syncTextarea(textarea, quill) {
    var html = typeof quill.getSemanticHTML === "function" ? quill.getSemanticHTML() : quill.root.innerHTML;
    textarea.value = quill.getText().trim() ? html.trim() : "";
  }

  function hideSourceTextarea(textarea) {
    textarea.classList.add("rich-text-source");
    textarea.hidden = true;
    textarea.tabIndex = -1;
    textarea.setAttribute("aria-hidden", "true");
    textarea.style.display = "none";
  }

  function enhance(textarea) {
    if (textarea.dataset.richTextReady === "true" || !window.Quill) {
      return;
    }
    textarea.dataset.richTextReady = "true";

    var wrapper = document.createElement("div");
    wrapper.className = "rich-text-editor";
    var editor = document.createElement("div");
    editor.className = "rich-text-quill";
    wrapper.appendChild(editor);

    hideSourceTextarea(textarea);
    textarea.insertAdjacentElement("afterend", wrapper);

    var quill = new window.Quill(editor, {
      theme: "snow",
      placeholder: textarea.getAttribute("placeholder") || "",
      modules: {
        toolbar: [
          ["bold", "italic", "underline"],
          [{ list: "ordered" }, { list: "bullet" }],
          ["link"],
          ["clean"],
        ],
      },
    });

    if (textarea.value) {
      if (hasRichMarkup(textarea.value)) {
        quill.clipboard.dangerouslyPasteHTML(textarea.value);
      } else {
        quill.setText(textarea.value, "silent");
      }
      syncTextarea(textarea, quill);
    }

    quill.on("text-change", function () {
      syncTextarea(textarea, quill);
    });

    if (textarea.form) {
      textarea.form.addEventListener("submit", function () {
        syncTextarea(textarea, quill);
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!window.Quill) {
      return;
    }
    Array.prototype.forEach.call(document.querySelectorAll(selector), enhance);
  });
})();
