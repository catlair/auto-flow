<script setup lang="ts">
import { computed, ref, onBeforeUnmount } from "vue";
import { useAppStore } from "@/stores/app";
import { open } from "@tauri-apps/plugin-dialog";
import type { ParamDef } from "@/rpc/types";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const store = useAppStore();
const capturing = ref(false);
let unsub: (() => void) | null = null;

const selected = computed(() => store.selectedNode);
const allParams = computed<ParamDef[]>(() => {
  if (!selected.value) return [];
  const def = store.nodeDefsByName[selected.value.type];
  if (!def) return [];
  return [...def.params, ...def.common_params];
});

function curValue(p: ParamDef): any {
  const node = selected.value;
  if (!node) return p.default;
  return node.params[p.key] !== undefined ? node.params[p.key] : p.default;
}

async function setVal(p: ParamDef, v: any) {
  const idx = store.selectedIndex;
  if (idx < 0) return;
  try {
    await store.setParam(idx, p.key, v);
  } catch (e) {
    MessagePlugin.error("设置参数失败：" + errMessage(e));
  }
}

async function pickFile(p: ParamDef) {
  const path = await open({ title: "选择文件" });
  if (typeof path === "string") await setVal(p, path);
}

async function startCapture(p: ParamDef) {
  capturing.value = true;
  unsub = store.onCapturedKey((name) => {
    setVal(p, name);
    capturing.value = false;
    unsub?.();
    unsub = null;
    store.stopCapture().catch(() => {});
  });
  try {
    await store.startCapture();
  } catch (e) {
    MessagePlugin.error("开始捕获失败：" + errMessage(e));
    capturing.value = false;
  }
}

onBeforeUnmount(() => unsub?.());
</script>

<template>
  <div>
    <div class="af-panel-title">参数</div>
    <div v-if="!selected" class="af-empty">未选择节点。</div>
    <div v-else class="af-form">
      <div v-for="p in allParams" :key="p.key" class="af-field">
        <label class="af-label">{{ p.label }}</label>

        <t-input-number
          v-if="p.ptype === 'int' || p.ptype === 'float'"
          :value="curValue(p)"
          :step="p.ptype === 'float' ? 0.1 : 1"
          @change="(v: number) => setVal(p, v)"
          size="small"
          theme="column"
        />

        <t-switch
          v-else-if="p.ptype === 'bool'"
          :value="!!curValue(p)"
          @change="(v: boolean) => setVal(p, v)"
        />

        <t-select
          v-else-if="p.ptype === 'select'"
          :value="curValue(p)"
          @change="(v: string) => setVal(p, v)"
          size="small"
        >
          <t-option v-for="o in p.options || []" :key="o" :value="o" :label="o" />
        </t-select>

        <div v-else-if="p.ptype === 'file'" style="display: flex; gap: 6px">
          <t-input :value="curValue(p)" size="small" readonly placeholder="未选择" />
          <t-button size="small" variant="outline" @click="pickFile(p)">选择</t-button>
        </div>

        <div v-else-if="p.ptype === 'keys'" style="display: flex; gap: 6px">
          <t-input :value="curValue(p)" size="small" placeholder="点击捕获" />
          <t-button
            size="small"
            :theme="capturing ? 'warning' : 'default'"
            @click="startCapture(p)"
            >{{ capturing ? "捕获中…" : "捕获" }}</t-button
          >
        </div>

        <t-input
          v-else
          :value="curValue(p)"
          size="small"
          @change="(v: string) => setVal(p, v)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.af-empty {
  color: #999;
  font-size: 13px;
}
.af-field {
  margin-bottom: 10px;
}
.af-label {
  display: block;
  font-size: 12px;
  color: #555;
  margin-bottom: 4px;
}
</style>
