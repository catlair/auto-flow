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

const total = computed(() => store.recordBuffer.length);
const win = computed(() =>
  computeWindow({
    count: total.value,
    itemH: ITEM_H,
    viewportH: viewportH.value,
    scrollTop: scrollTop.value,
  })
);
const visible = computed(() => store.recordBuffer.slice(win.value.start, win.value.end));

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
watch(total, () => {
  if (!follow.value) return;
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
      已录 {{ store.lastRecordInfo.count }} 条事件<template v-if="store.lastRecordInfo.filtered">
        · 阈值过滤微移动 {{ store.lastRecordInfo.filtered }} 条</template><template v-if="store.lastRecordInfo.limit_dropped">
        · 超限丢弃 {{ store.lastRecordInfo.limit_dropped }} 条</template><template v-if="store.lastRecordInfo.mouse_died || store.lastRecordInfo.kb_died">
        · <span style="color:#e34d59">监听中断</span></template>
    </div>
    <div v-if="total" class="af-stream-bar">
      <span>共 {{ total }} 条</span>
      <span v-if="total > win.end || win.start > 0" class="af-range">
        显示 {{ win.start + 1 }}–{{ win.end }}
      </span>
      <span style="flex: 1" />
      <button v-if="!follow" class="af-jump" @click="scrollToBottom">↓ 回到最新</button>
    </div>
    <div
      ref="streamEl"
      class="af-stream"
      :class="{ live: store.recording }"
      @scroll.passive="onScroll"
    >
      <div class="af-vlist" :style="{ height: win.totalH + 'px' }">
        <div class="af-vwin" :style="{ transform: `translateY(${win.offsetY}px)` }">
          <div
            v-for="(ev, i) in visible"
            :key="win.start + i"
            class="af-ev"
            :style="{ height: ITEM_H + 'px' }"
          >
            {{ ev.kind }} @ {{ ev.x ?? "-" }},{{ ev.y ?? "-" }} {{ ev.button ? ev.button : "" }}
            {{ ev.ts_ms }}ms
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
