import { defineConfig } from "@playwright/test";

// The acceptance run: the manual checklist for a release build, automated
// against the real desktop app and the real internet. Run on request only:
//
//   npm run test:acceptance
//
// Never in CI, and never by a plain `npx playwright test`: that reads the
// root playwright.config.mjs, whose testDir is tests/e2e. See README.md here.
//
// One app, one library, checks that build on each other's imports: one
// worker, in file order. The timeouts are long on purpose, unlike the e2e
// suite's: a check waits for real downloads and GPU separation, and a failure
// here is read from the report, not raced.
export default defineConfig({
  testDir: ".",
  testMatch: /acceptance\.spec\.mjs$/,
  timeout: 100 * 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  globalSetup: "./global-setup.mjs",
  outputDir: "../../acceptance-results/playwright",
  reporter: [["list"], ["./report.mjs"]],
  use: {
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
});
