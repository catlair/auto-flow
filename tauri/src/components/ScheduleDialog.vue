<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { open } from "@tauri-apps/plugin-dialog";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{ (e: "update:visible", v: boolean): void }>();

const store = useAppStore();
const mode = ref("每天时刻");
const atTime = ref("09:00");
const intervalMin = ref(30);
const path = ref("");
const enabled = ref(false);

/**
 * 库里一条的绝对路径。
 *
 * 定时配置存的是**路径**（`schedule.workflowPath`，后端 `_schedule_fire` 直接
 * `Workflow.load(path)`），而库里一条就是一个 json 文件，所以路径天然能对上——
 * 不需要为「库」再发明一套 id 寻址。
 */
function libPath(id: string): string {
  const d = store.workflowLibDir;
  return d ? d.replace(/\/+$/, "") + "/" + id + ".json" : "";
}

const inLib = computed(() => store.workflowLib.some((m) => libPath(m.id) === path.value));

watch(
  () => props.visible,
  async (v) => {
    if (!v) return;
    if (store.schedule) {
      mode.value = store.schedule.mode ?? "每天时刻";
      atTime.value = store.schedule.atTime ?? "09:00";
      intervalMin.value = store.schedule.intervalMin ?? 30;
      path.value = store.schedule.workflowPath ?? "";
      enabled.value = !!store.schedule.enabled;
    }
    // 工作流进了库之后，「选一份工作流」的默认答案就在库里。不先拉一次的话
    // 下拉是空的，用户只能去 ~/Library/Application Support 里翻文件。
    try {
      await store.refreshWorkflowLib();
    } catch {
      /* ignore */
    }
  }
);

async function pickPath() {
  const p = await open({ title: "选择工作流", filters: [{ name: "工作流", extensions: ["json"] }] });
  if (typeof p === "string") path.value = p;
}

async function onConfirm() {
  try {
    await store.configureSchedule({
      mode: mode.value,
      atTime: atTime.value,
      intervalMin: intervalMin.value,
      workflowPath: path.value,
      enabled: enabled.value,
    });
    MessagePlugin.success("定时已保存（下次启动自动恢复）");
    emit("update:visible", false);
  } catch (e) {
    MessagePlugin.error("保存失败：" + errMessage(e));
  }
}
</script>

<template>
  <t-dialog
    :visible="visible"
    header="定时运行"
    :confirm-btn="{ content: '确定', onClick: onConfirm }"
    :cancel-btn="{ content: '取消', onClick: () => emit('update:visible', false) }"
    @close="() => emit('update:visible', false)"
  >
    <t-form label-width="80px">
      <t-form-item label="模式">
        <t-select v-model="mode" size="small">
          <t-option value="每天时刻" label="每天时刻" />
          <t-option value="固定间隔" label="固定间隔" />
        </t-select>
      </t-form-item>
      <t-form-item v-if="mode === '每天时刻'" label="时刻">
        <t-input v-model="atTime" size="small" placeholder="HH:MM" />
      </t-form-item>
      <t-form-item v-else label="间隔(分)">
        <t-input-number v-model="intervalMin" :min="1" size="small" theme="column" />
      </t-form-item>
      <t-form-item label="工作流">
        <div style="display: flex; gap: 6px; width: 100%">
          <t-select v-model="path" size="small" style="flex: 1" placeholder="从工作流库里选一份">
            <t-option v-for="m in store.workflowLib" :key="m.id" :value="libPath(m.id)" :label="m.name" />
            <!-- 当前值不在库里（老配置指向任意路径、或那份已被删）时补一项：
                 不补的话下拉显示成**空白**，看起来就是「没选」，而定时其实还
                 指着那个文件，下次触发时才发现跑的是别的流程。 -->
            <t-option v-if="path && !inLib" :value="path" :label="path + '（库外文件）'" />
          </t-select>
          <t-button size="small" variant="outline" @click="pickPath">浏览…</t-button>
        </div>
      </t-form-item>
      <t-form-item label="启用">
        <t-switch v-model="enabled" />
      </t-form-item>
      <div v-if="store.nextFire" class="af-next">下次触发：{{ store.nextFire }}</div>
    </t-form>
  </t-dialog>
</template>

<style scoped>
.af-next {
  font-size: 12px;
  color: #0052d9;
}
</style>
