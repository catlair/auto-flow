import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 流程图的两个纯逻辑模块：出口名（ports）与节点摘要（summary）。
// 它们没有 Vue 依赖，可以直接编译求值，不需要 WebView。
const require = createRequire(import.meta.url);

function loadTs(relPath) {
  const src = readFileSync(new URL(relPath, import.meta.url), "utf8");
  const out = ts.transpileModule(src, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
  }).outputText;
  const mod = { exports: {} };
  const fn = runInThisContext(`(function(require, module, exports) {\n${out}\n})`, {
    filename: relPath.replace(/[^\w]/g, "_") + ".cjs",
  });
  fn((name) => require(name), mod, mod.exports);
  return mod.exports;
}

const ports = loadTs("../src/flow/ports.ts");
const summary = loadTs("../src/flow/summary.ts");

// --------------------------------------------------------------------------- //
// 与后端 core/events.py 的常量对齐
//
// 出口名是**跨语言协议**：前端按它提交边、后端按它选下一跳。任何一边改字面量
// 而另一边没跟上，表现都是「连线看着正常、执行时静默走错路」——没有任何报错。
// 所以这里直接读后端源码来比，而不是把字面量在测试里再抄一遍（抄一遍就等于
// 三份副本，照样会漂）。
// --------------------------------------------------------------------------- //
const eventsPy = readFileSync(new URL("../../core/events.py", import.meta.url), "utf8");

function pyConst(name) {
  const m = new RegExp(`^${name}\\s*=\\s*"([^"]*)"`, "m").exec(eventsPy);
  return m ? m[1] : null;
}

test("出口名与 core/events.py 逐一对齐", () => {
  for (const name of ["PORT_OUT", "PORT_TRUE", "PORT_FALSE", "PORT_ELSE"]) {
    const backend = pyConst(name);
    assert.ok(backend !== null, `后端 core/events.py 里找不到 ${name}`);
    assert.equal(ports[name], backend, `${name} 与后端不一致`);
  }
});

test("case 出口名与后端 case_port() 一致，上限也一致", () => {
  assert.match(eventsPy, /return f"case:\{i\}"/, "后端 case_port 的格式变了");
  for (const i of [1, 2, 6]) assert.equal(ports.casePort(i), `case:${i}`);

  const m = /^MAX_BRANCH_CASES\s*=\s*(\d+)/m.exec(eventsPy);
  assert.ok(m, "后端找不到 MAX_BRANCH_CASES");
  assert.equal(ports.MAX_BRANCH_CASES, Number(m[1]), "case 上限与后端不一致");
});

// --------------------------------------------------------------------------- //
// 目标落点侧：同样是跨语言协议——前端按它渲染手柄、按它提交边，后端按它存
// --------------------------------------------------------------------------- //
test("落点侧与 core/events.py 的 TARGET_SIDE_* 逐一对齐", () => {
  for (const name of ["TARGET_SIDE_LEFT", "TARGET_SIDE_TOP", "TARGET_SIDE_BOTTOM"]) {
    const backend = pyConst(name);
    assert.ok(backend !== null, `后端 core/events.py 里找不到 ${name}`);
    assert.equal(ports[name], backend, `${name} 与后端不一致`);
  }
});

test("落点侧的顺序与默认侧两边一致（前端按这个顺序渲染手柄）", () => {
  const m = /^TARGET_SIDES\s*=\s*\(([^)]*)\)/m.exec(eventsPy);
  assert.ok(m, "后端找不到 TARGET_SIDES 元组");
  const names = m[1].split(",").map((s) => s.trim()).filter(Boolean);
  assert.deepEqual(
    names,
    ["TARGET_SIDE_LEFT", "TARGET_SIDE_TOP", "TARGET_SIDE_BOTTOM"],
    "后端的落点侧顺序变了，前端渲染顺序要跟着改"
  );
  assert.deepEqual(ports.TARGET_SIDES, names.map((n) => ports[n]));
  assert.match(eventsPy, /^DEFAULT_TARGET_SIDE\s*=\s*TARGET_SIDE_LEFT/m,
    "后端默认落点侧不是左侧");
  assert.equal(ports.DEFAULT_TARGET_SIDE, ports.TARGET_SIDE_LEFT);
});

