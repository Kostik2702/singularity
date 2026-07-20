import { safeInit } from "./safe-init.js";

// run-mode flag BEFORE anything animates: ?visual-test=1 -> deterministic
// (canvas modules draw one seeded frame, css kill-switch stops animations)
const visualTest = new URLSearchParams(location.search).has("visual-test");
if (visualTest) document.documentElement.dataset.visualTest = "1";

// house module convention: js/modules/<name>.js exporting init<Name>(),
// hooked to [data-module="<name>"], registered here via safeInit.
// Golden patterns install the same way: `golden install --name <pattern>
// --dest <this project>` then wire the module below.

safeInit("current-year", () => {
  for (const el of document.querySelectorAll('[data-component="current-year"]')) {
    el.textContent = String(new Date().getFullYear());
  }
});
