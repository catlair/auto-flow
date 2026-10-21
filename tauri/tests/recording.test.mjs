import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 录制库在界面上的纯逻辑（flow/recording.ts）。
//
// 这一块全是「界面不报错、但用户会读错」的东西：
// - `source` 的取值是**跨语言协议**，对不上会让「选了录制库」静默退回内嵌；
// - 两条同名录制在下拉里分不出来 → 选错要靠猜；
// - 录制被删掉后 t-select 显示成空白，看着像「没填」，实际节点上还挂着那个 id；
// - 时长写成 `90000ms`。
const duration = (() => {
  const src = readFileSync(new URL("../src/flow/duration.ts", import.meta.url), "utf8");
  const out = ts.transpileModule(src, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021 },
  }).outputText;
  const mod = { exports: {} };
  runInThisContext(`(function(require, module, exports) {\n${out}\n})`, {
    filename: "flow-duration.cjs",
  })(() => {
    throw new Error("duration.ts 不应有运行时依赖");
  }, mod, mod.exports);
  return mod.exports;
})();

const rec = (() => {
  const src = readFileSync(new URL("../src/flow/recording.ts", import.meta.url), "utf8");
  const out = ts.transpileModule(src, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021 },
  }).outputText;
  const mod = { exports: {} };
  const deps = { "./duration": duration };
  runInThisContext(`(function(require, module, exports) {\n${out}\n})`, {
    filename: "flow-recording.cjs",
  })((name) => {
    if (name in deps) return deps[name];
    throw new Error(`recording.ts 出现了未声明的依赖 ${name}`);
  }, mod, mod.exports);
  return mod.exports;
})();

const { INLINE_SOURCE, LIB_SOURCE, isLibSource, fmtRecDuration, recOptionLabel, recOptions, recDisplayName } = rec;

const meta = (over = {}) => ({
  id: "20260915-190433-a1b2c3",
  name: "登录群聊",
  created_at: "2026-09-15 19:04:33",
  count: 42,
  duration_ms: 90000,
  origin: [0, 0],
  ...over,
});

// --------------------------------------------------------------------------- //
// source 的取值是跨语言协议
//
// 后端 `tasks/builtin.py` 的 ParamDef("source", …, ["内嵌", "录制库"]) 与
// RecordReplayTask.run() 里的字面量必须一模一样。任何一边改字面量而另一边没跟上，
// 表现是「选了录制库」**静默退回内嵌**（于是回放一个空序列）——不报错。
// 所以这里直接读后端源码来比，而不是把字面量在测试里再抄一遍。
// --------------------------------------------------------------------------- //
const builtinPy = readFileSync(new URL("../../tasks/builtin.py", import.meta.url), "utf8");

test("source 取值与后端 ParamDef 的 options 逐字对齐", () => {
  const block = /class RecordReplayTask[\s\S]*?\n@register/.exec(builtinPy);
  assert.ok(block, "tasks/builtin.py 里找不到 RecordReplayTask");
  const opts = /ParamDef\("source",\s*"[^"]*",\s*"select",\s*"[^"]*",\s*\[([^\]]*)\]/.exec(block[0]);
  assert.ok(opts, "source 的 ParamDef 形状变了");
  const list = opts[1].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean);
  assert.deepEqual(list, [INLINE_SOURCE, LIB_SOURCE], "options 与前端常量不一致");

  // run() 里的比较字面量也要对得上（同模块另一处断言，见 tests/test_recording_lib.py）
  assert.ok(block[0].includes(`"${LIB_SOURCE}"`), "run() 里没用到录制库这个取值");
  // 节点按 id 引用：run() 必须真的去库里取，而不是只看 source
  assert.match(block[0], /recording_id/);
});

test("缺 source（老流程文件）按内嵌算", () => {
  // 老节点 params 里根本没有 source 这个 key。判成「录制库」会让它们去查一个
  // 空 id、取不到就抛错——所有历史流程一次性全废。
  assert.equal(isLibSource(undefined), false);
  assert.equal(isLibSource(null), false);
  assert.equal(isLibSource({}), false);
  assert.equal(isLibSource({ source: INLINE_SOURCE }), false);
  assert.equal(isLibSource({ source: LIB_SOURCE }), true);
});

// --------------------------------------------------------------------------- //
// 时长
// --------------------------------------------------------------------------- //

test("时长复用 duration.ts 的那份实现", () => {
  assert.equal(fmtRecDuration(90000), "1分30秒");
  assert.equal(fmtRecDuration(0), "0毫秒");
  assert.equal(fmtRecDuration(5400000), "1时30分");
});

