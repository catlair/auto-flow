import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 时长（毫秒总数 ⇄ 时/分/秒/毫秒）与时刻（HH:mm:ss）的换算。
//
// 这两块是「参数面板上看得见、但算错了不报错」的典型：填 1 时 30 分却等成
// 30 分（把小时清掉了），或者旧流程里的 09:00 被选择器判成非法值清空——
// 界面一切正常，只有等过头/等到第二天才发现。
const duration = (() => {
  const relPath = "../src/flow/duration.ts";
  const src = readFileSync(new URL(relPath, import.meta.url), "utf8");
  const out = ts.transpileModule(src, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2021,
    },
  }).outputText;
  const mod = { exports: {} };
  const fn = runInThisContext(`(function(require, module, exports) {\n${out}\n})`, {
    filename: "flow-duration.cjs",
  });
  // duration.ts 刻意不依赖任何模块（它要被三个地方用：面板、摘要、测试）
  fn(() => {
    throw new Error("duration.ts 不应有运行时依赖");
  }, mod, mod.exports);
  return mod.exports;
})();

const { DUR_UNITS, TIME_FORMAT, TIME_DEFAULT, partOf, withPart, formatDuration, normalizeTime } =
  duration;

const U = Object.fromEntries(DUR_UNITS.map((u) => [u.key, u]));

// --------------------------------------------------------------------------- //
// 单位表的自洽性
// --------------------------------------------------------------------------- //

test("单位表：顺序固定为时→分→秒→毫秒，且相邻两格恰好是一个进位", () => {
  assert.deepEqual(
    DUR_UNITS.map((u) => u.key),
    ["h", "m", "s", "ms"]
  );
  assert.deepEqual(
    DUR_UNITS.map((u) => u.label),
    ["时", "分", "秒", "毫秒"]
  );
  // 「上一格的 div == 本格的 div × mod」——这条一旦破了就会出现
  // 「1 分显示成 0.6 分」这种没人预期得出来的换算。
  for (let i = 1; i < DUR_UNITS.length; i++) {
    assert.equal(DUR_UNITS[i - 1].div, DUR_UNITS[i].div * DUR_UNITS[i].mod, DUR_UNITS[i].key);
  }
  // 时没有进位上限（等 3 小时是合理需求）；其余三格必须有界，否则
  // 「90 分」会原样留在格里，和旁边显示的时/秒对不上。
  assert.equal(U.h.mod, 0);
  for (const k of ["m", "s", "ms"]) {
    assert.ok(DUR_UNITS.find((u) => u.key === k).max === DUR_UNITS.find((u) => u.key === k).mod - 1, k);
  }
});

// --------------------------------------------------------------------------- //
// 毫秒总数 → 四格
// --------------------------------------------------------------------------- //

test("分解：1 时 30 分 = 5400000ms，落在各自的格子里", () => {
  const t = 5400000;
  assert.equal(partOf(t, U.h), 1);
  assert.equal(partOf(t, U.m), 30);
  assert.equal(partOf(t, U.s), 0);
  assert.equal(partOf(t, U.ms), 0);
});

test("分解：1 时 1 分 1 秒 1 毫秒四格各是 1", () => {
  const t = 3661001;
  assert.deepEqual([partOf(t, U.h), partOf(t, U.m), partOf(t, U.s), partOf(t, U.ms)], [1, 1, 1, 1]);
});

test("分解：满一个进位就滚到上一格，不在本格显示 60 / 1000", () => {
  assert.equal(partOf(60000, U.s), 0); // 60 秒 = 1 分，秒格归零
  assert.equal(partOf(60000, U.m), 1);
  assert.equal(partOf(1500, U.ms), 500);
  assert.equal(partOf(1500, U.s), 1);
  // 反过来：本格里的值永远小于一个进位
  for (const t of [0, 999, 1000, 59999, 60000, 3599999, 3600000, 5400000]) {
    assert.ok(partOf(t, U.m) < 60, String(t));
    assert.ok(partOf(t, U.s) < 60, String(t));
    assert.ok(partOf(t, U.ms) < 1000, String(t));
  }
});

test("分解：四格能原样拼回毫秒总数（不丢不重）", () => {
  // 这条是上面「进位」那条的逆命题，也是最该守的不变式：面板显示的和
  // 实际等待的必须是同一个数。
  for (const t of [0, 1, 999, 1000, 61000, 59999, 60000, 3599999, 3600000, 5400000, 86399000]) {
    const back =
      partOf(t, U.h) * U.h.div +
      partOf(t, U.m) * U.m.div +
      partOf(t, U.s) * U.s.div +
      partOf(t, U.ms) * U.ms.div;
    assert.equal(back, t, `${t} 拼回来是 ${back}`);
  }
});

