/** 通过真实浏览器公开表单重放零改写/正常响应，验证点击、键盘、说明和状态恢复。 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const http = require("node:http");

/** 使用本机已有 Playwright，不安装或下载依赖。 */
function driver() {
  const links = path.join(process.env.LOCALAPPDATA, "ms-playwright/.links");
  for (const file of fs.readdirSync(links)) {
    const item = require(fs.readFileSync(path.join(links, file), "utf8").trim());
    if (item.chromium) return item;
  }
  throw new Error("Playwright driver missing");
}

/** 经生产 HTML 的上传、分析、应用事件验证全不可改入口与双下载。 */
async function main() {
  const root = path.resolve(__dirname, "../..");
  const baseline = JSON.parse(fs.readFileSync(path.join(root, "output/m8c_m9c/review_payload.json"), "utf8"));
  const html = fs.readFileSync(path.join(root, "app/static/index.html"), "utf8");
  const browser = await driver().chromium.launch({headless: true});
  const pdf = fs.readFileSync(path.join(root, "output/pdf/m8c_m9c/M9c_修改意见.pdf"));
  const server = http.createServer((request, response) => {
    response.writeHead(200, {"Content-Type": "application/pdf", "Content-Disposition": "attachment; filename=review.pdf", "Content-Length": pdf.length});
    response.end(pdf);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const errors = [], requests = [], results = [];
  try {
    const context = await browser.newContext({acceptDownloads: true, viewport: {width: 1280, height: 1000}});
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    let present = false;
    const review = structuredClone(baseline);
    const base = "/revise/file/" + "a".repeat(32);
    review.report.issues = Array.from({length: 6}, (_, i) => ({...baseline.report.issues[0], id: "R-" + i,
      category: i < 5 ? "missing" : "inconsistent", original_text: "原句用于界面验收", suggested_text: i < 5 ? "" : "原句用于界面验收"}));
    review.report.meta.auto_applicable_ids = [];
    review.report.skipped = review.report.issues.map((issue, i) => ({issue_id: issue.id,
      reason: i < 5 ? "需本人补充经历，系统不代写" : "跨段落，需手动改（原句在文档里被拆成了多段）"}));
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      requests.push(url.pathname);
      if (url.hostname !== "127.0.0.1") return route.abort();
      if (url.pathname === "/ui") return route.fulfill({contentType: "text/html; charset=utf-8", body: html});
      if (url.pathname === "/revise/review") return route.fulfill({json: review});
      if (url.pathname === "/revise/apply") {
        assert.equal(route.request().postDataJSON().selected_ids, null);
        return route.fulfill({json: {review_pdf_url: base + "/review", resume_url: present ? base + "/resume" : null,
          resume_ext: present ? "docx" : null, applied: present ? [{issue_id: "R-0"}] : [],
          skipped: present ? [] : review.report.skipped, delta: baseline.delta}});
      }
      if (url.pathname === base + "/review") return route.continue();
      return route.abort();
    });
    await page.goto("http://127.0.0.1:" + server.address().port + "/ui");
    await page.locator("#revise-tab").click();
    await page.locator("#revise-title").fill("Go 后端工程师");
    await page.locator("#revise-jd").fill("要求熟悉 Go、MySQL、Redis，交付可维护的业务接口并完善测试及日志。");
    await page.locator("#revise-file").setInputFiles(path.join(root, "tools/test_files/go_backend_resume.docx"));
    await page.locator("#revise-start").click();
    await page.locator("#revise-result").waitFor({state: "visible"});
    assert.equal(await page.locator("#revise-apply").isEnabled(), true);
    assert.equal(await page.locator("#revise-apply").textContent(), "生成修改意见报告");
    for (present of [false, true, false]) {
      await page.locator("#revise-apply").click();
      await page.locator("#revise-downloads").waitFor({state: "visible"});
      const resume = page.locator("#revise-resume-download");
      assert.equal(await resume.getAttribute("aria-disabled"), String(!present));
      assert.equal(await resume.getAttribute("href"), present ? base + "/resume" : null);
      assert.equal(await resume.evaluate(node => node.tabIndex), present ? 0 : -1);
      assert.equal(await page.locator("#revise-final-review").getAttribute("href"), base + "/review");
      assert.equal(await page.locator("#revise-error").isVisible(), false);
      if (!present) {
        const note = await page.locator("#revise-no-resume").textContent();
        assert(note.includes("5 条属于你缺的能力（需自己补）") && note.includes("1 条需你补全真实信息（如在职月份）"));
        const style = await resume.evaluate(node => ({background: getComputedStyle(node).backgroundColor,
          pointer: getComputedStyle(node).pointerEvents}));
        assert.deepEqual(style, {background: "rgb(216, 222, 230)", pointer: "none"});
        const count = requests.length;
        await resume.evaluate(node => node.click());
        assert.equal(requests.length, count);
        const downloadEvent = page.waitForEvent("download");
        await page.locator("#revise-final-review").click();
        const download = await downloadEvent;
        assert.equal(await download.failure(), null);
        results.push({resume: null, disabled: true, report_download: true, note, style});
      } else {
        assert.equal(await page.locator("#revise-no-resume").isVisible(), false);
        results.push({resume: "docx", disabled: false, stale_note_hidden: true});
      }
    }
    await page.locator("#revise-downloads").screenshot({path: path.join(__dirname, "downloads_desktop.png")});
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.locator("#revise-downloads").screenshot({path: path.join(__dirname, "downloads_mobile.png")});
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(__dirname, "browser_check.json"), JSON.stringify({results, errors, mobile_overflow: false}, null, 2));
    console.log("Browser passed: all-skipped report action enabled; null/normal/null transitions; resume gray, no href, no pointer/keyboard activation; report downloads; 5/1 explanation; mobile overflow 0; JS errors 0.");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
}

main().catch(error => { console.error(error.stack); process.exitCode = 1; });