// --------------------------------------------------------------------------- //
// 下拉选项
// --------------------------------------------------------------------------- //

test("选项文案带条数——同名录制才能被区分开", () => {
  assert.equal(recOptionLabel(meta()), "登录群聊（42 条）");
  // 同一秒录了两段 / 复制过库目录 → 名字一模一样，只靠名字没法选
  const a = recOptionLabel(meta({ name: "录制 2026-09-15 19:04:33", count: 3 }));
  const b = recOptionLabel(meta({ name: "录制 2026-09-15 19:04:33", count: 99 }));
  assert.notEqual(a, b);
  assert.equal(recOptionLabel(meta({ name: "  " })), "(未命名)（42 条）");
  assert.equal(recOptionLabel(null), "(未命名)（0 条）");
  // count 缺失 / 是垃圾时不能显示成 NaN
  assert.equal(recOptionLabel({ name: "x" }), "x（0 条）");
  assert.equal(recOptionLabel({ name: "x", count: "abc" }), "x（0 条）");
});

test("选项里带上当前值的兜底项（录制被删后不能显示成空白）", () => {
  const items = [meta(), meta({ id: "b2", name: "另一段", count: 1 })];
  const opts = recOptions(items, "20260915-190433-a1b2c3");
  assert.deepEqual(opts.map((o) => o.value), ["20260915-190433-a1b2c3", "b2"]);
  assert.ok(opts.every((o) => !o.missing));

  // 值已不在库里：必须补一项并标出来，否则 t-select 显示空白，
  // 看起来就是「这个字段没填」——用户于是重选一条，或压根没发现
  // 这里引着一个不存在的录制（回放才报错）。
  const gone = recOptions(items, "deleted-id");
  assert.equal(gone.length, 3);
  const last = gone[gone.length - 1];
  assert.equal(last.value, "deleted-id");
  assert.equal(last.missing, true);
  assert.ok(last.label.includes("已不在录制库"), last.label);
});

test("空值 / 空库 / 脏输入都不产生多余选项", () => {
  assert.deepEqual(recOptions([], ""), []);
  assert.deepEqual(recOptions(null, ""), []);
  assert.deepEqual(recOptions(undefined, undefined), []);
  assert.deepEqual(recOptions([], "   "), [], "只有空格的值不算已选");
  // 库为空但有选中值 → 只有兜底那一项
  const only = recOptions([], "x1");
  assert.equal(only.length, 1);
  assert.equal(only[0].missing, true);
  // 库里的条目不能因为 id 是数字就丢（后端一定是字符串，但手工改过的文件未必）
  const num = recOptions([meta({ id: 7 })], "7");
  assert.equal(num.length, 1, "数字 id 应被判为「已在库里」，不该再补兜底项");
});

// --------------------------------------------------------------------------- //
// 画布卡片
// --------------------------------------------------------------------------- //

test("卡片用后端捎来的 recording_name，拿不到就留空", () => {
  assert.equal(recDisplayName({ recording_name: "登录群聊" }), "登录群聊");
  assert.equal(recDisplayName({ recording_name: "  登录群聊  " }), "登录群聊");
  // 拿不到名字时**返回空串**（由 summary 跳过这一段），而不是退回显示 id：
  // 一串 20260915-190433-a1b2c3 比不显示更糟——用户会以为自己认得出来。
  assert.equal(recDisplayName({ recording_id: "20260915-190433-a1b2c3" }), "");
  assert.equal(recDisplayName({ recording_name: "   " }), "");
  assert.equal(recDisplayName(null), "");
  assert.equal(recDisplayName(undefined), "");
});

// --------------------------------------------------------------------------- //
// 与组件 / store 的接线（行为测得到，接线只能看源码）
// --------------------------------------------------------------------------- //

const panelSrc = readFileSync(new URL("../src/components/ParamsPanel.vue", import.meta.url), "utf8");
const recordSrc = readFileSync(new URL("../src/components/RecordPanel.vue", import.meta.url), "utf8");
const storeSrc = readFileSync(new URL("../src/stores/app.ts", import.meta.url), "utf8");
const typesSrc = readFileSync(new URL("../src/rpc/types.ts", import.meta.url), "utf8");

test("types.ts 声明了 recording ptype（否则后端声明了前端也不认）", () => {
  assert.match(typesSrc, /\|\s*"recording"/);
});

