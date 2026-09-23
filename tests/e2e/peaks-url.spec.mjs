// The studio loads waveform peaks from /jobs/{id}/peaks, not stems/peaks.json.
//
// peaks.json was served `immutable`, and an on-demand split rewrites it, so a
// browser kept drawing the waveform from before the split under the new stems,
// through reloads and hard reloads alike (#639). The server now revalidates
// it, but a browser that cached it under the old header will keep that copy
// for up to a year without asking. Only a different URL reaches it, so the one
// thing that has to stay true on the client is which URL is asked for.
import { test, expect } from "@playwright/test";
import { JOB_ID, openStudio } from "./helpers.mjs";

test("the studio asks for peaks at the URL no browser cached forever", async ({ page }) => {
  const asked = [];
  page.on("request", (req) => {
    const path = new URL(req.url()).pathname;
    if (path.includes("peaks")) asked.push(path);
  });

  await openStudio(page, { tauri: true });

  expect(asked).toContain(`/api/jobs/${JOB_ID}/peaks`);
  expect(asked.filter((p) => p.endsWith("/stems/peaks.json"))).toEqual([]);
});
