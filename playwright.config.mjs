import { defineConfig, devices } from "@playwright/test";

// The backend is real: these tests exercise the actual endpoints, with only the
// separation pipeline skipped (tests/e2e/seed.py writes a finished job instead
// of running demucs to test a menu).
//
// serve.sh seeds a throwaway jobs directory and execs uvicorn against it, so a
// run can never see or touch a developer's real library.
const PORT = process.env.STEMDECK_E2E_PORT || "8123";

export default defineConfig({
  testDir: "tests/e2e",
  testMatch: /.*\.spec\.mjs/,
  // A stuck export used to hang for 15 minutes by design (EXPORT_BUSY_MAX_MS),
  // so a generous per-test timeout would hide exactly the bug these tests exist
  // to catch.
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: process.env.CI ? "retain-on-failure" : "off",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `bash tests/e2e/serve.sh ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