test("参数面板渲染了录制下拉，且选项来自 recOptions", () => {
  assert.ok(panelSrc.includes("p.ptype === 'recording'"), "缺 recording 分支");
  assert.ok(panelSrc.includes("recOptions("), "下拉选项没有走 recOptions（兜底项就没了）");
  assert.ok(panelSrc.includes('from "@/flow/recording"'), "没有复用 flow/recording.ts");
});

test("录制面板提供了库的入口，store 提供了对应动作", () => {
  for (const call of [
    "recordingLibSave",
    "recordingLibRename",
    "recordingLibDelete",
    "recordingLoadFromLib",
    "recordingLibToNode",
    "refreshRecordingLib",
  ]) {
    assert.ok(storeSrc.includes(call), `store 缺 ${call}`);
  }
  assert.ok(recordSrc.includes("录制库"), "录制面板没有库的区块");
  assert.ok(recordSrc.includes("onLibToNode"), "缺少「落到画布」");
  // 删除必须走**应用内**确认框，且**必须 await**。
  // 曾经的写法 `if (!window.confirm(...)) return;` 是个静默失效的空壳：
  // Tauri 把 `window.confirm` 覆盖成 async（返回 Promise），`!Promise` 恒为 false，
  // 那个 `return` 永远不执行；而本项目又没授予 `dialog:allow-confirm`，
  // invoke 直接被权限拒绝 ⇒ **连框都不弹**，点「删除」直接把文件永久删掉。
  assert.ok(recordSrc.includes("confirmDanger"), "删除没有走应用内确认框");
  assert.match(
    recordSrc,
    /await\s+confirmDanger\(/,
    "确认框没有 await——不 await 就等于没拦（Promise 恒真）"
  );
  // 必须把「有几个节点在引用它」算出来并写进文案：库里删掉不影响节点里的 id，
  // 那些节点回放时会报错取不到录制——用户得先知道。
  assert.match(
    recordSrc,
    /used\s*\?\s*`[^`]*\$\{used\}/,
    "删除提示没有带上「有几个节点在引用它」"
  );
  // 回归：**代码**里不能再出现 `window.confirm`（注释里解释原因是可以的，
  // 所以按行首判断，只扫非注释行）。
  const codeLines = recordSrc
    .split("\n")
    .filter((l) => !l.trim().startsWith("//") && !l.trim().startsWith("*"));
  assert.ok(
    !codeLines.some((l) => l.includes("window.confirm")),
    "代码里又用回了 window.confirm：它在 Tauri 下是 async 且无 allow-confirm 权限，拦不住任何东西"
  );
});

test("store 在录制停止后重拉库列表（后端是自动入库的）", () => {
  // record.stop 的响应里带 library；不重拉的话用户得切走再切回来才看得见刚录的那条
  assert.ok(/refreshingRecordingLib|refreshRecordingLib/.test(storeSrc));
  assert.match(storeSrc, /r\?\.library/, "停止录制后没有据 library 刷新库列表");
});

test("store 的 _applyRecord 认得出「未入库」（lib 字段为 null）", () => {
  // 用 `in` 而不是 `??`：后端故意用 lib:null 表示「当前这段还没入库」，
  // 当成字段缺失忽略会让界面一直显示上一次的「已同步」。
  assert.match(storeSrc, /"lib" in r/);
});

// --------------------------------------------------------------------------- //
// 单条事件的编辑：字段白名单是**跨语言协议**
//
// 界面把字段名当字符串发给后端（`record.patch` 的 `fields`），后端拿
// `_PATCH_FIELDS` 逐字段校验。两边一旦错开，用户看到的是「编辑失败：…」——
// 而且是在**改完之后**才发现，草稿已经填好了。所以这里直接读后端源码来比。
// --------------------------------------------------------------------------- //
const controllerPy = readFileSync(new URL("../../rpc/controller.py", import.meta.url), "utf8");
const serverPy = readFileSync(new URL("../../rpc/server.py", import.meta.url), "utf8");

/** 从 rpc/controller.py 抽出 `_PATCH_FIELDS`：{kind: [字段…]}。 */
const patchFields = (() => {
  const m = /_PATCH_FIELDS = \{([\s\S]*?)\n    \}/.exec(controllerPy);
  assert.ok(m, "rpc/controller.py 里找不到 _PATCH_FIELDS");
  const out = {};
  for (const line of m[1].split("\n")) {
    const kv = /"(\w+)":\s*\(([^)]*)\)/.exec(line);
    if (!kv) continue;
    out[kv[1]] = kv[2]
      .split(",")
      .map((s) => s.trim().replace(/^"|"$/g, ""))
      .filter(Boolean);
  }
  assert.ok(Object.keys(out).length, "_PATCH_FIELDS 解析出来是空的（形状变了？）");
  return out;
})();

