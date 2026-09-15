import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInThisContext } from "node:vm";
import ts from "typescript";

// 画布相关的两个「测试全绿也发现不了」的坑，在这里钉住：
//   1. 首次适应视野的时机（fitOnce）——纯状态机，直接跑真实模块。
//   2. 手柄锚点 / 出口标签的 CSS 不变量——没有布局引擎可跑，退而求其次，
//      对源文件做结构断言。两个坑都是「画布不报错、节点都在，但用户看到的东西
//      不对」，靠截图才发现，所以值得用测试兜住回归。
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

const { createFitOnce } = loadModule("../src/flow/fitOnce.ts");

/** 造一个记账用的 fit 替身，返回它被调用的次数。 */
function spy() {
  const calls = { n: 0 };
  calls.fit = () => {
    calls.n += 1;
  };
  return calls;
}

// ---------- 适应视野的时机 ----------

test("只 request 不 commit 时绝不 fit（尺寸还没量，调了也是白调）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  // 这就是那个 bug：在「数据到位」这一步直接 fitView。节点此刻尺寸为 0，
  // 包围盒退化，fitView 等于没调，而且意图被消费掉，量完之后不会再补。
  assert.equal(s.n, 0, "request 阶段就调了 fitView —— 回归到旧 bug 了");
  assert.equal(f.isFitted(), false);
});

test("request 之后 commit 才 fit，且只 fit 一次", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  assert.equal(s.n, 1);
  assert.equal(f.isFitted(), true);
});

test("commit 可被重复触发（nodes-initialized 会多发），但只生效一次", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  f.commit();
  f.commit();
  assert.equal(s.n, 1, "重复 commit 把视野反复拉走了");
});

test("没有待办时 commit 空转（画布刚挂载就会收到一次 nodes-initialized）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.commit();
  assert.equal(s.n, 0);
  assert.equal(f.isFitted(), false);
});

test("空工作流不申请适应（否则用户打开空白画布也会被 fit 一次）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(false);
  f.commit();
  assert.equal(s.n, 0);
});

test("适应过之后再 request 不会再次 fit（每次编辑都把视野拉走是不能接受的）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  // 用户手动拖了视野，然后加了几个节点
  f.request(true);
  f.request(true);
  f.commit();
  assert.equal(s.n, 1, "编辑节点时视野被重新适应了");
});

test("清空工作流后下一份能重新适应", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.request(true);
  f.commit();
  f.reset();
  assert.equal(f.isFitted(), false);
  f.request(true);
  f.commit();
  assert.equal(s.n, 2, "换了一份工作流却没重新适应视野");
});

test("reset 之后空工作流仍然不申请（清空动作本身不该触发 fit）", () => {
  const s = spy();
  const f = createFitOnce(s.fit);
  f.reset();
  f.request(false);
  f.commit();
  assert.equal(s.n, 0);
});

test("完整的真实时序：挂载 → 尺寸量完 → 编辑 → 换工作流", () => {
  const s = spy();
  const f = createFitOnce(s.fit);

  // 画布挂载，此时 store 还是空的，Vue Flow 先发了一次 nodes-initialized
  f.request(false);
  f.commit();
  assert.equal(s.n, 0, "空白启动时不该 fit");

  // 握手拿到已有工作流
  f.request(true);
  f.commit();
  assert.equal(s.n, 1);

  // 用户拖动节点：nodes-initialized 可能再发一次
  f.commit();
  assert.equal(s.n, 1, "拖动节点把视野拉回去了");

  // 用户点了「新建」→ 空 → 加载另一份
  f.reset();
  f.request(false);
  f.commit();
  assert.equal(s.n, 1);
  f.request(true);
  f.commit();
  assert.equal(s.n, 2);
});

// ---------- 画布 CSS 不变量 ----------

const canvasSrc = readFileSync(
  new URL("../src/components/FlowCanvas.vue", import.meta.url),
  "utf8"
);

