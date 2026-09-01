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

const store = useAppStore();
const scheduleOpen = ref(false);

onMounted(() => store.init());
</script>

<template>
  <header class="af-header">
    <WorkflowMenu />
    <span :class="['af-conn', store.connected ? 'ok' : 'bad']">
      {{ store.connected ? "● 已连接" : "○ 未连接" }}
    </span>
    <span v-if="store.appVersion" class="af-ver">v{{ store.appVersion }}</span>
    <span v-if="!store.protocolOk" class="af-proto-warn">⚠ 协议不匹配</span>
    <span style="flex: 1" />
    <t-button size="small" variant="outline" @click="scheduleOpen = true">定时运行</t-button>
  </header>

  <PermissionBanner />

  <div v-if="store.banner" :class="['af-banner', store.bannerKind]">
    {{ store.banner }}
  </div>

  <div class="af-layout">
    <div class="af-col">
      <NodeList />
    </div>
    <div class="af-col">
      <ParamsPanel />
    </div>
    <div class="af-col">
      <RunPanel />
      <t-divider />
      <RecordPanel />
    </div>
  </div>

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
