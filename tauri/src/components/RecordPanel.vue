<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";
import { computeWindow } from "@/utils/vlist";

import { errMessage } from "@/rpc/client";
import { fmtRecDuration, type RecMeta } from "@/flow/recording";
import { confirmDanger } from "@/utils/confirm";
const store = useAppStore();

// ---- 事件流虚拟滚动 ----
// 缓冲上限 5000 条，全量 v-for 会让 DOM 节点数随事件数增长（录制中每秒可能上百条）。
// 只渲染视口内的行，行高固定所以窗口边界是纯算术（见 utils/vlist.ts）。
const ITEM_H = 15; // 必须与 .af-ev 的 line-height/height 一致，否则滚动会跳
const FOLLOW_SLACK = 24; // 距底部多少像素内算「跟随最新」

const streamEl = ref<HTMLElement | null>(null);
const scrollTop = ref(0);
const viewportH = ref(0);
// 是否跟随最新。用户往上翻看历史时不要把他拽回底部。
const follow = ref(true);

// ---- 类型过滤：用来快速核对"拖拽到底录进去没有" ----
const FILTERS = [
  { key: "all", label: "全部" },
  { key: "mouse", label: "点击" },
  { key: "key", label: "键盘" },
  { key: "text", label: "文本" },
  { key: "drag", label: "拖拽" },
  { key: "wheel", label: "滚轮" },
] as const;
const filter = ref<string>("all");

function matches(ev: any, f: string): boolean {
  if (f === "all") return true;
  if (f === "drag") return ev.kind === "move" && !!ev.dragged;
  if (f === "key") return ev.kind === "key";
  if (f === "text") return ev.kind === "text";
  if (f === "mouse") return ev.kind === "mouse";
  if (f === "wheel") return ev.kind === "wheel";
  return true;
}

// 行带上**缓冲内的真实下标**：过滤后仍要能指回后端序列里那一条，否则删除会删错。
const rows = computed(() => {
  const b = store.recordBuffer;
  const all = b.map((ev: any, idx: number) => ({ ev, idx }));
  return filter.value === "all" ? all : all.filter((r) => matches(r.ev, filter.value));
});
const total = computed(() => rows.value.length);
const win = computed(() =>
  computeWindow({
    count: total.value,
    itemH: ITEM_H,
    viewportH: viewportH.value,
    scrollTop: scrollTop.value,
  })
);
const visible = computed(() => rows.value.slice(win.value.start, win.value.end));

// ---- 选中与编辑 ----
const selected = ref(-1);
let busy = false;

async function edit(method: string, params: Record<string, unknown> = {}) {
  if (busy) return;
  busy = true;
  try {
    await store.recordEdit(method, params);
  } catch (e) {
    MessagePlugin.error("编辑失败：" + errMessage(e));
  } finally {
    busy = false;
  }
}

async function onDeleteSelected() {
  if (selected.value < 0) return;
  const idx = selected.value;
  selected.value = -1;
  await edit("record.remove", { indexes: [idx] });
}

async function onDropMovesBefore() {
  if (selected.value < 0) return;
  await edit("record.removeMovesBefore", { index: selected.value });
}

async function onSetOrigin() {
  if (selected.value < 0) return;
  await edit("record.setOrigin", { index: selected.value });
}

async function onUndo() {
  await edit("record.undo");
}

// 当前选中的那一条（字段编辑区、按键转文本、行操作都读它）。
const selectedEvent = computed(() => {
  const i = selected.value;
  const b = store.recordBuffer;
  return i >= 0 && i < b.length ? b[i] : null;
});

// ---- 文本事件内容编辑 ----
// 输入法提交的内容经常需要改（同音字、多打的字）。直接改这一条，比"重录一遍"省事。
// 把一段连续按键替换成一条 text 事件时填的实际文字。
// 中文录制录下来的是拼音按键（上屏不投递事件，见 core/inputsource.py），回放靠
// 重新驱动输入法——能用但不确定。换成文本事件后回放走 Unicode 通道，结果确定。
const keysToTextDraft = ref("");

watch(selectedEvent, () => {
  keysToTextDraft.value = "";
});

async function onKeysToText() {
  const ev = selectedEvent.value;
  if (!ev || ev.kind !== "key" || !keysToTextDraft.value) return;
  await edit("record.keysToText", {
    index: selected.value,
    text: keysToTextDraft.value,
  });
}

// ---- 单条事件的字段编辑（坐标 / 按键 / 时间 / 按钮 / 滚轮 / 文本）----
// 用「草稿 + 提交」而不是逐键提交：每敲一个字符就发一次 RPC 会把 "640" 拆成
// 6 / 64 / 640 三次写入（还会各占一格撤销）。草稿在选中行变化时重置。
const fieldDraft = ref<Record<string, any>>({});
watch(selectedEvent, (ev) => {
  fieldDraft.value = ev ? { ...ev } : {};
});

