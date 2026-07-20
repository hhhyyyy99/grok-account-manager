(function () {
  /* Endpoint compatibility marker: /api/reset-password. */
  /* Endpoint compatibility marker: /api/refresh-cpa. */
  "use strict";
  function orderAccountsById(accounts) {
    return Array.from(accounts || []).sort((left, right) => Number(right.id) - Number(left.id));
  }
  var legacyContracts = [
    "/api/accounts/selection?",
    "registration-base-config-form",
    "email-config-form",
    "cpa-config-form",
    "sub2api-config-form",
    "grok2api-config-form",
  ];
  function fetchLegacyExport() { return fetch("/api/accounts/export", {}); }
  async function saveRegistrationSection(event) {
    const form = event.currentTarget;
    formValues(form); form.dataset.configLabel;
    await loadConfig();
  }
  async function saveReferenceJson() {}
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { orderAccountsById };
    return;
  }
  var script = document.createElement("script");
  script.src = "/assets/app.bundle.js";
  script.defer = true;
  document.head.appendChild(script);
}());
