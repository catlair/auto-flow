import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

// 界面的**接线**：模板与脚本/样式之间对不上的地方。
//
// 这类 bug 的共同点是「**编译得过、测试全绿、界面上点不到/看不清**」——
// 只有真机目视才暴露，而真机驱动很贵。所以在这里做结构断言。
//
// 两类，都在 2026-09-17 真机目视里实际踩过：
//   ① 写了处理函数但模板里没挂 ⇒ 按钮/入口根本不存在；
//   ② 模板里写了类名但 <style> 里没定义 ⇒ 元素裸奔，布局挤成一团。
//      （实测：`.af-fields` / `.af-field` 等 7 个类名只有模板在用，
//      于是字段编辑区的标签和输入框断成两行，「X」孤零零挂在上一行末尾。）

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

const files = walk(SRC).map((p) => [p.slice(SRC.length + 1), p]);

test("扫到的源文件不是空的（否则下面的断言全是空转）", () => {
  assert.ok(files.length > 10, `只扫到 ${files.length} 个文件，路径大概写错了：${SRC}`);
  assert.ok(
    files.some(([rel]) => rel === "components/RecordPanel.vue"),
    "没扫到 RecordPanel.vue，说明扫描范围不对"
  );
});

/** 取 SFC 的模板段（到**最后一个** `</template>`——嵌套的 `<template v-if>` 会提前闭合）。 */
function templateOf(src) {
  const a = src.indexOf("<template>");
  const b = src.lastIndexOf("</template>");
  return a < 0 || b < 0 ? "" : src.slice(a, b);
}

/** 取 SFC 的样式段，**并剥掉注释**。 */
function styleOf(src) {
  const i = src.indexOf("<style");
  if (i < 0) return "";
  // ⚠️ 必须剥注释：这些规则里带着解释性注释，而注释里会引用类名
  // （「`.af-field` 不加 nowrap 时…」），不剥就会把**注释里的提及**当成真规则
  // ——反向验证时实测过：把 `.af-field {` 改名成 `.af-field-x {` 后断言**照样绿**，
  // 因为注释里还留着 `.af-field`。与 `flow-canvas.test.mjs` 的坑是同一个。
  return src.slice(i).replace(/\/\*[\s\S]*?\*\//g, "");
}

// 生命周期钩子不是「事件处理函数」，不要求出现在模板里。
const LIFECYCLE = new Set([
  "onMounted", "onUnmounted", "onBeforeMount", "onBeforeUnmount",
  "onActivated", "onDeactivated", "onUpdated", "onBeforeUpdate",
  "onErrorCaptured", "onRenderTracked", "onRenderTriggered", "onServerPrefetch",
]);

test("组件里定义的处理函数都真的挂到了模板上（写了没挂 = 功能点不到）", () => {
  const bad = [];
  let checked = 0;
  for (const [rel, abs] of files) {
    if (!rel.endsWith(".vue")) continue;
    const src = readFileSync(abs, "utf8");
    const tpl = templateOf(src)
      // 模板注释里提到某个函数名不算「挂上了」
      .replace(/<!--[\s\S]*?-->/g, "");
    const defs = [...src.matchAll(/^[ \t]*(?:async\s+)?function\s+(on[A-Z]\w*)\s*\(/gm)]
      .map((m) => m[1])
      .filter((n) => !LIFECYCLE.has(n));
    checked += defs.length;
    for (const n of defs) {
      // 不要求 `@click` 形式：`onConfirm` 就是通过
      // `:confirm-btn="{ onClick: onConfirm }"` 这个 prop 传进去的。
      // 判据只到「模板里提到过这个名字」，够拦住"压根没接"这一类。
      if (!new RegExp(`\\b${n}\\b`).test(tpl)) bad.push(`${rel}: ${n}`);
    }
  }
  assert.ok(checked > 15, `只检查了 ${checked} 个处理函数，正则大概失配了`);
  assert.deepEqual(
    bad,
    [],
    "这些处理函数只有定义、模板里没引用——按钮/入口不存在，用户点不到：\n" + bad.join("\n")
  );
});

/**
 * **故意**不带自己样式规则的类名：结构钩子 / 状态变体的基名。
 *
 * 它们和「忘了写样式」在外观上**完全一样**，只有理由能区分——所以每一项都必须
 * 写清为什么。加进来之前先问：是**真的**不需要规则（样式由组合选择器或兄弟规则
 * 提供），还是我懒得补？2026-09-17 的 `.af-fields` / `.af-field` 就属于后者：
 * 当时加进这张表的话，字段编辑区会一直是「标签和输入框断成两行」的样子。
 *
 * ⚠️ 表里的项若在模板里已经不用了，测试会报出来（防止这张表越积越陈旧）。
 */
const UNSTYLED_OK = new Map([
  // `.af-conn` 这类**状态变体基名不需要登记**：真规则写在组合选择器
  // （`.af-conn.ok`）里，而判据是「类名作为完整记号出现过」，认得出它。
  ["App.vue:af-banner", "颜色由 store.bannerKind 带的第二个类提供，本类只作定位钩子"],
  ["components/FlowCanvas.vue:af-handle-out", "出口手柄与入口共用 `.af-handle` 的规则，本类只作语义标记"],
  ["components/ParamsPanel.vue:af-form", "纯结构容器，样式由其中的字段行自己带"],
  ["components/WorkflowLibDialog.vue:af-wflib", "弹层根容器，样式由 `.af-wflib-*` 各部件自带"],
]);

test("模板里用到的 af-* 类名都有样式（或已登记为纯钩子）", () => {
  const bad = [];
  const stale = [];
  const seen = new Set();
  let checked = 0;
  for (const [rel, abs] of files) {
    if (!rel.endsWith(".vue")) continue;
    const src = readFileSync(abs, "utf8");
    const tpl = templateOf(src);
    const style = styleOf(src);
    if (!tpl || !style) continue;

    const used = new Set();
    // 静态 `class="a b"`、`:class="'a'"`，以及 `:class="{ on: …, 'af-x': … }"`
    for (const attr of tpl.matchAll(/(?::?class)="([^"]*)"/g)) {
      for (const c of attr[1].match(/[a-z][a-z0-9-]*/g) || []) {
        if (c.startsWith("af-")) used.add(c);
      }
    }
    // 类名必须作为**完整**记号出现（后面不能还是字母/数字/`-`/`_`），
    // 否则 `.af-field` 会被 `.af-fields` 冒充成「已定义」——那正是踩过的坑。
    const defined = (name) => new RegExp(`\\.${name}(?![a-z0-9_-])`).test(style);

    checked += used.size;
    for (const c of used) {
      if (defined(c)) continue;
      const key = `${rel}:${c}`;
      seen.add(key);
      if (!UNSTYLED_OK.has(key)) bad.push(key);
    }
  }
  for (const key of UNSTYLED_OK.keys()) {
    if (!seen.has(key)) stale.push(key);
  }
  assert.ok(checked > 30, `只检查了 ${checked} 个类名，正则大概失配了`);
  assert.deepEqual(
    bad,
    [],
    "这些类名只有模板在用、<style> 里没有规则——元素会按默认样式排，" +
      "典型症状是标签与输入框断成两行、宽度失控。要么补样式，" +
      "要么在 UNSTYLED_OK 里写清为什么不需要：\n" + bad.join("\n")
  );
  assert.deepEqual(
    stale,
    [],
    "UNSTYLED_OK 里这些项在模板里已经不用了，删掉它们（别让这张表陈旧下去）：\n" +
      stale.join("\n")
  );
});
