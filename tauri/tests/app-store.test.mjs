import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 使用项目已有的 TypeScript 和 Node 测试器，无需启动 WebView 或真实 sidecar。
// 运行实际 store 源码，仅替换 RPC、系统文件对话框等外部边界。
const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/stores/app.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2021,
  },
}).outputText;
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
  assert.deepEqual(store.workflow, wf);
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

// ---------- §18 P4：拖拽排序 / 崩溃重连 / 诊断面板 ----------

const threeNodes = () => [
  { type: "mouse", params: {}, enabled: true, uid: "a", name: "" },
  { type: "keyboard", params: {}, enabled: true, uid: "b", name: "" },
  { type: "delay", params: {}, enabled: true, uid: "c", name: "" },
];

/** 只补上基础握手，其余方法交给传入的 handler。 */
const baseHandler = (nodes, extra) => async (method, params) => {
  if (method === "app.info") return { appVersion: "0.1.0", protocolVersion: 1, permissions: {} };
  if (method === "nodes.definitions") return [];
  if (method === "workflow.current") return { workflow: workflow(nodes), running: false, recording: false };
  if (method === "schedule.get") return { schedule: null, nextFire: "" };
  return extra ? extra(method, params) : {};
};

test("拖拽排序：moveNode 透传 (index,to) 并以响应刷新工作流", async () => {
  const nodes = threeNodes();
  const seen = [];
  const { store } = setup(
    baseHandler(nodes, (method, params) => {
      if (method !== "node.move") return {};
      seen.push(params);
      // 复刻后端语义：pop(index) 再 insert(to)（to 是最终位置）
      const arr = nodes.slice();
      arr.splice(params.to, 0, arr.splice(params.index, 1)[0]);
      return { workflow: workflow(arr), running: false, recording: false };
    })
  );
  await store.init();

  await store.moveNode(0, 2); // 把第一项拖到最后
  assert.deepEqual(seen, [{ index: 0, to: 2 }]);
  assert.deepEqual(store.workflow.nodes.map((n) => n.uid), ["b", "c", "a"]);
});

test("拖拽失败：moveNode 抛出（供 UI 回滚），真源不被乐观修改", async () => {
  const nodes = threeNodes();
  const { store } = setup(
    baseHandler(nodes, (method) => {
      if (method === "node.move") throw new Error("目标索引越界");
      return {};
    })
  );
  await store.init();
  const before = store.workflow.nodes.map((n) => n.uid);

  await assert.rejects(() => store.moveNode(0, 2), /越界/);
  // store 只在 RPC 成功后写入，所以失败时本来就干净——NodeList 需要靠这个
  // 异常把本地镜像拉回来（vuedraggable 已经乐观改过 items）。
  assert.deepEqual(store.workflow.nodes.map((n) => n.uid), before);
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
