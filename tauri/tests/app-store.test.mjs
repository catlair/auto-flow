import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 使用项目已有的 TypeScript 和 Node 测试器，无需启动 WebView 或真实 sidecar。
// 运行实际 store 源码，仅替换 RPC、系统文件对话框等外部边界。
const require = createRequire(import.meta.url);

/**
 * 把仓库里的 .ts 模块编译成 CJS 并求值，返回它的 exports。
 *
 * 为什么要真编译而不是打桩：store 里 `addEdge(src, dst, port = PORT_OUT)`
 * 的默认值来自 `@/flow/ports`。打桩的话测的是桩的常量，而真实模块写错字面量
 * 时测试照样绿——那正好是「前端出口名和后端对不上」这类最难查的 bug。
 */
function compileTs(relPath) {
  const src = readFileSync(new URL(relPath, import.meta.url), "utf8");
  return ts.transpileModule(src, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
  }).outputText;
}

function loadModule(relPath, resolveShim) {
  const out = compileTs(relPath);
  const mod = { exports: {} };
  const fn = runInThisContext(`(function(require, module, exports) {\n${out}\n})`, {
    filename: relPath.replace(/[^\w]/g, "_") + ".cjs",
  });
  fn(resolveShim ?? ((name) => require(name)), mod, mod.exports);
  return mod.exports;
}

const compiled = compileTs("../src/stores/app.ts");
// 真实的出口名常量模块（无依赖，可直接求值）
const flowPorts = loadModule("../src/flow/ports.ts");
// 真实的环检测模块（同样无依赖；store.addEdge 用它算 cycleEdges）
const flowCycle = loadModule("../src/flow/cycle.ts");
// 真实的工作流库纯逻辑模块（无依赖；store 用它的 normalizeMetas / wfExportName）
const flowWorkflow = loadModule("../src/flow/workflow.ts");
const { createPinia } = require("pinia");

