<script setup lang="ts">
import { ref, watch } from "vue";
import { useAppStore } from "@/stores/app";
import { open } from "@tauri-apps/plugin-dialog";
import { MessagePlugin } from "tdesign-vue-next";

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{ (e: "update:visible", v: boolean): void }>();

const store = useAppStore();
const mode = ref("每天时刻");
const atTime = ref("09:00");
const intervalMin = ref(30);
const path = ref("");
const enabled = ref(false);

watch(
  () => props.visible,
  (v) => {
    if (v && store.schedule) {
      mode.value = store.schedule.mode ?? "每天时刻";
      atTime.value = store.schedule.atTime ?? "09:00";
      intervalMin.value = store.schedule.intervalMin ?? 30;
      path.value = store.schedule.workflowPath ?? "";
      enabled.value = !!store.schedule.enabled;
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
    MessagePlugin.error("保存失败：" + (e as Error).message);
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
          <t-input v-model="path" size="small" readonly placeholder="未选择" />
          <t-button size="small" variant="outline" @click="pickPath">选择</t-button>
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
