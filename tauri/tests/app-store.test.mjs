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
const { createPinia } = require("pinia");

function setup(request) {
  const calls = [];
  const notifyHandlers = new Set();
  const statusHandlers = new Set();
  const defaultRequest = async (method) => {
    switch (method) {
      case "app.info":
        return { appVersion: "0.1.0", protocolVersion: 1, permissions: {} };
      case "nodes.definitions":
        return [];
      case "workflow.current":
        return { workflow: workflow(), running: false, recording: false };
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
    if (name === "@tauri-apps/plugin-dialog") {
      return { open: () => { throw new Error("禁止打开系统对话框"); },
        save: () => { throw new Error("禁止打开系统对话框"); } };
    }
    return require(name);
  }, module, module.exports);
  return {
    store: module.exports.useAppStore(createPinia()),
    // 模块导出（如 RECORD_BUFFER_LIMIT），便于断言「实现与常量同源」
    mod: module.exports,
    ports: flowPorts,
    calls,
    emitStatus: (up, detail = "") => statusHandlers.forEach((h) => h(up, detail)),
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

  assert.equal(await store.addEdge("c", "a", ports.PORT_TRUE), false, "首次连线不算替换");
  assert.deepEqual(store.workflow.edges, [{ src: "c", port: "true", dst: "a" }]);

  // 同一出口再连一条：后端替换旧的，前端要能告诉用户「旧的那条没了」，
  // 否则用户会以为自己连了两条
  assert.equal(await store.addEdge("c", "b", ports.PORT_TRUE), true, "同出口第二次要回报替换");
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
