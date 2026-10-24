<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const store = useAppStore();
const speed = ref(store.workflow.speed);
const repeat = ref(store.workflow.repeat);
const maxRounds = ref(store.workflow.max_rounds ?? 0);

watch(
  () => store.workflow,
  (w) => {
    speed.value = w.speed;
    repeat.value = w.repeat;
    maxRounds.value = w.max_rounds ?? 0;
  }
);

async function onSpeed(v: number) {
  speed.value = v;
  await store.updateWorkflow({ speed: v });
}
async function onMaxRounds(v: any) {
  const n = Math.max(0, Math.floor(Number(v) || 0));
  maxRounds.value = n;
  await store.updateWorkflow({ max_rounds: n });
}

async function onRepeat(v: number) {
  repeat.value = v;
  await store.updateWorkflow({ repeat: Math.max(1, Math.floor(v)) });
}
async function onPick() {
  await store.pickBase();
  MessagePlugin.success(`基点：${store.base.x}, ${store.base.y}`);
}
async function onBaseX(v: number) {
  store.base = { ...store.base, x: Math.floor(v) };
}
async function onBaseY(v: number) {
  store.base = { ...store.base, y: Math.floor(v) };
}
async function onToggleRun() {
  try {
    await store.toggleRun();
  } catch (e) {
    MessagePlugin.error("运行失败：" + errMessage(e));
  }
}

const typeLabel = (t: string) => store.nodeDefsByName[t]?.name ?? t;

/** 轨迹列表的 DOM 句柄：新条目进来后要把它滚到底，否则永远停在最早那几步。 */
const traceList = ref<HTMLElement | null>(null);
const fmtTime = (t: number) =>
  new Date(t).toLocaleTimeString("zh-CN", { hour12: false });
const fmtDelta = (ms: number) => (ms > 0 ? "+" + (ms / 1000).toFixed(1) + "s" : "");
watch(
  () => store.runTrace.length,
  async () => {
    await nextTick();
    const el = traceList.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);
</script>

<template>
  <div>
    <div class="af-field">
      <label class="af-label">速度倍率</label>
      <t-input-number :value="speed" :step="0.1" :min="0.1" @change="onSpeed" size="small" theme="column" />
    </div>
    <div class="af-field">
      <label class="af-label">循环次数</label>
      <t-input-number :value="repeat" :step="1" :min="1" @change="onRepeat" size="small" theme="column" />
    </div>
    <div class="af-field">
      <label class="af-label">跑 N 轮后停（0=一直跑）</label>
      <t-input-number
        :value="maxRounds"
        :step="1"
        :min="0"
        @change="onMaxRounds"
        size="small"
        theme="column"
        title="到画布上「设为轮次起点」的那个节点每执行一次算一轮；没设起点时这个值不起作用"
      />
    </div>
    <div v-if="store.running && store.runRound.total > 0" class="af-run-state">
      第 {{ store.runRound.round }} / {{ store.runRound.total }} 轮
    </div>
    <div class="af-field">
      <label class="af-label">基点（F11 取点）</label>
      <div style="display: flex; gap: 6px; align-items: center">
        <t-input-number :value="store.base.x" :step="1" size="small" theme="column" @change="onBaseX" />
        <t-input-number :value="store.base.y" :step="1" size="small" theme="column" @change="onBaseY" />
        <t-button size="small" variant="outline" @click="onPick">取点</t-button>
      </div>
    </div>
    <div v-if="store.running" class="af-run-state">
      <div class="af-run-node">
        <span class="af-dot pulse" /> 运行中：{{ store.runNodeName || typeLabel(store.runNodeType) || "…" }}
      </div>
      <div v-if="store.runNodeDetail" class="af-run-detail">{{ store.runNodeDetail }}</div>
      <t-progress
        v-if="store.runProgress.total > 0"
        :percentage="Math.round((store.runProgress.done / store.runProgress.total) * 100)"
        size="small"
        theme="line"
      />
    </div>
    <div v-if="store.runTrace.length" class="af-run-trace">
      <div class="af-trace-head">
        {{ store.running ? "运行轨迹" : "上次运行" }} · {{ store.runTrace.length }} 步
      </div>
      <div ref="traceList" class="af-trace-list">
        <div v-for="(it, i) in store.runTrace" :key="i" class="af-trace-row">
          <span class="af-trace-time">{{ fmtTime(it.t) }}</span>
          <span class="af-trace-name" :title="it.detail">{{ it.name }}</span>
          <span class="af-trace-delta">{{ fmtDelta(it.deltaMs) }}</span>
        </div>
      </div>
    </div>
    <t-button block :theme="store.running ? 'danger' : 'primary'" @click="onToggleRun">
      {{ store.running ? "■ 停止运行 (F10)" : "▶ 运行 (F10)" }}
    </t-button>
    <div class="af-hotkeys">F9 录制 · F10 运行/停止 · F11 取点</div>
  </div>
</template>

<style scoped>
.af-run-state {
  margin: 4px 0 8px;
  padding: 8px 10px;
  background: #f2f7ff;
  border-radius: 6px;
  font-size: 13px;
  color: #2456c4;
}
.af-run-node {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.af-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #0052d9;
  animation: af-pulse 1s infinite;
}
@keyframes af-pulse {
  50% {
    opacity: 0.3;
  }
}
.af-run-detail {
  margin-bottom: 4px;
  font-size: 12px;
  color: #4a6fa5;
  word-break: break-all;
}
.af-run-trace {
  margin: 0 0 8px;
  border: 1px solid #e6ebf2;
  border-radius: 6px;
  overflow: hidden;
}
.af-trace-head {
  padding: 5px 8px;
  font-size: 11px;
  color: #666;
  background: #f7f9fc;
  border-bottom: 1px solid #e6ebf2;
}
.af-trace-list {
  max-height: 190px;
  overflow-y: auto;
  font-size: 11px;
}
.af-trace-row {
  display: flex;
  gap: 6px;
  align-items: baseline;
  padding: 3px 8px;
  border-bottom: 1px solid #f2f5f9;
}
.af-trace-row:last-child {
  border-bottom: none;
}
.af-trace-time {
  color: #999;
  flex: 0 0 auto;
  font-variant-numeric: tabular-nums;
}
.af-trace-name {
  flex: 1 1 auto;
  color: #333;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.af-trace-delta {
  flex: 0 0 auto;
  color: #0a7a3f;
  font-variant-numeric: tabular-nums;
}
.af-hotkeys {
  margin-top: 8px;
  font-size: 11px;
  color: #999;
  text-align: center;
}
.af-field {
  margin-bottom: 10px;
}
.af-label {
  display: block;
  font-size: 12px;
  color: #555;
  margin-bottom: 4px;
}
.af-progress {
  margin-top: 8px;
  font-size: 12px;
  color: #0052d9;
}
</style>
