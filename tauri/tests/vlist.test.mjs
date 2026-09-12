import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 直接跑真实的 src/utils/vlist.ts（纯函数，无 Vue/DOM 依赖），
// 因此不需要 WebView。虚拟列表最容易错的就是边界，这里把边界全钉住。
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

const { computeWindow } = loadModule("../src/utils/vlist.ts");
const { formatDiagnostics, fmtTime } = loadModule("../src/utils/diagnostics.ts");

const H = 15;

test("窗口只覆盖视口内的行，不随总条数增长", () => {
  const w = computeWindow({ count: 5000, itemH: H, viewportH: 200, scrollTop: 0, overscan: 8 });
  // 200/15 ≈ 14 行 + 1 + 上下各 8 行缓冲
  assert.ok(w.end - w.start <= 32, `渲染行数过多：${w.end - w.start}`);
  assert.equal(w.start, 0);
  assert.equal(w.totalH, 5000 * H);
  assert.equal(w.offsetY, 0);
});

test("滚动到中部时窗口跟着平移，且首行前的占位高度等于 start*行高", () => {
  const w = computeWindow({ count: 5000, itemH: H, viewportH: 200, scrollTop: 1500, overscan: 8 });
  assert.equal(w.offsetY, w.start * H);
  assert.ok(w.start > 0 && w.end < 5000);
  // 视口首行（1500/15 = 100）必须落在窗口内
  assert.ok(w.start <= 100 && 100 < w.end, `视口首行不在窗口内：${w.start}..${w.end}`);
});

test("滚到底部时窗口贴住末尾，不越界", () => {
  const count = 5000;
  const viewportH = 200;
  const w = computeWindow({
    count,
    itemH: H,
    viewportH,
    scrollTop: count * H, // 故意超出
    overscan: 8,
  });
  assert.equal(w.end, count);
  assert.ok(w.start < count);
  assert.equal(w.offsetY, w.start * H);
});

test("scrollTop 远超内容高度时不出现 start>end 的空窗口", () => {
  // 过度滚动（惯性滚动/程序设置 scrollTop）会让 first 落到 count 之外。
  // 不夹取的话 start 会超过 end，slice(start,end) 返回空数组 —— 列表整片消失。
  const count = 5000;
  const viewportH = 200;
  const w = computeWindow({ count, itemH: H, viewportH, scrollTop: 1e9, overscan: 8 });
  assert.ok(w.start < w.end, `窗口为空：${w.start}..${w.end}`);
  assert.ok(w.end <= count, `end 越界：${w.end}`);
  assert.ok(w.offsetY <= w.totalH - H, `占位高度超过内容总高：${w.offsetY}`);
  assert.ok(w.start < count);
});

test("count=0 返回空窗口", () => {
  const w = computeWindow({ count: 0, itemH: H, viewportH: 200, scrollTop: 0 });
  assert.deepEqual(w, { start: 0, end: 0, offsetY: 0, totalH: 0 });
});

test("视口高度未测量（0）时退化为全量渲染而不是白屏", () => {
  const w = computeWindow({ count: 300, itemH: H, viewportH: 0, scrollTop: 0 });
  assert.equal(w.start, 0);
  assert.equal(w.end, 300);
  assert.equal(w.totalH, 300 * H);
});

test("行高非法时退化为全量渲染，且不会算出错位窗口", () => {
  for (const bad of [0, -1, NaN, Infinity]) {
    const w = computeWindow({ count: 50, itemH: bad, viewportH: 200, scrollTop: 100 });
    assert.equal(w.start, 0, `itemH=${bad}`);
    assert.equal(w.end, 50, `itemH=${bad}`);
  }
});

test("负 scrollTop 与 NaN 都夹到 0", () => {
  for (const bad of [-999, NaN]) {
    const w = computeWindow({ count: 100, itemH: H, viewportH: 200, scrollTop: bad });
    assert.equal(w.start, 0, `scrollTop=${bad}`);
    assert.equal(w.offsetY, 0, `scrollTop=${bad}`);
  }
});

test("overscan=0 时窗口恰好覆盖可见行，且至少包含一行", () => {
  const w = computeWindow({ count: 100, itemH: H, viewportH: H, scrollTop: 0, overscan: 0 });
  assert.equal(w.start, 0);
  // 1 行视口 + 1 行（顶端露出的部分）
  assert.equal(w.end, 2);
});

test("总条数少于视口容量时窗口就是全量", () => {
  const w = computeWindow({ count: 3, itemH: H, viewportH: 200, scrollTop: 0, overscan: 8 });
  assert.equal(w.start, 0);
  assert.equal(w.end, 3);
});

// ---------- 诊断文本 ----------

const sampleDiag = (over = {}) => ({
  appVersion: "0.1.0",
  protocolExpected: 1,
  protocolOk: true,
  connected: true,
  rpcDownCount: 0,
  rpcDownAt: 0,
  lastRecoveredAt: 0,
  rpcDownDetail: "",
  lastError: "",
  permissions: { accessibility: false, inputMonitoring: true, screenRecording: true },
  workflowName: "回归工作流",
  nodeCount: 3,
  enabledNodeCount: 2,
  definitionCount: 9,
  base: { x: 10, y: 20 },
  scheduleMode: "",
  nextFire: "",
  recordBufferCount: 0,
  recordBufferLimit: 5000,
  lastRecordInfo: null,
  running: false,
  recording: false,
  ...over,
});

test("fmtTime 对 0/非法值返回破折号，不吐出 1970 年", () => {
  assert.equal(fmtTime(0), "—");
  assert.equal(fmtTime(NaN), "—");
  assert.equal(fmtTime(undefined), "—");
  assert.match(fmtTime(Date.now()), /^\d{2}:\d{2}:\d{2}$/);
});

test("诊断文本包含权限真假、协议匹配与断连详情全文", () => {
  const txt = formatDiagnostics(
    sampleDiag({
      protocolOk: false,
      rpcDownCount: 2,
      rpcDownAt: Date.now(),
      lastError: "运行出错：boom",
      rpcDownDetail: "sidecar 未找到\n搜索路径：/Applications/x\nstderr:\nTraceback...",
    })
  );
  assert.match(txt, /辅助功能 未授权/);
  assert.match(txt, /输入监控 已授权/);
  assert.match(txt, /★不匹配★/);
  assert.match(txt, /断连次数\s+2/);
  assert.match(txt, /最近错误\s+运行出错：boom/);
  // 多行详情必须原样保留——这是排查的主要线索
  assert.ok(txt.includes("搜索路径：/Applications/x"));
  assert.ok(txt.includes("Traceback..."));
});

test("未配置定时、无录制记录时不输出多余的空字段行", () => {
  const txt = formatDiagnostics(sampleDiag());
  assert.match(txt, /定时\s+未配置/);
  assert.ok(!txt.includes("上次录制"));
  assert.ok(!txt.includes("最近错误"));
  assert.ok(!txt.includes("最近断连详情"));
});

test("有录制记录时列出过滤/超限/监听中断", () => {
  const txt = formatDiagnostics(
    sampleDiag({
      lastRecordInfo: { count: 120, filtered: 5, limit_dropped: 3, mouse_died: true, kb_died: false },
    })
  );
  assert.match(txt, /上次录制\s+共 120 条/);
  assert.match(txt, /过滤 5/);
  assert.match(txt, /超限丢弃 3/);
  assert.match(txt, /监听中断（鼠标）/);
});
