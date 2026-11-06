/**
 * 浏览器级冒烟：真的打开页面，确认脚本跑起来、运行列表渲染出节点。
 *
 * 为什么需要它：语法检查只能证明"能解析"，证明不了"能运行"。
 * 一个 `document.querySelector('.x').addEventListener` 后面跟 null，
 * 或者某个 id 改名了，脚本照样能通过 node --check，但页面是死的。
 *
 * 用法：
 *   $env:SMOKE_BASE="http://127.0.0.1:8330"
 *   ego-browser nodejs -e "await import('file:///C:/.../scripts/smoke_ui.mjs')"
 */
const base = process.env.SMOKE_BASE || "http://127.0.0.1:8330";
const task = await taskSpace("bid-workbench-smoke");
const page = task.page("p1");

try {
  const { windowId } = await page.cdp("Browser.getWindowForTarget");
  await page.cdp("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
} catch (e) {}

await page.goto(`${base}/?v=${Date.now()}`, { waitUntil: "load" });
await page.waitForTimeout(1800);

const result = await page.evaluate(() => ({
  arms: document.querySelectorAll("#arms .run-item").length,
  runTitle: (document.getElementById("runTitle") || {}).innerText || "",
  tabs: [...document.querySelectorAll("#tabs button")].map((node) => node.innerText.trim()),
  hasQueue: !!document.getElementById("queue"),
  hasMatrixTab: !!document.querySelector('[data-tab="matrix"]'),
  ledgerRows: document.querySelectorAll("#ledBody tr").length,
}));

console.log(JSON.stringify(result, null, 2));

if (result.arms === 0) {
  throw new Error("运行列表没有渲染出任何节点：页面脚本可能在运行时抛错");
}
if (!result.hasMatrixTab || !result.hasQueue) {
  throw new Error("页面结构缺少队列或多标对比入口");
}

console.log("UI smoke OK");
