/**
 * 把诊断数据格式化成可粘贴的纯文本（§18 P4「诊断面板」）。
 *
 * 单独成纯函数的原因和虚拟列表一样：组件渲染在无 WebView 的环境里测不到，
 * 但「复制给开发者/贴进 issue 的那段文本」是最需要稳定、也最容易写错的部分
 * （漏字段、时间戳格式不一致、权限真假说不清）。
 */

/** store.diagnostics 的返回形状。用宽松类型，避免与 store 循环依赖。 */
export interface DiagnosticsData {
  appVersion: string;
  protocolExpected: number;
  protocolOk: boolean;
  connected: boolean;
  rpcDownCount: number;
  rpcDownAt: number;
  lastRecoveredAt: number;
  rpcDownDetail: string;
  lastError: string;
  permissions: { accessibility: boolean; inputMonitoring: boolean; screenRecording: boolean };
  workflowName: string;
  nodeCount: number;
  enabledNodeCount: number;
  definitionCount: number;
  base: { x: number; y: number };
  scheduleMode: string;
  nextFire: string;
  recordBufferCount: number;
  recordBufferLimit: number;
  lastRecordInfo: any;
  running: boolean;
  recording: boolean;
  /** 后端 WARNING 及以上的日志（log.warning 通知），新的在前。 */
  backendNotes?: { ts: number; level: string; logger: string; message: string }[];
}

const PERM_LABEL: Record<string, string> = {
  accessibility: "辅助功能",
  inputMonitoring: "输入监控",
  screenRecording: "屏幕录制",
};

/** 时间戳 → 本地 `HH:MM:SS`；0/非法值返回「—」，避免出现 1970 年。 */
export function fmtTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function formatDiagnostics(d: DiagnosticsData): string {
  const perm = (k: keyof DiagnosticsData["permissions"]) =>
    `${PERM_LABEL[k]} ${d.permissions?.[k] ? "已授权" : "未授权"}`;

  const lines: string[] = [
    "=== Auto Flow 诊断信息 ===",
    `版本        ${d.appVersion}`,
    `协议        v${d.protocolExpected} ${d.protocolOk ? "匹配" : "★不匹配★"}`,
    `连接状态    ${d.connected ? "已连接" : "未连接"}`,
    `权限        ${perm("accessibility")} / ${perm("inputMonitoring")} / ${perm("screenRecording")}`,
    "",
    `工作流      ${d.workflowName || "(未命名)"}`,
    `节点数      ${d.nodeCount}（启用 ${d.enabledNodeCount}）`,
    `节点类型数  ${d.definitionCount}`,
    `基点        ${d.base?.x ?? 0}, ${d.base?.y ?? 0}`,
    `定时        ${d.scheduleMode || "未配置"}${d.nextFire ? `（下次 ${d.nextFire}）` : ""}`,
    `运行/录制   ${d.running ? "运行中" : "空闲"} / ${d.recording ? "录制中" : "未录制"}`,
    "",
    `事件缓冲    ${d.recordBufferCount} / ${d.recordBufferLimit}`,
  ];

  const ri = d.lastRecordInfo;
  if (ri) {
    lines.push(
      `上次录制    共 ${ri.count ?? 0} 条` +
        `，过滤 ${ri.filtered ?? 0}` +
        `，超限丢弃 ${ri.limit_dropped ?? 0}` +
        (ri.mouse_died || ri.kb_died
          ? `，监听中断（${[ri.mouse_died ? "鼠标" : "", ri.kb_died ? "键盘" : ""]
              .filter(Boolean)
              .join("、")}）`
          : "")
    );
  }

  lines.push(
    "",
    `断连次数    ${d.rpcDownCount}`,
    `最近断连    ${fmtTime(d.rpcDownAt)}`,
    `最近恢复    ${fmtTime(d.lastRecoveredAt)}`
  );

  if (d.lastError) lines.push("", `最近错误    ${d.lastError}`);
  const notes = d.backendNotes ?? [];
  if (notes.length) {
    // 后端告警单独成段：这些是「不报错、只是点歪/找不到」的静默失败诊断
    // （模板失效 / 纯色模板 / 密度不一致），贴 issue 时往往是关键线索。
    lines.push("", `--- 后端告警（最近 ${notes.length} 条，新的在前）---`);
    for (const n of notes) {
      lines.push(`[${fmtTime(n.ts)}] ${n.level} ${n.logger}: ${n.message}`);
    }
  }
  if (d.rpcDownDetail) {
    // 断连详情常含查找路径 + sidecar stderr 多行，原样保留（这是排查的主要线索）。
    lines.push("", "--- 最近断连详情 ---", d.rpcDownDetail.trim());
  }
  return lines.join("\n");
}
