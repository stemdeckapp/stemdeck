import { defineConfig } from "@playwright/test";

// Checks against a desktop app that is already running (see the spec's
// header). No webServer and no browser launch: every test attaches to the
// app's own WebView2 over CDP, so this never runs in CI.
export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.mjs/,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
});
