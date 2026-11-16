/** 通过网页公开表单验证真实生产脚本的语义徽标，供应商响应使用合成/已保存 fixture。 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "../..");

/** 只读取已安装的 Playwright 驱动，不下载依赖或浏览器。 */
function browserPackage() {
  const candidates = ["playwright"];
  const links = path.join(process.env.LOCALAPPDATA, "ms-playwright/.links");
  for (const file of fs.readdirSync(links)) candidates.push(fs.readFileSync(path.join(links, file), "utf8").trim());
  for (const candidate of candidates) {
    try { const value = require(candidate); if (value.chromium) return value; } catch {}
  }
  throw new Error("找不到已安装 Playwright");
}

/** 实跑 renderLearning，并检查徽标位置、旧状态清空、外链和窄屏。 */
async function main() {
  const payload = JSON.parse(fs.readFileSync(path.join(root, "output/m8c_m9c/review_payload.json"), "utf8"));
  const request = JSON.parse(fs.readFileSync(path.join(root, "output/m8c_m9c/ui_request.json"), "utf8"));
  const samples = JSON.parse(fs.readFileSync(path.join(__dirname, "real_model_samples.json"), "utf8"));
  payload.learning_plan = samples.all_methods;
  const plan = structuredClone(payload.learning_plan);
  const html = fs.readFileSync(path.join(root, "app/static/index.html"), "utf8");
  const browser = await browserPackage().chromium.launch({headless: true});
  const errors = [];
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.hostname !== "127.0.0.1") return route.abort();
      if (url.pathname === "/ui") return route.fulfill({contentType: "text/html; charset=utf-8", body: html});
      if (url.pathname === "/revise/review") return route.fulfill({json: payload});
      return route.abort();
    });
    await page.goto("http://127.0.0.1:8766/ui");
    await page.locator("#revise-tab").click();
    await page.locator("#revise-title").fill(request.job_title);
    await page.locator("#revise-jd").fill(request.jd_text);
    await page.locator("#revise-file").setInputFiles(path.join(root, "tools/test_files", request.filename));
    await page.locator("#revise-start").click();
    await page.locator("#revise-result").waitFor({state: "visible"});
    const cards = page.locator("#revise-learning .learning-gap");
    assert.equal(await cards.count(), plan.gaps.length);
    for (let i = 0; i < plan.gaps.length; i++) {
      const gap = plan.gaps[i];
      assert.equal(await cards.nth(i).locator(".semantic-badge").count(), gap.meta.matched_by === "semantic" ? gap.materials.length : 0);
    }
    const badge = page.locator("#revise-learning .semantic-badge");
    assert.equal(await badge.textContent(), "语义推荐");
    assert.equal(await badge.evaluate(node => node.previousElementSibling.tagName), "A");
    const badgeStyle = await badge.evaluate(node => ({color: getComputedStyle(node).color, background: getComputedStyle(node).backgroundColor}));
    assert.equal(badgeStyle.color, "rgb(104, 120, 138)");
    assert.equal(badgeStyle.background, "rgb(238, 242, 246)");
    await page.locator("#revise-learning").screenshot({path: path.join(__dirname, "learning_badge_desktop.png")});
    await page.setViewportSize({width: 390, height: 1100});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator("#revise-learning").screenshot({path: path.join(__dirname, "learning_badge_mobile.png")});
    payload.learning_plan = samples.keyword_regression;
    await page.locator("#revise-start").click();
    await page.waitForFunction(() => document.querySelectorAll("#revise-learning .learning-gap").length === 3);
    assert.equal(await page.locator("#revise-learning .semantic-badge").count(), 0);
    payload.learning_plan = samples.rejected_ai_coding;
    await page.locator("#revise-start").click();
    await page.waitForFunction(() => document.querySelectorAll("#revise-learning .learning-gap").length === 1);
    assert.equal(await page.locator("#revise-learning .semantic-badge").count(), 0);
    assert.match(await page.locator("#revise-learning").textContent(), /暂无对应材料/);
    assert(!await page.locator("#revise-learning").textContent().then(text => text.includes("fde-prompt-engineering")));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({passed: true, semantic_badge: "语义推荐", title_sibling: true,
      keyword_family_none_badges: 0, rerender_clears_badges: true, rejected_candidate_hidden: true,
      mobile_width: 390, horizontal_overflow: 0, page_errors: errors, badge_style: badgeStyle}, null, 2));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
