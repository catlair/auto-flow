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