/** 模板里 `class="af-fields"` 那一块（字段编辑区）。 */
const fieldsBlock = (() => {
  const m = /class="af-fields">([\s\S]*?)\n    <\/div>/.exec(recordSrc);
  assert.ok(m, "RecordPanel.vue 里找不到 .af-fields 字段编辑区");
  return m[1];
})();

/** 某段模板里绑了哪些 `fieldDraft.<名>`。 */
const draftFields = (body) =>
  [...body.matchAll(/fieldDraft\.(\w+)/g)].map((m) => m[1]);

test("字段编辑区暴露的字段，逐类型落在后端白名单里", () => {
  // 按 `<template v-…="selectedEvent.kind === 'x'">` 切块；split 带捕获组会把
  // 类型名一起返回，于是 parts 是 [公共段, kind1, body1, kind2, body2, …]。
  const parts = fieldsBlock.split(/<template v-(?:if|else-if)="selectedEvent\.kind === '(\w+)'">/);
  const common = draftFields(parts[0]);

  // 公共段（时间 / X / Y）对**每种**类型都可用 —— 白名单里少一种就会有一类
  // 事件改不了坐标，而录制回放最常要修的就是坐标。
  for (const [kind, fields] of Object.entries(patchFields)) {
    for (const f of common) {
      assert.ok(fields.includes(f), `公共字段 ${f} 不在 ${kind} 的白名单里`);
    }
  }

  const seen = [];
  for (let i = 1; i < parts.length; i += 2) {
    const kind = parts[i];
    const body = parts[i + 1];
    seen.push(kind);
    assert.ok(kind in patchFields, `界面有 ${kind} 的字段区，后端白名单里却没有这一类`);
    for (const f of draftFields(body)) {
      assert.ok(
        patchFields[kind].includes(f),
        `${kind} 的字段区绑了 ${f}，但后端白名单 ${kind} 里没有——改完只会收到「编辑失败」`
      );
    }
  }
  // 反向：后端认得的每一种都该能在界面上改（否则那一类事件只能重录）
  assert.deepEqual(
    seen.sort(),
    Object.keys(patchFields).sort(),
    "界面的字段区和后端白名单的类型对不上"
  );
});

