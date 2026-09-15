import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 环检测是**纯函数**，所以能真跑（不像 CSS 只能退而求其次做源文件结构断言）。
//
// 这里守的是一个很容易被「顺手改坏」的东西：v4 **允许**回边——循环是一等公民，
// 所以任何「连成环就不让连」的改动都是错的；但「连错成环」又必须提醒，
// 而且提醒要能**指到具体的边**上（只说「有环」，节点一多用户照样找不到）。
// 两个方向都要有用例：只测「能报出环」的话，把提示改成拦截也照样绿。
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

// `cycle.ts` 只对 `ports.ts` 做**类型**导入，而类型导入会被 transpileModule 擦掉，
// 所以这里不需要 shim。哪天它改成值导入，本文件会立刻因 require 解析失败而报错
// ——正好是想要的那种「响亮失败」，而不是静默换一份实现。
const ports = loadModule("../src/flow/ports.ts");
const { pathTo, cycleEdges } = loadModule("../src/flow/cycle.ts");
const { liveEdges, PORT_OUT } = ports;

/** 造一条边（默认出口 `out`，省得每个用例都写一遍 port）。 */
const e = (src, dst, port = PORT_OUT) => ({ src, port, dst });

/**
 * 断言 `loop` 是一条**从 `src` 出发、沿边走又回到 `src`** 的闭合走法。
 *
 * 这是本文件最有价值的断言：它不依赖实现怎么找环，只检查「返回的边拼起来是不是
 * 一个真的圈」。父指针回溯那种写法最容易出的错（漏一条、顺序反了、差一个节点）
 * 都会被它抓住，而只断言「长度是 2」「包含某条边」是抓不住的。
 */
function assertClosedWalk(loop, src) {
  assert.ok(Array.isArray(loop) && loop.length >= 1, "环至少要有一条边（自环）");
  assert.equal(loop[0].src, src, "必须从起点出发");
  for (let i = 0; i < loop.length - 1; i++) {
    assert.equal(
      loop[i].dst,
      loop[i + 1].src,
      `第 ${i} 条（${loop[i].src}→${loop[i].dst}）与第 ${i + 1} 条没有接上`
    );
  }
  assert.equal(loop[loop.length - 1].dst, src, "最后一条没有回到起点");
}

// ---------- pathTo：从 from 走到 to 的一条路径 ----------

test("pathTo：直链能到，并给出沿途的边（按行走顺序）", () => {
  const edges = [e("a", "b"), e("b", "c")];
  assert.deepEqual(pathTo(edges, "a", "c"), edges, "应原样给出 a→b→c 这两条边");
  assert.deepEqual(pathTo(edges, "a", "b"), [edges[0]]);
});

test("pathTo：反向走不到", () => {
  assert.equal(pathTo([e("a", "b"), e("b", "c")], "c", "a"), null);
});

test("pathTo：汇合（两条边指向同一节点）不等于能互相到达", () => {
  const edges = [e("a", "c"), e("b", "c")];
  assert.deepEqual(pathTo(edges, "a", "c"), [edges[0]]);
  assert.equal(pathTo(edges, "a", "b"), null, "汇合是「进来」不是「互通」");
});

test("pathTo：自己到自己算零长路径（空数组，不是 null）", () => {
  assert.deepEqual(pathTo([], "a", "a"), []);
  assert.notEqual(pathTo([], "a", "a"), null, "「到得了」与「走不到」必须分得开");
});

test("pathTo：空的 dst 不构成跳转，不会连出幽灵节点", () => {
  // 后端允许存「这个出口留空」。若把它当成一条指向 "" 的边，就可能经由
  // "" 这个幽灵节点「走到」任何地方——凭空多出无数条不存在的路径。
  const edges = [e("a", ""), e("", "b")];
  assert.equal(pathTo(edges, "a", "b"), null, "空 dst 被当成了真实跳转");
});

test("pathTo：多条路径时给最短的一条（BFS）", () => {
  // a→b→c 与 a→d→e→c 都能到，画布上应标最短那个圈（最好认）。
  const edges = [e("a", "b"), e("b", "c"), e("a", "d"), e("d", "e"), e("e", "c")];
  assert.deepEqual(
    pathTo(edges, "a", "c").map((x) => `${x.src}-${x.dst}`),
    ["a-b", "b-c"]
  );
});

test("pathTo：长链用迭代而不是递归，不会爆栈", () => {
  const edges = [];
  for (let i = 0; i < 20000; i++) edges.push(e(`n${i}`, `n${i + 1}`));
  assert.equal(pathTo(edges, "n0", "n20000").length, 20000);
  assert.equal(pathTo(edges, "n20000", "n0"), null);
});

test("pathTo：图里已经有环时不会转不出来", () => {
  const edges = [e("a", "b"), e("b", "a")];
  assert.equal(pathTo(edges, "a", "b").length, 1);
  assert.equal(pathTo(edges, "a", "c"), null, "有环不等于哪儿都能到");
});

// ---------- cycleEdges：加一条边会不会成环，成环则给出环上的边 ----------

test("cycleEdges：接到下游新节点不成环", () => {
  assert.equal(cycleEdges([e("a", "b")], e("b", "c")), null);
});