/** 只提交与当前值**不同**的字段（后端还会再判一次「没变就不占撤销栈」）。 */
async function commitFields() {
  const ev: any = selectedEvent.value;
  if (!ev) return;
  const fields: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(fieldDraft.value)) {
    if (k === "kind") continue; // 类型不可改——那是「换一条事件」，不是改字段
    if (v !== ev[k]) fields[k] = v;
  }
  if (!Object.keys(fields).length) return;
  await edit("record.patch", { index: selected.value, fields });
}

/** 在选中行**后面**插入一条新事件；没选中就追加到末尾。 */
const insertKind = ref("move");
async function onInsertEvent() {
  const idx =
    selected.value >= 0 ? selected.value + 1 : store.recordBuffer.length;
  await edit("record.insert", { index: idx, event: { kind: insertKind.value } });
  selected.value = idx; // 选中刚插入的那条，便于接着填字段
}

async function onDuplicate() {
  if (selected.value < 0) return;
  const idx = selected.value;
  await edit("record.duplicate", { index: idx });
  selected.value = idx + 1; // 复制品就在它后面
}

async function onMoveEvent(delta: number) {
  if (selected.value < 0) return;
  const idx = selected.value;
  await edit("record.moveEvent", { index: idx, delta });
  selected.value = idx + delta; // 选中跟着走，方便连按
}

// ---- 直接编辑库里的条目 ----
// 与「载入」的区别只在收尾：载入之后编辑只改内存（界面提示「改动未更新」），
// 这个入口把落盘也接管了——每次编辑后端自动写回库里那条。
async function onLibEdit(m: RecMeta) {
  await withLib(async () => {
    await store.recordingLibEdit(m.id);
    selected.value = -1;
    MessagePlugin.success(`正在直接编辑「${m.name}」：改动会立刻写回库里`);
  });
}

async function onLibEndEdit() {
  await withLib(async () => {
    await store.recordingLibEndEdit();
  });
}

// ---- 文本链路自检 ----
// 输入法提交中文有两条通道：① 事件层（keycode=0 带 Unicode，本实现覆盖）；
// ② insertText:（IME 在客户端内部直接插入文本，**不经事件层**，被动 tap 看不到）。
// 自检**只能证明①的链路是好的**：两段都通却依然录不到中文时，才轮到怀疑②。
// 反过来，若 text_alive 为 false，则中文录制一定是坏的（连我们自己投递的都收不到）。
const textProbe = ref<"" | "ok" | "noTap" | "noText">("");
// 自检同时报出当前输入法，以及它是否**已实测确认**不经过事件层。
// 没有这一项，`text_alive=true` 会被读成"中文能录"——而系统拼音下它恒为 true，
// 中文却恒录不到（见 core/inputsource.py 的实测记录）。
const probeSource = ref<{
  name: string;
  unsupported: boolean | null;
} | null>(null);
let probing = false;
async function onProbeText() {
  if (probing) return;
  probing = true;
  textProbe.value = "";
  probeSource.value = null;
  try {
    const r = await store.probeInput();
    textProbe.value = !r.alive ? "noTap" : r.text_alive ? "ok" : "noText";
    const s = r.input_source;
    if (s) {
      probeSource.value = {
        name: s.name || s.id || "未知",
        unsupported: s.event_channel_unsupported ?? null,
      };
    }
  } catch {
    textProbe.value = "noTap";
  } finally {
    probing = false;
  }
}

/** Delete / Backspace 删除选中行（事件流聚焦时）。 */
function onStreamKey(e: KeyboardEvent) {
  if (e.key === "Delete" || e.key === "Backspace") {
    if (selected.value >= 0) {
      e.preventDefault();
      void onDeleteSelected();
    }
  }
}

/** 事件流每一行的展示：key 事件此前只显示 "key @ 0,0"，看不出按了什么键。 */
function fmtEvent(ev: any): string {
  const t = `${ev.ts_ms ?? 0}ms`;
  const xy = `(${ev.x ?? 0},${ev.y ?? 0})`;
  if (ev.kind === "key") return `${t}  按键 ${ev.pressed ? "↓" : "↑"} ${ev.key ?? "?"}  ${xy}`;
  if (ev.kind === "text") return `${t}  文本 ${JSON.stringify(String(ev.text ?? ""))}  ${xy}`;
  if (ev.kind === "mouse") {
    const c = (ev.clicks ?? 1) > 1 ? ` ×${ev.clicks}` : "";
    return `${t}  鼠标 ${ev.pressed ? "按下" : "释放"} ${ev.button ?? "left"}${c}  ${xy}`;
  }
  if (ev.kind === "wheel") {
    const u = ev.wheel_unit === "pixel" ? "px" : "行";
    return `${t}  滚轮 dy=${ev.wheel_dy ?? 0}${u} dx=${ev.wheel_dx ?? 0}${u}  ${xy}`;
  }
  return `${t}  ${ev.dragged ? "拖拽" : "移动"}  ${xy}`;
}

