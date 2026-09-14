<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAppStore } from "@/stores/app";
import WorkflowMenu from "@/components/WorkflowMenu.vue";
import PermissionBanner from "@/components/PermissionBanner.vue";
import FlowCanvas from "@/components/FlowCanvas.vue";
import ParamsPanel from "@/components/ParamsPanel.vue";
import RunPanel from "@/components/RunPanel.vue";
import RecordPanel from "@/components/RecordPanel.vue";
import ScheduleDialog from "@/components/ScheduleDialog.vue";
import DiagnosticsPanel from "@/components/DiagnosticsPanel.vue";

import { computed } from "vue";
const store = useAppStore();
const scheduleOpen = ref(false);
const diagOpen = ref(false);
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
    <t-button size="small" variant="outline" @click="diagOpen = true">诊断</t-button>
    <t-button size="small" variant="outline" @click="scheduleOpen = true">定时运行</t-button>
  </header>

  <PermissionBanner />

  <!-- 后端断连/恢复：与下方 banner 分开。banner 只放「待处理的问题」，
       恢复提示放这里，否则「已恢复」会把真正需要用户处理的错误顶掉。 -->
  <div v-if="store.reconnectNotice" class="af-reconnect">
    <span>{{ store.reconnectNotice }}</span>
    <button class="af-link" @click="diagOpen = true">查看诊断</button>
    <span style="flex: 1" />
    <button class="af-link" @click="store.dismissReconnect()">知道了</button>
  </div>

  <!-- 旧版工作流迁移提示：后端把 v3 的线性列表接成了线性边链，但条件的
       run_when 门控语义无法一对一映射，必须让用户知道要去重连。 -->
  <div v-if="store.migratedNotice" class="af-migrated">
    <span>{{ store.migratedNotice }}</span>
    <span style="flex: 1" />
    <button class="af-link" @click="store.dismissMigrated()">知道了</button>
  </div>

  <div v-if="store.banner" :class="['af-banner', store.bannerKind]">
    {{ store.banner }}
    <!-- 非 ok 的横幅都给一个通往诊断面板的入口：断连详情、运行错误都在那里 -->
    <button
      v-if="store.bannerKind !== 'ok'"
      class="af-link"
      style="margin-left: 8px"
      @click="diagOpen = true"
    >
      诊断
    </button>
  </div>

  <div class="af-layout">
    <section class="af-col af-flow-col">
      <div class="af-panel-head">
        <b>流程图</b><span class="af-hint">拖节点移动 · 从右侧圆点拖出连线 · Delete 删除</span>
      </div>
      <FlowCanvas />
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
  <DiagnosticsPanel v-model:visible="diagOpen" />
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
/* 画布这一列自己管滚动：让 .af-col 滚动会把画布一起滚走，
   而画布要的是「固定大小 + 内部平移缩放」。 */
.af-flow-col {
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
.af-reconnect {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 14px;
  background: #eaf7ef;
  color: #057a4a;
  font-size: 12px;
  border-bottom: 1px solid #cdebd9;
}
.af-migrated {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 14px;
  background: #fff8e6;
  color: #8a5a00;
  font-size: 12px;
  border-bottom: 1px solid #f2e2b5;
}
.af-link {
  border: none;
  background: none;
  padding: 0;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
  font-size: inherit;
  font-family: inherit;
}
</style>
