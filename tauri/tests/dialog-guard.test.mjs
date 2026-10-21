import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

// 界面上的**全局**约束：不许用 WebView 宿主提供的原生对话框。
//
// 为什么值得单独一个文件、扫整个 `src/`：
// `window.confirm` 在 Tauri 下不是「不可靠」，而是**会让危险操作无条件执行**。
//   - 宿主把它注入成 `async function(){ return await invoke("plugin:dialog|confirm", …) }`
//     ⇒ 返回 **Promise**，而 Promise 恒 truthy ⇒ 拦截条件恒不成立
//     （`if (!…) return;` 的 `return` 永不执行；`… && doIt()` 的右侧无条件执行）；
//   - 本项目**没授予 `dialog:allow-confirm`**（`tauri-plugin-dialog` 的 `default`
//     只有 allow-message / allow-open / allow-save）⇒ invoke 被权限拒绝 ⇒ 连框都不弹。
// 合起来：`window.confirm` 的语义是「没有任何提示，直接做」。
// 真踩过一次——`RecordPanel.vue` 删录制就这么把文件 `os.remove` 掉了。
//
// 只在 `RecordPanel.vue` 上断言不够：**下一个组件再引入照样漏**，所以这里扫全仓。

const SRC = fileURLToPath(new URL("../src", import.meta.url));

/** 递归收集 `src/` 下所有 `.ts` / `.vue`。 */
function walk(dir, out = []) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(ts|vue)$/.test(e.name)) out.push(p);
  }
  return out;
}

/**
 * 去掉注释后剩下的代码行。
 *
 * 够用即可：本仓库的注释要么整行（`//` / `*`），要么行尾 `//`。行尾剥离用
 * `(?<!:)` 避开 `https://`——不然会把 URL 当注释切掉。
 * **注释里出现 `window.confirm` 是允许的**（甚至鼓励：要在那儿写清为什么不能用），
 * 这条断言管的是「代码里真的去调它」。
 */
function codeLines(src) {
  return src
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("//") && !l.startsWith("*") && !l.startsWith("/*"))
    .map((l) => l.replace(/(?<!:)\/\/.*$/, "").trim())
    .filter(Boolean);
}

const files = walk(SRC).map((p) => [p.slice(SRC.length + 1), p]);

test("扫到的源文件不是空的（否则下面的断言全是空转）", () => {
  assert.ok(files.length > 10, `只扫到 ${files.length} 个文件，路径大概写错了：${SRC}`);
  assert.ok(
    files.some(([rel]) => rel === "utils/confirm.ts"),
    "没扫到 utils/confirm.ts，说明扫描范围不对"
  );
});

test("代码里不调用 window.confirm / alert / prompt（它们在 Tauri 下拦不住任何东西）", () => {
  const bad = [];
  for (const [rel, abs] of files) {
    for (const l of codeLines(readFileSync(abs, "utf8"))) {
      const m = l.match(/window\.(confirm|alert|prompt)\s*\(/);
      if (m) bad.push(`${rel}: ${l}`);
    }
  }
  assert.deepEqual(
    bad,
    [],
    "发现原生对话框调用——它们不吃 Tauri 权限也拿不到返回值，" +
      "确认框请用 `@/utils/confirm` 的 confirmDanger 并 await：\n" + bad.join("\n")
  );
});

test("每个 confirmDanger 调用点都带 await（不 await 等于没拦）", () => {
  const bad = [];
  for (const [rel, abs] of files) {
    for (const l of codeLines(readFileSync(abs, "utf8"))) {
      if (!l.includes("confirmDanger(")) continue;
      if (l.includes("import ") || l.includes("export function")) continue; // 导入与定义
      if (!l.includes("await")) bad.push(`${rel}: ${l}`);
    }
  }
  assert.deepEqual(bad, [], "confirmDanger 调用没 await：\n" + bad.join("\n"));
});

test("confirmDanger 里 settle 排在 destroy() 前面（反过来「确定」会当取消）", () => {
  const src = readFileSync(join(SRC, "utils/confirm.ts"), "utf8");
  // destroy() 会触发 onClose，而 onClose 里也 settle(false)。
  // 先 destroy 再 settle(true) 的话，promise 已经被结算成 false 了。
  assert.match(
    src,
    /onConfirm:\s*\(\)\s*=>\s*\{\s*settle\(true\);\s*d\.destroy\(\);/,
    "onConfirm 里 settle(true) 必须排在 d.destroy() 前面"
  );
  assert.match(
    src,
    /onCancel:\s*\(\)\s*=>\s*\{\s*settle\(false\);\s*d\.destroy\(\);/,
    "onCancel 里 settle(false) 必须排在 d.destroy() 前面"
  );
});

// 「模板接线 / 类名与样式一致」这两类结构断言在 `ui-wiring.test.mjs`——
// 它们的性质是「编译得过、测试全绿、界面上点不到/看不清」，与原生对话框无关。
