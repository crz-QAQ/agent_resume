/** 用本轮实际 HTTP 脱敏响应重放网页，截取涨分和材料区块，不重复调用模型。 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

/** 优先使用常规依赖，再复用本机 Playwright 浏览器安装记录所指向的驱动包。 */
function browserPackage() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, "playwright"].filter(Boolean);
  const links = path.join(process.env.LOCALAPPDATA || "", "ms-playwright", ".links");
  // 只读取已有安装位置，不下载安装浏览器或 npm 包。
  if (fs.existsSync(links)) for (const file of fs.readdirSync(links)) candidates.push(fs.readFileSync(path.join(links, file), "utf8").trim());
  for (const candidate of candidates) {
    try { const value = require(candidate); if (value.chromium) return value; } catch { /* 尝试下一处已安装驱动。 */ }
  }
  throw new Error("没有可用的本地 Playwright 驱动");
}

/** 驱动真实页面表单与生产 JavaScript，并验证展开状态、外链属性和移动端边界。 */
async function main() {
  const root = path.resolve(__dirname, "..");
  const output = path.join(root, "output/m8c_m9c");
  const screenshots = path.join(output, "screenshots");
  fs.mkdirSync(screenshots, {recursive: true});
  const payload = JSON.parse(fs.readFileSync(path.join(output, "review_payload.json"), "utf8"));
  const request = JSON.parse(fs.readFileSync(path.join(output, "ui_request.json"), "utf8"));
  // 响应由 verify_revise_http 保存，页面本身不内嵌材料或分数测试数据。
  const html = fs.readFileSync(path.join(root, "app/static/index.html"), "utf8");
  assert(!html.includes("https://"));
  const browser = await browserPackage().chromium.launch({headless: true});
  const errors = [], requests = [];
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1100}, deviceScaleFactor: 1});
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    // 仅本地页面和本次响应重放可用；材料链接不点击，不访问任何外站。
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      requests.push({method: route.request().method(), path: url.pathname});
      if (url.hostname !== "127.0.0.1") return route.abort();
      if (url.pathname === "/ui") return route.fulfill({contentType: "text/html; charset=utf-8", body: html});
      if (url.pathname === "/revise/review" && route.request().method() === "POST") {
        return route.fulfill({contentType: "application/json; charset=utf-8", body: JSON.stringify(payload)});
      }
      return route.abort();
    });
    await page.goto("http://127.0.0.1:8766/ui");
    await page.locator("#revise-tab").click();
    await page.locator("#revise-title").fill(request.job_title);
    await page.locator("#revise-jd").fill(request.jd_text);
    // 走公开上传和提交入口，不导出或直接调用页面内部闭包函数。
    await page.locator("#revise-file").setInputFiles(path.join(root, "tools/test_files", request.filename));
    await page.locator("#revise-start").click();
    await page.locator("#revise-result").waitFor({state: "visible"});
    for (const id of ["#revise-uplift", "#revise-learning"]) assert.equal(await page.locator(id).evaluate(node => node.open), true);
    const links = await page.locator("#revise-learning a").evaluateAll(nodes => nodes.map(node => ({href: node.href, target: node.target, rel: node.rel})));
    const expected = new Set(payload.learning_plan.gaps.flatMap(gap => gap.materials.filter(row => row.kind === "external").map(row => row.locator)));
    assert(links.length > 0 && links.every(link => expected.has(link.href) && link.target === "_blank" && link.rel.includes("noopener")));
    assert.equal(await page.locator("#revise-uplift th").allTextContents().then(values => values.join("|")), "分数项|当前|最多可加|该做什么|前提");
    const blockedColors = await page.locator("#revise-uplift .plan-blocked").evaluateAll(nodes => nodes.map(node => getComputedStyle(node).color));
    assert(blockedColors.every(color => color === "rgb(120, 134, 149)"));
    await page.locator("#revise-uplift").screenshot({path: path.join(screenshots, "uplift.png")});
    await page.locator("#revise-learning").screenshot({path: path.join(screenshots, "learning.png")});
    // 窄屏表格可以在本区块内滚动，页面本身不能横向溢出。
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    assert.deepEqual(errors, []);
    const evidence = {source_response: "output/m8c_m9c/review_payload.json", replay: true,
      model_calls_for_screenshots: 0, defaults_open: true, links, blocked_colors: blockedColors,
      mobile_width: 390, mobile_overflow: false, page_errors: errors, requests};
    fs.writeFileSync(path.join(output, "browser_check.json"), JSON.stringify(evidence, null, 2) + "\n");
    console.log("Browser checks passed: two sections open; blocked rows gray; material links verified; mobile overflow 0; JS errors 0.");
    console.log("Saved screenshots: output/m8c_m9c/screenshots/uplift.png; output/m8c_m9c/screenshots/learning.png");
  } finally { await browser.close(); }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