function setup(request, dialog = {}) {
  const calls = [];
  const notifyHandlers = new Set();
  const statusHandlers = new Set();
  const invokes = [];
  const listeners = [];
  const defaultRequest = async (method) => {
    switch (method) {
      case "app.info":
        return { appVersion: "0.1.0", protocolVersion: 1, permissions: {} };
      case "nodes.definitions":
        return [];
      case "workflow.current":
        return { workflow: workflow(), running: false, recording: false };
      case "workflow.new":
        // path: null 是**真实**的后端取值（新建后还没落盘），别改成 ""
        return { workflow: workflow(), path: null, lib_id: "", dirty: false };
      case "workflow.save":
        return { path: "/tmp/wf/a1.json", lib_id: "a1", dirty: false, item: null };
      case "workflow.lib.list":
        return { items: [], current: "", dirty: false, dir: "/tmp/wf" };
      case "schedule.get":
        return { schedule: null, nextFire: "" };
      case "hotkey.set":
        return { hotkeys: ["record", "run", "pick"] };
      case "run.start":
      case "run.stop":
        return { running: true };
      default:
        return {};
    }
  };
  const handler = request ?? defaultRequest;
  const rpc = {
    request(method, params) {
      calls.push({ method, params });
      return handler(method, params);
    },
    async connect() {},
    onNotify(h) {
      notifyHandlers.add(h);
      return () => notifyHandlers.delete(h);
    },
    onStatus(h) {
      statusHandlers.add(h);
      return () => statusHandlers.delete(h);
    },
  };
  const module = { exports: {} };
  const load = runInThisContext(`(function(require, module, exports) {\n${compiled}\n})`, {
    filename: "app-store-under-test.cjs",
  });
  load((name) => {
    if (name === "@/rpc/client") return { rpc, errMessage: (e) => String(e?.message ?? e) };
    if (name === "@/flow/ports") return flowPorts;
    if (name === "@/flow/cycle") return flowCycle;
    if (name === "@/flow/workflow") return flowWorkflow;
    // `invoke` 记下来：窗口/托盘类命令没有 RPC 帧可看，只能这样断言"到底发了什么"。
    // 真跑 Node 时它本来就会抛（window 未定义），所以替身不影响其它用例的语义。
    if (name === "@tauri-apps/api/core") {
      return { invoke: (cmd, args) => { invokes.push({ cmd, args }); return Promise.resolve(); } };
    }
    if (name === "@tauri-apps/api/event") {
      return { listen: async (ev, h) => { listeners.push({ ev, h }); return () => {}; } };
    }
    if (name === "@tauri-apps/plugin-dialog") {
      // 默认**禁止**打开系统对话框：真弹出来会挂住测试。需要走导入/导出的用例
      // 显式传 dialog 替身，让「到底传了什么 defaultPath」可断言。
      return {
        open: dialog.open ?? (() => { throw new Error("禁止打开系统对话框"); }),
        save: dialog.save ?? (() => { throw new Error("禁止打开系统对话框"); }),
      };
    }
    return require(name);
  }, module, module.exports);
  return {
    store: module.exports.useAppStore(createPinia()),
    // 模块导出（如 RECORD_BUFFER_LIMIT），便于断言「实现与常量同源」
    mod: module.exports,
    ports: flowPorts,
    calls,
    invokes,
    listeners,
    emitStatus: (up, detail = "") => statusHandlers.forEach((h) => h(up, detail)),
    emitTauri: (ev, payload) => listeners.filter((l) => l.ev === ev).forEach((l) => l.h(payload)),
    methods: () => calls.map((c) => c.method),
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function notify(store, method, params = {}) {
  store.handleNotification({ jsonrpc: "2.0", method, params });
}

const workflow = (nodes = []) => ({ name: "回归工作流", speed: 1, repeat: 1, nodes });

test("workflow.changed 使用后端直接发送的工作流，并修正选中索引", () => {
  const { store } = setup();
  store.selectedIndex = 3;
  const wf = workflow([{ type: "delay", params: { ms: 1 }, enabled: true }]);
  notify(store, "workflow.changed", wf);
  // 后端帧缺 edges/start 时会被补成默认值（画布直接读这两个字段），
  // 其余字段必须原样保留、不能被复制丢内容
  assert.deepEqual(store.workflow, { ...wf, edges: [], start: "" });
  assert.equal(store.selectedIndex, 0);
  notify(store, "workflow.changed", workflow());
  assert.equal(store.selectedIndex, -1);
});

test("无效工作流不能覆盖现有状态，后续有效通知仍可处理", () => {
  const { store } = setup();
  const before = store.workflow;
  assert.throws(() => store.applyWorkflow(undefined));
  assert.equal(store.workflow, before);
  assert.throws(() => store.applyWorkflow({ name: "损坏数据" }));
  assert.equal(store.workflow, before);
  notify(store, "workflow.changed", workflow());
  assert.equal(store.workflow.name, "回归工作流");
});

test("手动启动立即进入运行态，下一次切换发送停止而非再次启动", async () => {
  const start = deferred();
  const { store, calls } = setup((method) => method === "run.start" ? start.promise : Promise.resolve({ running: true }));
  store.base = { x: 10, y: 20 };
  store.runProgress = { done: 2, total: 5 };
  store.runNodeType = "old";
  const pending = store.toggleRun();
  assert.equal(store.running, true);
  assert.deepEqual(store.runProgress, { done: 0, total: 0 });
  assert.equal(store.runNodeType, "");
  await store.toggleRun();
  assert.deepEqual(calls, [
    { method: "run.start", params: { base_x: 10, base_y: 20 } },
    { method: "run.stop", params: undefined },
  ]);
  // stop 应答仅表示停止请求已接收，等 finished 再退出运行态。
  assert.equal(store.running, true);
  notify(store, "run.finished", { stopped: true });
  start.resolve({ running: true });
  await pending;
  assert.equal(store.running, false);
});

test("快速工作流先结束后收到启动应答，不得恢复为运行中", async () => {
  const start = deferred();
  const { store } = setup(() => start.promise);
  const pending = store.toggleRun();
  notify(store, "run.node", { index: 0, type: "delay" });
  notify(store, "run.finished", { stopped: false });
  start.resolve({ running: true });
  await pending;
  assert.equal(store.running, false);
  assert.equal(store.runNodeType, "");
});

test("启动失败回滚运行状态并保留错误给调用方", async () => {
  const error = new Error("workflow_empty");
  const { store } = setup(async () => { throw error; });
  await assert.rejects(store.toggleRun(), (e) => e === error);
  assert.equal(store.running, false);
});

test("后端已结束时，停止响应直接收敛运行态（无需等待通知）", async () => {
  const { store } = setup(async (method) =>
    method === "run.stop" ? { running: false } : { running: true }
  );
  store.running = true;
  await store.toggleRun();
  assert.equal(store.running, false);
});

test("停止请求失败不会伪造已停止状态", async () => {
  const { store } = setup(async () => { throw new Error("send failed"); });
  store.running = true;
  await assert.rejects(store.toggleRun(), /send failed/);
  assert.equal(store.running, true);
});

test("节点通知同步后端运行态，结束时清理进度", () => {
  const { store } = setup();
  notify(store, "run.node", { index: 0, type: "record_replay" });
  assert.equal(store.running, true);
  assert.equal(store.runNodeType, "record_replay");
  notify(store, "run.progress", { done: 3, total: 10 });
  assert.deepEqual(store.runProgress, { done: 3, total: 10 });
  notify(store, "run.finished", { stopped: false });
  assert.equal(store.running, false);
  assert.deepEqual(store.runProgress, { done: 0, total: 0 });
});

test("运行错误在启动应答之前到达时，保持非运行态", async () => {
  const start = deferred();
  const { store } = setup(() => start.promise);
  const pending = store.toggleRun();
  notify(store, "run.error", { message: "boom" });
  start.resolve({ running: true });
  await pending;
  assert.equal(store.running, false);
  assert.match(store.banner, /boom/);
});

test("快速定时工作流结束后，延迟的 schedule.fired 不得重新置为运行中", () => {
  const { store } = setup();
  notify(store, "run.node", { type: "delay" });
  notify(store, "run.finished", { stopped: false });
  notify(store, "schedule.fired", { ran: true, path: "scheduled.json" });
  assert.equal(store.running, false);
  assert.match(store.banner, /scheduled.json/);
});

const settle = () => new Promise((r) => setTimeout(r, 20));

test("init 完成握手：拉定义、工作流、定时配置并装定热键", async () => {
  const { store, methods } = setup();
  await store.init();
  for (const m of ["app.info", "nodes.definitions", "workflow.current", "schedule.get", "hotkey.set"]) {
    assert.ok(methods().includes(m), `缺少握手步骤：${m}`);
  }
  assert.equal(store.connected, true);
  assert.equal(store.appVersion, "0.1.0");
  assert.equal(store.protocolOk, true);
});

test("断线显示横幅，重连后清除横幅并重新握手", async () => {
  const { store, methods, emitStatus } = setup();
  await store.init();
  const afterInit = methods().filter((m) => m === "app.info").length;

  emitStatus(false, "sidecar 崩溃：boom");
  assert.equal(store.connected, false);
  assert.match(store.banner, /boom/);
  // 断线期间不应误触发握手
  assert.equal(methods().filter((m) => m === "app.info").length, afterInit);

  emitStatus(true, "");
  await settle();
  assert.equal(store.connected, true);
  assert.equal(store.banner, "");
  // 重连必须重新握手：新 sidecar 的工作流是空的，不拉取会一直显示旧数据
  assert.ok(
    methods().filter((m) => m === "app.info").length > afterInit,
    "重连后未重新握手"
  );
  assert.ok(methods().includes("workflow.current"));
  assert.ok(methods().includes("hotkey.set"));
});

test("重连握手会用后端状态覆盖运行/录制态", async () => {
  const { store, emitStatus } = setup(async (method) => {
    if (method === "app.info") return { appVersion: "0.1.0", protocolVersion: 1, permissions: {} };
    if (method === "workflow.current")
      return { workflow: workflow(), running: false, recording: false };
    if (method === "nodes.definitions") return [];
    if (method === "schedule.get") return { schedule: null, nextFire: "" };
    return {};
  });
  await store.init();
  store.running = true;
  store.recording = true;
  emitStatus(false, "断开");
  emitStatus(true, "");
  await settle();
  // 新后端没有在跑任何东西，前端不得继续显示「运行中」
  assert.equal(store.running, false);
  assert.equal(store.recording, false);
});

// ---------- v4 流程图：边 / 坐标 / 起点 / 迁移 ----------

const threeNodes = () => [
  { type: "mouse", params: {}, enabled: true, uid: "a", name: "" },
  { type: "keyboard", params: {}, enabled: true, uid: "b", name: "" },
  { type: "delay", params: {}, enabled: true, uid: "c", name: "" },
];

/** 只补上基础握手，其余方法交给传入的 handler。 */
const baseHandler = (nodes, extra, wfExtra = {}) => async (method, params) => {
  if (method === "app.info") return { appVersion: "0.1.0", protocolVersion: 1, permissions: {} };
  if (method === "nodes.definitions") return [];
  if (method === "workflow.current")
    return {
      workflow: { ...workflow(nodes), edges: [], start: "", ...wfExtra },
      running: false,
      recording: false,
    };
  if (method === "schedule.get") return { schedule: null, nextFire: "" };
  return extra ? extra(method, params) : {};
};

const wfOf = (nodes, edges = [], start = "") => ({
  ...workflow(nodes),
  edges,
  start,
});
const res = (nodes, edges = [], start = "") => ({
  workflow: wfOf(nodes, edges, start),
  running: false,
  recording: false,
});

test("旧帧缺少 edges/start 时补齐默认值，画布不会拿到 undefined", () => {
  const { store } = setup();
  store.applyWorkflow({
    name: "v3 帧",
    speed: 1,
    repeat: 1,
    nodes: [{ type: "delay", params: {}, enabled: true, uid: "a" }],
  });
  // 旧版后端（或重连前缓存的帧）没有这两个字段。画布的 computed 直接读
  // `workflow.edges`，补不上就是 undefined.some(...) 抛异常、整个界面白屏。
  assert.deepEqual(store.workflow.edges, []);
  assert.equal(store.workflow.start, "");
  assert.equal(store.workflow.name, "v3 帧", "其余字段要原样保留");
  assert.equal(store.workflow.nodes.length, 1, "nodes 不能被复制丢内容");
});

test("起始节点解析与后端 Executor.entry_uid 同规则", () => {
  const { store } = setup();
  const apply = (nodes, start = "") => store.applyWorkflow(wfOf(nodes, [], start));
  const n = (uid, type) => ({ type, params: {}, enabled: true, uid });

  apply([]);
  assert.equal(store.entryUid, "", "空工作流没有起点");

  // 既没有显式 start 也没有 start 节点 → 第一个节点
  apply([n("a", "delay"), n("b", "mouse")]);
  assert.equal(store.entryUid, "a");

  // 有 start 节点 → 它优先于「第一个节点」
  apply([n("a", "delay"), n("s", "start")]);
  assert.equal(store.entryUid, "s");

  // 显式 start 最优先
  apply([n("s", "start"), n("b", "mouse")], "b");
  assert.equal(store.entryUid, "b");

  // start 指向已不存在的节点 → 退回默认规则。
  // 不退回的话画布会把「起」徽标挂在一个空气节点上，用户按画布排查会彻底跑偏。
  apply([n("s", "start")], "ghost");
  assert.equal(store.entryUid, "s");
});

test("旧版迁移提示只弹一次，换成非迁移工作流后撤掉", () => {
  const { store } = setup();
  const nodes = [{ type: "delay", params: {}, enabled: true, uid: "a" }];
  const migrated = { ...wfOf(nodes), migrated_from_list: true };

  store.applyWorkflow(migrated);
  assert.match(store.migratedNotice, /有序列表/);
  assert.equal(store.workflowMigrated, true);

  // 用户关掉之后，后续任何一次 workflow.changed（改个参数就会有）
  // 都不得把提示重新弹出来——否则它等于关不掉。
  store.dismissMigrated();
  store.applyWorkflow(migrated);
  assert.equal(store.migratedNotice, "", "已经关掉的提示不得复活");

  // 换成普通工作流 → 标记与提示都要撤，否则提示会一直说上一个工作流的事
  store.applyWorkflow(wfOf(nodes));
  assert.equal(store.workflowMigrated, false);
  assert.equal(store.migratedNotice, "");
});

test("添加节点带上画布坐标，并以响应刷新工作流", async () => {
  const { store, calls } = setup(
    baseHandler([], (method, params) => {
      if (method !== "node.add") return {};
      return {
        ...res([{ type: params.type, params: {}, enabled: true, uid: "n1", x: params.x, y: params.y }]),
        index: 0,
      };
    })
  );
  const idx = await store.addNode("mouse", 120, 240);
  assert.equal(idx, 0);
  assert.deepEqual(calls.find((c) => c.method === "node.add").params, {
    type: "mouse",
    x: 120,
    y: 240,
  });
  assert.equal(store.workflow.nodes[0].x, 120);
});

test("节点坐标只在拖拽结束时提交一次，后端负责取整", async () => {
  const nodes = [{ type: "delay", params: {}, enabled: true, uid: "a", x: 0, y: 0 }];
  const { store, calls } = setup(
    baseHandler(nodes, (method, params) =>
      method === "node.setPos"
        ? res([
            {
              type: "delay",
              params: {},
              enabled: true,
              uid: "a",
              // 复刻后端 node_set_pos 的 int(round(...))
              x: Math.round(params.x),
              y: Math.round(params.y),
            },
          ])
        : {}
    )
  );
  await store.setNodePos("a", 33.4, 66.6);
  const pos = calls.filter((c) => c.method === "node.setPos");
  // 逐帧上报会把整份工作流广播几十次，刷满通知队列把运行状态通知挤掉
  assert.equal(pos.length, 1, "一次拖拽只能发一次坐标");
  assert.deepEqual(pos[0].params, { uid: "a", x: 33.4, y: 66.6 });
  assert.equal(store.workflow.nodes[0].x, 33);
});

test("连线：同一出口只留一条，替换时回报给调用方", async () => {
  const nodes = [
    { type: "condition", params: {}, enabled: true, uid: "c" },
    { type: "delay", params: {}, enabled: true, uid: "a" },
    { type: "delay", params: {}, enabled: true, uid: "b" },
  ];
  let edges = [];
  const { store, ports } = setup(
    baseHandler(nodes, (method, params) => {
      if (method === "edge.add") {
        edges = edges.filter((e) => !(e.src === params.src && e.port === params.port));
        edges.push({ src: params.src, port: params.port, dst: params.dst });
      } else if (method === "edge.remove") {
        edges = edges.filter((e) => !(e.src === params.src && e.port === params.port));
      } else {
        return {};
      }
      return res(nodes, edges.map((e) => ({ ...e })));
    })
  );

  assert.equal(
    (await store.addEdge("c", "a", ports.PORT_TRUE)).replaced,
    false,
    "首次连线不算替换"
  );
  assert.deepEqual(store.workflow.edges, [{ src: "c", port: "true", dst: "a" }]);

  // 同一出口再连一条：后端替换旧的，前端要能告诉用户「旧的那条没了」，
  // 否则用户会以为自己连了两条
  assert.equal(
    (await store.addEdge("c", "b", ports.PORT_TRUE)).replaced,
    true,
    "同出口第二次要回报替换"
  );
  assert.deepEqual(store.workflow.edges, [{ src: "c", port: "true", dst: "b" }]);

  // 另一个出口互不影响：条件节点两个出口本来就该各连一条
  await store.addEdge("c", "a", ports.PORT_FALSE);
  assert.equal(store.workflow.edges.length, 2);

  await store.removeEdge("c", ports.PORT_FALSE);
  assert.deepEqual(store.workflow.edges, [{ src: "c", port: "true", dst: "b" }]);
});

test("addEdge 不传出口时默认 out，与后端 edge_add 的默认值一致", async () => {
  const nodes = [{ type: "delay", params: {}, enabled: true, uid: "a" }];
  const { store, calls, ports } = setup(baseHandler(nodes, () => res(nodes)));
  await store.addEdge("a", "a");
  assert.equal(ports.PORT_OUT, "out", "字面量必须与 core/events.py 一致");
  assert.deepEqual(calls.find((c) => c.method === "edge.add").params, {
    src: "a",
    dst: "a",
    port: "out",
    dst_side: "left",
  });
});

test("addEdge 把落点侧原样送给后端（画布据此决定线画在目标节点的哪一侧）", async () => {
  const nodes = [{ type: "delay", params: {}, enabled: true, uid: "a" }];
  const { store, calls, ports } = setup(baseHandler(nodes, () => res(nodes)));
  await store.addEdge("a", "a", ports.PORT_OUT, ports.TARGET_SIDE_BOTTOM);
  assert.deepEqual(calls.find((c) => c.method === "edge.add").params, {
    src: "a",
    dst: "a",
    port: "out",
    dst_side: "bottom",
  });
});

test("连线：闭环时回报环上的边，而且环**不被拦**", async () => {
  const nodes = [
    { type: "delay", params: {}, enabled: true, uid: "a" },
    { type: "delay", params: {}, enabled: true, uid: "b" },
  ];
  let edges = [];
  const { store } = setup(
    baseHandler(nodes, (method, params) => {
      if (method !== "edge.add") return {};
      edges = edges.filter((e) => !(e.src === params.src && e.port === params.port));
      edges.push({ src: params.src, port: params.port, dst: params.dst });
      return res(nodes, edges.map((e) => ({ ...e })));
    })
  );
  // 必须显式置成「两个节点、还没连线」：`setup` 不会自己握手，store 初始是空的，
  // 不置的话第一条断言会因为「图里一个节点都没有」而**假通过**。
  store.applyWorkflow(wfOf(nodes, []));

  assert.equal((await store.addEdge("a", "b")).cycleEdges, null, "直链不成环");
  const loop = (await store.addEdge("b", "a")).cycleEdges;
  assert.ok(loop, "回边成环要把环上的边回报给调用方");
  // 回报的必须**是一条闭合走法**，而不是「环上随便哪些边」：画布拿它直接标红，
  // 标错一条用户就会顺着看错方向。第 0 条是刚连的那条。
  assert.equal(loop[0].src, "b");
  assert.equal(loop[0].dst, "a");
  assert.equal(loop[loop.length - 1].dst, "b", "最后一条要回到起点才闭合");
  // 环是 v4 的一等公民（「等到条件成立再往下走」只能靠回边表达），所以 store
  // **照样落地**——拦掉就等于把合法用法一起挡了。提示只在画布层。
  assert.equal(store.workflow.edges.length, 2, "成环的边必须真的连上，不能被拦");
});

test("连线：被调小 case_count 后残留的边不参与环判定（否则提示一个看不见的环）", async () => {
  // 分支从 4 个情形调回 3 个之后，`case:4` 上的边**仍留在文件里**（刻意的：
  // 调回去它还在），但画布不显示、执行器也不会走。
  const nodes = [
    { type: "branch", params: { case_count: 3 }, enabled: true, uid: "b" },
    { type: "delay", params: {}, enabled: true, uid: "d" },
  ];
  const stale = [{ src: "b", port: "case:4", dst: "d" }];
  let edges = stale.map((x) => ({ ...x }));
  const { store } = setup(
    baseHandler(nodes, (method, params) => {
      if (method !== "edge.add") return {};
      edges = edges.filter((e) => !(e.src === params.src && e.port === params.port));
      edges.push({ src: params.src, port: params.port, dst: params.dst });
      return res(nodes, edges.map((e) => ({ ...e })));
    })
  );
  store.applyWorkflow(wfOf(nodes, stale));

  // 把那条残留边算进去的话，d→b 就成了环（b→d 已存在）——用户会收到一条关于
  // 「画布上根本看不见的边」的提示，无从下手。**这条断言是区分性的**：
  // 一旦 store 不再用 liveEdges 过滤，它就会变成一条非 null 的环而失败。
  assert.equal(
    (await store.addEdge("d", "b")).cycleEdges,
    null,
    "拿看不见的边判环会误报"
  );
});

test("设置与恢复起始节点", async () => {
  const nodes = [
    { type: "start", params: {}, enabled: true, uid: "s" },
    { type: "mouse", params: {}, enabled: true, uid: "m" },
  ];
  const { store, calls } = setup(
    baseHandler(nodes, (method, params) =>
      method === "workflow.setStart" ? res(nodes, [], params.uid) : {}
    )
  );
  await store.setStart("m");
  assert.equal(store.entryUid, "m");

  await store.setStart("");
  assert.equal(store.workflow.start, "");
  assert.equal(store.entryUid, "s", "清空后回到「第一个 start 节点」的默认规则");
  assert.deepEqual(
    calls.filter((c) => c.method === "workflow.setStart").map((c) => c.params.uid),
    ["m", ""]
  );
});

test("删除别的节点时，选中跟着 uid 走而不是跟着下标漂移", async () => {
  // 用可变的 nodes 当「后端真源」：每次删除都从**当前**状态里摘，
  // 而不是从最初那份列表里摘——否则第二次删除的索引会指向错的人。
  let nodes = [
    { type: "delay", params: {}, enabled: true, uid: "a" },
    { type: "delay", params: {}, enabled: true, uid: "b" },
    { type: "delay", params: {}, enabled: true, uid: "c" },
  ];
  const { store } = setup(
    baseHandler(nodes, (method, params) => {
      if (method !== "node.remove") return {};
      nodes = nodes.filter((n) => n.uid !== params.uid);
      return res(nodes);
    })
  );
  await store.init();
  store.selectNode(2); // 选中 c
  assert.equal(store.selectedNode.uid, "c");

  await store.removeNode("a"); // 删掉 a → 下标整体前移
  // 只按 index 判断的话选中会从 2 落到 1，界面显示的是 b 的参数，
  // 用户接着一改就改错了对象
  assert.equal(store.selectedNode?.uid, "c", "选中必须还是 c，不能滑到 b 上");

  await store.removeNode("c");
  assert.equal(store.selectedIndex, -1);
  assert.equal(store.selectedNode, null);
  assert.deepEqual(nodes.map((n) => n.uid), ["b"]);
});

test("节点改动按 uid 寻址：多选删除不会删错人", async () => {
  // 画布上「多选后按 Delete」会给每个 remove 各发一次请求。若请求带的是
  // 下标，它们都按**同一份删除前的列表**算 → 第二笔起指向别的节点，删错且不报错。
  // 这条用例锁住「请求里必须带 uid、且不带 index」这个契约。
  let nodes = [
    { type: "delay", params: {}, enabled: true, uid: "a" },
    { type: "delay", params: {}, enabled: true, uid: "b" },
    { type: "delay", params: {}, enabled: true, uid: "c" },
  ];
  const seen = [];
  const { store } = setup(
    baseHandler(nodes, (method, params) => {
      if (!method.startsWith("node.")) return {};
      seen.push({ method, params });
      if (method === "node.remove") nodes = nodes.filter((n) => n.uid !== params.uid);
      return res(nodes);
    })
  );
  await store.init();

  // 并发发出（真实画布就是这么快），断言的是**后端真源**的结果
  await Promise.all([store.removeNode("a"), store.removeNode("c")]);
  assert.deepEqual(nodes.map((n) => n.uid), ["b"], "只该剩下 b");

  await store.renameNode("b", "改过名");
  await store.toggleNode("b", false);
  await store.setParam("b", "ms", 123);

  for (const s of seen) {
    assert.equal(typeof s.params.uid, "string", `${s.method} 必须带 uid`);
    assert.equal(s.params.index, undefined, `${s.method} 不该再送 index`);
  }
});

test("诊断面板报出边数、起点与迁移标记", () => {
  const { store } = setup();
  store.applyWorkflow({
    ...wfOf(
      [
        { type: "start", params: {}, enabled: true, uid: "s" },
        { type: "delay", params: {}, enabled: true, uid: "a" },
      ],
      [{ src: "s", port: "out", dst: "a" }]
    ),
    migrated_from_list: true,
  });
  const d = store.diagnostics;
  assert.equal(d.nodeCount, 2);
  assert.equal(d.edgeCount, 1);
  assert.equal(d.startUid, "s");
  assert.equal(d.migratedFromList, true);
});


test("断线显示原因与「正在重连」，恢复后给出恢复提示且 banner 清空", async () => {
  const { store, emitStatus } = setup();
  await store.init();
  assert.equal(store.reconnectNotice, "", "首次连接不应有恢复提示");
  assert.equal(store.rpcDownCount, 0);

  emitStatus(false, "sidecar 崩溃：boom\n更多细节第二行");
  assert.equal(store.connected, false);
  assert.match(store.banner, /正在重连/);
  assert.match(store.banner, /boom/);
  assert.ok(!store.banner.includes("第二行"), "横幅只放首行，换行会撑坏布局");
  assert.equal(store.rpcDownCount, 1);
  assert.ok(store.rpcDownAt > 0);

  emitStatus(true, "");
  await settle();
  assert.equal(store.connected, true);
  assert.equal(store.banner, "");
  assert.match(store.reconnectNotice, /已重新连接/);
  assert.match(store.reconnectNotice, /1 次/);
  assert.ok(store.lastRecoveredAt > 0);

  store.dismissReconnect();
  assert.equal(store.reconnectNotice, "");
});

test("重连后断连详情仍可查——用户常在恢复之后才去看诊断", async () => {
  const { store, emitStatus } = setup();
  await store.init();
  emitStatus(false, "sidecar 未找到\n搜索路径：/Applications/x");
  assert.ok(store.diagnostics.rpcDownDetail.includes("搜索路径：/Applications/x"));

  emitStatus(true, "");
  await settle();
  assert.equal(store.connected, true);
  // 「当前断连详情」随恢复清空（横幅不再指向它）
  assert.equal(store.rpcDownDetail, "");
  // 但诊断面板仍要能看到最近一次——否则最需要的那份线索只剩 console 里有
  assert.ok(
    store.diagnostics.rpcDownDetail.includes("搜索路径：/Applications/x"),
    "恢复后诊断里应仍保留最近一次断连详情"
  );
});

test("诊断信息汇总连接/权限/节点数，且后端已挂时仍可读", async () => {
  const { store, emitStatus, mod } = setup(baseHandler(threeNodes()));
  await store.init();
  notify(store, "permission.changed", {
    accessibility: false, inputMonitoring: true, screenRecording: true,
  });
  notify(store, "workflow.changed", workflow([
    { type: "delay", params: {}, enabled: true, uid: "a", name: "" },
    { type: "note", params: {}, enabled: false, uid: "b", name: "" },
  ]));
  emitStatus(false, "sidecar 未找到\n搜索路径：/Applications/x");

  const d = store.diagnostics;
  assert.equal(d.connected, false);
  assert.equal(d.protocolOk, true);
  assert.equal(d.nodeCount, 2);
  assert.equal(d.enabledNodeCount, 1);
  assert.equal(d.permissions.inputMonitoring, true);
  assert.equal(d.permissions.accessibility, false);
  assert.equal(d.rpcDownCount, 1);
  assert.ok(d.rpcDownDetail.includes("搜索路径：/Applications/x"), "详情全文要保留");
  assert.equal(d.recordBufferLimit, mod.RECORD_BUFFER_LIMIT);
  // 诊断面板的硬约束：不发任何 RPC（后端已挂时打开也不能卡住）
  const before = store.diagnostics;
  assert.ok(before);
});

test("诊断面板不触发 RPC：断开后取值不新增请求", async () => {
  const { store, emitStatus, methods } = setup(baseHandler(threeNodes()));
  await store.init();
  emitStatus(false, "断开");
  const n = methods().length;
  // 先确认 getter 真的可用，否则下面的断言在「getter 不存在」时会空过
  const d = store.diagnostics;
  assert.ok(d && d.appVersion, "诊断信息应可用");
  void store.diagnostics;
  assert.equal(methods().length, n);
});

test("录制事件缓冲超上限时丢最旧的，且上限与导出常量同源", () => {
  const { store, mod } = setup();
  const limit = mod.RECORD_BUFFER_LIMIT;
  assert.ok(limit > 0 && limit <= 20000, `上限量级不合理：${limit}`);

  notify(store, "record.event", {
    events: Array.from({ length: limit + 50 }, (_, i) => ({ kind: "move", ts_ms: i })),
  });
  assert.equal(store.recordBuffer.length, limit);
  assert.equal(store.recordBuffer[0].ts_ms, 50, "应丢最旧的");
  assert.equal(store.diagnostics.recordBufferCount, limit);
  // 这条不是同义反复：若 handler 里硬编码一个与导出常量不同的数字，
  // 缓冲长度就会与 diagnostics 报出的上限对不上。
  assert.equal(store.recordBuffer.length, store.diagnostics.recordBufferLimit);
});

test("error 级提示记入 lastError，后续 ok/warn 不覆盖它", () => {
  const { store } = setup();
  store.setBanner("运行出错：boom", "error");
  assert.equal(store.lastError, "运行出错：boom");
  store.setBanner("定时触发：x", "ok");
  store.setBanner("后端 sidecar 断开，正在重连…", "warn");
  assert.equal(store.banner, "后端 sidecar 断开，正在重连…");
  assert.equal(store.lastError, "运行出错：boom", "诊断要能看到最近一次真正的错误");
  assert.equal(store.diagnostics.lastError, "运行出错：boom");
});

test("录制停止：只有按钮停止才裁停止点击，快捷键停止不裁", async () => {
  // 回归 2026-09-12：两个入口曾共用默认 trim:true，于是按 F9 停止也会去序列里
  // 找"最后一个窗口内的按下"当停止点击，从那里把后面整段截掉——一次 7.8 秒的
  // 移动录制被裁成 0 条。快捷键停止时序列里根本没有停止动作可裁。
  const { store, calls } = setup();

  store.recording = true;
  await store.toggleRecord({ by: "hotkey" });
  let stop = calls.filter((c) => c.method === "record.stop").at(-1);
  assert.equal(stop.params.trim, false, "F9 停止不得裁剪");

  store.recording = true;
  await store.toggleRecord({ by: "button" });
  stop = calls.filter((c) => c.method === "record.stop").at(-1);
  assert.equal(stop.params.trim, true, "按钮停止要裁掉这次点击");
});

// --------------------------------------------------------------------------- //
// 后端告警进界面（log.warning）
//
// 背景：core.vision 那些「不报错、只是点歪/找不到」的诊断此前只进
// ~/Library/Logs/autoflow-tauri.log，而那是 5000+ 行的 RPC 帧流水，
// 让用户去里面 grep 等于没有诊断。
// --------------------------------------------------------------------------- //
test("后端 log.warning 记入诊断面板，且新的在前", () => {
  const { store } = setup();
  notify(store, "log.warning", {
    level: "WARNING", logger: "core.vision", message: "模板读不出来：/tmp/x.png",
  });
  notify(store, "log.warning", {
    level: "WARNING", logger: "core.vision", message: "模板与屏幕像素密度不一致",
  });

  assert.equal(store.backendNotes.length, 2);
  assert.equal(store.backendNotes[0].message, "模板与屏幕像素密度不一致", "新的应排前面");
  assert.equal(store.backendNotes[0].logger, "core.vision");
  assert.ok(store.backendNotes[0].ts > 0, "要带时间戳，否则面板里看不出先后");
  assert.equal(store.diagnostics.backendNotes.length, 2);
});

test("后端告警有上限，且上限与导出常量同源", () => {
  const { store, mod } = setup();
  const limit = mod.BACKEND_NOTE_LIMIT;
  assert.ok(limit > 0 && limit <= 100, `上限量级不合理：${limit}`);
  for (let i = 0; i < limit + 5; i++) {
    notify(store, "log.warning", {
      level: "WARNING", logger: "core.vision", message: `w${i}`,
    });
  }
  assert.equal(store.backendNotes.length, limit, "不能无限增长");
  assert.equal(store.backendNotes[0].message, `w${limit + 4}`, "应保留最新的");
});

test("后端告警不顶掉正在显示的运行错误，但空横幅时会露脸", () => {
  const { store } = setup();
  store.setBanner("运行出错：boom", "error");
  notify(store, "log.warning", {
    level: "WARNING", logger: "core.vision", message: "模板失效",
  });
  assert.equal(store.banner, "运行出错：boom", "不能把正在显示的错误顶掉");
  assert.equal(store.backendNotes.length, 1, "但仍要记进诊断面板");

  const { store: s2 } = setup();
  notify(s2, "log.warning", {
    level: "WARNING", logger: "core.vision", message: "模板失效",
  });
  assert.ok(s2.banner.includes("模板失效"), "横幅空着时应露脸，否则用户不会想到去开面板");
  assert.equal(s2.bannerKind, "warn");
  assert.equal(s2.lastError, "", "告警不是错误，不该污染 lastError");
});

test("空 message 的 log.warning 不产生空条目、不动横幅", () => {
  const { store } = setup();
  notify(store, "log.warning", { level: "WARNING", logger: "x", message: "" });
  notify(store, "log.warning", {});
  assert.equal(store.backendNotes.length, 0);
  assert.equal(store.banner, "");
});

// --------------------------------------------------------------------------- //
// 托盘（菜单栏）
//
// 窗口藏起来之后，托盘是**唯一**能看见/操作的地方，所以这里的错都不会当场
// 报错，而是表现为「点菜单没反应」「角标一直亮着」。其中事件名/命令名是
// **跨语言协议**：Rust 发什么、前端听什么，两边写错都不会编译失败。
// --------------------------------------------------------------------------- //
const rustSrc = readFileSync(new URL("../src-tauri/src/lib.rs", import.meta.url), "utf8");
const appVueSrc = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("托盘状态同步：只有真的翻转才发 IPC", () => {
  const { store, invokes } = setup();
  store.recording = false;
  // 第一推：recording 是 false，与 trayRecording(null) 不同 → 推一次
  store.syncTray();
  assert.deepEqual(invokes.at(-1), {
    cmd: "af_tray_state",
    args: { recording: false },
  });
  // 再推同样的值不该再发。录制中事件流每秒造成几十次状态变更，去重不能省。
  store.syncTray();
  store.syncTray();
  assert.equal(invokes.length, 1, "重复状态不该重复发 IPC");

  // 真翻转了要推
  store.recording = true;
  store.syncTray();
  assert.equal(invokes.length, 2);
  assert.equal(invokes.at(-1).args.recording, true);
});

test("隐藏到托盘：发 af_hide_window", () => {
  const { store, invokes } = setup();
  store.hideToTray();
  assert.equal(invokes.at(-1).cmd, "af_hide_window");
});

test("托盘事件名与命令名，前后端逐字一致", () => {
  // 事件名：Rust 在**录制开关那一支**里 emit 的名字 ↔ 前端 listen(...) 的名字。
  // 只取这一支、不扫全文件：`set_status` 里还有个 `app.emit("rpc_up")`，
  // 扫第一个匹配会取到它，于是这条测试变成验错对象、永远是绿的。
  const arm = /MENU_TOGGLE_RECORD\s*=>\s*\{([\s\S]*?)\n\s*\}/.exec(rustSrc);
  assert.ok(arm, "找不到录制开关的处理分支");
  const emitted = /app\.emit\(\s*"([^"]+)"/.exec(arm[1]);
  assert.ok(emitted, "录制开关没有 emit 事件");
  assert.ok(
    storeSrcIncludes(`listen<string>("${emitted[1]}"`),
    `前端没有监听 ${emitted[1]}`
  );

  // 命令名：Rust `#[tauri::command] fn X` ↔ generate_handler 注册 ↔ 前端 invoke
  const handler = /generate_handler!\[([\s\S]*?)\]/.exec(rustSrc);
  assert.ok(handler, "找不到 generate_handler");
  for (const cmd of ["af_tray_state", "af_hide_window"]) {
    assert.match(rustSrc, new RegExp(`fn ${cmd}\\(`), `Rust 缺 ${cmd}`);
    assert.ok(handler[1].includes(cmd), `generate_handler 里缺 ${cmd}`);
    assert.ok(storeSrcIncludes(`invoke("${cmd}"`), `前端没有调用 ${cmd}`);
  }
});

test("关闭窗口只隐藏不退出，且在 setup 里建了托盘", () => {
  // 点 × 若真的退掉，定时任务/录制/正在跑的回放会无声断在半路
  assert.match(rustSrc, /CloseRequested\s*\{\s*api/, "关窗没有拦截");
  assert.match(rustSrc, /api\.prevent_close\(\)/, "关窗没有 prevent_close");
  assert.match(rustSrc, /window\.hide\(\)/, "关窗没有隐藏");
  // 托盘创建失败不能阻断启动（它只是"收起来"的入口）
  assert.match(rustSrc, /if let Err\([^)]*\)\s*=\s*setup_tray\(&handle\)/, "托盘失败没有兜底");
  // 窗口藏起来后点 Dock：系统发 Reopen，而 Tauri **不会**自动把窗口叫回来。
  // 不接这一下，用户点 Dock 会"什么都没发生"。
  assert.match(rustSrc, /RunEvent::Reopen/, "没接 Reopen：点 Dock 唤不回窗口");
  // `Reopen` 只在 macOS 上存在，所以那一支必须被 cfg 门住，否则其它平台编译不过
  assert.match(
    rustSrc,
    /#\[cfg\(target_os = "macos"\)\]\s*\n\s*if let tauri::RunEvent::Reopen/,
    "Reopen 那一支没有 macos 门控（其它平台编译不过）"
  );
  // `tray-icon` 是非默认 feature：不开它 `tauri::tray` 整个模块编译期就不存在
  assert.match(
    readFileSync(new URL("../src-tauri/Cargo.toml", import.meta.url), "utf8"),
    /features\s*=\s*\[[^\]]*"tray-icon"/,
    "Cargo 里没开 tray-icon feature"
  );
});

test("托盘菜单里有恢复窗口和退出，且录制开关不顺手弹出窗口", () => {
  for (const text of ["显示主窗口", "退出 Auto Flow", "开始录制"]) {
    assert.ok(rustSrc.includes(text), `托盘子菜单缺「${text}」`);
  }
  // 录制开关**不**调 show_main：把窗口收起来在别处操作正是托盘存在的意义
  const arm = /MENU_TOGGLE_RECORD\s*=>\s*\{([\s\S]*?)\n\s*\}/.exec(rustSrc);
  assert.ok(arm, "找不到录制开关的处理分支");
  assert.ok(!arm[1].includes("show_main"), "录制开关不该把窗口弹出来");
});

test("界面上有「隐藏到托盘」的入口（否则用户不知道 × 只是收起）", () => {
  assert.ok(appVueSrc.includes("hideToTray"), "界面没有隐藏入口");
});

test("托盘图标有没有出现在菜单栏、角标记没记上：只能靠运行期日志回答", () => {
  // 「图标在不在菜单栏里」「录制中角标有没有真的设上去」都是**问不到**的问题：
  // 产物断言里命令名/id 被 LLVM 内联成立即数比较（二进制搜不到，见 setup_tray 的注释），
  // 屏幕截图又受显示器休眠/锁屏影响。唯一能自动化回答它们的是运行期日志，所以这几行是
  // **功能的一部分**，不是调试残留——被删掉时用户报「找不到图标」就没有现场可查了。
  //
  // 建成功必须留一行：托盘建失败只是"图标没出现"，与"根本没建"在日志里同样空白。
  assert.match(rustSrc, /log_line\("托盘已创建/, "建托盘成功没留日志：失败与没建分不清");

  // rect 必须取自托盘本体。`TrayIcon::rect()` 由 AppKit 给出，None 就是没有状态项；
  // 自造字段（窗口位置、缓存坐标）在图标其实不存在时也会给个数，等于没验。
  assert.match(rustSrc, /tray\.rect\(\)/, "没有 tray.rect()：问不到图标在不在菜单栏");

  // 状态行必须**同时**带上：推来的状态、打算设的角标、三个 set_* 的结果。
  // 只有 rect 的话，「图标在、但角标没设上去」（set_title 静默失败）与「图标不在」
  // 在日志里长得一模一样，等于把最需要区分的两种故障混成了一种。
  for (const [needle, why] of [
    ["托盘状态项 recording={recording}", "状态行没带推来的 recording：分不清该不该有角标"],
    ["角标={badge:?}", "状态行没带打算设的角标值：看不出设的是不是错的"],
    ["set_title={badge_ok}", "状态行没带 set_title 的结果：角标静默失败会被当成正常"],
    ["set_tooltip={tip_ok}", "状态行没带 set_tooltip 的结果"],
    ["set_menu={menu_ok:?}", "状态行没带菜单文案改写的结果"],
  ]) {
    assert.ok(rustSrc.includes(needle), why);
  }
  assert.match(
    rustSrc,
    /log_line\(&format!\(\s*\n?\s*"托盘状态项 recording=/,
    "状态行没进日志：用户报「找不到图标」时没有现场"
  );

  // 光有字段名不够：把结果写死成 `true` 也能让上面全部通过，而那正是"谎报成功"。
  // 所以要求这三个值**取自真实调用**的返回值。
  assert.match(
    rustSrc,
    /let badge_ok = tray\.set_title\(badge\)\.is_ok\(\);/,
    "角标结果不是取自 set_title 的返回值：静默失败会被谎报成成功"
  );
  assert.match(
    rustSrc,
    /let tip_ok = tray\.set_tooltip\(Some\(tip\)\)\.is_ok\(\);/,
    "工具提示结果不是取自 set_tooltip 的返回值"
  );
  assert.match(
    rustSrc,
    /\.map\(\|h\| h\.toggle\.set_text\(text\)\.is_ok\(\)\)/,
    "菜单文案结果不是取自 set_text 的返回值"
  );

  // 角标取值必须**两边都写对**：录制中设上、非录制时清掉。
  // 只设不清的话角标会一直亮着 → 用户以为还在录，实际早就停了（比没有角标更坏）。
  const badgeArm = /let \(tip, badge\) = if recording \{([\s\S]*?)\} else \{([\s\S]*?)\};/.exec(
    rustSrc
  );
  assert.ok(badgeArm, "找不到角标取值分支");
  assert.match(badgeArm[1], /"●"/, "录制中那一支没设角标（录制时看不到任何提示）");
  assert.match(badgeArm[2], /\bNone\b/, "非录制那一支没清角标：会一直亮着，像是还在录");

  // `tray_by_id` 返回 None 是**另一种**故障（build 成功但 id 被改/被移出），
  // 与"图标被系统藏起来"表现一样，必须留下一行区分得开的记录，且要带上
  // recording —— 这一支的两侧表现（该有角标时没有 / 本不该有）完全不同。
  assert.match(
    rustSrc,
    /log_line\(&format!\(\s*\n?\s*"托盘状态项查不到（tray_by_id 返回 None）recording=/,
    "句柄查不到没有单独日志：与图标不显示分不开"
  );
});

function storeSrcIncludes(needle) {
  return readFileSync(new URL("../src/stores/app.ts", import.meta.url), "utf8").includes(needle);
}

// --------------------------------------------------------------------------- #
// 工作流库（后端 core/workflow_lib.py）
// --------------------------------------------------------------------------- #
const libList = (extra = {}) => ({
  items: [{ id: "a1", name: "甲", node_count: 2, edge_count: 1, updated_at: "2026-09-15 11:20:33" }],
  current: "",
  dirty: false,
  dir: "/tmp/wf",
  ...extra,
});

test("新建把 force 原样送过去（前端的「丢弃改动」靠它）", async () => {
  const { store, calls } = setup();
  await store.newWorkflow();
  await store.newWorkflow(true);
  assert.deepEqual(
    calls.filter((c) => c.method === "workflow.new"),
    [
      { method: "workflow.new", params: { force: false } },
      { method: "workflow.new", params: { force: true } },
    ]
  );
});

test("新建之后不能沿用上一份的库 id", async () => {
  // 后端给的 path 是 **null**（还没落盘）。若用 `typeof cur.path === "string"` 判断，
  // 这一帧会被整个跳过——界面仍显示着上一份的库 id，用户按「保存」时以为存的是
  // 新那份，实际覆盖掉了刚才打开的那份流程。
  const { store } = setup();
  store.workflowLibCurrent = "上一份";
  store.workflowPath = "/tmp/wf/old.json";
  store.workflowDirty = true;
  await store.newWorkflow();
  assert.equal(store.workflowLibCurrent, "");
  assert.equal(store.workflowPath, "");
  assert.equal(store.workflowDirty, false);
});

test("保存把 path / lib_id / dirty 抄进 store，并立刻重拉库列表", async () => {
  const { store, calls } = setup((m) => {
    if (m === "workflow.save")
      return Promise.resolve({ path: "/tmp/wf/a1.json", lib_id: "a1", dirty: false, item: null });
    if (m === "workflow.lib.list") return Promise.resolve(libList({ current: "a1" }));
    return Promise.resolve({});
  });
  store.workflowDirty = true;
  await store.saveWorkflow();
  assert.equal(store.workflowPath, "/tmp/wf/a1.json");
  assert.equal(store.workflowLibCurrent, "a1");
  assert.equal(store.workflowDirty, false);
  assert.equal(store.workflowLib.length, 1);
  // 首次保存会在库里**新建**一条：不重拉的话用户接着打开「工作流库」
  // 会发现刚存的那份不在列表里，然后以为没存上、再存一遍
  assert.deepEqual(calls.map((c) => c.method), ["workflow.save", "workflow.lib.list"]);
});

test("打开库里的一条：应用工作流 + 位置，并清掉选中", async () => {
  const { store, calls } = setup((m) =>
    m === "workflow.lib.open"
      ? Promise.resolve({
          workflow: workflow([{ type: "delay", params: {}, enabled: true }]),
          path: "/tmp/wf/a1.json",
          lib_id: "a1",
          dirty: false,
          running: false,
          recording: false,
        })
      : Promise.resolve({})
  );
  store.selectedIndex = 2;
  await store.openWorkflowFromLib("a1");
  assert.equal(store.workflowLibCurrent, "a1");
  assert.equal(store.workflowPath, "/tmp/wf/a1.json");
  assert.equal(store.workflowDirty, false);
  assert.equal(store.workflow.nodes.length, 1);
  assert.equal(store.selectedIndex, -1);
  assert.deepEqual(calls, [{ method: "workflow.lib.open", params: { id: "a1", force: false } }]);
});

test("运行中不许从库里切一份跑（否则会把正在跑的这份停掉）", async () => {
  // toggleRun 按 running 决定「启动」还是「停止」：不拦的话用户以为在启动
  // 另一份，实际把正在跑的停了。拦下来时**一个请求都不该发**。
  const { store, calls } = setup();
  store.running = true;
  await assert.rejects(() => store.runWorkflowFromLib("a1"), /正在运行中/);
  assert.deepEqual(calls, []);
});

test("更名当前那份要同步标题栏，更名别人那份不能动标题栏", async () => {
  const { store, calls } = setup((m) => {
    if (m === "workflow.lib.rename")
      return Promise.resolve({ item: { id: "a1", name: "新名字" }, current: "a1", dirty: false });
    if (m === "workflow.lib.list") return Promise.resolve(libList({ current: "a1" }));
    return Promise.resolve({});
  });
  store.workflowLibCurrent = "a1";
  await store.renameWorkflowInLib("a1", "新名字");
  assert.equal(store.workflow.name, "新名字");
  assert.deepEqual(calls.map((c) => c.method), ["workflow.lib.rename", "workflow.lib.list"]);

  const other = setup((m) =>
    m === "workflow.lib.rename"
      ? Promise.resolve({ item: { id: "b2", name: "别人的" }, current: "a1", dirty: false })
      : Promise.resolve(libList())
  );
  other.store.workflowLibCurrent = "a1";
  await other.store.renameWorkflowInLib("b2", "别人的");
  assert.equal(other.store.workflow.name, "未命名", "改别人的名字动到了当前这份");
});

test("删除后清空当前库 id（文件没了，画布还在）", async () => {
  const { store, calls } = setup((m) => {
    if (m === "workflow.lib.delete")
      return Promise.resolve({
        deleted: "a1",
        trash: "/tmp/trash/a1.json",
        current: "",
        dirty: true,
      });
    if (m === "workflow.lib.list")
      return Promise.resolve(libList({ items: [], current: "", dirty: true }));
    return Promise.resolve({});
  });
  store.workflowLibCurrent = "a1";
  store.workflowDirty = false;
  await store.deleteWorkflowFromLib("a1");
  assert.equal(store.workflowLibCurrent, "");
  // 画布上的内容没被清（后端只清路径），所以现在是「有未保存的改动」
  assert.equal(store.workflowDirty, true);
  assert.deepEqual(calls.map((c) => c.method), ["workflow.lib.delete", "workflow.lib.list"]);
});

test("导出：默认文件名洗过，取消时一个请求都不发", async () => {
  let opts = null;
  const { store, calls } = setup(
    (m) =>
      Promise.resolve(
        m === "workflow.lib.list"
          ? libList({
              current: "a1",
              // 名字里带 `/`：直接用它会成为 defaultPath，而保存对话框会把文件
              // **悄悄写进一个子目录**，用户以为自己只是沿用了原来的名字
              items: [{ id: "a1", name: "登录/测试", node_count: 0, edge_count: 0 }],
            })
          : {}
      ),
    { save: (o) => { opts = o; return Promise.resolve("/tmp/out.json"); } }
  );
  await store.refreshWorkflowLib();
  const p = await store.exportWorkflow("a1");
  assert.equal(p, "/tmp/out.json");
  assert.equal(opts.defaultPath, "登录_测试.json");
  assert.deepEqual(calls.find((c) => c.method === "workflow.lib.export").params, {
    path: "/tmp/out.json",
    id: "a1",
  });

  const cancelled = setup(() => Promise.resolve({}), { save: () => Promise.resolve(null) });
  assert.equal(await cancelled.store.exportWorkflow(), "");
  assert.deepEqual(cancelled.calls, [], "取消之后还发了请求");
});

test("导入：取消不发请求；成功时重拉列表且不切换当前工作流", async () => {
  const { store, calls } = setup(
    (m) =>
      Promise.resolve(
        m === "workflow.lib.import"
          ? { item: { id: "new1", name: "导进来的" }, items: [] }
          : libList({ current: "a1" })
      ),
    { open: () => Promise.resolve("/tmp/in.json") }
  );
  store.workflowLibCurrent = "a1";
  assert.equal(await store.importWorkflowFromFile(), "new1");
  assert.deepEqual(calls.map((c) => c.method), ["workflow.lib.import", "workflow.lib.list"]);
  assert.equal(store.workflowLibCurrent, "a1", "导入把当前工作流换掉了");

  const cancelled = setup(() => Promise.resolve({}), { open: () => Promise.resolve(null) });
  assert.equal(await cancelled.store.importWorkflowFromFile(), "");
  assert.deepEqual(cancelled.calls, []);
});

test("库列表遇到坏数据不崩，也不把当前库 id 弄丢", async () => {
  const { store } = setup((m) =>
    Promise.resolve(
      m === "workflow.lib.list"
        ? { items: "not-a-list", current: "a1", dirty: "yes", dir: 5 }
        : {}
    )
  );
  store.workflowDirty = true;
  await store.refreshWorkflowLib();
  assert.deepEqual(store.workflowLib, []);
  assert.equal(store.workflowLibCurrent, "a1");
  assert.equal(store.workflowLibDir, "5");
  assert.equal(store.workflowDirty, true, "dirty 不是布尔值就不该动它");
});

test("dirty 只在后端给了布尔值时才覆盖", () => {
  const { store } = setup();
  store.workflowDirty = true;
  // 重连前缓存的旧帧 / 旧版后端都不带 dirty，`!!undefined` 会把标记清掉，
  // 表现为「改了一堆东西，标记却忽然没了」
  notify(store, "workflow.changed", workflow());
  assert.equal(store.workflowDirty, true, "缺 dirty 的帧把标记清掉了");
  notify(store, "workflow.changed", { ...workflow(), dirty: false });
  assert.equal(store.workflowDirty, false);
});

