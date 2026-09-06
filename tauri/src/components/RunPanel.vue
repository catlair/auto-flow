<script setup lang="ts">
import { ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const store = useAppStore();
const speed = ref(store.workflow.speed);
const repeat = ref(store.workflow.repeat);

watch(
  () => store.workflow,
  (w) => {
    speed.value = w.speed;
    repeat.value = w.repeat;
  }
);

async function onSpeed(v: number) {
  speed.value = v;
  await store.updateWorkflow({ speed: v });
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
      <label class="af-label">基点（F11 取点）</label>
      <div style="display: flex; gap: 6px; align-items: center">
        <t-input-number :value="store.base.x" :step="1" size="small" theme="column" @change="onBaseX" />
        <t-input-number :value="store.base.y" :step="1" size="small" theme="column" @change="onBaseY" />
        <t-button size="small" variant="outline" @click="onPick">取点</t-button>
      </div>
    </div>
    <div v-if="store.running" class="af-run-state">
      <div class="af-run-node">
        <span class="af-dot pulse" /> 运行中：{{ typeLabel(store.runNodeType) || "…" }}
      </div>
      <t-progress
        v-if="store.runProgress.total > 0"
        :percentage="Math.round((store.runProgress.done / store.runProgress.total) * 100)"
        size="small"
        theme="line"
      />
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
