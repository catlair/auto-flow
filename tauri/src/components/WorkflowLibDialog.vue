<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
import { wfDisplayName, wfSubtitle } from "@/flow/workflow";
import type { WorkflowMeta } from "@/flow/workflow";
import { confirmDanger } from "@/utils/confirm";

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{ (e: "update:visible", v: boolean): void }>();

const store = useAppStore();
const busy = ref(false);
const editingId = ref("");
const draft = ref("");

/**
 * 改名输入框的句柄。
 *
 * 用**函数式 ref** 而不是 `ref="nameInput"`：`ref` 写在 `v-for` 里时 Vue 会把
 * 它收集成**数组**，`nameInput.value.focus()` 于是报「focus 不是函数」，
 * 而症状只是「点更名之后光标没进输入框」——很难联想到是 ref 的形态问题。
 * 这里也不放进 `ref()`：拿到的就是个 DOM 元素，不需要响应式。
 */
let nameInputEl: any = null;
const setNameInput = (el: any) => {
  nameInputEl = el;
};

/** 见 WorkflowMenu.vue 的同名常量：后端对「有未保存的改动」的固定错误码。 */
const UNSAVED_CHANGES = -32006;

const items = computed(() => store.workflowLib);
const currentId = computed(() => store.workflowLibCurrent);

watch(
  () => props.visible,
  (v) => {
    if (!v) {
      editingId.value = "";
      return;
    }
    void refresh();
  }
);

async function refresh() {
  try {
    await store.refreshWorkflowLib();
  } catch (e) {
    MessagePlugin.error("读取工作流库失败：" + errMessage(e));
  }
}

/**
 * 一次库操作的全过程：防重入 + 统一报错。返回是否成功。
 *
 * 防重入不能省：双击「删除」会发两次请求，第二次回「工作流不存在」——
 * 用户看到的是「明明只删了一次，却弹了个错」。
 */
async function withBusy(label: string, fn: () => Promise<void>): Promise<boolean> {
  if (busy.value) return false;
  busy.value = true;
  try {
    await fn();
    return true;
  } catch (e) {
    MessagePlugin.error(`${label}失败：${errMessage(e)}`);
    return false;
  } finally {
    busy.value = false;
  }
}

function isUnsaved(e: unknown): boolean {
  return (e as any)?.code === UNSAVED_CHANGES;
}

/**
 * 「打开 / 运行」前统一处理未保存的改动。
 *
 * 后端是**拒掉**（-32006）而不是静默丢弃——这里负责把那个错误码翻译成一个人话
 * 确认框，用户确认后再带 force 重试一次。判断留在后端的原因见
 * `rpc/controller.py::_guard_unsaved`：前端漏了就会静默丢掉一整张图。
 */
async function withUnsavedGuard(
  fn: (force: boolean) => Promise<void>,
  what: string
): Promise<boolean> {
  try {
    await fn(false);
    return true;
  } catch (e) {
    if (!isUnsaved(e)) throw e;
    const ok = await confirmDanger({
      header: "有未保存的改动",
      body: `当前「${wfDisplayName(store.workflow.name)}」有未保存的改动，${what}会丢弃它们。`,
      confirmBtn: "丢弃并继续",
    });
    if (!ok) return false;
    await fn(true);
    return true;
  }
}

function onOpen(m: WorkflowMeta) {
  void withBusy("打开", async () => {
    const done = await withUnsavedGuard(
      (f) => store.openWorkflowFromLib(m.id, f),
      `打开「${m.name}」`
    );
    if (done) MessagePlugin.success(`已打开「${m.name}」`);
  });
}

function onRun(m: WorkflowMeta) {
  void withBusy("运行", async () => {
    // 运行中不许切：`toggleRun` 会把它当「停止」处理，于是用户以为在启动
    // 另一份，实际把正在跑的这份停了。store 里也有同一道闸，这里先说清楚。
    if (store.running) {
      MessagePlugin.warning("正在运行中，请先停止再运行另一份工作流");
      return;
    }
    const done = await withUnsavedGuard(
      (f) => store.runWorkflowFromLib(m.id, f),
      `运行「${m.name}」`
    );
    if (!done) return;
    // 关掉弹层：运行是要**看着画布**的，留着弹层挡在前面等于什么都没发生。
    emit("update:visible", false);
    MessagePlugin.success(`正在运行「${m.name}」`);
  });
}

async function onStartRename(m: WorkflowMeta) {
  editingId.value = m.id;
  draft.value = m.name;
  await nextTick();
  nameInputEl?.focus?.();
}

async function onRename(m: WorkflowMeta) {
  // 回车与失焦都会走到这里；第一次已经收工时 editingId 已清空，直接退出。
  // 少了这一句，一次改名会发两次请求。
  if (editingId.value !== m.id) return;
  const name = draft.value.trim();
  editingId.value = "";
  if (!name || name === m.name) return;
  if (await withBusy("更名", () => store.renameWorkflowInLib(m.id, name))) {
    MessagePlugin.success("已更名");
  }
}

