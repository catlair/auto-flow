<script setup lang="ts">
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const store = useAppStore();

async function onToggleRecord() {
  try {
    await store.toggleRecord();
    const s = store.lastRecordInfo;
    if (s?.mouse_died || s?.kb_died) {
      MessagePlugin.warning(
        (s.mouse_died ? "鼠标" : "") + (s.mouse_died && s.kb_died ? "、" : "") +
        (s.kb_died ? "键盘" : "") + "监听中途停止，死亡时刻后的事件未记录"
      );
    } else if (s?.stopped_by_limit) {
      MessagePlugin.warning(`事件数达到上限，超限丢弃 ${s.limit_dropped ?? 0} 条`);
    } else if (s && (s.captured ?? 0) - (s.filtered ?? 0) - (s.limit_dropped ?? 0) !== (s.count ?? 0)) {
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
    <div v-if="store.lastRecordInfo" class="af-rec-stats">
      已录 {{ store.lastRecordInfo.count }} 条事件<template v-if="store.lastRecordInfo.filtered">
        · 阈值过滤微移动 {{ store.lastRecordInfo.filtered }} 条</template><template v-if="store.lastRecordInfo.limit_dropped">
        · 超限丢弃 {{ store.lastRecordInfo.limit_dropped }} 条</template><template v-if="store.lastRecordInfo.mouse_died || store.lastRecordInfo.kb_died">
        · <span style="color:#e34d59">监听中断</span></template>
    </div>
    <div class="af-stream">
      <div v-if="store.recordBuffer.length > 200" class="af-more">
        共 {{ store.recordBuffer.length }} 条，仅显示最近 200 条（回放以完整数据为准）
      </div>
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
.af-rec-stats {
  margin-top: 6px;
  font-size: 11px;
  color: #666;
}
.af-more {
  color: #999;
  font-size: 10px;
  padding-bottom: 2px;
  border-bottom: 1px dashed #e7e7e7;
  margin-bottom: 2px;
}
</style>
