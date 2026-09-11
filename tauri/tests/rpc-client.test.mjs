import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 运行真实的 rpc/client.ts，只把 Tauri 的两个 IPC 边界（listen / invoke）换成桩，
// 因此不需要 WebView，也不会真的起 sidecar。
const require = createRequire(import.meta.url);

function loadClient(invoke) {
  const source = readFileSync(new URL("../src/rpc/client.ts", import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
  }).outputText;
  const listeners = new Map();
  const module = { exports: {} };
  const load = runInThisContext(`(function(require, module, exports) {\n${compiled}\n})`, {
    filename: "rpc-client-under-test.cjs",
  });
  load((name) => {
    if (name === "@tauri-apps/api/event") {
      return {
        listen: async (event, h) => {
          listeners.set(event, h);
          return () => listeners.delete(event);
        },
      };
    }
    if (name === "@tauri-apps/api/core") return { invoke };
    return require(name);
  }, module, module.exports);

  const emit = (event, payload) => {
    const h = listeners.get(event);
    assert.ok(h, `未注册事件：${event}`);
    h({ payload });
  };
  return { rpc: module.exports.rpc, emit, listeners };
}

/** 收集发出的请求行，并允许测试按需回帧。 */
function makeHarness() {
  const sent = [];
  let rpcStatus = [true, ""];
  const harness = loadClient(async (cmd, args) => {
    if (cmd === "rpc_status") return rpcStatus;
    if (cmd === "send_rpc") {
      const req = JSON.parse(args.line);
      sent.push(req);
      return undefined;
    }
    throw new Error("未知命令：" + cmd);
  });
  return {
    ...harness,
    sent,
    setRpcStatus: (up, detail = "") => { rpcStatus = [up, detail]; },
    /** 按 id 回一个成功响应帧。 */
    reply: (id, result) => harness.emit("rpc_event", JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n"),
    replyError: (id, code, message) =>
      harness.emit("rpc_event", JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n"),
  };
}

test("按换行切分粘包并匹配请求 id", async () => {
  const h = makeHarness();
  await h.rpc.connect();
  const p1 = h.rpc.request("a");
  const p2 = h.rpc.request("b");
  assert.deepEqual(h.sent.map((r) => r.method), ["a", "b"]);
  // 一块里塞两条响应（粘包），其中第二条帧被从中间切开（半包）
  const frame2 = JSON.stringify({ jsonrpc: "2.0", id: 2, result: "B" });
  h.emit("rpc_event", `${JSON.stringify({ jsonrpc: "2.0", id: 1, result: "A" })}\n${frame2.slice(0, 20)}`);
  h.emit("rpc_event", `${frame2.slice(20)}\n`);
  assert.equal(await p1, "A");
  assert.equal(await p2, "B");
});

test("通知（无 id）走 onNotify，不占用 pending", async () => {
  const h = makeHarness();
  await h.rpc.connect();
  const got = [];
  h.rpc.onNotify((n) => got.push(n));
  const pending = h.rpc.request("x");
  h.emit("rpc_event", JSON.stringify({ jsonrpc: "2.0", method: "run.node", params: { type: "delay" } }) + "\n");
  assert.equal(got.length, 1);
  assert.equal(got[0].method, "run.node");
  h.reply(1, "ok");
  assert.equal(await pending, "ok");
});

test("错误帧带上 code 与 data 一起 reject", async () => {
  const h = makeHarness();
  await h.rpc.connect();
  const p = h.rpc.request("run.start");
  h.replyError(1, -32004, "workflow_empty");
  await assert.rejects(p, (e) => e.code === -32004 && /workflow_empty/.test(e.message));
});

test("后端不回帧时按超时 reject，不会永久挂起", async () => {
  const h = makeHarness();
  await h.rpc.connect();
  h.rpc.requestTimeoutMs = 20;
  await assert.rejects(h.rpc.request("app.info"), /超时/);
});

test("超时后才到达的响应不会二次结算", async () => {
  const h = makeHarness();
  await h.rpc.connect();
  h.rpc.requestTimeoutMs = 20;
  await assert.rejects(h.rpc.request("slow"));
  // 此时 pending 已清空，迟到的帧应被安全忽略
  h.reply(1, "late");
  assert.equal(h.rpc.isConnected, true);
});

test("断线时所有在途请求立即 reject，而不是等到超时", async () => {
  const h = makeHarness();
  await h.rpc.connect();
  h.rpc.requestTimeoutMs = 10000; // 故意设很大：证明是断线触发的 reject
  const p = h.rpc.request("workflow.save");
  const statuses = [];
  h.rpc.onStatus((up, detail) => statuses.push([up, detail]));
  h.emit("rpc_down", "sidecar 崩溃：boom");
  await assert.rejects(p, /boom/);
  assert.equal(h.rpc.isConnected, false);
  assert.deepEqual(statuses, [[false, "sidecar 崩溃：boom"]]);
});

test("connect() 会主动查一次末次状态，补齐注册前的竞态", async () => {
  const h = makeHarness();
  h.setRpcStatus(false, "sidecar 启动即失败");
  const statuses = [];
  await h.rpc.connect();
  h.rpc.onStatus((up, detail) => statuses.push([up, detail]));
  // 注册晚于 connect() 的查询，故这里只验证 connect 后状态为 false（不会误判为在线）
  assert.equal(h.rpc.isConnected, false);
  // 再模拟一次真实上线
  h.emit("rpc_up", "");
  assert.equal(h.rpc.isConnected, true);
});