test("「插入」下拉的类型与后端白名单一致", () => {
  const sel = /<select v-model="insertKind"[\s\S]*?<\/select>/.exec(recordSrc);
  assert.ok(sel, "找不到插入类型下拉");
  const kinds = [...sel[0].matchAll(/<option value="(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(
    kinds.sort(),
    Object.keys(patchFields).sort(),
    "插入下拉的取值与 _PATCH_FIELDS 的 key 不一致"
  );
});

test("字段编辑走「草稿 + change 提交」，不是逐键发 RPC", () => {
  // 逐键提交会把 "640" 拆成 6 / 64 / 640 三次写入，各占一格撤销——
  // 用户按一次撤销只退掉最后一位数字。
  assert.ok(recordSrc.includes("fieldDraft"), "没有草稿对象");
  // 逐个控件核对，而不是数「@change 出现了几次」——后者是个魔数阈值：
  // 少给一个控件挂提交，总数照样过线，那个字段就**改不动**（草稿填了没处发）。
  const controls = [
    ...fieldsBlock.matchAll(/<input\b[\s\S]*?\/>/g),
    ...fieldsBlock.matchAll(/<select\b[\s\S]*?<\/select>/g),
  ].map((m) => m[0]);
  assert.ok(controls.length >= 12, `字段区只找到 ${controls.length} 个控件，正则可能失配`);
  const silent = [];
  for (const c of controls) {
    const bound = [...c.matchAll(/fieldDraft\.(\w+)/g)].map((m) => m[1]);
    if (!bound.length) continue;
    assert.ok(
      !c.includes("@input"),
      `${bound.join("/")} 用了 @input——每敲一个字符都会发一次 RPC`
    );
    const onChange = c.includes('@change="commitFields"');
    // 文本输入不能只挂 @change（要失焦才触发），所以允许「回车 + 失焦」这一对
    const onBlurEnter =
      c.includes('@blur="commitFields"') && c.includes('@keydown.enter.prevent="commitFields"');
    if (!onChange && !onBlurEnter) silent.push(bound.join("/"));
  }
  assert.deepEqual(silent, [], `这些字段的控件没有提交入口，草稿填了发不出去：${silent.join(", ")}`);
  // 类型不可改：改 kind 是「换一条事件」，不是改字段
  assert.match(recordSrc, /if \(k === "kind"\) continue/);
  // 只提交变了的字段（后端还会再判一次「没变就不占撤销栈」）
  assert.match(recordSrc, /if \(v !== ev\[k\]\) fields\[k\] = v/);
});

test("上移 / 下移传的是「相对位移」，不是绝对下标", () => {
  // 回放排期是绝对时间线（core/player.py 的 due = t0 + ts_ms），与数组顺序无关。
  // 所以后端交换位置时必须**连 ts_ms 一起换**；前端只该说「往前挪一格」。
  assert.match(recordSrc, /record\.moveEvent", \{ index: idx, delta \}/);
  assert.match(controllerPy, /evs\[idx\], evs\[j\] = replace\(b, ts_ms=a\.ts_ms\), replace\(a, ts_ms=b\.ts_ms\)/);
  // 选中跟着走，方便连按
  assert.match(recordSrc, /selected\.value = idx \+ delta/);
});

test("库里每条都能「直接编辑」，编辑模式有常驻提示条", () => {
  // ⚠️ 只断言 `includes("onLibEdit")` 是不够的——**函数定义本身就满足它**。
  // 2026-09-17 就是栽在这：处理函数写好了，模板里一个 @click 都没挂，
  // 界面**点不到**这个入口，而所有测试全绿（真机目视才发现）。
  // 所以这里必须断言**模板里挂上了**（`@click="onLibEdit(m)"`）。
  assert.match(recordSrc, /@click="onLibEdit\(m\)"/, "「直接编辑」按钮没挂到模板上");
  assert.match(recordSrc, /@click="onLibEndEdit"/, "「完成编辑」按钮没挂到模板上");
  assert.match(recordSrc, /v-if="libCurrent\?\.editing"/, "没有编辑模式提示条");
  // 提示条必须说清「改动会立刻写回」——否则用户分不出它和「载入」的区别，
  // 以为还要再点一次「更新到库」。
  assert.ok(recordSrc.includes("立刻写回"), "提示条没说清写回语义");
  // editing 标志由后端 _lib_info() 给，前端只读
  assert.match(controllerPy, /"editing":\s*self\._record_lib_edit_id == s\["id"\]/);
  // 且**必须在构造响应之前**把写回目标设好，否则响应里 editing 恒为 False
  // （见 tests/test_core.py::test_lib_edit_response_reports_editing_so_the_banner_shows）
  assert.match(
    controllerPy,
    /self\._record_lib_edit_id = rid\n\s+payload\["lib"\] = self\._lib_info\(\)/,
    "record_lib_edit 没在构造响应前设好写回目标——提示条不会出现"
  );
  assert.match(storeSrc, /editing\?: boolean/);
});

test("store 的编辑动作名与后端注册的方法名逐个对得上", () => {
  for (const [action, method] of [
    ["recordingLibEdit", "record.lib.edit"],
    ["recordingLibEndEdit", "record.lib.endEdit"],
  ]) {
    assert.ok(storeSrc.includes(action), `store 缺 ${action}`);
    assert.ok(storeSrc.includes(`rpc.request("${method}"`), `${action} 没有调 ${method}`);
    assert.ok(serverPy.includes(`"${method}"`), `后端没有注册 ${method}`);
  }
  // 编辑动作走 recordEdit（后端返回完整权威序列，前端不自己算）
  for (const m of ["record.patch", "record.insert", "record.duplicate", "record.moveEvent"]) {
    assert.ok(serverPy.includes(`"${m}"`), `后端没有注册 ${m}`);
  }
  assert.match(storeSrc, /async recordEdit\(method: string/);
});

test("store 里每一个 record.* 方法名都在后端注册过", () => {
  // 方法名是字符串，写错了 TS 不会报，只在运行时才炸。
  const used = [...storeSrc.matchAll(/rpc\.request\(\s*"(record\.[\w.]+)"/g)].map((m) => m[1]);
  assert.ok(used.length >= 10, `只扫到 ${used.length} 个 record.* 调用，正则可能失配`);
  const missing = [...new Set(used)].filter((m) => !serverPy.includes(`"${m}"`));
  assert.deepEqual(missing, [], `store 用了后端没注册的方法：${missing.join(", ")}`);
});
