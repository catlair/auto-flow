<script setup lang="ts">
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const store = useAppStore();

async function onToggleRecord() {
  try {
    await store.toggleRecord();
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
    <div class="af-panel-title">录制</div>
    <t-button
      block
      :theme="store.recording ? 'warning' : 'primary'"
      @click="onToggleRecord"
    >
      {{ store.recording ? "停止录制" : "开始录制" }}
    </t-button>
    <t-button
      block
      variant="outline"
      style="margin-top: 8px"
      :disabled="store.recording || store.lastRecordCount === 0"
      @click="onToNode"
    >
      写入节点{{ store.lastRecordCount ? `（${store.lastRecordCount} 事件）` : "" }}
    </t-button>
    <div class="af-stream">
      <div v-for="(ev, i) in store.recordBuffer.slice(-200)" :key="i" class="af-ev">
        {{ ev.kind }} @ {{ ev.x ?? "-" }},{{ ev.y ?? "-" }} {{ ev.button ? ev.button : "" }}
        {{ ev.ts_ms }}ms
      </div>
      <div v-if="!store.recordBuffer.length" class="af-empty">暂无事件</div>
    </div>
  </div>
</template>

<style scoped>
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
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.af-empty {
  color: #999;
}
</style>