test("分解：脏值一律当 0，不抛错也不出现 NaN", () => {
  // 输入框可以被清空 / 被粘进非数字，NaN 一旦漏进 to_dict 会写进文件
  for (const bad of [undefined, null, "", "   ", "abc", NaN, -5, -0.5, {}]) {
    for (const u of DUR_UNITS) {
      assert.equal(partOf(bad, u), 0, `${JSON.stringify(bad)} ${u.key}`);
    }
  }
  assert.equal(partOf(90000.9, U.s), 30); // 小数向下取整，不四舍五入
});

// --------------------------------------------------------------------------- //
// 改一格 → 回填毫秒总数
// --------------------------------------------------------------------------- //

test("回填：只动被改的那一格，另外三格原样保留", () => {
  // 这是本模块存在的**主要理由**。若实现成「读四格 → 各自相加 → 写回」，
  // 用户改「分」时另外三格（还没失焦提交）会被当成 0——1 时 30 分改成 45 分，
  // 小时那格就被清零，等的时间少了一大截，而且不报错。
  assert.equal(withPart(5400000, U.m, 45), 6300000, "1时30分 → 1时45分（时不能丢）");
  assert.equal(withPart(5400000, U.h, 2), 9000000, "1时30分 → 2时30分（分不能丢）");
  assert.equal(withPart(5400000, U.s, 30), 5430000, "1时30分 → 1时30分30秒");
  assert.equal(withPart(5400000, U.ms, 250), 5400250, "1时30分 → 1时30分250毫秒");
});

test("回填：一格填超了自动进位，不是把总数算错", () => {
  assert.equal(withPart(1000, U.ms, 1500), 2500); // 1秒 + 1500毫秒 = 2秒500毫秒
  assert.equal(withPart(0, U.m, 90), 5400000); // 90 分 = 1时30分
});

test("回填：清空/负数/非法值当成 0，不会把总数搞成负数", () => {
  assert.equal(withPart(1000, U.s, -5), 0);
  assert.equal(withPart(5400000, U.m, ""), 3600000);
  assert.equal(withPart(NaN, U.ms, 100), 100);
  assert.equal(withPart(undefined, U.h, 1), 3600000);
  assert.ok(withPart(1, U.ms, "abc") >= 0);
});

test("回填：值只有一格时，四次改动能还原出一个精确的复合时长", () => {
  // 模拟用户依次填 1 时 → 30 分 → 0 秒 → 500 毫秒（每格都是「当前值改一格」）
  let v = 0;
  v = withPart(v, U.h, 1);
  v = withPart(v, U.m, 30);
  v = withPart(v, U.s, 0);
  v = withPart(v, U.ms, 500);
  assert.equal(v, 5400500);
  assert.deepEqual(
    [partOf(v, U.h), partOf(v, U.m), partOf(v, U.s), partOf(v, U.ms)],
    [1, 30, 0, 500]
  );
});

// --------------------------------------------------------------------------- //
// 卡片摘要
// --------------------------------------------------------------------------- //

test("摘要：只列非零单位，最多两个", () => {
  assert.equal(formatDuration(5400000), "1时30分");
  assert.equal(formatDuration(3600000), "1时");
  assert.equal(formatDuration(61000), "1分1秒");
  assert.equal(formatDuration(1000), "1秒");
  assert.equal(formatDuration(500), "500毫秒");
  assert.equal(formatDuration(1500), "1秒500毫秒");
  assert.equal(formatDuration(3661001), "1时1分"); // 默认只取前两个非零单位
  assert.equal(formatDuration(3661001, 4), "1时1分1秒1毫秒");
});

test("摘要：0 是合法值（「不等待」），显示成 0毫秒 而不是空", () => {
  // 空串会被 nodeSummary 当成「没值」跳过，卡片上就少了一段信息
  assert.equal(formatDuration(0), "0毫秒");
  assert.equal(formatDuration(""), "0毫秒");
  assert.equal(formatDuration(undefined), "0毫秒");
});

// --------------------------------------------------------------------------- //
// 时刻
// --------------------------------------------------------------------------- //