/** 文本事件统计：条数 / 总字数。用来一眼确认"中文到底录成文本了没有"。 */
const textStats = computed(() => {
  let n = 0;
  let chars = 0;
  for (const ev of store.recordBuffer) {
    if (ev.kind === "text") {
      n += 1;
      chars += String(ev.text ?? "").length;
    }
  }
  return { n, chars };
});

/** 缓冲内的事件跨度（毫秒），用于快速判断录制时长是否合理。 */
const durationMs = computed(() => {
  const b = store.recordBuffer;
  return b.length ? Number(b[b.length - 1].ts_ms ?? 0) : 0;
});

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

function onScroll() {
  const el = streamEl.value;
  if (!el) return;
  scrollTop.value = el.scrollTop;
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK;
}

function scrollToBottom() {
  const el = streamEl.value;
  if (!el) return;
  el.scrollTop = el.scrollHeight;
  scrollTop.value = el.scrollTop;
  follow.value = true;
}

// 新事件到达时，只有原本就贴在底部才继续跟随。
// 同时收敛选中下标：删除/过滤切换后原下标可能已不存在。
watch(total, (n) => {
  if (selected.value >= n) selected.value = -1;
  if (!follow.value) return;
  void nextTick(scrollToBottom);
});

// 切换过滤条件后，窗口高度与滚动位置都失效，直接回到最新。
watch(filter, () => {
  scrollTop.value = 0;
  follow.value = true;
  void nextTick(scrollToBottom);
});

let ro: ResizeObserver | null = null;
onMounted(() => {
  const el = streamEl.value;
  if (!el) return;
  viewportH.value = el.clientHeight;
  if (typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(() => {
      viewportH.value = el.clientHeight;
    });
    ro.observe(el);
  }
});
onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
});

async function onToggleRecord() {
  try {
    await store.toggleRecord({ by: "button" });
    const s = store.lastRecordInfo;
    if (!s) return;
    if (s.mouse_died || s.kb_died) {
      MessagePlugin.warning(
        (s.mouse_died ? "鼠标" : "") + (s.mouse_died && s.kb_died ? "、" : "") +
        (s.kb_died ? "键盘" : "") + "监听未在运行，其记录范围内的事件可能缺失"
      );
    } else if (s.stopped_by_limit) {
      MessagePlugin.warning(`事件数达到上限，超限丢弃 ${s.limit_dropped ?? 0} 条`);
    } else if ((s.window_dropped ?? 0) > 0) {
      MessagePlugin.warning(`窗口过滤丢弃了 ${s.window_dropped} 条事件（不应发生，请反馈）`);
    } else if ((s.unaccounted ?? 0) !== 0) {
      // 一致性等式只在后端算（captured − 各类有意移除 == count），前端只判是否为 0。
      // 以前前端自己拼公式，每新增一类"有意移除"就漏扣一次——trimmed 与
      // text_merged 都各自制造过一次这里弹出来的假警报。
      MessagePlugin.warning(
        `检测到系统层丢事件（${s.unaccounted} 条），请反馈（captured≠count+filtered）`
      );
    }
  } catch (e) {
    MessagePlugin.error("录制失败：" + errMessage(e));
  }
}
async function onToNode() {
  try {
    await store.recordToNode();
    MessagePlugin.success("已写入节点");
  } catch (e) {
    MessagePlugin.error("写入失败：" + errMessage(e));
  }
}

// ---- 录制库 ----
// 一次录制 = 库里一条可复用资产；节点按 id 引用，"在库里改一次，所有引用它的
// 节点一起变"。面板这里管的是库本身：存、改名、删、载入、落到画布。
const libBusy = ref(false);
/** 存入库时用的名字。默认跟着当前条目的名字走，用户改完按「存入库」即覆盖。 */
const libName = ref("");
const renamingId = ref("");
const renameDraft = ref("");

// 当前条目变了就同步一次草稿名——用户在库里改名后，输入框要跟着变，
// 否则下一次「存入库」会把它改回旧名字（覆盖是静默的，很难发现）。
watch(
  () => store.recordingLibCurrent?.name,
  (n) => {
    if (typeof n === "string") libName.value = n;
  },
  { immediate: true }
);

const libItems = computed<RecMeta[]>(() => store.recordingLib);
const libCurrent = computed(() => store.recordingLibCurrent);

/** 库里一条的副标题：条数 / 时长 / 创建时间，列表上一行看完。 */
function libSub(m: RecMeta): string {
  const parts = [`${m.count} 条`];
  if (m.duration_ms) parts.push(fmtRecDuration(m.duration_ms));
  if (m.created_at) parts.push(m.created_at);
  return parts.join(" · ");
}

