<script setup lang="ts">
/**
 * 置顶日志窗（F-UI-21）。
 *
 * 它**不连后端**：所有内容都由主窗经 Tauri 事件推来（见 `./format.ts`）。
 * 理由是这套日志要回答的是「主窗看到的运行到哪一步了」——主窗才是持有运行态、
 * 运行轨迹、断连信息的那一方。日志窗自己去订阅 `rpc_event` 会变成第二份判断，
 * 两边迟早对不上（而那时用户不知道该信哪个）。
 *
 * 另一个约束：运行期间它是**鼠标穿透**的（由 Rust 的 `set_click_through` 一并设置）。
 * 它常年浮在最上层，若吃点击，回放出来的点击就会打在自己身上——目标应用一次都点不到，
 * 而画面上看起来"正在点"。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import { emit, listen, type UnlistenFn } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import {
  LOG_BACKLOG_EVENT,
  LOG_CAP,
  LOG_EVENT,
  LOG_HELLO_EVENT,
  fmtClock,
  type LogLine,
} from "./format";

const lines = ref<LogLine[]>([]);
const running = ref(false);
const progress = ref({ done: 0, total: 0 });
const hint = ref("");
const list = ref<HTMLElement | null>(null);
/** 用户往回翻时不要把他拽回底部——那是"看日志"最常见的动作。 */
const stick = ref(true);
const unlisteners: UnlistenFn[] = [];

const statusText = computed(() => (running.value ? "运行中" : "空闲"));
const progressText = computed(() =>
  progress.value.total > 0 ? `${progress.value.done}/${progress.value.total}` : ""
);

function atBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 24;
}

async function scrollToEnd() {
  if (!stick.value) return;
  await nextTick();
  const el = list.value;
  if (el) el.scrollTop = el.scrollHeight;
}

/**
 * 收一行。
 *
 * `progress` 不进列表（每步一条会把节点日志冲掉），只更新表头进度；
 * `run.node` / `run.finished` 顺带翻转「运行中」指示灯——日志窗没有别的运行态来源。
 */
function append(l: LogLine) {
  if (l.kind === "progress") {
    const [d, t] = String(l.text).split("/");
    progress.value = { done: Number(d) || 0, total: Number(t) || 0 };
    return;
  }
  if (l.kind === "run.node") running.value = true;
  if (l.kind === "run.finished" || l.kind === "run.error") {
    running.value = false;
    progress.value = { done: 0, total: 0 };
  }
  const el = list.value;
  if (el) stick.value = atBottom(el);
  lines.value.push(l);
  if (lines.value.length > LOG_CAP) {
    lines.value.splice(0, lines.value.length - LOG_CAP);
  }
  void scrollToEnd();
}

function onScroll() {
  const el = list.value;
  if (el) stick.value = atBottom(el);
}

function onClear() {
  lines.value = [];
  hint.value = "";
}

/** 复制全文：日志窗的用途之一是"把这段贴给谁看"，所以必须能整段拿走。 */
async function onCopy() {
  const text = lines.value.map((l) => fmtClock(l.t) + " " + l.text).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    hint.value = `已复制 ${lines.value.length} 行`;
  } catch {
    // WKWebView 里 clipboard 可能被拒（非安全上下文/无用户手势）；
    // 退化为"全选"，用户按 ⌘C 自己拿。
    const el = list.value;
    if (el) {
      const r = document.createRange();
      r.selectNodeContents(el);
      const s = window.getSelection();
      s?.removeAllRanges();
      s?.addRange(r);
    }
    hint.value = "已全选，请按 ⌘C";
  }
}

/** 收起（不是销毁）：窗口留着，下次显示时历史还在。 */
function onHide() {
  try {
    void invoke("af_logwin_hide").catch(() => {});
  } catch {
    /* 无 Tauri 环境：忽略 */
  }
}

onMounted(async () => {
  unlisteners.push(await listen<LogLine>(LOG_EVENT, (e) => append(e.payload)));
  unlisteners.push(
    await listen<LogLine[]>(LOG_BACKLOG_EVENT, (e) => {
      for (const l of e.payload ?? []) append(l);
    })
  );
  // Tauri 事件不缓存、不重放：晚开的窗口看不到开窗前的行，主动要一份历史。
  try {
    await emit(LOG_HELLO_EVENT);
  } catch {
    /* 主窗没起（极少见）：先空着，后续的行照样会来 */
  }
});