test("cycleEdges：回边成环，返回的是一条闭合走法", () => {
  const loop = cycleEdges([e("a", "b")], e("b", "a"));
  assertClosedWalk(loop, "b");
  assert.equal(loop[0].src, "b", "第 0 条必须是**刚连的**那条，调用方据此认它");
  assert.equal(loop[0].dst, "a");
  assert.equal(loop.length, 2);
});

test("cycleEdges：自环（src === dst）", () => {
  const loop = cycleEdges([], e("a", "a"));
  assertClosedWalk(loop, "a");
  assert.equal(loop.length, 1);
});

test("cycleEdges：悬空出口（dst 为空）不构成边", () => {
  assert.equal(cycleEdges([], e("a", "")), null);
});

test("cycleEdges：绕一圈回来也能查出来，且边按顺序接得上", () => {
  const edges = [e("a", "b"), e("b", "c"), e("c", "d")];
  const loop = cycleEdges(edges, e("d", "a"));
  assertClosedWalk(loop, "d");
  assert.deepEqual(
    loop.map((x) => `${x.src}-${x.dst}`),
    ["d-a", "a-b", "b-c", "c-d"],
    "顺序必须是「新边 + 从 dst 回到 src 的路径」"
  );
  assert.equal(cycleEdges(edges, e("d", "e")), null, "接到新节点不成环");
});

test("cycleEdges：传连线前的边就够——被替换掉的旧边不可能改变判定", () => {
  // 后端会把同 (src, port) 的旧边替换掉，所以严格的写法似乎是「先从图里剔掉旧边」。
  // 这里记录为什么不需要：旧边从 `src` 出发，而判据问的是「`dst` 能否走到 `src`」，
  // 广度优先一碰到 `src` 就返回 true，根本不会去走那条旧边。
  //
  // ⚠️ 这条用例**不是区分性的**：把实现改成「先剔除旧边」它照样通过（两者在数学上
  // 等价）。它的价值是把这个前提写成可执行的记录——将来有人想改成剔除、并怀疑
  // 「不剔会不会误报」时，先看这条，而不是重新推一遍。
  const before = [e("a", "b")];
  assert.equal(cycleEdges(before, e("a", "c")), null, "a 的出口改接到 c：不成环");
  assert.equal(cycleEdges(before, e("a", "b")), null, "改接到 b（等于没改）：不成环");
  const loop = cycleEdges(before, e("b", "a"));
  assertClosedWalk(loop, "b");
});

// ---------- liveEdges：哪些边算数 ----------

test("liveEdges：两端节点都在、且出口名对源节点合法的边才算活边", () => {
  const nodes = [
    { uid: "a", type: "delay", params: {} },
    { uid: "b", type: "branch", params: { case_count: 3 } },
  ];
  const edges = [
    e("a", "b"), // 活
    e("b", "a", "case:2"), // 活
    e("b", "a", "case:4"), // 不活：case_count=3，压根没有 case:4 这个手柄
    e("a", "ghost"), // 不活：目标节点不存在
    e("ghost", "a"), // 不活：源节点不存在
    e("a", ""), // 不活：悬空出口
  ];
  assert.deepEqual(
    liveEdges(nodes, edges).map((x) => `${x.src}-${x.port}->${x.dst}`),
    ["a-out->b", "b-case:2->a"]
  );
});

test("liveEdges：end 节点没有出口，从它出去的边都不是活边", () => {
  const nodes = [
    { uid: "x", type: "end", params: {} },
    { uid: "a", type: "delay", params: {} },
  ];
  assert.deepEqual(liveEdges(nodes, [e("x", "a")]), []);
});

test("liveEdges：边对象原样透传（落点侧这类展示字段不能丢）", () => {
  // ⚠️ 这条用例**不区分过滤与否**（一条活边在两种实现下都会出来）。它守的是
  // 「透传同一个对象」这个实现约束——重建对象会丢掉 `dst_side` 之类的展示字段。
  const nodes = [{ uid: "a", type: "delay", params: {} }];
  const edge = { src: "a", port: "out", dst: "a", dst_side: "bottom" };
  assert.equal(liveEdges(nodes, [edge])[0], edge, "必须是同一个对象，不能重建");
});

test("环检测必须用活边：拿调小 case_count 后残留的边判环会误报", () => {
  // 分支从 4 个情形调回 3 个之后，`case:4` 上的边**仍留在文件里**（刻意的：
  // 调回去它还在）。但它画布上不显示、执行器也不会走——`next_map` 按
  // `(src, port)` 查表，而分支根本不会发出 `case:4`。
  const nodes = [
    { uid: "b", type: "branch", params: { case_count: 3 } },
    { uid: "d", type: "delay", params: {} },
  ];
  const stale = [e("b", "d", "case:4")];

  // 不过滤：`b→d` 这条幽灵边 + 新连的 `d→b` 就成了环——用户会收到一条关于
  // 「画布上根本看不见的边」的提示，完全无从下手。这正是过滤要避免的误报。
  const ghost = cycleEdges(stale, e("d", "b"));
  assert.ok(ghost, "这正是不过滤会产生的误报");
  assert.equal(ghost[1].port, "case:4", "误报用的正是那条用户看不见的 case:4（第 0 条是新边）");

  // 过滤后：那条边不活，图里其实没有环。
  assert.equal(cycleEdges(liveEdges(nodes, stale), e("d", "b")), null);
});