async function onDelete(m: WorkflowMeta) {
  const isCurrent = m.id === currentId.value;
  const ok = await confirmDanger({
    header: "删除工作流",
    // 说清两件事：文件去哪了（能捞回来）、画布会怎样（内容留着但不再对应文件）。
    body:
      `「${m.name}」会被移到废纸篓，可以从 Finder 的废纸篓恢复。` +
      (isCurrent
        ? "\n它正是当前打开的这一份：画布上的内容会保留，但不再对应任何文件，再按「保存」会新建一条。"
        : ""),
    confirmBtn: "移到废纸篓",
  });
  if (!ok) return;
  if (await withBusy("删除", () => store.deleteWorkflowFromLib(m.id))) {
    MessagePlugin.success("已移到废纸篓");
  }
}

async function onExport(m: WorkflowMeta) {
  let out = "";
  const ok = await withBusy("导出", async () => {
    out = await store.exportWorkflow(m.id);
  });
  if (ok && out) MessagePlugin.success("已导出到 " + out);
}

async function onImport() {
  let id = "";
  const ok = await withBusy("导入", async () => {
    id = await store.importWorkflowFromFile();
  });
  // 用户在文件对话框里点了取消 → 静默返回，不报错、也不提示
  if (ok && id) MessagePlugin.success("已导入到工作流库");
}
</script>

<template>
  <t-dialog
    :visible="visible"
    header="工作流库"
    width="680px"
    :footer="false"
    @close="() => emit('update:visible', false)"
  >
    <div class="af-wflib">
      <div class="af-wflib-head">
        <span class="af-wflib-count">{{ items.length }} 份</span>
        <span class="af-wflib-dir" :title="store.workflowLibDir">{{ store.workflowLibDir }}</span>
        <span style="flex: 1" />
        <t-button size="small" variant="outline" :disabled="busy" @click="onImport">
          导入…
        </t-button>
        <t-button size="small" variant="text" :disabled="busy" @click="refresh">刷新</t-button>
      </div>

      <!-- 当前这份的状态：在库里是哪一条、有没有没存下来的改动 -->
      <div class="af-wflib-cur">
        <span v-if="currentId">当前：<b>{{ wfDisplayName(store.workflow.name) }}</b></span>
        <span v-else>当前这份<b>还没入库</b>（按「保存」就会自动建一条）。</span>
        <span v-if="store.workflowDirty" class="af-wflib-dirty">● 有未保存的改动</span>
      </div>

      <div v-if="!items.length" class="af-wflib-empty">
        库还是空的。先「新建」再连好节点，然后按「保存」；也可以「导入」一份现成的。
      </div>

      <div v-for="m in items" :key="m.id" :class="['af-wflib-row', { on: m.id === currentId }]">
        <div class="af-wflib-main">
          <t-input
            v-if="editingId === m.id"
            :ref="setNameInput"
            v-model="draft"
            size="small"
            @keydown.enter.prevent="onRename(m)"
            @keydown.esc.prevent="editingId = ''"
            @blur="onRename(m)"
          />
          <template v-else>
            <div class="af-wflib-name" :title="m.id">
              {{ m.name }}
              <span v-if="m.id === currentId" class="af-wflib-badge">当前</span>
            </div>
            <div class="af-wflib-sub">{{ wfSubtitle(m) }}</div>
          </template>
        </div>
        <t-space size="small">
          <t-button size="small" theme="primary" variant="text" :disabled="busy" @click="onRun(m)">
            运行
          </t-button>
          <t-button size="small" variant="text" :disabled="busy" @click="onOpen(m)">打开</t-button>
          <t-button size="small" variant="text" :disabled="busy" @click="onStartRename(m)">
            更名
          </t-button>
          <t-button size="small" variant="text" :disabled="busy" @click="onExport(m)">
            导出
          </t-button>
          <t-button size="small" theme="danger" variant="text" :disabled="busy" @click="onDelete(m)">
            删除
          </t-button>
        </t-space>
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.af-wflib-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.af-wflib-count {
  font-size: 13px;
  font-weight: 600;
}
.af-wflib-dir {
  font-size: 11px;
  color: #aaa;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.af-wflib-cur {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding: 6px 8px;
  background: #f7f8fa;
  border-radius: 4px;
  font-size: 12px;
  color: #555;
}
.af-wflib-dirty {
  color: #e37318;
}
.af-wflib-empty {
  margin-top: 10px;
  padding: 16px 8px;
  text-align: center;
  font-size: 12px;
  color: #999;
}
.af-wflib-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px dashed #f0f1f3;
}
.af-wflib-row.on {
  background: #f5f8ff;
}
.af-wflib-main {
  flex: 1;
  min-width: 0;
}
.af-wflib-name {
  font-size: 13px;
  color: #333;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.af-wflib-badge {
  margin-left: 4px;
  font-size: 10px;
  color: #0052d9;
  border: 1px solid #b9d0f5;
  border-radius: 8px;
  padding: 0 5px;
}
.af-wflib-sub {
  font-size: 10px;
  color: #aaa;
}
</style>
