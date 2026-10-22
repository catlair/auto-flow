<script setup lang="ts">
import { computed, ref } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
import { wfTitle } from "@/flow/workflow";
import { confirmDanger } from "@/utils/confirm";
import WorkflowLibDialog from "@/components/WorkflowLibDialog.vue";
const store = useAppStore();

/**
 * 后端对「有未保存的改动」的固定错误码（`rpc/controller.py::_guard_unsaved`）。
 *
 * 界面必须认得它并弹确认框，不能把它当成普通失败直接报错——那样用户会发现
 * 「新建」和「打开」在改过东西之后**莫名其妙就点不动了**，而提示文字
 * （unsaved_changes）也读不出该怎么办。
 */
const UNSAVED_CHANGES = -32006;

const libVisible = ref(false);
const title = computed(() => wfTitle(store.workflow.name, store.workflowDirty));
const titleHint = computed(() =>
  store.workflowDirty
    ? `${store.workflow.name}（有未保存的改动）`
    : `${store.workflow.name}${store.workflowPath ? "\n" + store.workflowPath : ""}`
);

function isUnsaved(e: unknown): boolean {
  return (e as any)?.code === UNSAVED_CHANGES;
}

async function onNew() {
  try {
    await store.newWorkflow();
  } catch (e) {
    if (!isUnsaved(e)) {
      MessagePlugin.error("新建失败：" + errMessage(e));
      return;
    }
    const ok = await confirmDanger({
      header: "有未保存的改动",
      body: `「${store.workflow.name}」有未保存的改动，新建会丢弃它们。`,
      confirmBtn: "丢弃并新建",
    });
    if (!ok) return;
    await store.newWorkflow(true);
  }
  MessagePlugin.success("已新建工作流");
}

/**
 * 从磁盘重新读取当前这份。
 *
 * 改延迟、挪坐标是编辑器的日常，但文件的真源在磁盘上、App 不会自动重读——
 * 以前每调一个参数都得「另存为新的一条」再切过去，两天攒了二十个版本。
 * 有未保存的改动时后端会拦（-32006），这里弹确认框再 force。
 */
async function onReload() {
  if (!store.workflowPath) {
    MessagePlugin.info("当前工作流还没保存过，没有可重新读取的文件");
    return;
  }
  try {
    await store.reloadWorkflow();
    MessagePlugin.success("已从磁盘重新读取");
  } catch (e) {
    if (!isUnsaved(e)) {
      MessagePlugin.error("重新加载失败：" + errMessage(e));
      return;
    }
    const ok = await confirmDanger({
      header: "有未保存的改动",
      body: `「${store.workflow.name}」有未保存的改动，重新加载会丢弃它们。`,
      confirmBtn: "丢弃并重新加载",
    });
    if (!ok) return;
    await store.reloadWorkflow(true);
    MessagePlugin.success("已从磁盘重新读取");
  }
}

async function onSave() {
  try {
    const first = !store.workflowLibCurrent;
    await store.saveWorkflow();
    MessagePlugin.success(first ? "已保存到工作流库" : "已保存");
  } catch (e) {
    MessagePlugin.error("保存失败：" + errMessage(e));
  }
}
</script>

<template>
  <t-space size="small" align="center">
    <t-button size="small" variant="text" @click="onNew">新建</t-button>
    <t-button size="small" variant="text" @click="libVisible = true">工作流库</t-button>
    <t-button size="small" variant="text" @click="onSave">保存</t-button>
    <t-button size="small" variant="text" title="改完磁盘上的 JSON 后点这里，不用另存新的一条" @click="onReload">重新加载</t-button>
    <!--
      名字 + 未保存标记。标记是名字后面的「●」而不是一个独立的小圆点：
      6 像素的点在浅色标题栏上很容易被当成渲染瑕疵，而用户读的是这行字。
    -->
    <span :class="['af-wf', { dirty: store.workflowDirty }]" :title="titleHint">{{ title }}</span>
  </t-space>
  <WorkflowLibDialog v-model:visible="libVisible" />
</template>

<style scoped>
.af-wf {
  font-size: 12px;
  color: #666;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.af-wf.dirty {
  color: #e37318;
}
</style>
