import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 画布相关的两个「测试全绿也发现不了」的坑，在这里钉住：
//   1. 首次适应视野的时机（fitOnce）——纯状态机，直接跑真实模块。
//   2. 手柄锚点 / 出口标签的 CSS 不变量——没有布局引擎可跑，退而求其次，
//      对源文件做结构断言。两个坑都是「画布不报错、节点都在，但用户看到的东西
//      不对」，靠截图才发现，所以值得用测试兜住回归。
const require = createRequire(import.meta.url);

function loadModule(relPath) {
  const source = readFileSync(new URL(relPath, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
  }).outputText;
  const module = { exports: {} };
  const load = runInThisContext(`(function(require, module, exports) {\n${compiled}\n})`, {
    filename: relPath.replace(/[^a-z0-9]+/gi, "-") + ".cjs",
  });
  load(require, module, module.exports);
  return module.exports;
}

const { createFitOnce } = loadModule("../src/flow/fitOnce.ts");

/** 造一个记账用的 fit 替身，返回它被调用的次数。 */
function spy() {
  const calls = { n: 0 };
  calls.fit = () => {
    calls.n += 1;
  };
  return calls;
}

// ---------- 适应视野的时机 ----------

test("只 request 不 commit 时绝不 fit（尺寸还没量，调了也是白调）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  // 这就是那个 bug：在「数据到位」这一步直接 fitView。节点此刻尺寸为 0，
  // 包围盒退化，fitView 等于没调，而且意图被消费掉，量完之后不会再补。
  assert.equal(s.n, 0, "request 阶段就调了 fitView —— 回归到旧 bug 了");
  assert.equal(f.isFitted(), false);
});

test("request 之后 commit 才 fit，且只 fit 一次", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  assert.equal(s.n, 1);
  assert.equal(f.isFitted(), true);
});

test("commit 可被重复触发（nodes-initialized 会多发），但只生效一次", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  f.commit();
  f.commit();
  assert.equal(s.n, 1, "重复 commit 把视野反复拉走了");
});

test("没有待办时 commit 空转（画布刚挂载就会收到一次 nodes-initialized）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.commit();
  assert.equal(s.n, 0);
  assert.equal(f.isFitted(), false);
});

test("空工作流不申请适应（否则用户打开空白画布也会被 fit 一次）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(false);
  f.commit();
  assert.equal(s.n, 0);
});

test("适应过之后再 request 不会再次 fit（每次编辑都把视野拉走是不能接受的）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  // 用户手动拖了视野，然后加了几个节点
  f.request(true);
  f.request(true);
  f.commit();
  assert.equal(s.n, 1, "编辑节点时视野被重新适应了");
});

test("清空工作流后下一份能重新适应", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  f.reset();
  assert.equal(f.isFitted(), false);
  f.request(true);
  f.commit();
  assert.equal(s.n, 2, "换了一份工作流却没重新适应视野");
});

test("reset 之后空工作流仍然不申请（清空动作本身不该触发 fit）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.reset();
  f.request(false);
  f.commit();
  assert.equal(s.n, 0);
});

test("完整的真实时序：挂载 → 尺寸量完 → 编辑 → 换工作流", () => {
  const s = spy();
  const f = createFitOnce(s.fit);

  // 画布挂载，此时 store 还是空的，Vue Flow 先发了一次 nodes-initialized
  f.request(false);
  f.commit();
  assert.equal(s.n, 0, "空白启动时不该 fit");

  // 握手拿到已有工作流
  f.request(true);
  f.commit();
  assert.equal(s.n, 1);

  // 用户拖动节点：nodes-initialized 可能再发一次
  f.commit();
  assert.equal(s.n, 1, "拖动节点把视野拉回去了");

  // 用户点了「新建」→ 空 → 加载另一份
  f.reset();
  f.request(false);
  f.commit();
  assert.equal(s.n, 1);
  f.request(true);
  f.commit();
  assert.equal(s.n, 2);
});

// ---------- 画布 CSS 不变量 ----------

const canvasSrc = readFileSync(
  new URL("../src/components/FlowCanvas.vue", import.meta.url),
  "utf8"
);

/**
 * 取出某个选择器的规则体（`[^}]*` 足够——这些规则里没有嵌套花括号）。
 *
 * **必须先去注释**：这些规则里带着解释性注释，注释里会引用 `position: relative`
 * 之类的字样（正是在讲「不要这么写」），不去掉的话断言会命中注释里的反例而误报。
 */
function ruleBody(src, selector) {
  const m = new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`).exec(src);
  assert.ok(m, `找不到选择器 ${selector} 的规则`);
  return m[1].replace(/\/\*[\s\S]*?\*\//g, "");
}

test("出口行不能是定位元素，否则手柄锚到这一行、圆点正好压住标签最后一个字", () => {
  const body = ruleBody(canvasSrc, ".af-exit");
  // Vue Flow 的手柄是 position:absolute + right:0 + translate(50%,-50%)，锚在
  // **最近的定位祖先**的右边缘。.af-exit 一旦 position:relative，锚点就从卡片
  // 挪到这一行（内缩约 10px），圆点正好盖住「情形 1」的数字 —— 用户分不清
  // 哪条边是情形几。
  assert.ok(
    !/position\s*:\s*(relative|absolute|fixed|sticky)/.test(body),
    ".af-exit 又变成定位元素了，出口标签会被手柄圆点压住"
  );
  // 取而代之：留出宽度给手柄
  assert.match(body, /padding-right\s*:\s*\d/, ".af-exit 没有为手柄留出右侧间距");
});

test("节点卡片必须是定位元素，手柄才锚在卡片右边缘", () => {
  const body = ruleBody(canvasSrc, ".af-gnode");
  assert.match(
    body,
    /position\s*:\s*relative/,
    ".af-gnode 不是定位元素，手柄会锚到 .vue-flow__node 上，单/多出口的竖线对不齐"
  );
});

test("nodes-initialized 事件必须接到 tryFit 上（否则视野永远不适应）", () => {
  assert.match(
    canvasSrc,
    /@nodes-initialized="tryFit"/,
    "没有监听 nodes-initialized，fitView 永远不会在尺寸量完之后执行"
  );
  assert.match(canvasSrc, /createFitOnce\(/, "画布没用 fitOnce 状态机");
});
