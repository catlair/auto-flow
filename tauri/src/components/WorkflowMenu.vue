<script setup lang="ts">
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

const store = useAppStore();

async function onNew() {
  await store.newWorkflow();
  MessagePlugin.success("已新建工作流");
}
async function onOpen() {
  try {
    await store.loadWorkflow();
  } catch (e) {
    MessagePlugin.error("打开失败：" + (e as Error).message);
  }
}
async function onSave() {
  try {
    await store.saveWorkflow();
    MessagePlugin.success("已保存");
  } catch (e) {
    MessagePlugin.error("保存失败：" + (e as Error).message);
  }
}
async function onSaveAs() {
  try {
    await store.saveWorkflowAs();
  } catch (e) {
    MessagePlugin.error("另存失败：" + (e as Error).message);
  }
}
</script>

<template>
  <t-space size="small">
    <t-button size="small" variant="text" @click="onNew">新建</t-button>
    <t-button size="small" variant="text" @click="onOpen">打开</t-button>
    <t-button size="small" variant="text" @click="onSave">保存</t-button>
    <t-button size="small" variant="text" @click="onSaveAs">另存为</t-button>
  </t-space>
</template>
