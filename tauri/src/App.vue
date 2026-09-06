<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAppStore } from "@/stores/app";
import WorkflowMenu from "@/components/WorkflowMenu.vue";
import PermissionBanner from "@/components/PermissionBanner.vue";
import NodeList from "@/components/NodeList.vue";
import ParamsPanel from "@/components/ParamsPanel.vue";
import RunPanel from "@/components/RunPanel.vue";
import RecordPanel from "@/components/RecordPanel.vue";
import ScheduleDialog from "@/components/ScheduleDialog.vue";

import { computed } from "vue";
const store = useAppStore();
const scheduleOpen = ref(false);
const selectedTitle = computed(() => {
  const n = store.selectedNode;
  if (!n) return "未选择节点";
  const custom = (n as any).name as string | undefined;
  const type = store.nodeDefsByName[n.type]?.name ?? n.type;
  return custom?.trim() ? `${custom}（${type}）` : type;
});

onMounted(() => store.init());
</script>

<template>
  <header class="af-header">
    <span class="af-brand">Auto Flow</span>
    <WorkflowMenu />
    <span :class="['af-conn', store.connected ? 'ok' : 'bad']">
      {{ store.connected ? "● 已连接" : "○ 未连接" }}
    </span>
    <span v-if="store.appVersion" class="af-ver">v{{ store.appVersion }}</span>
    <span style="flex: 1" />
    <span class="af-hk">F9 录制 · F10 运行 · F11 取点</span>
    <t-button size="small" variant="outline" @click="scheduleOpen = true">定时运行</t-button>
  </header>

  <PermissionBanner />

  <div v-if="store.banner" :class="['af-banner', store.bannerKind]">
    {{ store.banner }}
  </div>

  <div class="af-layout">
    <section class="af-col">
      <div class="af-panel-head"><b>工作流</b><span class="af-hint">拖拽排序 · 双击节点改名</span></div>
      <NodeList />
    </section>
    <section class="af-col">
      <div class="af-panel-head"><b>参数</b><span class="af-hint">{{ selectedTitle }}</span></div>
      <ParamsPanel />
    </section>
    <section class="af-col">
      <div class="af-panel-head"><b>运行</b></div>
      <RunPanel />
      <div class="af-panel-head" style="margin-top: 12px"><b>录制</b></div>
      <RecordPanel />
    </section>
  </div>

  <footer class="af-status">
    <span>{{ store.running ? "▶ 运行中" : store.recording ? "● 录制中" : "就绪" }}</span>
    <span style="flex: 1" />
    <span v-if="store.running && store.runProgress.total > 0">
      进度 {{ store.runProgress.done }}/{{ store.runProgress.total }}
    </span>
  </footer>

  <ScheduleDialog v-model:visible="scheduleOpen" />
</template>

<style scoped>
.af-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid #e7e7e7;
  background: #fff;
}
.af-brand {
  font-weight: 700;
  font-size: 15px;
  letter-spacing: 0.5px;
}
.af-hk {
  font-size: 11px;
  color: #999;
  background: #f5f6f8;
  border-radius: 10px;
  padding: 3px 10px;
}
.af-layout {
  display: grid;
  grid-template-columns: minmax(240px, 3fr) minmax(240px, 3fr) minmax(280px, 3.4fr);
  gap: 10px;
  padding: 10px 12px;
  height: calc(100vh - 90px);
  overflow: hidden;
}
.af-col {
  background: #fff;
  border: 1px solid #eceef1;
  border-radius: 10px;
  padding: 10px 12px;
  overflow-y: auto;
}
.af-panel-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 10px;
}
.af-panel-head b {
  font-size: 14px;
}
.af-hint {
  font-size: 11px;
  color: #aaa;
}
.af-status {
  display: flex;
  gap: 10px;
  padding: 5px 14px;
  border-top: 1px solid #eceef1;
  background: #fafbfc;
  font-size: 12px;
  color: #666;
}
.af-conn.ok {
  color: #059669;
  font-size: 13px;
}
.af-conn.bad {
  color: #e34d59;
  font-size: 13px;
}
.af-ver {
  color: #888;
  font-size: 12px;
}
.af-proto-warn {
  color: #ed7b2f;
  font-size: 12px;
}
</style>
