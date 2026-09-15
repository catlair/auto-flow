import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 侧栏状态（宽度钳制 / 存取 / 拖拽 / 选节点后跳页）是纯逻辑，在这里直跑。
// 另加两条**源文件结构断言**：它们盯的是「界面不报错、但用起来不对」的坑
// ——切页把面板销毁掉、宽度边界在组件里又抄了一份。这类问题截图才看得见。
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
  fn((name) => require(name), mod, mod.exports);
  return mod.exports;
}

const side = loadTs("../src/flow/sidebar.ts");
const appSrc = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

test("宽度钳制：越界被拉回范围内，空值/非数字回默认宽度", () => {
  assert.equal(side.clampWidth(300), 300);
  assert.equal(side.clampWidth(side.MIN_W - 1), side.MIN_W);
  assert.equal(side.clampWidth(side.MAX_W + 100), side.MAX_W);
  // 字符串数字要当数字用（localStorage 里可能被别处写成字符串）
  assert.equal(side.clampWidth("420"), 420);
  // 0 是「显式给了个太小的值」→ 钳到下限
  assert.equal(side.clampWidth(0), side.MIN_W);
  for (const bad of [undefined, "abc", NaN, {}]) {
    assert.equal(side.clampWidth(bad), side.DEFAULT_W);
  }
});

test("空值不能被 Number() 的隐式转型带到下限上去", () => {
  // Number("") 与 Number(null) 都是 0。不单独挡掉的话，「没设过」会变成
  // 「拖到最窄」——用户重开一次应用，侧栏莫名其妙变到最窄。这条就是钉住这个。
  assert.equal(side.clampWidth(""), side.DEFAULT_W);
  assert.equal(side.clampWidth("   "), side.DEFAULT_W);
  assert.equal(side.clampWidth(null), side.DEFAULT_W);
  assert.equal(side.parseSide('{"w":null}').w, side.DEFAULT_W);
  assert.equal(side.parseSide('{"w":""}').w, side.DEFAULT_W);
});

test("读状态：解析失败/形状不对一律回默认值，不抛异常", () => {
  // 这是**启动路径**上的代码：抛出等于整个界面起不来，所以只允许回默认值
  for (const raw of [null, "", "{oops", "123", "null", '"str"']) {
    assert.deepEqual(side.parseSide(raw), side.DEFAULT_SIDE);
  }
  assert.deepEqual(side.parseSide("{}"), side.DEFAULT_SIDE);
});

test("open 只在明确存了 false 时才收起（字段缺失按展开算）", () => {
  assert.equal(side.parseSide('{"w":300}').open, true);
  assert.equal(side.parseSide('{"w":300,"open":false}').open, false);
  assert.equal(side.parseSide('{"w":300,"open":"no"}').open, true);
});

test("存过的标签页如今已不存在 → 落回「参数」，而不是显示一个空面板", () => {
  assert.equal(side.parseSide('{"tab":"nope"}').tab, "params");
  assert.equal(side.parseSide('{"tab":null}').tab, "params");
  assert.equal(side.parseSide('{"tab":"record"}').tab, "record");
});

test("往返：写进去再读回来一致；宽度写入时就已经钳好", () => {
  const saved = side.serializeSide({ w: 480, open: false, tab: "record" });
  assert.deepEqual(side.parseSide(saved), { w: 480, open: false, tab: "record" });
  // 越界值不能以越界的样子落盘，否则下次开机会先按离谱宽度渲染一帧
  const wide = JSON.parse(side.serializeSide({ w: 9999, open: true, tab: "params" }));
  assert.equal(wide.w, side.MAX_W);
});

test("拖侧栏左边缘：向左拖变宽、向右拖变窄、拖到头就钳住", () => {
  assert.equal(side.widthFromDrag(340, 500, 460), 380); // 向左 40px
  assert.equal(side.widthFromDrag(340, 500, 540), 300); // 向右 40px
  assert.equal(side.widthFromDrag(340, 500, 0), side.MAX_W); // 一路拖到最左
  assert.equal(side.widthFromDrag(340, 500, 9999), side.MIN_W); // 一路拖到最右
});

test("选中节点后：侧栏开着才切到「参数」，收起时不强切", () => {
  assert.equal(side.tabAfterSelect("run", true), "params");
  assert.equal(side.tabAfterSelect("params", true), "params");
  // 收起时保持原页：展开侧栏是用户自己的动作，不该顺手把页签也换掉
  assert.equal(side.tabAfterSelect("record", false), "record");
});

test("页签清单只有一份（sidebar.ts），组件按它渲染", () => {
  assert.deepEqual(
    side.TABS.map((t) => t.key),
    ["params", "run", "record"]
  );
  assert.equal(side.TABS.length, 3);
  assert.ok(/v-for="t in TABS"/.test(appSrc));
});

test("三个页签正文用 v-show 保持挂载，不是 v-if", () => {
  // 录制面板的 textDraft / keysToTextDraft / selected / scrollTop 都是组件内状态
  // （见 RecordPanel.vue）。v-if 会在切页时销毁组件：切出去看一眼再切回来，
  // 填了一半的草稿就没了——而界面不会报任何错。
  const at = appSrc.indexOf('class="af-side-body"');
  assert.ok(at > 0, "找不到标签页正文容器");
  const block = appSrc.slice(at, appSrc.indexOf("</aside>", at));
  assert.equal((block.match(/v-show="tab === '/g) || []).length, 3, "三个页签都要用 v-show");
  assert.ok(!/v-if=/.test(block), "页签正文里出现了 v-if：切页会销毁组件、丢掉组件内状态");
});

test("宽度边界与存取只在 sidebar.ts 里有一份，App.vue 委托给它", () => {
  assert.ok(/widthFromDrag\(startW, startX, ev\.clientX\)/.test(appSrc), "拖拽没有走模块");
  assert.ok(/parseSide\(localStorage\.getItem\(LS_KEY\)\)/.test(appSrc), "读取没有走模块");
  // 旧的组件内实现在这里留残迹就是「两份边界」，改了模块忘了组件会表现为
  // 「拖到不能拖的宽度」——所以钉住它不许回来
  assert.ok(!/Number\(raw\.w\)/.test(appSrc), "App.vue 里还留着旧的读状态实现");
});