test("右侧不能进落点词表——它和出口手柄同点重合", () => {
  // 右边是输出侧（出口手柄都在卡片右边缘）。单出口节点的出口手柄正好在右边缘
  // 中线，与右目标手柄同点重合；Vue Flow 按「离指针最近的手柄」判定落点，
  // 两点重合时行为不稳定，表现是「有时连得上有时连不上」。所以入口只开放 左/上/下。
  assert.ok(!ports.TARGET_SIDES.includes("right"), "右侧不该在落点词表里");
  assert.ok(!/^TARGET_SIDE_RIGHT\s*=/m.test(eventsPy), "后端又定义了 TARGET_SIDE_RIGHT");
});

test("落点侧的脏值收敛到默认侧，不会产出不存在的手柄", () => {
  // 不收敛的话 targetHandle 会指向一个不存在的手柄，Vue Flow 找不到锚点就画不出
  // 这条边（只在控制台刷告警）——用户看到的是「连线莫名消失」。
  for (const dirty of ["right", "middle", "", null, undefined, 123, "LEFT", "out"]) {
    assert.equal(ports.normalizeTargetSide(dirty), ports.DEFAULT_TARGET_SIDE, String(dirty));
  }
  assert.equal(ports.normalizeTargetSide(" bottom "), "bottom", "前后空白应被吃掉");
  for (const good of ports.TARGET_SIDES) {
    assert.equal(ports.normalizeTargetSide(good), good);
  }
});

// --------------------------------------------------------------------------- //
// exitPorts：前端唯一一处按节点类型决定「有几个出口」的地方
// --------------------------------------------------------------------------- //
test("出口列表按节点类型给出，顺序即画布上的上下顺序", () => {
  assert.deepEqual(ports.exitPorts("start"), ["out"]);
  assert.deepEqual(ports.exitPorts("mouse"), ["out"]);
  assert.deepEqual(ports.exitPorts("condition"), ["true", "false"]);
  assert.deepEqual(ports.exitPorts("branch", 3), ["case:1", "case:2", "case:3", "else"]);
  // 结束节点没有出口：它是终点，渲染出手柄只会让人以为还能往下走
  assert.deepEqual(ports.exitPorts("end"), []);
});

test("未知节点类型退化为「唯一出口 out」，而不是没有出口", () => {
  // 返回空数组会让节点一个手柄都没有，直接连不出线——比给个默认出口更糟
  assert.deepEqual(ports.exitPorts("未来才有的类型"), ["out"]);
  assert.deepEqual(ports.exitPorts(""), ["out"]);
});

test("case 数被钳制在 [2, MAX]，非法输入回落到下限", () => {
  assert.deepEqual(ports.exitPorts("branch", 0), ["case:1", "case:2", "else"]);
  assert.deepEqual(ports.exitPorts("branch", 1), ["case:1", "case:2", "else"]);
  assert.equal(ports.exitPorts("branch", 99).length, ports.MAX_BRANCH_CASES + 1);
  assert.deepEqual(ports.exitPorts("branch", "3"), ["case:1", "case:2", "case:3", "else"]);
  assert.deepEqual(ports.exitPorts("branch", undefined), ["case:1", "case:2", "else"]);
  assert.deepEqual(ports.exitPorts("branch", null), ["case:1", "case:2", "else"]);
  assert.deepEqual(ports.exitPorts("branch", "abc"), ["case:1", "case:2", "else"]);
  assert.deepEqual(ports.exitPorts("branch", 3.4), ["case:1", "case:2", "case:3", "else"]);
});

test("出口名有可读的中文标签，未知出口不显示成空白", () => {
  assert.equal(ports.portLabel("out"), "下一步");
  assert.equal(ports.portLabel("true"), "成立");
  assert.equal(ports.portLabel("false"), "不成立");
  assert.equal(ports.portLabel("else"), "其他");
  assert.equal(ports.portLabel("case:2"), "情形 2");
  assert.equal(ports.portLabel("something-else"), "下一步");
});

