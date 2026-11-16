(() => {
  const form = document.querySelector('.api-key-form');
  if (!form) return;

  const syncOwner = () => {
    const type = form.querySelector('[name="owner_type"]:checked')?.value;
    form.querySelectorAll('[data-owner-field]').forEach(field => {
      const active = field.dataset.ownerField === type;
      field.hidden = !active;
      field.querySelectorAll('input, select').forEach(input => {
        input.disabled = !active;
        input.required = active;
      });
    });
  };
  form.querySelectorAll('[name="owner_type"]').forEach(input => {
    input.addEventListener('change', syncOwner);
  });
  syncOwner();
})();
