<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";
import { computeWindow } from "@/utils/vlist";

import { errMessage } from "@/rpc/client";
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
  { key: "drag", label: "拖拽" },
  { key: "wheel", label: "滚轮" },
] as const;
const filter = ref<string>("all");

function matches(ev: any, f: string): boolean {
  if (f === "all") return true;
  if (f === "drag") return ev.kind === "move" && !!ev.dragged;
  if (f === "key") return ev.kind === "key";
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
    await store.toggleRecord();
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
    } else if ((s.captured ?? 0) - (s.filtered ?? 0) - (s.limit_dropped ?? 0) !== (s.count ?? 0)) {
      MessagePlugin.warning("检测到系统层丢事件，请反馈（captured≠count+filtered）");
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
      <template v-if="store.lastRecordInfo.trimmed">
        · 裁掉停止交互 {{ store.lastRecordInfo.trimmed }} 条</template>
      <template v-if="store.lastRecordInfo.limit_dropped">
        · 超限丢弃 {{ store.lastRecordInfo.limit_dropped }} 条</template>
      <template v-if="store.lastRecordInfo.window_dropped">
        · <span style="color:#e34d59">窗口过滤丢弃 {{ store.lastRecordInfo.window_dropped }} 条</span></template>
      <template v-if="store.lastRecordInfo.mouse_died || store.lastRecordInfo.kb_died">
        · <span style="color:#e34d59">监听中断</span></template>
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
    <div v-if="store.recordBuffer.length && !store.recording" class="af-edit">
      <span class="af-edit-sel">
        {{ selected >= 0 ? `已选 #${selected}` : "点一行选中（Delete 可删）" }}
      </span>
      <button class="af-chip" :disabled="selected < 0" @click="onDeleteSelected">删除</button>
      <button class="af-chip" :disabled="selected < 0" @click="onDropMovesBefore">
        删此前的移动
      </button>
      <button class="af-chip" :disabled="selected < 0" @click="onSetOrigin">设为原点</button>
      <button class="af-chip" :disabled="!store.recordCanUndo" @click="onUndo">撤销</button>
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
.af-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
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
