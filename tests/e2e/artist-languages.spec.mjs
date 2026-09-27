// The artist box for artists who are not English-speaking: a Chinese singer
// read under an English app and under a simplified-Chinese one, and a Korean
// band's members. Their own name beside the reader's, their history from
// their own Wikipedia when the reader's and the English one are stubs, and the
// Chinese text in the script the reader or the artist writes.
//
// Every Wikimedia request is answered here. The answers are trimmed from the
// live ones for Jay Chou (Q238819) and BIGBANG (Q282104); Jay Chou's English
// article is long in fact, and is made a stub here to show the fallback.
// What the lookup does with each shape of answer is covered in
// tests/js/artist-lookup.test.mjs; this is what the box shows.
import { test, expect } from "@playwright/test";
import { JOB_ID, seedLibrary, stubAudioTags, stubExportEndpoints, stubUpdateCheck } from "./helpers.mjs";

// Wide enough for the now-playing card, which the bar hides below 1460px.
test.use({ viewport: { width: 1600, height: 900 } });

const WIKIMEDIA = /^https:\/\/([a-z]+\.wikipedia\.org|www\.wikidata\.org|query\.wikidata\.org)\//;

const item = (id, ended = false) => ({
  rank: "normal",
  mainsnak: { datavalue: { value: { id } } },
  qualifiers: ended ? { P582: [{}] } : {},
});

const ENTITIES = {
  Q238819: {
    id: "Q238819",
    labels: {
      en: { language: "en", value: "Jay Chou" },
      "zh-hans": { language: "zh-hans", value: "周杰伦" },
    },
    descriptions: { en: { value: "Taiwanese singer" }, "zh-hans": { value: "台湾男歌手" } },
    sitelinks: {
      enwiki: { site: "enwiki", title: "Jay Chou", url: "https://en.wikipedia.org/wiki/Jay_Chou" },
      zhwiki: { site: "zhwiki", title: "周杰倫", url: "https://zh.wikipedia.org/wiki/%E5%91%A8%E6%9D%B0%E5%80%AB" },
    },
    claims: { P434: [{}], P27: [item("Q865")] },
  },
  Q282104: {
    id: "Q282104",
    labels: { en: { language: "en", value: "BigBang" } },
    descriptions: { en: { value: "South Korean boy band" } },
    sitelinks: { enwiki: { site: "enwiki", title: "BigBang (South Korean band)", url: "https://en.wikipedia.org/wiki/BigBang_(South_Korean_band)" } },
    claims: { P434: [{}], P495: [item("Q884")], P527: [item("Q16149"), item("Q484876"), item("Q483966", true)] },
  },
};

// Names in the languages the second request asks for, the artist's own
// among them.
const LABELS = {
  Q238819: { ...ENTITIES.Q238819.labels, "zh-tw": { language: "zh-tw", value: "周杰倫" } },
  Q282104: { en: { language: "en", value: "BigBang" }, ko: { language: "ko", value: "빅뱅" } },
  Q16149: { en: { language: "en", value: "G-Dragon" }, ko: { language: "ko", value: "지드래곤" } },
  Q484876: { en: { language: "en", value: "Taeyang" }, ko: { language: "ko", value: "태양" } },
  Q483966: { en: { language: "en", value: "Seungri" }, ko: { language: "ko", value: "승리" } },
};

const ZH_HISTORY = "周杰伦于2000年发行首张专辑《Jay》，由此成名。".repeat(12);
const ARTICLES = {
  en: {
    "Jay Chou": "Jay Chou is a Taiwanese singer.",
    "BigBang (South Korean band)": `Lead.\n== History ==\n${"BigBang debuted in 2006 under YG Entertainment. ".repeat(12)}`,
  },
  zh: { "周杰倫": `周杰伦，台湾男歌手。\n== 早年 ==\n童年。\n== 音乐事业 ==\n${ZH_HISTORY}\n== 个人生活 ==\n略。` },
};

const ALBUMS = {
  results: {
    bindings: [
      {
        album: { value: "http://www.wikidata.org/entity/Q1" },
        albumLabel: { value: "范特西 (周杰倫專輯)" },
        released: { value: "2001-09-14T00:00:00Z" },
        studio: { value: "0" },
      },
    ],
  },
};

async function stubWikimedia(page) {
  const asked = [];
  await page.route(WIKIMEDIA, async (route) => {
    const url = new URL(route.request().url());
    asked.push(url);
    const p = url.searchParams;
    let body;
    if (url.host === "query.wikidata.org") {
      body = p.get("query").includes("wd:Q238819") ? ALBUMS : { results: { bindings: [] } };
    } else if (url.host.endsWith("wikipedia.org")) {
      const edition = url.host.split(".")[0];
      body = { query: { pages: [{ extract: ARTICLES[edition]?.[p.get("titles")] || "" }] } };
    } else if (p.get("action") === "wbsearchentities") {
      const search = p.get("search");
      body = { search: [{ id: search === "周杰倫" ? "Q238819" : "Q282104" }] };
    } else if (p.get("props").startsWith("labels")) {
      body = { entities: Object.fromEntries(p.get("ids").split("|").map((id) => [id, { id, labels: LABELS[id] || {} }])) };
    } else {
      body = { entities: Object.fromEntries(p.get("ids").split("|").filter((id) => ENTITIES[id]).map((id) => [id, ENTITIES[id]])) };
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });
  });
  return asked;
}

