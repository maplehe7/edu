(function () {
  "use strict";

  var webAssembly = window.WebAssembly || null;
  var hasNativeJspi = !!(webAssembly && typeof webAssembly.Suspending === "function");
  var hasLegacyJspi = !!(webAssembly && typeof webAssembly.Suspender === "function");
  var trialMeta = document.querySelector('meta[data-ocean-jspi="1"]');
  var bootstrap = window.__oceanJspiBootstrap || {};
  var token =
    typeof window.OCEAN_JSPI_ORIGIN_TRIAL_TOKEN === "string"
      ? window.OCEAN_JSPI_ORIGIN_TRIAL_TOKEN.trim()
      : "";
  var status = "missing";

  if (hasNativeJspi) {
    status = "native";
  } else if (hasLegacyJspi) {
    status = "legacy-api";
  } else if (trialMeta) {
    status = "trial-meta";
  }

  window.__oceanJspi = {
    status: status,
    nativeSupported: hasNativeJspi,
    legacySupported: hasLegacyJspi,
    tokenPresent: token.length > 0,
    trialMetaInjected: !!trialMeta,
    currentOrigin: window.location.origin || "",
    expectedOrigin: bootstrap.expectedOrigin || ""
  };

  document.documentElement.setAttribute("data-ocean-jspi", status);

  if (status === "native") {
    console.info("Ocean JSPI: native support detected via WebAssembly.Suspending");
    return;
  }

  if (status === "legacy-api") {
    console.warn(
      "Ocean JSPI: this browser exposes deprecated WebAssembly.Suspender, but these clients expect WebAssembly.Suspending"
    );
    return;
  }

  if (status === "trial-meta") {
    console.info(
      "Ocean JSPI: origin-trial meta tag was injected. If JSPI is still unavailable, the token is missing or invalid for " +
        (window.location.origin || "this origin")
    );
    return;
  }

  console.warn(
    "Ocean JSPI: no JSPI support detected. Use Chrome 137+ or add a JSPI origin-trial token for " +
      (window.location.origin || "this origin")
  );
})();
