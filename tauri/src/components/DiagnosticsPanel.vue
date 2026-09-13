<script setup lang="ts">
/**
 * 诊断面板（§18 P4）。
 *
 * 设计约束：**后端挂掉时它必须照样能打开**——所有数据都取自前端已持有的状态
 * （store.diagnostics），这里不发任何 RPC。用户来开这个面板，往往正是因为
 * 后端连不上，这时候再依赖后端就等于没有面板。
 */
import { computed, ref } from "vue";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";
import { formatDiagnostics, fmtTime } from "@/utils/diagnostics";

const props = defineProps<{ visible: boolean }>();
const emit = defineEmits<{ (e: "update:visible", v: boolean): void }>();

const store = useAppStore();
const d = computed(() => store.diagnostics as any);
const report = computed(() => formatDiagnostics(d.value));
/** 后端告警逐行渲染成一段文本；组件只管排版，格式规则与「复制报告」保持一致。 */
const notesText = computed(() =>
  (d.value.backendNotes ?? [])
    .map((n: any) => `[${fmtTime(n.ts)}] ${n.level} ${n.logger}: ${n.message}`)
    .join("\n")
);
const textarea = ref<HTMLTextAreaElement | null>(null);

function yn(v: boolean): string {
  return v ? "已授权" : "未授权";
}

async function copyReport() {
  try {
    await navigator.clipboard.writeText(report.value);
    MessagePlugin.success("诊断信息已复制");
  } catch {
    // WKWebView 里 clipboard 可能被拒；退化为全选，让用户自己 Cmd+C。
    textarea.value?.focus();
    textarea.value?.select();
    MessagePlugin.info("已全选，请按 ⌘C 复制");
  }
}

function close() {
  emit("update:visible", false);
}
</script>

<template>
  <t-dialog
    :visible="props.visible"
    header="诊断信息"
    width="640px"
    :footer="false"
    @close="close"
  >
    <div class="af-diag">
      <div class="af-diag-actions">
        <t-button size="small" variant="outline" @click="copyReport">复制全部</t-button>
        <span style="flex: 1" />
        <t-button size="small" variant="text" @click="close">关闭</t-button>
      </div>

      <div :class="['af-conn-row', d.connected ? 'ok' : 'bad']">
        <b>{{ d.connected ? "● 已连接后端" : "○ 未连接后端" }}</b>
        <span v-if="d.rpcDownCount" class="af-dim">
          累计断连 {{ d.rpcDownCount }} 次 · 最近断开 {{ fmtTime(d.rpcDownAt) }} ·
          最近恢复 {{ fmtTime(d.lastRecoveredAt) }}
        </span>
      </div>

      <table class="af-tbl">
        <tbody>
          <tr>
            <th>版本 / 协议</th>
            <td>
              {{ d.appVersion }} ·
              <span :class="{ bad: !d.protocolOk }">
                协议 v{{ d.protocolExpected }}{{ d.protocolOk ? " 匹配" : " 不匹配" }}
              </span>
            </td>
          </tr>
          <tr>
            <th>权限</th>
            <td>
              <span :class="{ bad: !d.permissions.accessibility }">辅助功能 {{ yn(d.permissions.accessibility) }}</span>
              ·
              <span :class="{ bad: !d.permissions.inputMonitoring }">输入监控 {{ yn(d.permissions.inputMonitoring) }}</span>
              ·
              <span :class="{ bad: !d.permissions.screenRecording }">屏幕录制 {{ yn(d.permissions.screenRecording) }}</span>
            </td>
          </tr>
          <tr>
            <th>工作流</th>
            <td>{{ d.workflowName || "(未命名)" }} · 节点 {{ d.nodeCount }}（启用 {{ d.enabledNodeCount }}）</td>
          </tr>
          <tr>
            <th>节点类型</th>
            <td>{{ d.definitionCount }} 种（来自后端 nodes.definitions）</td>
          </tr>
          <tr>
            <th>基点</th>
            <td>{{ d.base.x }}, {{ d.base.y }}</td>
          </tr>
          <tr>
            <th>定时</th>
            <td>
              {{ d.scheduleMode || "未配置" }}
              <span v-if="d.nextFire" class="af-dim">· 下次 {{ d.nextFire }}</span>
            </td>
          </tr>
          <tr>
            <th>运行 / 录制</th>
            <td>{{ d.running ? "运行中" : "空闲" }} / {{ d.recording ? "录制中" : "未录制" }}</td>
          </tr>
          <tr>
            <th>事件缓冲</th>
            <td>
              {{ d.recordBufferCount }} / {{ d.recordBufferLimit }}
              <span class="af-dim">（完整数据在后端，此处仅实时预览）</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="d.lastError" class="af-block err">
        <div class="af-block-head">最近错误</div>
        <pre>{{ d.lastError }}</pre>
      </div>

      <div v-if="d.backendNotes?.length" class="af-block warn">
        <div class="af-block-head">
          后端告警（{{ d.backendNotes.length }} 条，新的在前）
        </div>
        <pre>{{ notesText }}</pre>
        <div class="af-dim">
          这类告警是「不报错、只是点歪/找不到」的静默失败线索——例如模板图失效、
          模板与屏幕像素密度不一致。完整日志见 ~/Library/Logs/autoflow-tauri.log。
        </div>
      </div>
      <div v-else class="af-dim af-nodetail">暂无后端告警。</div>

      <div v-if="d.rpcDownDetail" class="af-block">
        <div class="af-block-head">最近断连详情（含查找路径与 sidecar stderr）</div>
        <pre>{{ d.rpcDownDetail }}</pre>
      </div>
      <div v-else class="af-dim af-nodetail">
        暂无断连记录。后端异常退出时，这里会显示 Rust 侧记录的查找路径与 sidecar 最后几行日志。
      </div>

      <textarea ref="textarea" class="af-raw" readonly :value="report" rows="6" />
    </div>
  </t-dialog>
</template>

<style scoped>
.af-diag {
  font-size: 13px;
}
.af-diag-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.af-conn-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  border-radius: 6px;
  margin-bottom: 10px;
}
.af-conn-row.ok {
  background: #eaf7ef;
  color: #057a4a;
}
.af-conn-row.bad {
  background: #fdecee;
  color: #c0392b;
}
.af-tbl {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 10px;
}
.af-tbl th {
  text-align: left;
  font-weight: 400;
  color: #888;
  white-space: nowrap;
  padding: 3px 10px 3px 0;
  vertical-align: top;
  width: 92px;
}
.af-tbl td {
  padding: 3px 0;
  color: #333;
}
.bad {
  color: #e34d59;
}
.af-dim {
  color: #999;
  font-size: 11px;
}
.af-block {
  margin-bottom: 8px;
}
.af-block-head {
  font-size: 11px;
  color: #888;
  margin-bottom: 3px;
}
.af-block pre {
  margin: 0;
  max-height: 140px;
  overflow: auto;
  background: #f7f8fa;
  border: 1px solid #eceef1;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
}
.af-block.err pre {
  background: #fdecee;
  border-color: #f7c9cd;
}
.af-block.warn pre {
  background: #fdf6e6;
  border-color: #f0dcb0;
}
.af-nodetail {
  margin-bottom: 8px;
}
.af-raw {
  width: 100%;
  box-sizing: border-box;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: #666;
  border: 1px solid #eceef1;
  border-radius: 6px;
  padding: 6px 8px;
  resize: vertical;
}
</style>
