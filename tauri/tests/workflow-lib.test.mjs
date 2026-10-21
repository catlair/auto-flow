import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 工作流库的界面侧：纯逻辑模块 + 组件接线 + 一条跨语言的常量一致性检查。
// 没有 Vue 依赖的部分直接编译求值，不需要 WebView。
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
  fn(require, mod, mod.exports);
  return mod.exports;
}

const W = loadTs("../src/flow/workflow.ts");
const readSrc = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

// --------------------------------------------------------------------------- #
// 纯逻辑
// --------------------------------------------------------------------------- #
test("名字为空时一律给占位，不留空白", () => {
  assert.equal(W.wfDisplayName("甲"), "甲");
  assert.equal(W.wfDisplayName("  乙  "), "乙");
  // 后端给的名字一定是字符串；这里只兜「空/缺」这一类
  for (const v of ["", "   ", null, undefined]) {
    assert.equal(W.wfDisplayName(v), "未命名工作流", String(v));
  }
});

test("时间戳按字符串切片解析，不碰 new Date", () => {
  // "2026-09-15 11:20:33"（空格分隔）不是 ECMAScript 规定的格式：
  // V8 收、别的引擎可能收成 Invalid Date，而那时列表上会显示 "Invalid Date"，
  // 在 macOS 上永远复现不出来。
  assert.equal(W.fmtStamp("2026-09-15 11:20:33"), "09-15 11:20");
  assert.equal(W.fmtStamp("2026-09-15T11:20:33"), "09-15 11:20");
  assert.equal(W.fmtStamp(""), "");
  assert.equal(W.fmtStamp(null), "");
  assert.equal(W.fmtStamp("昨天"), "");
  assert.equal(W.fmtStamp("2026-09-15"), "");
});

test("列表副标题：节点数 / 连线数 / 最近修改", () => {
  assert.equal(
    W.wfSubtitle({ node_count: 3, edge_count: 2, updated_at: "2026-09-15 11:20:33" }),
    "3 个节点 · 2 条连线 · 修改于 09-15 11:20"
  );
  // 空流程说「空流程」而不是「0 个节点」：前者是状态，后者看着像计数出错
  assert.equal(W.wfSubtitle({ node_count: 0, edge_count: 0 }), "空流程 · 0 条连线");
  // 时间解析不出来就**不显示时间**，不显示 "Invalid Date"、也不留个空尾巴
  assert.equal(
    W.wfSubtitle({ node_count: 1, edge_count: 0, updated_at: "坏时间" }),
    "1 个节点 · 0 条连线"
  );
  // 计数是垃圾时按 0 算，别把 "3" 和 3 拼成 "3 个节点" 之外的东西
  assert.equal(W.wfSubtitle({ node_count: "3", edge_count: null }), "3 个节点 · 0 条连线");
  assert.equal(W.wfSubtitle({ node_count: -5, edge_count: -1 }), "空流程 · 0 条连线");
  assert.equal(W.wfSubtitle(null), "空流程 · 0 条连线");
});

test("导出的默认文件名必须洗过：`/` 会把文件悄悄写进子目录", () => {
  assert.equal(W.wfExportName("登录流程"), "登录流程.json");
  assert.equal(W.wfExportName("a/b"), "a_b.json");
  assert.equal(W.wfExportName("a:b"), "a_b.json");
  assert.equal(W.wfExportName("a\\b"), "a_b.json");
  assert.equal(W.wfExportName("a/b:c\\d"), "a_b_c_d.json");
  // 开头的点会变成隐藏文件，导完在 Finder 里根本看不见
  assert.equal(W.wfExportName(".hidden"), "hidden.json");
  assert.equal(W.wfExportName("...x"), "x.json");
  // 控制字符会把文件名搞成不可见/不可点
  assert.equal(W.wfExportName("a\u0001b"), "ab.json");
  // 洗空了要有兜底，不能给一个 ".json"
  assert.equal(W.wfExportName(""), "未命名工作流.json");
  assert.equal(W.wfExportName("   "), "未命名工作流.json");
  assert.equal(W.wfExportName(null), "未命名工作流.json");
  // 全是分隔符时洗成下划线而不是走兜底：用户确实打了字，只是都是非法字符，
  // 保留「这里有东西」比换成默认名更不意外
  assert.equal(W.wfExportName("///"), "___.json");
  // 超长名字要截断：macOS 单个文件名上限 255 字节，中文一个字 3 字节
  const long = W.wfExportName("工".repeat(200));
  assert.ok(long.endsWith(".json"));
  assert.ok(Buffer.byteLength(long, "utf8") < 255, long.length + " 字");
});

test("列表数据规整：坏一条不能把整个弹层打崩", () => {
  assert.deepEqual(W.normalizeMetas(null), []);
  assert.deepEqual(W.normalizeMetas("x"), []);
  assert.deepEqual(W.normalizeMetas({ items: [] }), []);
  // 没有 id 的条目丢掉：它渲染出来点哪个按钮都会发一个空 id 出去
  assert.deepEqual(W.normalizeMetas([{ name: "没 id" }, null, 3]), []);
  const [m] = W.normalizeMetas([
    { id: "a", name: "", node_count: "3", edge_count: null, updated_at: undefined },
  ]);
  assert.deepEqual(m, {
    id: "a",
    name: "未命名工作流",
    created_at: "",
    updated_at: "",
    node_count: 3,
    edge_count: 0,
  });
  // 数字 id 也要变成字符串：它会被拿去当 key 和 RPC 参数
  assert.equal(W.normalizeMetas([{ id: 5 }])[0].id, "5");
});

