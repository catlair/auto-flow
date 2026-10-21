import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 流程图的两个纯逻辑模块：出口名（ports）与节点摘要（summary）。
// 它们没有 Vue 依赖，可以直接编译求值，不需要 WebView。
const require = createRequire(import.meta.url);

/**
 * 编译并求值一个 TS 模块。
 *
 * `deps` 是**同项目模块**的替身：它们在 src 下，是 `.ts` 而不是 `.js`，
 * Node 的 require 认不出来。谁依赖谁就显式列出来，比给 loader 装一套路径解析
 * 规则简单，也让「这个模块依赖了谁」在测试里看得见。
 */
function loadTs(relPath, deps = {}) {
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
  fn((name) => (name in deps ? deps[name] : require(name)), mod, mod.exports);
  return mod.exports;
}

const ports = loadTs("../src/flow/ports.ts");
// summary 对 duration / recording 都是**值**导入：时长参数在卡片上要显示成
// 「1时30分」而不是毫秒总数，引用录制库的节点要显示录制名而不是一串 id。
// 两者自己都无依赖（recording 只用到 duration），所以按依赖顺序递进去。
const durationMod = loadTs("../src/flow/duration.ts");
const summary = loadTs("../src/flow/summary.ts", {
  "./duration": durationMod,
  "./recording": loadTs("../src/flow/recording.ts", { "./duration": durationMod }),
});

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

test("条件组与条件节点共用 true/false 出口", () => {
  // 两者对用户都是「一个判断、两条路」，出口形状一致才不用在画布上区分
  // 两种连线方式；出口名相同也意味着后端的边表不用为新节点做任何特殊处理。
  assert.deepEqual(ports.exitPorts("condition_group"), ["true", "false"]);
  assert.deepEqual(
    ports.exitPorts("condition_group"),
    ports.exitPorts("condition")
  );
  // 条件组的出口与项数无关：cond_count 改了手柄不该跟着变（边上只可能是
  // true/false，不像 branch 的 case:N 会随数量增删）
  assert.deepEqual(ports.exitPorts("condition_group", 6), ["true", "false"]);
  assert.equal(ports.isValidPort("condition_group", "true"), true);
  assert.equal(ports.isValidPort("condition_group", "case:1"), false);
  assert.equal(ports.canConnectFrom("condition_group"), true);
  assert.equal(ports.canConnectTo("condition_group"), true);
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
  condition_group: {
    type: "condition_group",
    name: "条件组",
    order: 82,
    common_params: [],
    params: [
      { key: "cond_count", label: "条件数量", ptype: "int", default: 2 },
      { key: "combine", label: "组合方式", ptype: "select", default: "全部成立(与)" },
      { key: "cond1_kind", label: "条件 1 检测方式", ptype: "select" },
      { key: "cond1_value", label: "条件 1 取值", ptype: "text" },
    ],
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

test("空事件数组不显示「0 个事件」", () => {
  // 引用录制库的节点把 events 存成 []（事件在那条录制里）。显示「0 个事件」会让
  // 卡片看起来像「这个节点是空的」，而它其实引用着一段完整录制。
  assert.equal(summary.nodeSummary(node("replay", { events: [] }), defs), "");
  assert.equal(summary.nodeSummary(node("replay", { events: { count: 0 } }), defs), "");
});

test("引用录制库的节点：卡片上显示录制名，不显示 id", () => {
  const rdefs = {
    record_replay: {
      type: "record_replay",
      name: "录制回放",
      order: 40,
      common_params: [],
      params: [
        { key: "source", label: "录制来源", ptype: "select", default: "内嵌" },
        { key: "recording_id", label: "选择录制", ptype: "recording" },
        { key: "events", label: "事件序列", ptype: "events" },
      ],
    },
  };
  // 后端 _public_node 会跟着 id 补一个 recording_name
  assert.equal(
    summary.nodeSummary(
      node("record_replay", { source: "录制库", recording_id: "20260915-190433-a1b2c3", recording_name: "登录群聊", events: [] }),
      rdefs
    ),
    "录制库 · 登录群聊"
  );
  // 名字没拿到（那条已被删 / 定义刚拉到）时不能退化成显示 id——
  // 一串 20260915-190433-a1b2c3 比不显示更糟：用户会以为自己认得出来。
  assert.equal(
    summary.nodeSummary(
      node("record_replay", { source: "录制库", recording_id: "20260915-190433-a1b2c3", events: [] }),
      rdefs
    ),
    "录制库"
  );
});

test("条件组摘要走通用规则，一眼能看出「几项、怎么组合」", () => {
  // 刻意**不给**条件组写专用格式化：summary 的设计就是与类型无关，专用表会
  // 随着新增节点悄悄过时（模块头注释里写明了这个取舍）。这条用例锁的是
  // 「通用规则对条件组给出的摘要够用」——前两个参数正好是项数与组合方式。
  const s = summary.nodeSummary(
    node("condition_group", {
      cond_count: 2,
      combine: "全部成立(与)",
      cond1_kind: "图像存在",
    }),
    defs
  );
  assert.ok(s.startsWith("2 · 全部成立(与)"), s);
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

// --------------------------------------------------------------------------- //
// duration 参数的摘要
//
// 后端 delay 的 `ms` ptype 是 duration，存的是**毫秒总数**。参数面板上它是
// 「时/分/秒/毫秒」四格，卡片上只印一个 5400000 谁也读不出来——换控件把输入
// 变方便了，别把卡片反而变难懂。格式化放在 flow/duration.ts，与面板同一份实现。
// --------------------------------------------------------------------------- //
const durDefs = {
  d: {
    type: "d",
    name: "d",
    order: 1,
    common_params: [],
    params: [{ key: "ms", label: "等待时长", ptype: "duration" }],
  },
};
const durNode = (ms) => ({ type: "d", params: { ms }, enabled: true, uid: "u" });

test("时长参数在卡片上显示成「1时30分」，而不是毫秒总数", () => {
  assert.equal(summary.nodeSummary(durNode(5400000), durDefs), "1时30分");
  assert.equal(summary.nodeSummary(durNode(3600000), durDefs), "1时");
  assert.equal(summary.nodeSummary(durNode(500), durDefs), "500毫秒");
  // 0 是合法值（「不等待」），不能被当成空值跳过
  assert.equal(summary.nodeSummary(durNode(0), durDefs), "0毫秒");
});

test("时长摘要只由 ptype 决定，与节点类型无关", () => {
  // summary.ts 刻意做成「与节点类型无关」。有人为了 delay 顺手加一张按 type
  // 分派的格式化表时，这条会挡住——那张表会随着新节点悄悄过时。
  const other = {
    x: {
      type: "x",
      name: "x",
      order: 1,
      common_params: [],
      params: [{ key: "ms", label: "随便什么", ptype: "duration" }],
    },
  };
  assert.equal(summary.nodeSummary({ type: "x", params: { ms: 90000 } }, other), "1分30秒");
});