/**
 * 取出某个选择器的规则体（`[^}]*` 足够——这些规则里没有嵌套花括号）。
 *
 * **必须先去注释**：这些规则里带着解释性注释，注释里会引用 `position: relative`
 * 之类的字样（正是在讲「不要这么写」），不去掉的话断言会命中注释里的反例而误报。
 */
function ruleBody(src, selector) {
  const m = new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`).exec(src);
  assert.ok(m, `找不到选择器 ${selector} 的规则`);
  return m[1].replace(/\/\*[\s\S]*?\*\//g, "");
}

test("出口行必须是定位元素，否则每个出口的手柄都塌到卡片中线上", () => {
  const body = ruleBody(canvasSrc, ".af-exit");
  // Vue Flow 的 .vue-flow__handle-right 是 `top:50%; right:0`，而 `top:50%` 相对
  // **最近的定位祖先**解析。`.af-exit` 不是定位元素时锚点会一路落到 `.af-gnode`
  // 上，于是**每个出口的手柄都跑到卡片的中线上、几个完全重合**——表现是四个出口
  // 只有一个圆点，而且 getClosestHandle 永远只命中第一个，
  // **根本连不出「情形 2/3」**（2026-09-15 实测踩到，且当时被误判成「已修好」）。
  assert.match(
    body,
    /position\s*:\s*relative/,
    ".af-exit 不是定位元素，出口手柄会全部塌到卡片中线上"
  );
  // 同时要给手柄让位，别压住标签最后一个字：手柄中心落在行右边缘上，半径约 4.5px。
  assert.match(body, /padding-right\s*:\s*\d/, ".af-exit 没有为手柄留出右侧间距");
});

test("出口行要延伸到卡片内沿，多出口手柄才和单出口的在同一竖线上", () => {
  const body = ruleBody(canvasSrc, ".af-exits");
  // 卡片左右内边距是 10px。不加负外边距的话，出口行的右边缘会内缩 10px，
  // 多出口的手柄就比单出口的靠里 10px——同一种「出口」画在两处。
  assert.match(
    body,
    /margin\s*:\s*[^;]*-10px/,
    ".af-exits 没有用负外边距抵消卡片内边距，多出口手柄会比单出口的缩进 10px"
  );
});

test("节点卡片必须是定位元素，手柄才锚在卡片右边缘", () => {
  const body = ruleBody(canvasSrc, ".af-gnode");
  assert.match(
    body,
    /position\s*:\s*relative/,
    ".af-gnode 不是定位元素，手柄会锚到 .vue-flow__node 上，单/多出口的竖线对不齐"
  );
});

test("边投影用持久化的落点侧，并经过收敛", () => {
  // 落点侧存了却不用 = 白存（用户拖到上面松手，打开文件后又全跑到左边）。
  // 收敛也是必须的：脏值会让 targetHandle 指向不存在的手柄，边直接画不出来。
  assert.match(
    canvasSrc,
    /targetHandle:\s*normalizeTargetSide\(e\.dst_side\)/,
    "边投影没有用持久化的 dst_side"
  );
  // 手柄必须按 TARGET_SIDES 渲染：否则存了某一侧却没有对应手柄，那条边画不出来
  assert.match(
    canvasSrc,
    /v-for="side in TARGET_SIDES"/,
    "目标手柄没有按 TARGET_SIDES 渲染"
  );
});

test("成环时画布要提示用户、把环上的边标出来，且提示里带节点名", () => {
  // ⚠️ 这里只能断言「提示存在」。**「不拦」断言不了**——那是 store 层的行为，
  // 由 `app-store.test.mjs` 的「闭环时回报环上的边，而且环不被拦」覆盖。
  // 分开写是因为「环必须能连上」比「环要提示」重要得多：拦掉环会把
  // 「等到条件成立再往下走」这类合法流程一起挡死。
  assert.match(
    canvasSrc,
    /if\s*\(cycleEdges\)\s*\{[\s\S]{0,400}?MessagePlugin\.warning\(/,
    "成环时没有提示用户"
  );
  // 提示要指名是哪两个节点，否则用户得自己在一堆边里找那个环
  assert.match(
    canvasSrc,
    /nodeLabel\(c\.source\)[\s\S]{0,80}?nodeLabel\(c\.target\)/,
    "成环提示没有带节点名"
  );
  // 画布与环检测必须共用同一套「哪些边算数」：各写一份会漂，漂了的表现是
  // 「画布上明明看着没环、却提示有环」
  assert.match(canvasSrc, /liveEdges\(store\.workflow\.nodes/, "边投影没有用 liveEdges");
});

test("环上的边要真的被标出来（只报两个节点名，节点一多用户照样找不到）", () => {
  // 高亮靠的是「vEdges 算出来的 id」与「cycleEdges 映射出来的 id」能对上。
  // 两边各写一遍 `${src}|${port}` 就是等着漂——所以 id 格式只允许有一处定义，
  // 两个调用点都走它。这条断言把「只有一处」钉住。
  const idDefs = canvasSrc.match(/`\$\{[A-Za-z0-9_.]+\.src\}\|\$\{[A-Za-z0-9_.]+\.port\}`/g) ?? [];
  assert.equal(idDefs.length, 1, `边 id 的格式应只定义一处，实际 ${idDefs.length} 处：${idDefs}`);
  assert.match(canvasSrc, /cycleIds\.value\s*=\s*cycleEdges\.map\(edgeId\)/, "没有把环上的边记成高亮");
  assert.match(canvasSrc, /const hot = new Set\(cycleIds\.value\)/, "vEdges 没有读高亮集合");
  // 红色 + 加粗 + 虚线流动：静止的细线在一屏边里仍然会被忽略
  assert.match(canvasSrc, /stroke:\s*"#e34d59",\s*strokeWidth:\s*2\.4/, "环上的边没有换色加粗");
  assert.match(canvasSrc, /animated:\s*inLoop/, "环上的边没有虚线流动");
  // 高亮是「刚才那一下」的反馈，不是常驻装饰：下一次交互必须清掉，
  // 否则它会一直留着，用户分不清哪个是刚连的、哪个是早就有的。
  // 逐个 handler 检查而不是数总数——数总数的话删掉一处、另一处多写一遍也能凑够。
  for (const fn of ["onConnectStart", "onNodeDragStop", "onNodeClick", "onPaneClick"]) {
    const body = canvasSrc.match(new RegExp(`function ${fn}\\([^)]*\\)\\s*\\{[\\s\\S]{0,320}?\\n\\}`));
    assert.ok(body, `找不到 ${fn} 的函数体`);
    assert.match(body[0], /clearCycleHighlight\(\)/, `${fn} 里没有清掉环高亮`);
  }
});

test("常驻标环开关：默认关、走 edgesOnCycles、且清高亮不会把它一起清掉", () => {
  // 这个开关解决 `cycleIds` 覆盖不到的场景：**打开别人的文件 / 手工编辑过的 json**
  // 时想知道环在哪——那种时候没有任何「刚连上的边」，`cycleIds` 永远是空的。
  //
  // 默认**关**：环在 v4 合法，常开会把合法回边也涂红，等于对每份文件都暗示「这有错」。
  assert.match(canvasSrc, /const showCycles = ref\(false\)/, "开关必须默认关");

  // 常驻集合必须走「活边 + edgesOnCycles」：环检测与画布渲染共用同一套「哪些边算数」，
  // 否则会出现「画布上看着没环却提示有环」那种没人能想明白的现象。
  const allBody = canvasSrc.match(/const cycleAllIds = computed\(\(\) => \{[\s\S]{0,420}?\n\}\);/);
  assert.ok(allBody, "找不到 cycleAllIds");
  assert.match(allBody[0], /liveEdges\(/, "常驻集合没有过滤活边");
  assert.match(allBody[0], /edgesOnCycles\(/, "常驻集合没有真的找环");
  assert.match(allBody[0], /\.map\(edgeId\)/, "常驻集合没有用统一的边 id 格式");
  // 关着的时候要短路，别白算一遍整张图的强连通分量
  assert.match(allBody[0], /if \(!showCycles\.value\) return new Set<string>\(\)/);

  // 两个来源取并集：开关开着时又连出一条新环，开关关掉后那条新环仍然可见。
  assert.match(canvasSrc, /for \(const id of cycleAllIds\.value\) hot\.add\(id\)/,
    "vEdges 没有把常驻集合并进高亮");

  // ⚠️ 最容易写坏的一条：`clearCycleHighlight()` 是「下一次交互」的清理，
  // 它**只能**清 `cycleIds`。顺手把常驻集合也清了，开关就白开了——每次点空白
  // 红标记都消失，用户会以为开关坏了。
  const clearBody = canvasSrc.match(/function clearCycleHighlight\(\) \{[\s\S]{0,200}?\n\}/);
  assert.ok(clearBody, "找不到 clearCycleHighlight");
  assert.match(clearBody[0], /cycleIds\.value = \[\]/, "clearCycleHighlight 没有清 cycleIds");
  assert.doesNotMatch(clearBody[0], /cycleAllIds|showCycles/,
    "clearCycleHighlight 把常驻标环也清掉了（开关会看起来是坏的）");

  // 开关开着时按钮要显示条数：点了开关却什么都没变红时，用户得能分清
  // 「确实没有环」和「开关没生效」。
  assert.match(canvasSrc, /showCycles\.value \? `标出环（\$\{cycleAllIds\.value\.size\}）` : "标出环"/,
    "按钮文案没有在开启时给出条数");
  assert.match(canvasSrc, /@click="showCycles = !showCycles"/, "按钮没有接到开关上");
});

test("nodes-initialized 事件必须接到 tryFit 上（否则视野永远不适应）", () => {
  assert.match(
    canvasSrc,
    /@nodes-initialized="tryFit"/,
    "没有监听 nodes-initialized，fitView 永远不会在尺寸量完之后执行"
  );
  assert.match(canvasSrc, /createFitOnce\(/, "画布没用 fitOnce 状态机");
});

test("拖连线时上/下两个落点手柄必须提亮，否则用户以为只能连左边", () => {
  // 上/下两个手柄平时 opacity: .3。**拖拽期间正是用户找落点的时候**，
  // 这时还淡显就等于「有落点却看不见」——功能加了跟没加一样。
  assert.match(canvasSrc, /function onConnectStart\(/, "没有 onConnectStart");
  assert.match(canvasSrc, /function onConnectEnd\(/, "没有 onConnectEnd");
  assert.match(
    canvasSrc,
    /@connect-start="onConnectStart"/,
    "没监听 connect-start：拖拽时落点手柄不会提亮"
  );
  assert.match(
    canvasSrc,
    /@connect-end="onConnectEnd"/,
    "没监听 connect-end：提亮状态会一直留着，松手后所有手柄都是亮的"
  );
  // 光有状态没用，得挂成 class，CSS 才生效
  assert.match(
    canvasSrc,
    /:class="\{ 'af-connecting': connecting \}"/,
    "画布没把 connecting 挂成 class，CSS 无从生效"
  );
  // 基准淡显不能被顺手改掉（改成 0 就看不见落点了）
  assert.match(
    ruleBody(canvasSrc, ".af-handle-in-extra"),
    /opacity\s*:\s*0\.3/,
    "上/下落点手柄的基准透明度变了；改成 0 会让用户根本不知道能往那儿连"
  );
  assert.match(
    canvasSrc,
    /\.af-connecting\s+\.af-handle-in-extra\s*\{[^}]*opacity\s*:\s*1/,
    "拖拽期间没有把落点手柄提亮（.af-connecting 规则缺失或不生效）"
  );
});
