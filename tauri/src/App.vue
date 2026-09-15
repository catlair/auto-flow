<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
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
import {
  LS_KEY,
  TABS,
  parseSide,
  serializeSide,
  tabAfterSelect,
  widthFromDrag,
  type TabKey,
} from "@/flow/sidebar";
const store = useAppStore();
const scheduleOpen = ref(false);
const diagOpen = ref(false);

/**
 * 右侧侧栏的标签页。
 *
 * 为什么把「参数 / 运行 / 录制」收进标签页而不是三栏并排：三个面板并排时，
 * 画布只能分到约 1/3 宽度，而画布恰恰是**最需要空间**的那个——流程图一宽就
 * 得反复平移缩放。并排还有第二个代价：中栏（参数）经常是空的，空着也得占位。
 *
 * 收进侧栏后画布拿到除侧栏外的全部宽度，参数面板独占整条高度（多路分支这类
 * 长表单不再被压成一小截滚动条）。运行与录制的实时状态靠标签角标提示，
 * 不必为了看状态而切回来。
 */
// 标签页清单、宽度钳制与存取都在 `flow/sidebar.ts` 里（纯逻辑，可直测）。
const initial = parseSide(localStorage.getItem(LS_KEY));
const sideW = ref(initial.w);
const sideOpen = ref(initial.open);
const tab = ref<TabKey>(initial.tab);

/** 侧栏状态持久化：宽度和开合是「这台机器上怎么用」的习惯，不该每次重开都重置。 */
function saveSide() {
  try {
    localStorage.setItem(
      LS_KEY,
      serializeSide({ w: sideW.value, open: sideOpen.value, tab: tab.value })
    );
  } catch {
    /* 隐私模式等场景下写不进去，无所谓 */
  }
}
watch([sideW, sideOpen, tab], saveSide);

// 选中节点 = 要改它的参数。此时还停在「运行」页上只能让用户自己找回来。
watch(
  () => store.selectedIndex,
  (i) => {
    if (i >= 0) tab.value = tabAfterSelect(tab.value, sideOpen.value);
  }
);

/** 拖侧栏左边缘调宽。宽度作用于右侧栏，故向左拖 = 变宽（取负）。 */
function startResize(e: MouseEvent) {
  const startX = e.clientX;
  const startW = sideW.value;
  const move = (ev: MouseEvent) => {
    sideW.value = widthFromDrag(startW, startX, ev.clientX);
  };
  const up = () => {
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
  e.preventDefault();
}

const selectedTitle = computed(() => {
  const n = store.selectedNode;
  if (!n) return "未选择节点";
  const custom = (n as any).name as string | undefined;
  const type = store.nodeDefsByName[n.type]?.name ?? n.type;
  return custom?.trim() ? `${custom}（${type}）` : type;
});

/** 标签角标：别的页签上也能看到「正在跑 / 正在录」。 */
const runBadge = computed(() => (store.running ? "▶" : ""));
const recBadge = computed(() => (store.recording ? "●" : ""));

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
    <section class="af-flow-col">
      <div class="af-panel-head">
        <b>流程图</b>
        <span class="af-hint">
          拖节点移动 · 从右侧圆点拖出连线 · Delete 删除
        </span>
        <span style="flex: 1" />
        <button
          v-if="store.selectedNode"
          class="af-side-btn"
          title="取消选择"
          @click="store.selectNode(-1)"
        >
          取消选择
        </button>
      </div>
      <FlowCanvas />
    </section>

    <aside v-if="sideOpen" class="af-side" :style="{ width: sideW + 'px' }">
      <div class="af-resizer" title="拖动调整宽度" @mousedown="startResize" />
      <div class="af-side-tabs">
        <button
          v-for="t in TABS"
          :key="t.key"
          :class="['af-tab', { on: tab === t.key }]"
          @click="tab = t.key"
        >
          {{ t.label }}
          <span v-if="t.key === 'run' && runBadge" class="af-tab-dot">{{ runBadge }}</span>
          <span v-if="t.key === 'record' && recBadge" class="af-tab-dot">{{ recBadge }}</span>
        </button>
        <span style="flex: 1" />
        <button class="af-side-btn" title="收起侧栏（给画布更多空间）" @click="sideOpen = false">
          ›
        </button>
      </div>
      <div class="af-side-body">
        <!-- 用 v-show 保持挂载：录制面板的文本草稿（textDraft / keysToTextDraft）
             与事件列表的选中项、滚动位置都是组件内状态，切出去看一眼再切回来
             就白填了。侧栏宽度/开合也持久化，理由同上。 -->
        <div v-show="tab === 'params'">
          <div class="af-panel-head">
            <b>参数</b><span class="af-hint">{{ selectedTitle }}</span>
          </div>
          <ParamsPanel />
        </div>
        <div v-show="tab === 'run'">
          <div class="af-panel-head"><b>运行</b></div>
          <RunPanel />
        </div>
        <div v-show="tab === 'record'">
          <div class="af-panel-head"><b>录制</b></div>
          <RecordPanel />
        </div>
      </div>
    </aside>

    <button
      v-else
      class="af-side-expand"
      title="展开侧栏"
      @click="sideOpen = true"
    >
      ‹
    </button>
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
/* 画布 + 右侧栏。画布是 flex:1（吃满剩余宽度），侧栏定宽且可拖。
   不再用等分三栏——那会把最需要宽度的画布压成 1/3。 */
.af-layout {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  height: calc(100vh - 90px);
  overflow: hidden;
}
.af-flow-col {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid #eceef1;
  border-radius: 10px;
  padding: 10px 12px;
  /* 画布这一列自己管滚动：让外层滚动会把画布一起滚走，
     而画布要的是「固定大小 + 内部平移缩放」。 */
  overflow: hidden;
}
.af-side {
  flex: 0 0 auto;
  position: relative;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid #eceef1;
  border-radius: 10px;
  overflow: hidden;
}
/* 左边缘的拖拽把手。做宽一点（6px）但视觉上只有 1px 线，
   命中区比看起来大，拖起来才不费劲。 */
.af-resizer {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 6px;
  margin-left: -3px;
  cursor: col-resize;
  z-index: 2;
}
.af-resizer:hover {
  background: #e8f0fe;
}
.af-side-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 6px 8px;
  border-bottom: 1px solid #eceef1;
  background: #fafbfc;
  flex: 0 0 auto;
}
.af-tab {
  border: none;
  background: none;
  font-family: inherit;
  font-size: 13px;
  color: #666;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
}
.af-tab:hover {
  background: #eef1f5;
}
.af-tab.on {
  background: #e8f0fe;
  color: #1a53c4;
  font-weight: 600;
}
.af-tab-dot {
  font-size: 10px;
  margin-left: 3px;
  color: #e34d59;
}
.af-side-btn {
  border: none;
  background: none;
  font-family: inherit;
  font-size: 13px;
  color: #888;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.af-side-btn:hover {
  background: #eef1f5;
  color: #333;
}
.af-side-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 10px 12px;
}
/* 侧栏收起后贴在右边缘的小把手，点它展开。 */
.af-side-expand {
  flex: 0 0 auto;
  width: 18px;
  border: 1px solid #eceef1;
  border-radius: 8px;
  background: #fff;
  color: #999;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.af-side-expand:hover {
  background: #f5f6f8;
  color: #333;
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
