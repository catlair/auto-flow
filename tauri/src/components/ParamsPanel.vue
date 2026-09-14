<script setup lang="ts">
import { computed, ref, onBeforeUnmount, onMounted } from "vue";
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

/**
 * 按 `show_if` 过滤后的可见字段。
 *
 * 分支节点有 6 组 case 字段，全平铺出来是一堵墙，而用户通常只用 2~3 个。
 * `show_if: {key: "case_count", gte: N}` 让第 N 组只在 case 数够时才出现。
 */
const visibleParams = computed<ParamDef[]>(() =>
  allParams.value.filter((p) => passesShowIf(p))
);

/**
 * 取某个参数的**生效值**：节点上没设过就用 ParamDef 的默认值。
 *
 * 这一步不能省。`show_if` 依赖 `case_count`，而节点刚添加时 params 里
 * 根本没有这个 key——直接读 `node.params[key]` 会得到 undefined，
 * `undefined >= 1` 为 false，于是**所有** case 字段都不显示，用户看到的是
 * 一个只剩「情形个数」的分支节点。默认值正是后端实际会用的值。
 */
function paramValue(key: string): any {
  const node = selected.value;
  if (!node) return undefined;
  const raw = node.params?.[key];
  if (raw !== undefined) return raw;
  return allParams.value.find((d) => d.key === key)?.default;
}

function passesShowIf(p: ParamDef): boolean {
  const c = p.show_if;
  if (!c) return true;
  const v = paramValue(c.key);
  if (c.gte !== undefined) return Number(v) >= c.gte;
  if (c.lte !== undefined) return Number(v) <= c.lte;
  if (c.eq !== undefined) return v === c.eq;
  return !!v;
}

function curValue(p: ParamDef): any {
  const node = selected.value;
  if (!node) return p.default;
  return node.params[p.key] !== undefined ? node.params[p.key] : p.default;
}

async function setVal(p: ParamDef, v: any) {
  // 按 uid 送，不送下标：参数面板是**异步**交互（改一个值 → 等响应 → 再刷新），
  // 期间选中项或节点列表都可能变。用下标的话「用户改的是 A 的参数，实际写进了
  // B」——而且不报错，参数面板显示的又是 A 的值，看不出哪里不对。
  const uid = selected.value?.uid;
  if (!uid) return;
  try {
    await store.setParam(uid, p.key, v);
  } catch (e) {
    MessagePlugin.error("设置参数失败：" + errMessage(e));
  }
}

async function pickFile(p: ParamDef) {
  const path = await open({ title: "选择文件" });
  if (typeof path === "string") await setVal(p, path);
}

function eventsLabel(v: any): string {
  if (Array.isArray(v)) return `${v.length} 个事件`;
  if (v && typeof v === "object" && typeof v.count === "number")
    return `${v.count} 个事件`;
  return "0 个事件";
}

// 「截取」：后端起系统框选截图，完成经 template.snipped 通知回填本字段
const snipping = ref(false);
let snipTarget: ParamDef | null = null;
onMounted(() => {
  store.onNotifyRaw((n) => {
    if (n.method !== "template.snipped") return;
    snipping.value = false;
    if (n.params?.ok && snipTarget) {
      setVal(snipTarget, n.params.path);
      MessagePlugin.success("模板已截取");
    } else if (!n.params?.ok) {
      MessagePlugin.warning("已取消截取");
    }
    snipTarget = null;
  });
});
async function snipTemplate(p: ParamDef) {
  try {
    snipTarget = p;
    snipping.value = true;
    await store.snipTemplate();
  } catch (e) {
    snipping.value = false;
    snipTarget = null;
    MessagePlugin.error("截取失败：" + errMessage(e));
  }
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
    <div v-if="!selected" class="af-empty">未选择节点。</div>
    <div v-else class="af-form">
      <div v-for="p in visibleParams" :key="p.key" class="af-field">
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

        <t-input
          v-else-if="p.ptype === 'events'"
          :value="eventsLabel(curValue(p))"
          size="small"
          readonly
        />

        <div v-else-if="p.ptype === 'file'" style="display: flex; gap: 6px">
          <t-input :value="curValue(p)" size="small" readonly placeholder="未选择" />
          <t-button size="small" variant="outline" @click="pickFile(p)">选择</t-button>
          <t-button
            size="small"
            variant="outline"
            :disabled="snipping"
            @click="snipTemplate(p)"
            >{{ snipping ? "框选中…" : "截取" }}</t-button
          >
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

        <!-- `pick`：文本框旁边挂个「选择」。
             用于「既可能是模板图路径、也可能是一段文字」的字段——`file` 类型是
             只读的，装不下文字，所以只能给可编辑文本框加选择按钮。 -->
        <div v-else-if="p.pick" style="display: flex; gap: 6px">
          <t-input
            :value="curValue(p)"
            size="small"
            @change="(v: string) => setVal(p, v)"
          />
          <t-button size="small" variant="outline" @click="pickFile(p)">选择</t-button>
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
