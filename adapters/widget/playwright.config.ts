// Browser tests for the widget (S4.1). Run with `npm run test:e2e`.
//
// Chromium only, deliberately: the device matrix this stands in for is Android
// System WebView and Chrome, which are Chromium. Adding Firefox and WebKit here
// would test browsers no pilot user runs and make the suite slower for it.
//
// The widget must be built first; these serve `dist/` exactly as deployed.

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./test/e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env["CI"],
  retries: 0, // a flaky hydration warning is a finding, not noise to retry away
  workers: process.env["CI"] ? 2 : undefined,
  reporter: process.env["CI"] ? "line" : [["list"]],
  use: {
    trace: "retain-on-failure",
    // No baseURL: the fixture server picks a free port per run.
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