async function openBox(page, language = "en") {
  const asked = await stubWikimedia(page);
  await page.addInitScript((code) => {
    localStorage.setItem("stemdeck.language", JSON.stringify(code));
  }, language);
  await seedLibrary(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await stubAudioTags(page, null);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(".cat-item").first().waitFor({ timeout: 20000 });
  await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 15000 });
  await page.locator("#np-details-btn").click();
  await expect(page.locator("#artistDialog")).toBeVisible();
  return asked;
}

async function lookUp(page, name) {
  await page.locator("#artistQuery").fill(name);
  await page.locator("#artistQuery").press("Enter");
}

test.describe("artist box in other languages", () => {
  test("a Chinese singer under English: his own name, and his history from the Chinese Wikipedia", async ({ page }) => {
    const asked = await openBox(page, "en");
    await lookUp(page, "周杰倫");
    const dialog = page.locator("#artistDialog");
    const native = dialog.locator(".artist-name .artist-native");
    await expect(native).toHaveText("周杰倫");
    await expect(native).toHaveAttribute("lang", "zh-TW");
    await expect(dialog.locator(".artist-name")).toContainText("Jay Chou");

    // The English article says one line; the Chinese one has the history, in
    // the traditional script Taiwan writes, and says where it is from.
    await expect(dialog.locator(".artist-section-title")).toHaveText(["History", "Albums"]);
    await expect(dialog.locator(".artist-edition")).toHaveText("From the Chinese Wikipedia");
    const history = dialog.locator(".artist-history");
    await expect(history).toHaveAttribute("lang", "zh-TW");
    await expect(history).toContainText("发行首张专辑");
    await expect(history).not.toContainText("童年");
    await expect(dialog.locator(".artist-more")).toHaveAttribute("href", /^https:\/\/zh\.wikipedia\.org\/zh-tw\//);
    await expect(dialog.locator(".artist-more")).toHaveAttribute("hreflang", "zh");

    // A plain album, with the article's qualifier left off its title, under
    // "Albums" rather than "Studio albums".
    await expect(dialog.locator(".artist-albums")).toContainText("范特西");
    await expect(dialog.locator(".artist-albums")).not.toContainText("專輯");

    // Searched among Chinese labels, and the article asked in Taiwan's script.
    expect(asked.find((u) => u.searchParams.get("action") === "wbsearchentities")?.searchParams.get("language")).toBe("zh");
    expect(asked.some((u) => u.host === "zh.wikipedia.org" && u.searchParams.get("variant") === "zh-tw")).toBe(true);

    await page.screenshot({ path: test.info().outputPath("jay-chou-en.png") });
  });

  test("the same singer under simplified Chinese: the simplified name, and no second one", async ({ page }) => {
    const asked = await openBox(page, "zh-Hans");
    await lookUp(page, "周杰倫");
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-name")).toHaveText("周杰伦");
    await expect(dialog.locator(".artist-native")).toHaveCount(0);
    await expect(dialog.locator(".artist-desc")).toHaveText("台湾男歌手");
    await expect(dialog.locator(".artist-section-title")).toHaveText(["历史", "专辑"]);
    // The reader's own edition: no line saying which it is.
    await expect(dialog.locator(".artist-edition")).toHaveCount(0);
    await expect(dialog.locator(".artist-history")).toHaveAttribute("lang", "zh-Hans");
    await expect(dialog.locator(".artist-more")).toHaveAttribute("href", /^https:\/\/zh\.wikipedia\.org\/zh-hans\//);

    // Simplified labels, and the article once, in simplified script.
    expect(asked.some((u) => (u.searchParams.get("languages") || "").startsWith("zh-hans|zh-cn|zh|en"))).toBe(true);
    const articles = asked.filter((u) => u.host.endsWith("wikipedia.org"));
    expect(articles.map((u) => `${u.host} ${u.searchParams.get("variant")}`)).toEqual(["zh.wikipedia.org zh-hans"]);

    await page.screenshot({ path: test.info().outputPath("jay-chou-zh-hans.png") });
  });

  test("a Korean band's members, each with their Korean name", async ({ page }) => {
    await openBox(page, "en");
    await lookUp(page, "BIGBANG");
    const dialog = page.locator("#artistDialog");
    await expect(dialog.locator(".artist-name .artist-native")).toHaveText("빅뱅");
    await expect(dialog.locator(".artist-name .artist-native")).toHaveAttribute("lang", "ko");
    const current = dialog.locator(".artist-names").first().locator("li");
    await expect(current).toHaveText(["G-Dragon 지드래곤", "Taeyang 태양"]);
    await expect(current.first().locator(".artist-native")).toHaveAttribute("lang", "ko");
    await expect(dialog.locator(".artist-names").nth(1)).toHaveText("Seungri 승리");
    // Read from the English Wikipedia under English, so nothing says so.
    await expect(dialog.locator(".artist-edition")).toHaveCount(0);
    await expect(dialog.locator(".artist-history")).toHaveAttribute("lang", "en");
  });
});