test("标题栏的名字带未保存标记", () => {
  assert.equal(W.wfTitle("甲", false), "甲");
  assert.equal(W.wfTitle("甲", true), "甲 ●");
  assert.equal(W.wfTitle("", true), "未命名工作流 ●");
});

// --------------------------------------------------------------------------- #
// 跨语言常量：前端认的错误码必须与后端一致
// --------------------------------------------------------------------------- #
test("「有未保存的改动」的错误码前后端一致", () => {
  // 对不上不会报错，只会让「新建 / 打开」在改过东西之后**莫名其妙点不动**：
  // 前端把 -32006 当普通失败弹个错，用户读到的是一句英文错误码。
  const py = readSync("../../rpc/controller.py");
  const m = /raise ControllerError\((-?\d+),\s*"unsaved_changes"/.exec(py);
  assert.ok(m, "后端不再抛 unsaved_changes 了，前端这段确认流程需要跟着改");
  const code = Number(m[1]);
  assert.equal(code, -32006, "错误码变了，检查 docs/modules/workflow-lib.md");

  for (const f of ["WorkflowMenu.vue", "WorkflowLibDialog.vue"]) {
    const src = readSrc(`../src/components/${f}`);
    assert.match(
      src,
      new RegExp(`UNSAVED_CHANGES\\s*=\\s*${code}\\b`),
      `${f} 里认的错误码和 rpc/controller.py 对不上`
    );
  }
});

function readSync(rel) {
  return readFileSync(new URL(rel, import.meta.url), "utf8");
}

// --------------------------------------------------------------------------- #
// 组件接线
// --------------------------------------------------------------------------- #
test("菜单不再直通文件对话框，改为「工作流库」入口", () => {
  const menu = readSrc("../src/components/WorkflowMenu.vue");
  for (const label of ["新建", "工作流库", "保存"]) {
    assert.ok(menu.includes(`>${label}<`) || menu.includes(`${label}</t-button>`), label);
  }
  // 「另存为」直通文件对话框是这次要换掉的东西；留着两套入口会让用户
  // 一半流程在库里、一半在任意路径上
  assert.ok(!menu.includes("另存为"), "菜单里还留着旧的「另存为」");
  assert.ok(!menu.includes("saveWorkflowAs"), "还在调已被替换的 saveWorkflowAs");
  // 未保存标记要真的渲染出来
  assert.ok(menu.includes("wfTitle"), "没有把未保存标记拼进标题");
  assert.ok(menu.includes("store.workflowDirty"), "没有读后端的 dirty");
  assert.ok(menu.includes("@/utils/confirm"), "丢弃改动的确认没有用应用内弹层");
});

test("库弹层把「查看 / 列表 / 更名 / 运行 / 编辑」都接上了", () => {
  const dlg = readSrc("../src/components/WorkflowLibDialog.vue");
  for (const label of ["运行", "打开", "更名", "导出", "删除"]) {
    assert.ok(dlg.includes(`>${label}</t-button>`) || dlg.includes(`${label}\n`), label);
  }
  // 每个动作都要落到 store 的对应动作上（少接一个就是「按钮点了没反应」）
  for (const fn of [
    "openWorkflowFromLib",
    "runWorkflowFromLib",
    "renameWorkflowInLib",
    "deleteWorkflowFromLib",
    "exportWorkflow",
    "importWorkflowFromFile",
  ]) {
    assert.ok(dlg.includes(`store.${fn}`), `没有接 ${fn}`);
  }
  // 列表副标题用纯逻辑模块拼，别在组件里手搓日期
  assert.ok(dlg.includes("wfSubtitle"), "副标题没有复用 flow/workflow.ts");
  // 改名输入框必须是函数式 ref：`ref="x"` 写在 v-for 里会被收集成数组，
  // 症状只是「点更名后光标没进输入框」，很难联想到 ref 的形态
  assert.ok(dlg.includes(":ref=\"setNameInput\""), "改名输入框的 ref 写法不对");
});

test("定时运行从库里选工作流，不再只能翻文件对话框", () => {
  // 工作流进了库之后，文件对话框会让用户去
  // ~/Library/Application Support/AutoFlow/workflows 里翻文件——那是这次
  // 改动**引入**的回归，必须一起修掉。
  const sched = readSrc("../src/components/ScheduleDialog.vue");
  assert.ok(sched.includes("store.workflowLib"), "定时面板没有读工作流库");
  assert.ok(sched.includes("libPath"), "没有把库里的 id 换算成路径");
  // 库里没有的路径（老配置）要补一项显示出来，否则下拉显示空白
  assert.ok(sched.includes("库外文件"), "库外路径没有兜底显示");
});