test("时刻：老文件里的 HH:mm 补齐成 HH:mm:ss", () => {
  // 改控件前时刻是手输的，存下来就是 "09:00"。TDesign 的 TimePicker 拿 format
  // 校验 value，"09:00" 对 HH:mm:ss 是非法值、会被静默重置——用户打开旧流程
  // 会发现时刻字段空了，然后等到零点。
  assert.equal(normalizeTime("09:00"), "09:00:00");
  assert.equal(normalizeTime("9:30"), "09:30:00");
  assert.equal(normalizeTime(" 10:30 "), "10:30:00");
  assert.equal(normalizeTime("09:00:00"), "09:00:00");
  assert.equal(normalizeTime("9:30:5"), "09:30:05");
  assert.equal(TIME_FORMAT, "HH:mm:ss");
});

test("时刻：空值/越界/垃圾回默认，绝不让非法值进选择器", () => {
  for (const bad of ["", "   ", "abc", "24:00", "09:60", "09:00:61", "9", "9:30:00:00"]) {
    assert.equal(normalizeTime(bad), TIME_DEFAULT, JSON.stringify(bad));
  }
  assert.equal(normalizeTime(undefined), TIME_DEFAULT);
  assert.equal(normalizeTime(null), TIME_DEFAULT);
});

test("时刻：往返稳定——把结果再洗一遍不变（否则选择器会反复重置）", () => {
  for (const t of ["09:00", "23:59:59", "00:00:00", "9:5", "12:34:56"]) {
    const once = normalizeTime(t);
    assert.equal(normalizeTime(once), once, t);
  }
});

// --------------------------------------------------------------------------- //
// 与组件的接线（纯逻辑测得到行为，接线只能看源码）
//
// 这几个 ptype 名字是**跨文件协议**：后端 ParamDef 声明、ParamsPanel 按它选控件。
// 名字对不上不会报错，只会静默退化成「一个手打毫秒的文本框」——正是这次要改掉的东西。
// --------------------------------------------------------------------------- //

const panelSrc = readFileSync(
  new URL("../src/components/ParamsPanel.vue", import.meta.url),
  "utf8"
);
const builtinPy = readFileSync(new URL("../../tasks/builtin.py", import.meta.url), "utf8");

test("delay 的 ptype 与参数面板的分支名对得上", () => {
  const block = /class DelayTask[\s\S]*?\n@register/.exec(builtinPy);
  assert.ok(block, "tasks/builtin.py 里找不到 DelayTask");
  // 时长接 duration、时刻接 time
  assert.match(block[0], /ParamDef\("ms",\s*"[^"]+",\s*"duration"/);
  assert.match(block[0], /ParamDef\("at",\s*"[^"]+",\s*"time"/);
  // key 与 mode 的取值都不许动：它们已经写进了用户保存的流程文件
  assert.match(block[0], /ParamDef\("ms"/);
  assert.match(block[0], /ParamDef\("at"/);
  assert.match(block[0], /\["延时毫秒", "等待至时刻"\]/);
});

test("参数面板确实渲染了这两种控件", () => {
  assert.ok(panelSrc.includes("p.ptype === 'duration'"), "缺 duration 分支");
  assert.ok(panelSrc.includes("p.ptype === 'time'"), "缺 time 分支");
  // 四格由 DUR_UNITS 驱动，不在模板里另抄一份单位表
  assert.ok(panelSrc.includes('v-for="u in DUR_UNITS"'), "四格没有走 DUR_UNITS");
  assert.ok(panelSrc.includes('from "@/flow/duration"'), "换算没有复用 duration.ts");
  assert.ok(panelSrc.includes("withPart("), "回填没有用 withPart");
});

test("时刻是「选」不是「打」，且不能被清空", () => {
  assert.ok(panelSrc.includes("<t-time-picker"), "缺时刻选择器");
  assert.ok(panelSrc.includes(':allow-input="false"'), "允许手打就失去意义了");
  // clearable 默认 true：清空会 onChange(null)，一路写进 params.at，后端
  // _seconds_until(None) 抛 ValueError（进 run.error，节点直接失败）
  assert.ok(panelSrc.includes(':clearable="false"'), "清空会写 null 进后端");
});

test("四格各有自己的上限，避免「90 分」原样留着", () => {
  for (const u of DUR_UNITS) {
    assert.ok(Number.isFinite(u.max) && u.max > 0, u.key);
  }
  assert.ok(panelSrc.includes(":max=\"u.max\""), "四格没有接上各自的上限");
});
