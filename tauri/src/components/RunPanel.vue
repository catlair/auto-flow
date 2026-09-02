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
async function onToggleRun() {
  try {
    await store.toggleRun();
  } catch (e) {
    MessagePlugin.error("运行失败：" + errMessage(e));
  }
}
</script>

<template>
  <div>
    <div class="af-panel-title">运行</div>
    <div class="af-field">
      <label class="af-label">速度倍率</label>
      <t-input-number :value="speed" :step="0.1" :min="0.1" @change="onSpeed" size="small" theme="column" />
    </div>
    <div class="af-field">
      <label class="af-label">循环次数</label>
      <t-input-number :value="repeat" :step="1" :min="1" @change="onRepeat" size="small" theme="column" />
    </div>
    <div class="af-field">
      <label class="af-label">基点 (F11 取点)</label>
      <div style="display: flex; gap: 6px; align-items: center">
        <t-input-number :value="store.base.x" :step="1" size="small" theme="column" disabled />
        <t-input-number :value="store.base.y" :step="1" size="small" theme="column" disabled />
        <t-button size="small" variant="outline" @click="onPick">取点</t-button>
      </div>
    </div>
    <t-button
      block
      :theme="store.running ? 'warning' : 'primary'"
      @click="onToggleRun"
    >
      {{ store.running ? "停止运行" : "运行" }}
    </t-button>
    <div v-if="store.running" class="af-progress">
      进度 {{ store.runProgress.done }}/{{ store.runProgress.total }}
    </div>
  </div>
</template>

<style scoped>
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