test("isValidPort 只认该类型真实存在的出口", () => {
  assert.equal(ports.isValidPort("condition", "true"), true);
  assert.equal(ports.isValidPort("condition", "out"), false, "条件节点没有 out 出口");
  assert.equal(ports.isValidPort("branch", "case:5", 3), false, "超出 case 数的出口不合法");
  assert.equal(ports.isValidPort("branch", "case:5", 5), true);
  assert.equal(ports.isValidPort("mouse", "out"), true);
  assert.equal(ports.isValidPort("end", "out"), false);
});

test("开始节点不能有入边，结束节点不能有出边", () => {
  assert.equal(ports.canConnectFrom("end"), false);
  assert.equal(ports.canConnectTo("start"), false);
  for (const t of ["mouse", "condition", "branch", "note"]) {
    assert.equal(ports.canConnectFrom(t), true, `${t} 应该能出`);
    assert.equal(ports.canConnectTo(t), true, `${t} 应该能入`);
  }
});

// --------------------------------------------------------------------------- //
// nodeSummary：画布卡片上的那行摘要
// --------------------------------------------------------------------------- //
const defs = {
  delay: {
    type: "delay",
    name: "延迟",
    order: 30,
    common_params: [],
    params: [
      { key: "ms", label: "毫秒", ptype: "int", default: 1000 },
      { key: "jitter", label: "抖动", ptype: "int" },
    ],
  },
  replay: {
    type: "replay",
    name: "回放",
    order: 40,
    common_params: [],
    params: [{ key: "events", label: "事件", ptype: "events" }],
  },
};

const node = (type, params = {}) => ({ type, params, enabled: true, uid: "u" });

test("摘要按 ParamDef 顺序取前几个有值的参数", () => {
  assert.equal(summary.nodeSummary(node("delay", { ms: 500 }), defs), "500");
  assert.equal(summary.nodeSummary(node("delay", { ms: 500, jitter: 20 }), defs), "500 · 20");
  // 空值（空串 / 0 之外的 falsy）跳过：0 是合法参数值，必须显示
  assert.equal(summary.nodeSummary(node("delay", { ms: 0 }), defs), "0");
  assert.equal(summary.nodeSummary(node("delay", { ms: "", jitter: 5 }), defs), "5");
});

test("没有可用参数时返回空串，调用方据此不渲染那一行", () => {
  assert.equal(summary.nodeSummary(node("delay", {}), defs), "");
  assert.equal(summary.nodeSummary(node("unknown", {}), defs), "");
  // 定义没拉到（后端刚崩/正在重连）时必须退化为「不显示」而不是抛错：
  // 画布要能在后端不可用时照常渲染
  assert.equal(summary.nodeSummary(node("delay", { ms: 1 }), null), "");
  assert.equal(summary.nodeSummary(node("delay", { ms: 1 }), {}), "");
  assert.equal(summary.nodeSummary(null, defs), "");
  assert.equal(summary.nodeSummary(undefined, defs), "");
});

test("事件数组摘要显示条数——本地真源是数组，对外视图是 {count}", () => {
  // 后端 _public_node 会把 events 换成 {count}，前端不能因此显示成 [object Object]
  assert.equal(summary.nodeSummary(node("replay", { events: [{}, {}] }), defs), "2 个事件");
  assert.equal(summary.nodeSummary(node("replay", { events: { count: 7 } }), defs), "7 个事件");
});

test("摘要过长会截断，不会把节点卡片撑破", () => {
  const long = "x".repeat(200);
  const s = summary.nodeSummary(node("delay", { ms: long }), defs);
  assert.equal(s.length, summary.SUMMARY_MAX_CHARS);
  assert.ok(s.endsWith("…"));
});

test("参数值里的空串与空白不会被当成有效内容", () => {
  assert.equal(summary.nodeSummary(node("delay", { ms: "   " }), defs), "");
});
