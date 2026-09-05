<script setup lang="ts">
import { computed, ref } from "vue";
import { useAppStore } from "@/stores/app";
import { errMessage } from "@/rpc/client";

const store = useAppStore();

const items = computed(() => [
  { key: "accessibility", label: "辅助功能", ok: store.permissions.accessibility },
  { key: "inputMonitoring", label: "输入监控", ok: store.permissions.inputMonitoring },
  { key: "screenRecording", label: "屏幕录制", ok: store.permissions.screenRecording },
]);

const missing = computed(() => items.value.filter((i) => !i.ok).length > 0);

// 系统快照全 ✗ 或快照 ✓ 但实际收不到事件（TCC 按二进制授权，tap 建立成功却静默
// 不投递——正是「权限看似正常、热键/录制无效」的元凶），都要提示真实自检。
const probeResult = ref<"" | "alive" | "dead">("");
async function onProbe() {
  probeResult.value = "";
  try {
    const r = await store.probeInput();
    probeResult.value = r.alive ? "alive" : "dead";
  } catch {
    probeResult.value = "dead";
  }
}

async function onRequest() {
  try {
    await store.requestPermissions();
    store.setBanner("已弹出系统授权提示，请在系统设置中允许", "ok");
  } catch (e) {
    store.setBanner("请求授权失败：" + errMessage(e), "error");
  }
}
async function onOpenPanel(panel: string) {
  await store.openSettings(panel);
}
</script>

<template>
  <div v-if="missing" class="af-perm">
    <span class="af-perm-title">缺少权限：</span>
    <t-tag
      v-for="i in items"
      :key="i.key"
      :theme="i.ok ? 'success' : 'danger'"
      variant="light"
      size="small"
    >
      {{ i.label }}{{ i.ok ? " ✓" : " ✗" }}
    </t-tag>
    <t-space size="small" style="margin-left: 8px">
      <t-button size="small" theme="primary" @click="onRequest">去授权</t-button>
      <t-button
        size="small"
        variant="outline"
        @click="onOpenPanel('input_monitoring')"
        >系统设置</t-button
      >
      <t-button size="small" variant="outline" :loading="probeResult === ''" @click="onProbe"
        >自检</t-button
      >
    </t-space>
    <span v-if="probeResult === 'alive'" class="af-probe ok"
      >✓ 输入监听实际可用</span
    >
    <span v-else-if="probeResult === 'dead'" class="af-probe bad">
      ✗ 收不到键盘事件：请把 Auto Flow 加入 系统设置→隐私与安全性→输入监控
      （即使上方显示已授权也可能存在此情况），然后重启本应用
    </span>
  </div>
</template>

<style scoped>
.af-perm {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: #fff7e6;
  border-bottom: 1px solid #ffe7ba;
}
.af-perm-title {
  font-size: 13px;
  color: #ad6800;
}
.af-probe {
  font-size: 12px;
}
.af-probe.ok {
  color: #059669;
}
.af-probe.bad {
  color: #e34d59;
}
</style>