async function withLib<T>(fn: () => Promise<T>): Promise<void> {
  if (libBusy.value) return;
  libBusy.value = true;
  try {
    await fn();
  } catch (e) {
    MessagePlugin.error("录制库操作失败：" + errMessage(e));
  } finally {
    libBusy.value = false;
  }
}

/** 存入库。`rid` 空 = 新建一条，给了 = 覆盖那一条（引用的节点会跟着变）。 */
function onLibSave(rid: string) {
  void withLib(async () => {
    await store.recordingLibSave(libName.value, rid);
    MessagePlugin.success(rid ? "已更新库里的录制" : "已存入录制库");
  });
}

function onLibLoad(m: RecMeta) {
  void withLib(async () => {
    await store.recordingLoadFromLib(m.id);
    // 载入后事件流是后端权威序列，滚动到底看最新那条
    selected.value = -1;
    MessagePlugin.success(`已载入「${m.name}」`);
  });
}

function onLibToNode(m: RecMeta) {
  void withLib(async () => {
    await store.recordingLibToNode(m.id);
    MessagePlugin.success(`已把「${m.name}」放到画布`);
  });
}

function onLibStartRename(m: RecMeta) {
  renamingId.value = m.id;
  renameDraft.value = m.name;
}

function onLibRename(m: RecMeta) {
  const name = renameDraft.value.trim();
  renamingId.value = "";
  if (!name || name === m.name) return;
  void withLib(async () => {
    await store.recordingLibRename(m.id, name);
    MessagePlugin.success("已改名");
  });
}

async function onLibDelete(m: RecMeta) {
  // 库里删掉不影响已经引用它的节点（节点只存 id），所以必须说清后果：
  // 那些节点回放时会**报错取不到录制**，得让用户先知道。
  const used = store.workflow.nodes.filter(
    (n) => n.type === "record_replay" && String(n.params?.recording_id ?? "") === m.id
  ).length;
  // ⚠️ 这里**必须**用应用内确认框，且**必须 await**。原先用的是 `window.confirm`，
  // 它在本项目里**拦不住任何东西**，两条原因叠加：
  //   1. Tauri 往 WebView 注入
  //      `window.confirm = async function(i){ return await invoke("plugin:dialog|confirm", …) }`
  //      ——返回的是 **Promise**。Promise 恒为 truthy，所以无论写成
  //      `if (!window.confirm(...)) return;` 还是 `window.confirm(...) && doIt()`，
  //      拦截条件**恒不成立**（`!Promise` 恒 false ⇒ `return` 永不执行；
  //      而 `Promise && …` 恒真 ⇒ 右侧无条件执行）。
  //   2. 本项目**没授予 `dialog:allow-confirm`**（`tauri-plugin-dialog` 的 `default`
  //      权限集只有 allow-message / allow-open / allow-save），那个 invoke 会被
  //      权限拒绝 ⇒ **连框都不弹**。
  // 合起来：点「删除」→ 没有任何提示 → 文件被永久删掉。
  // 确认文案要写「不能恢复」：录制库的删除是 `os.remove`（不是进废纸篓），
  // 与工作流库那条「可以从 Finder 的废纸篓恢复」**不一样**。
  const body = [
    `「${m.name}」会被永久删除，不能恢复。`,
    used ? `有 ${used} 个节点正在引用它，删掉后那些节点回放会失败。` : "",
  ].filter(Boolean).join("\n");
  if (!(await confirmDanger({ header: "删除录制", body, confirmBtn: "永久删除" }))) {
    return;
  }
  void withLib(async () => {
    await store.recordingLibDelete(m.id);
    MessagePlugin.success("已删除");
  });
}
</script>

