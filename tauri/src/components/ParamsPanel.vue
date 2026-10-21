<script setup lang="ts">
import { computed, ref, onBeforeUnmount, onMounted } from "vue";
import { useAppStore } from "@/stores/app";
import { open } from "@tauri-apps/plugin-dialog";
import type { ParamDef } from "@/rpc/types";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
import {
  DUR_UNITS,
  TIME_FORMAT,
  normalizeTime,
  partOf,
  withPart,
  type DurUnit,
} from "@/flow/duration";
import { recOptions, type RecOption } from "@/flow/recording";
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

// ---- 时长参数（ptype="duration"）：值是一个毫秒总数，面板上摊成时/分/秒/毫秒 ----
// 「等 1 小时 30 分」手算成 5400000 毫秒没人愿意干，四格让用户直接填。
function durPart(v: any, u: DurUnit): number {
  return partOf(v, u);
}

/**
 * 改一格 = 读当前毫秒数 → 换掉这一段 → 写回。**必须串行**。
 *
 * `store.setParam` 是「发 RPC → 等响应 → 回写本地状态」，用户在同一秒里连着改
 * 「分」和「秒」时，第二次 `curValue()` 读到的可能还是**没算上第一次**的旧值
 * （响应还没回来），于是第一次的修改被覆盖——界面上表现为「改完分，分又跳回去了」。
 * 按 `uid:key` 排队，让第二次读值发生在第一次写回之后。
 */
const durChain = new Map<string, Promise<void>>();

function setDurPart(p: ParamDef, u: DurUnit, v: number) {
  const uid = selected.value?.uid;
  if (!uid) return;
  const chainKey = `${uid}:${p.key}`;
  const prev = durChain.get(chainKey) ?? Promise.resolve();
  const next = prev.then(async () => {
    await setVal(p, withPart(curValue(p), u, v));
  });
  durChain.set(chainKey, next);
  // 队尾自我回收，否则用户开过的每个节点都会在 Map 里留一条
  next.finally(() => {
    if (durChain.get(chainKey) === next) durChain.delete(chainKey);
  });
}

// ---- 时刻参数（ptype="time"）：进选择器前先补齐秒 ----
function timeValue(p: ParamDef): string {
  return normalizeTime(curValue(p));
}

// ---- 录制参数（ptype="recording"）：值是一个**录制库 id** ----
/**
 * 下拉选项。**当前值不在库里时也会有一项**（标 `missing`）——否则 t-select 在
 * 「值不在选项里」时显示成空白，看着像「这个字段没填」，而节点其实还引着一个
 * 已经被删掉的录制（问题要到回放时才暴露）。
 */
function recOpts(p: ParamDef): RecOption[] {
  return recOptions(store.recordingLib, curValue(p));
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

        <!-- 时长：时 / 分 / 秒 / 毫秒 四格，值仍是**一个毫秒总数**（后端 delay 读 `ms`）。
             四格只是渲染层，换算在 flow/duration.ts 里，已保存的流程零迁移。 -->
        <div v-else-if="p.ptype === 'duration'" class="af-duration">
          <div v-for="u in DUR_UNITS" :key="u.key" class="af-duration-cell">
            <t-input-number
              :value="durPart(curValue(p), u)"
              :min="0"
              :max="u.max"
              :step="1"
              @change="(v: number) => setDurPart(p, u, v)"
              size="small"
              theme="column"
            />
            <span class="af-duration-unit">{{ u.label }}</span>
          </div>
        </div>

        <!-- 时刻：只能**选**不能手打（allowInput=false）。手输 "9:3"、"９点" 这类
             会一路走到后端 _seconds_until 才抛 ValueError（进 run.error），
             选择器从源头消掉这种输入。 -->
        <t-time-picker
          v-else-if="p.ptype === 'time'"
          :value="timeValue(p)"
          :format="TIME_FORMAT"
          :clearable="false"
          :allow-input="false"
          size="small"
          @change="(v: string) => setVal(p, v)"
        />

        <!-- 录制：从**录制库**里选一条（值存的是 id，节点不复制事件）。
             选项是「名字（N 条）」——只显示名字的话，两段同名录制（同一秒录了两次、
             或复制过库目录）在下拉里完全一样，选哪个全靠猜。 -->
        <div v-else-if="p.ptype === 'recording'" class="af-rec-pick">
          <t-select
            :value="curValue(p)"
            :placeholder="recOpts(p).length ? '选择录制' : '录制库还是空的'"
            size="small"
            style="flex: 1; min-width: 0"
            @change="(v: string) => setVal(p, v)"
          >
            <t-option
              v-for="o in recOpts(p)"
              :key="o.value"
              :value="o.value"
              :label="o.label"
            />
          </t-select>
          <t-button
            size="small"
            variant="outline"
            title="到侧栏「录制」页的录制库里管理"
            @click="store.refreshRecordingLib()"
            >刷新</t-button
          >
        </div>

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
/* 四格横排，等宽；单位名放格子下方而不是上方，省一行高度 */
.af-duration {
  display: flex;
  gap: 6px;
}
.af-duration-cell {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}
.af-duration-cell :deep(.t-input-number) {
  width: 100%;
}
.af-duration-unit {
  font-size: 11px;
  color: #888;
  line-height: 1;
}
/* 录制下拉 + 刷新按钮：下拉吃掉剩余宽度，按钮窄且不换行 */
.af-rec-pick {
  display: flex;
  gap: 6px;
}
</style>