onUnmounted(() => {
  unlisteners.forEach((u) => u());
  unlisteners.length = 0;
});
</script>

<template>
  <div class="af-logwin">
    <header class="af-logwin-head" data-tauri-drag-region>
      <span :class="['af-logwin-dot', running ? 'on' : 'off']" />
      <b class="af-logwin-title">运行日志</b>
      <span class="af-logwin-state">{{ statusText }}</span>
      <span v-if="progressText" class="af-logwin-prog">{{ progressText }}</span>
      <span class="af-logwin-grow" />
      <button class="af-logwin-btn" title="复制全部日志" @click="onCopy">复制</button>
      <button class="af-logwin-btn" title="清空列表" @click="onClear">清空</button>
      <button class="af-logwin-btn" title="收起（F10 运行时它不挡鼠标）" @click="onHide">×</button>
    </header>

    <div ref="list" class="af-logwin-list" @scroll="onScroll">
      <div v-for="(l, i) in lines" :key="i" :class="['af-logwin-row', 'lv-' + l.level]">
        <span class="af-logwin-time">{{ fmtClock(l.t) }}</span>
        <span class="af-logwin-text" :title="l.text">{{ l.text }}</span>
      </div>
      <div v-if="!lines.length" class="af-logwin-empty">
        等待运行… 按 F10 开始，这里会逐步显示每个节点。
      </div>
    </div>

    <footer class="af-logwin-foot">
      <span v-if="hint">{{ hint }}</span>
      <span v-else>拖动标题栏移动 · 运行中不挡鼠标</span>
    </footer>
  </div>
</template>

<style scoped>
.af-logwin {
  display: flex;
  flex-direction: column;
  height: 100vh;
  box-sizing: border-box;
  background: #1b1e24;
  color: #d7dbe3;
  font-size: 12px;
  overflow: hidden;
}
/* 无边框窗口没有标题栏，这一条就是"标题栏"：既是拖动把手，也是唯一能放按钮的地方。 */
.af-logwin-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 6px 5px 9px;
  background: #23272f;
  border-bottom: 1px solid #31363f;
  flex: 0 0 auto;
  /* 整条都能拖；按钮自己不带 data-tauri-drag-region，所以点得到。 */
  cursor: default;
}
.af-logwin-title {
  font-size: 12px;
  font-weight: 600;
  color: #eef1f6;
}
.af-logwin-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
}
.af-logwin-dot.on {
  background: #35c46a;
  box-shadow: 0 0 6px #35c46a;
}
.af-logwin-dot.off {
  background: #5b6270;
}
.af-logwin-state {
  font-size: 11px;
  color: #8b93a3;
}
.af-logwin-prog {
  font-size: 11px;
  color: #7fb2ff;
  font-variant-numeric: tabular-nums;
}
.af-logwin-grow {
  flex: 1 1 auto;
}
.af-logwin-btn {
  border: none;
  background: #2d333d;
  color: #b9c1cf;
  font-family: inherit;
  font-size: 11px;
  line-height: 1;
  padding: 4px 7px;
  border-radius: 5px;
  cursor: pointer;
}
.af-logwin-btn:hover {
  background: #3a4250;
  color: #fff;
}
.af-logwin-list {
  flex: 1 1 auto;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 4px 0;
  /* 日志窗自己滚，别让整页跟着滚 */
  overscroll-behavior: contain;
}
.af-logwin-row {
  display: flex;
  gap: 6px;
  align-items: baseline;
  padding: 2px 8px;
  line-height: 1.45;
}
.af-logwin-time {
  flex: 0 0 auto;
  color: #6b7383;
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}
.af-logwin-text {
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
  color: #d7dbe3;
}
/* 级别只改文字颜色，不改行高：行高一变，滚动位置会跳。 */
.lv-ok .af-logwin-text {
  color: #6fdc9b;
}
.lv-warn .af-logwin-text {
  color: #ffcf70;
}
.lv-error .af-logwin-text {
  color: #ff8b8b;
}
.af-logwin-empty {
  padding: 14px 10px;
  color: #6b7383;
  font-size: 11px;
  line-height: 1.6;
}
.af-logwin-foot {
  flex: 0 0 auto;
  padding: 3px 9px;
  border-top: 1px solid #31363f;
  background: #20242b;
  color: #6b7383;
  font-size: 10px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