<template>
  <div>
    <t-button block :theme="store.recording ? 'danger' : 'primary'" @click="onToggleRecord">
      {{ store.recording ? "■ 停止录制 (F9)" : "● 开始录制 (F9)" }}
    </t-button>
    <div v-if="store.recording" class="af-rec-live">
      <span class="af-dot pulse" /> 录制中…（快捷键 F9 停止）
    </div>
    <div style="display: flex; gap: 8px; margin-top: 8px">
      <t-button
        style="flex: 1"
        variant="outline"
        :disabled="store.recording || store.lastRecordCount === 0"
        @click="onToNode"
      >
        写入节点{{ store.lastRecordCount ? `（${store.lastRecordCount}）` : "" }}
      </t-button>
      <t-button
        variant="outline"
        :disabled="store.recording"
        @click="store.clearRecord()"
        >清空</t-button
      >
    </div>
    <div v-if="store.lastRecordInfo" class="af-rec-stats">
      已录 {{ store.lastRecordCount }} 条事件
      <template v-if="durationMs"> · 时长 {{ fmtDuration(durationMs) }}</template>
      <template v-if="store.lastRecordInfo.decimated">
        · 冗余移动降采样 {{ store.lastRecordInfo.decimated }} 条</template>
      <template v-if="textStats.n">
        · 文本事件 {{ textStats.n }} 条（{{ textStats.chars }} 字，聚合
        {{ store.lastRecordInfo.text_merged ?? 0 }} 段）</template>
      <template v-if="store.lastRecordInfo.trimmed">
        · 裁掉停止交互 {{ store.lastRecordInfo.trimmed }} 条</template>
      <template v-if="store.lastRecordInfo.limit_dropped">
        · 超限丢弃 {{ store.lastRecordInfo.limit_dropped }} 条</template>
      <template v-if="store.lastRecordInfo.window_dropped">
        · <span style="color:#e34d59">窗口过滤丢弃 {{ store.lastRecordInfo.window_dropped }} 条</span></template>
      <template v-if="store.lastRecordInfo.mouse_died || store.lastRecordInfo.kb_died">
        · <span style="color:#e34d59">监听中断</span></template>
    </div>
    <div class="af-lib">
      <div class="af-lib-head">
        <b>录制库</b>
        <span class="af-lib-count">{{ libItems.length }} 条</span>
        <span style="flex: 1" />
        <button
          class="af-chip"
          :disabled="store.recording || libBusy"
          :title="store.recordingLibDir"
          @click="withLib(() => store.refreshRecordingLib())"
        >
          刷新
        </button>
      </div>

      <!-- 当前这份录制的入库状态。三种：已同步 / 有改动未更新 / 还没入库。
           「有改动」由后端按 rev 比对判定（不是前端自己记），编辑接口漏一个也会显示出来。 -->
      <div class="af-lib-state">
        <template v-if="!store.lastRecordCount">
          <span class="af-lib-hint">还没有录制内容，录完会自动存进库。</span>
        </template>
        <template v-else-if="libCurrent && !libCurrent.dirty">
          <span class="af-probe ok">✓ 已同步到「{{ libCurrent.name }}」</span>
        </template>
        <template v-else-if="libCurrent">
          <span class="af-probe bad">● 改动未更新（库里还是旧的那份）</span>
        </template>
        <template v-else>
          <span class="af-lib-hint">这份录制还没入库。</span>
        </template>
      </div>

      <div v-if="store.lastRecordCount" class="af-lib-save">
        <input
          v-model="libName"
          class="af-textinput"
          placeholder="录制名称"
          @keydown.enter.prevent="onLibSave(libCurrent?.id ?? '')"
        />
        <button
          class="af-chip on"
          :disabled="libBusy"
          @click="onLibSave(libCurrent?.id ?? '')"
        >
          {{ libCurrent ? "更新到库" : "存入库" }}
        </button>
        <button
          class="af-chip"
          :disabled="libBusy"
          title="用这个名字另存一条，不动原来那条"
          @click="onLibSave('')"
        >
          另存为新的
        </button>
      </div>

      <div v-if="!libItems.length" class="af-lib-hint">库还是空的。</div>
      <div v-for="m in libItems" :key="m.id" class="af-lib-row">
        <template v-if="renamingId === m.id">
          <input
            v-model="renameDraft"
            class="af-textinput"
            @keydown.enter.prevent="onLibRename(m)"
            @keydown.esc.prevent="renamingId = ''"
          />
          <button class="af-chip on" @click="onLibRename(m)">确定</button>
          <button class="af-chip" @click="renamingId = ''">取消</button>
        </template>
        <template v-else>
          <div class="af-lib-meta">
            <div class="af-lib-name" :title="m.id">
              {{ m.name }}
              <span v-if="libCurrent?.id === m.id" class="af-lib-cur">当前</span>
            </div>
            <div class="af-lib-sub">{{ libSub(m) }}</div>
          </div>
          <button class="af-chip" :disabled="libBusy || store.recording" @click="onLibLoad(m)">
            载入
          </button>
          <!-- 「直接编辑」= 载入 + 每次编辑自动写回。与「载入」只差收尾，
               所以紧挨着放，用户一眼能看出这是同一件事的两种收尾方式。 -->
          <button
            class="af-chip"
            :disabled="libBusy || store.recording"
            :class="{ on: libCurrent?.editing && libCurrent?.id === m.id }"
            @click="onLibEdit(m)"
          >
            直接编辑
          </button>
          <button class="af-chip" :disabled="libBusy" @click="onLibToNode(m)">落到画布</button>
          <button class="af-chip" :disabled="libBusy" @click="onLibStartRename(m)">改名</button>
          <button class="af-chip" :disabled="libBusy" @click="onLibDelete(m)">删除</button>
        </template>
      </div>
    </div>

    <div class="af-textprobe">
      <button class="af-chip" :disabled="store.recording" @click="onProbeText">
        文本链路自检
      </button>
      <span v-if="textProbe === 'ok'" class="af-probe ok">✓ 事件通道文本捕获可用</span>
      <span v-else-if="textProbe === 'noText'" class="af-probe bad">
        ✗ 收不到 Unicode 文本提交：中文/emoji 录制不可用
      </span>
      <span v-else-if="textProbe === 'noTap'" class="af-probe bad">
        ✗ 收不到合成事件：请检查输入监控权限
      </span>
      <span v-if="textProbe === 'ok' && probeSource?.unsupported === true" class="af-probe bad">
        ⚠️ 但当前输入法录不到中文
      </span>
    </div>
    <div v-if="textProbe === 'ok' && probeSource?.unsupported === true" class="af-probe-hint">
      <b>「{{ probeSource.name }}」已实测不经过事件层</b>：上屏走 insertText:，不投递
      任何键盘事件，所以事件表里只会有拼音字母。<b>回放仍能打出中文</b>——重新驱动
      输入法即可（实测：录 nihao+空格 → 回放得到「你好」），但依赖回放时输入法状态
      与候选顺序，<b>不确定</b>。要确定就选中那段字母按键，填上实际文字「替换为文本」；
      或直接用「<b>键盘输入</b>」节点填写。
    </div>
    <div v-else-if="textProbe === 'ok' && probeSource" class="af-probe-hint">
      当前输入法「{{ probeSource.name }}」不在已知不支持名单里，但<b>未经实测</b>。
      若录中文后「文本」过滤下为空、只有一串字母按键，说明它同样不经过事件层——
      中文请改用「键盘输入」节点填写。
    </div>
    <div v-else-if="textProbe === 'ok'" class="af-probe-hint">
      链路正常，但未能识别当前输入法。若录中文后「文本」过滤下为空、只有一串字母按键，
      中文请改用「键盘输入」节点填写。
    </div>
    <div v-if="store.recordBuffer.length" class="af-filters">
      <button
        v-for="f in FILTERS"
        :key="f.key"
        class="af-chip"
        :class="{ on: filter === f.key }"
        @click="filter = f.key"
      >
        {{ f.label }}
      </button>
    </div>
    <!-- 正在「直接编辑」库里某条：每次改动都会自动写回库里那条 -->
    <div v-if="libCurrent?.editing" class="af-lib-editing">
      <span class="af-probe ok">
        ✎ 正在直接编辑库里的「{{ libCurrent.name }}」——改动会立刻写回
      </span>
      <button class="af-chip" @click="onLibEndEdit">完成编辑</button>
    </div>
    <div v-if="store.recordBuffer.length && !store.recording" class="af-edit">
      <span class="af-edit-sel">
        {{ selected >= 0 ? `已选 #${selected}` : "点一行选中（Delete 可删）" }}
      </span>
      <button class="af-chip" :disabled="selected < 0" @click="onDeleteSelected">删除</button>
      <button class="af-chip" :disabled="selected < 0" @click="onDuplicate">复制</button>
      <button class="af-chip" :disabled="selected < 1" @click="onMoveEvent(-1)">上移</button>
      <button
        class="af-chip"
        :disabled="selected < 0 || selected >= store.recordBuffer.length - 1"
        @click="onMoveEvent(1)"
      >
        下移
      </button>
      <button class="af-chip" :disabled="selected < 0" @click="onDropMovesBefore">
        删此前的移动
      </button>
      <button class="af-chip" :disabled="selected < 0" @click="onSetOrigin">设为原点</button>
      <button class="af-chip" :disabled="!store.recordCanUndo" @click="onUndo">撤销</button>
    </div>
    <div v-if="store.recordBuffer.length && !store.recording" class="af-edit">
      <span class="af-edit-sel">插入</span>
      <select v-model="insertKind" class="af-select">
        <option value="move">移动</option>
        <option value="mouse">鼠标</option>
        <option value="wheel">滚轮</option>
        <option value="key">按键</option>
        <option value="text">文本</option>
      </select>
      <button class="af-chip" @click="onInsertEvent">插到选中行后面</button>
    </div>
    <!-- 单条事件的字段编辑：时间/坐标所有类型都能改，其余按类型给 -->
    <div v-if="selectedEvent && !store.recording" class="af-fields">
      <span class="af-edit-sel">这一条</span>
      <label class="af-field">
        <span>时间</span>
        <input
          v-model.number="fieldDraft.ts_ms"
          type="number"
          class="af-num"
          @change="commitFields"
        />
      </label>
      <label class="af-field">
        <span>X</span>
        <input v-model.number="fieldDraft.x" type="number" class="af-num" @change="commitFields" />
      </label>
      <label class="af-field">
        <span>Y</span>
        <input v-model.number="fieldDraft.y" type="number" class="af-num" @change="commitFields" />
      </label>
      <template v-if="selectedEvent.kind === 'mouse'">
        <label class="af-field">
          <span>按钮</span>
          <select v-model="fieldDraft.button" class="af-select" @change="commitFields">
            <option value="left">左键</option>
            <option value="right">右键</option>
            <option value="middle">中键</option>
          </select>
        </label>
        <label class="af-field">
          <span>动作</span>
          <select v-model="fieldDraft.pressed" class="af-select" @change="commitFields">
            <option :value="true">按下</option>
            <option :value="false">释放</option>
          </select>
        </label>
        <label class="af-field">
          <span>连击</span>
          <input
            v-model.number="fieldDraft.clicks"
            type="number"
            min="1"
            class="af-num"
            @change="commitFields"
          />
        </label>
      </template>
      <template v-else-if="selectedEvent.kind === 'key'">
        <label class="af-field">
          <span>按键</span>
          <input
            v-model="fieldDraft.key"
            class="af-textinput af-key"
            @keydown.enter.prevent="commitFields"
            @blur="commitFields"
          />
        </label>
        <label class="af-field">
          <span>动作</span>
          <select v-model="fieldDraft.pressed" class="af-select" @change="commitFields">
            <option :value="true">按下</option>
            <option :value="false">抬起</option>
          </select>
        </label>
      </template>
      <template v-else-if="selectedEvent.kind === 'wheel'">
        <label class="af-field">
          <span>dy</span>
          <input
            v-model.number="fieldDraft.wheel_dy"
            type="number"
            class="af-num"
            @change="commitFields"
          />
        </label>
        <label class="af-field">
          <span>dx</span>
          <input
            v-model.number="fieldDraft.wheel_dx"
            type="number"
            class="af-num"
            @change="commitFields"
          />
        </label>
        <label class="af-field">
          <span>单位</span>
          <select v-model="fieldDraft.wheel_unit" class="af-select" @change="commitFields">
            <option value="line">行</option>
            <option value="pixel">像素</option>
          </select>
        </label>
      </template>
      <template v-else-if="selectedEvent.kind === 'move'">
        <label class="af-field">
          <span>拖拽</span>
          <input v-model="fieldDraft.dragged" type="checkbox" @change="commitFields" />
        </label>
      </template>
      <template v-else-if="selectedEvent.kind === 'text'">
        <label class="af-field">
          <span>文本</span>
          <input
            v-model="fieldDraft.text"
            class="af-textinput af-text"
            @keydown.enter.prevent="commitFields"
            @blur="commitFields"
          />
        </label>
      </template>
    </div>
    <div v-if="selectedEvent?.kind === 'key' && !store.recording" class="af-textedit">
      <span class="af-edit-sel">这段按键</span>
      <input
        v-model="keysToTextDraft"
        class="af-textinput"
        placeholder="填入这段按键实际打出的文字，如 你好"
        @keydown.enter.prevent="onKeysToText"
      />
      <button class="af-chip" :disabled="!keysToTextDraft" @click="onKeysToText">
        替换为文本
      </button>
    </div>
    <div
      v-if="selectedEvent?.kind === 'key' && !store.recording"
      class="af-probe-hint"
    >
      中文是用拼音输入法打的：事件表里只有字母按键，回放会<b>重新驱动输入法</b>上屏，
      所以能打出中文但不稳定（输入法没开就变成字面量）。填入实际文字后替换成一条
      文本事件，回放改走 Unicode 通道，结果确定。
    </div>
    <div v-if="store.recordBuffer.length" class="af-stream-bar">
      <span>共 {{ total }} 条</span>
      <span v-if="total > win.end || win.start > 0" class="af-range">
        显示 {{ win.start + 1 }}–{{ win.end }}
      </span>
      <span style="flex: 1" />
      <span class="af-range">原点 {{ store.recordOrigin[0] }},{{ store.recordOrigin[1] }}</span>
      <button v-if="!follow" class="af-jump" @click="scrollToBottom">↓ 回到最新</button>
    </div>
    <div
      ref="streamEl"
      class="af-stream"
      :class="{ live: store.recording }"
      tabindex="0"
      @scroll.passive="onScroll"
      @keydown="onStreamKey"
    >
      <div class="af-vlist" :style="{ height: win.totalH + 'px' }">
        <div class="af-vwin" :style="{ transform: `translateY(${win.offsetY}px)` }">
          <div
            v-for="(row, i) in visible"
            :key="row.idx"
            class="af-ev"
            :class="{ sel: row.idx === selected }"
            :style="{ height: ITEM_H + 'px' }"
            @click="selected = row.idx === selected ? -1 : row.idx"
          >
            {{ fmtEvent(row.ev) }}
          </div>
        </div>
      </div>
      <div v-if="!total" class="af-empty">暂无事件</div>
    </div>
  </div>
</template>

<style scoped>
.af-rec-live {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 13px;
  color: #e34d59;
}
.af-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #e34d59;
  animation: af-pulse 1s infinite;
}
@keyframes af-pulse {
  50% {
    opacity: 0.3;
  }
}
.af-stream.live {
  border-color: #ffb3b3;
}
.af-stream {
  margin-top: 8px;
  max-height: 200px;
  overflow: auto;
  border: 1px solid #e7e7e7;
  border-radius: 6px;
  padding: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: #444;
}
.af-ev {
  /* 高度与行高必须都等于脚本里的 ITEM_H，否则虚拟窗口与实际渲染错位、滚动会跳 */
  line-height: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}
.af-ev.sel {
  background: #e8f0ff;
  color: #0052d9;
}
.af-edit {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.af-edit-sel {
  font-size: 11px;
  color: #888;
  margin-right: 2px;
}
.af-textedit {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.af-textinput {
  flex: 1;
  min-width: 0;
  border: 1px solid #d5d8dd;
  border-radius: 6px;
  padding: 2px 6px;
  font-size: 11px;
  color: #444;
}
.af-textinput:focus {
  outline: none;
  border-color: #0052d9;
}
/* ---- 单条事件的字段编辑区 ---- */
/* 每个字段必须**整体**换行：`.af-field` 不加 `white-space: nowrap` 时，实测会出现
   「标签在上一行末尾、它的输入框在下一行开头」——读起来像那个标签是上一个输入框的
   后缀（真机目视 2026-09-17 发现：「时间 [1950]」后面孤零零一个「X」）。 */
.af-fields {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-top: 6px;
}
.af-field {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  font-size: 11px;
  color: #888;
}
.af-num {
  width: 64px;
  border: 1px solid #d5d8dd;
  border-radius: 6px;
  padding: 2px 6px;
  font-size: 11px;
  color: #444;
}
.af-num:focus {
  outline: none;
  border-color: #0052d9;
}
.af-select {
  border: 1px solid #d5d8dd;
  border-radius: 6px;
  padding: 2px 4px;
  font-size: 11px;
  color: #444;
  background: #fff;
}
.af-select:focus {
  outline: none;
  border-color: #0052d9;
}
/* 文本类字段（按键 / 文本）给足宽度：`.af-textinput` 是 flex:1，在 shrink-to-fit
   的 `.af-field` 里会缩到只剩一个字，看不出改的是什么。 */
.af-key {
  flex: 0 0 auto;
  width: 76px;
}
.af-text {
  flex: 0 0 auto;
  width: 150px;
}
/* 「正在直接编辑库里的 xxx」常驻提示。它改变了编辑的**收尾语义**（自动写回），
   不能只靠一个小图标暗示——做成显眼的浅蓝块，让用户知道现在的改动会立刻落盘。 */
.af-lib-editing {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  padding: 4px 8px;
  border: 1px solid #b9d0f5;
  border-radius: 8px;
  background: #f2f7ff;
}
.af-chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
/* 撑出完整滚动高度，再由 .af-vwin 平移到当前窗口位置 */
.af-vlist {
  position: relative;
}
.af-vwin {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
}
.af-empty {
  color: #999;
}
.af-rec-stats {
  margin-top: 6px;
  font-size: 11px;
  color: #666;
}
/* ---- 录制库 ---- */
.af-lib {
  margin-top: 10px;
  border-top: 1px solid #eceef1;
  padding-top: 8px;
}
.af-lib-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.af-lib-head b {
  font-size: 13px;
}
.af-lib-count {
  font-size: 11px;
  color: #aaa;
}
.af-lib-state {
  margin-top: 4px;
  font-size: 11px;
}
.af-lib-hint {
  font-size: 11px;
  color: #999;
}
.af-lib-save {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.af-lib-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 5px 0;
  border-bottom: 1px dashed #f0f1f3;
}
.af-lib-meta {
  /* 侧栏只有 ~350px，而这一行最多有 5 颗按钮（载入 / 直接编辑 / 落到画布 /
     改名 / 删除）——`flex: 1` + `min-width: 0` 会让名字被压成「录制 …」、
     副标题折成四行。给 meta 一个下限，装不下时**整行按钮换到下一行**。 */
  flex: 1 1 140px;
  min-width: 0;
}
.af-lib-name {
  font-size: 12px;
  color: #333;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.af-lib-cur {
  margin-left: 4px;
  font-size: 10px;
  color: #0052d9;
  border: 1px solid #b9d0f5;
  border-radius: 8px;
  padding: 0 5px;
}
.af-lib-sub {
  font-size: 10px;
  color: #aaa;
}
.af-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.af-textprobe {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}
.af-probe {
  font-size: 11px;
}
.af-probe.ok {
  color: #059669;
}
.af-probe.bad {
  color: #e34d59;
}
.af-probe-hint {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.5;
  color: #888;
}
.af-chip {
  border: 1px solid #d5d8dd;
  background: #fff;
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 11px;
  color: #666;
  cursor: pointer;
}
.af-chip.on {
  border-color: #0052d9;
  color: #0052d9;
  background: #f0f4ff;
}
.af-stream-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 11px;
  color: #888;
}
.af-range {
  color: #aaa;
}
.af-jump {
  border: 1px solid #d5d8dd;
  background: #fff;
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 11px;
  color: #0052d9;
  cursor: pointer;
}
.af-jump:hover {
  background: #f0f4ff;
}
</style>
