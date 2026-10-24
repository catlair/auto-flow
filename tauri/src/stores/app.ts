// 全局状态：后端是唯一真源（§10），前端近乎无状态——只持有视图态、表单草稿、运行/录制态镜像。
import { defineStore } from "pinia";
import { errMessage, rpc } from "@/rpc/client";
import type {
  JsonRpcNotification,
  NodeDefinition,
  Permissions,
  Workflow,
} from "@/rpc/types";
import { PORT_OUT, DEFAULT_TARGET_SIDE, liveEdges } from "@/flow/ports";
import type { GraphEdge } from "@/flow/ports";
import { cycleEdges } from "@/flow/cycle";
import type { RecMeta } from "@/flow/recording";
import { normalizeMetas, wfExportName } from "@/flow/workflow";
import type { WorkflowMeta } from "@/flow/workflow";
import { invoke } from "@tauri-apps/api/core";
import { emit, listen } from "@tauri-apps/api/event";
import {
  LOG_BACKLOG_EVENT,
  LOG_CAP,
  LOG_EVENT,
  LOG_HELLO_EVENT,
  LogRing,
  notificationToLine,
} from "@/logwin/format";
import type { LogLevel, LogLine } from "@/logwin/format";
import { open, save } from "@tauri-apps/plugin-dialog";

export const PROTOCOL_VERSION = 1;

/**
 * 录制事件缓冲上限。超出后丢最旧的——完整数据在后端内存树里，
 * 前端这份只用于实时预览，不参与回放/保存。
 */
export const RECORD_BUFFER_LIMIT = 5000;

/**
 * 诊断面板里保留的后端告警条数。后端已用 `_warn_once` 对同类问题去重，
 * 这里只是兜底，防止长跑会话里无限增长。
 */
export const BACKEND_NOTE_LIMIT = 20;

/**
 * 置顶日志窗的历史缓冲（主窗留一份，开窗时补给它）。
 *
 * 放在**模块级**而不是 Pinia state：界面不渲染它，进 state 只会让每条日志都触发一次
 * 响应式更新和 `$subscribe`——而 `$subscribe` 里跑的是托盘同步，录制中每秒几十次
 * 状态变更，白白放大成本。
 */
const logRing = new LogRing(LOG_CAP);

/** 后端 `log.warning` 通知的一条记录。 */
export type BackendNote = {
  ts: number;
  level: string;
  logger: string;
  message: string;
};

type CapturedKeyHandler = (name: string) => void;

/**
 * 运行轨迹里的一条：**哪一步、叫什么、隔了多久**。
 *
 * 为什么要有它：`run.node` 只带类型，界面上只能显示「找文字点击」——用户建了六个
 * 点击节点，根本分不清现在是哪一个，于是每次卡住都得去翻日志。后端现在把节点的
 * 自定义名和参数摘要一起发过来（见 controller.node_digest），这里攒成一条时间线，
 * 「现在在干什么」和「刚才走到哪一步」就都能直接看。
 */
export type RunTraceItem = {
  /** 本地时间戳（ms），只用于显示与算间隔 */
  t: number;
  /** 节点自定义名（没起名时回落成类型名） */
  name: string;
  /** 参数摘要，如「文字「继续」」「20.0s」「判据：A / B」 */
  detail: string;
  /** 距上一步的毫秒数（第一条为 0） */
  deltaMs: number;
};

/** 轨迹上限：一次长跑几千步，全留着会一直涨内存，也翻不动。 */
export const RUN_TRACE_LIMIT = 200;

/**
 * 实际的起始节点 uid——**必须与后端 `Executor.entry_uid` 完全一致**，
 * 否则画布上的「起点」徽标会标在一个后端并不从这里开始跑的节点上，
 * 用户照着画布排查会越排越糊涂。
 *
 * 规则：显式 `start`（且节点还在）> 第一个 start 节点 > 第一个节点。
 * 「节点还在」这层判断不能省：`start` 是后端清空的，但手工编辑的 json
 * 或半途失败的操作都可能留下指向已删节点的 start。
 *
 * 写成模块级函数而不是只在 getter 里实现，是因为 `diagnostics` 也要报它——
 * 而在 getter 里写 `this.entryUid` 会让 Pinia 的 getters 类型推断成环，
 * 结果是**所有** getter 在组件里都变成「不存在」（TS2339 一大片）。
 */
function resolveEntryUid(wf: Workflow | null | undefined): string {
  const nodes = wf?.nodes ?? [];
  if (!nodes.length) return "";
  const wanted = wf?.start;
  if (wanted && nodes.some((n) => n.uid === wanted)) return wanted;
  const s = nodes.find((n) => n.type === "start");
  return s?.uid ?? nodes[0]?.uid ?? "";
}

export const useAppStore = defineStore("app", {
  state: () => ({
    connected: false,
    // Rust 侧最近一次断连原因（含查找路径 / sidecar stderr），用于给出可读提示。
    rpcDownDetail: "",
    // 最近一次断连详情，**不被重连清空**：用户往往是在「已经恢复」之后才想起来
    // 去看诊断，那时把详情清掉，正好看不到最需要的那份线索（只在 console 里留痕）。
    lastRpcDownDetail: "",
    // 崩溃重连状态：断开时刻/累计断连次数/恢复提示。
    // 恢复提示刻意**不塞进 banner**——banner 只放「有问题」的信息，
    // 否则「已恢复」会把真正需要用户处理的错误顶掉。
    rpcDownAt: 0,
    rpcDownCount: 0,
    lastRecoveredAt: 0,
    reconnectNotice: "",
    // 最近一次以 error 级别上报的信息，诊断面板用。
    lastError: "",
    // 后端 WARNING 及以上的日志（log.warning 通知）。
    //
    // 为什么要有它：`core.vision` 那些「不报错、只是点歪/找不到」的诊断
    // （模板文件失效 / 纯色模板 / 密度不一致）原本只进 stderr 与
    // ~/Library/Logs/autoflow-tauri.log，而那份日志是 5000+ 行的 RPC 帧流水，
    // 让用户去里面 grep 等于没有诊断。这里收下来给诊断面板显示。
    // 有界：日志可能反复触发，不能无限增长。
    backendNotes: [] as BackendNote[],
    protocolOk: true,
    appVersion: "",
    running: false,
    recording: false,
    lastRecordCount: 0,
    lastRecordInfo: null as any,
    // 事件编辑（record.remove / removeMovesBefore / setOrigin / undo）后由返回刷新
    recordCanUndo: false,
    recordOrigin: [0, 0] as [number, number],
    permissions: { accessibility: false, inputMonitoring: false, screenRecording: false } as Permissions,
    workflow: { name: "未命名", speed: 1.0, repeat: 1, nodes: [], edges: [], start: "" } as Workflow,
    definitions: [] as NodeDefinition[],
    selectedIndex: -1,
    // 「本次加载的工作流由旧版有序列表迁移而来」——后端只存内存不落盘，
    // 界面据此给一次提示（否则用户会发现条件不再门控却完全不知道为什么）。
    workflowMigrated: false,
    migratedNotice: "",
    base: { x: 0, y: 0 },
    schedule: null as any,
    nextFire: "",
    recordBuffer: [] as any[],
    /**
     * 录制库列表（**不含 events**，见后端 `recording_lib.list_meta`）。
     *
     * 放在 store 而不是 RecordPanel 里：参数面板的「选择录制」下拉也要用同一份，
     * 两处各拉一次会出现「库里刚存的那条在参数面板里选不到」。
     */
    recordingLib: [] as RecMeta[],
    /**
     * 当前编辑缓冲对应库里的哪一条（后端 `_lib_info()`）。
     * `dirty=true` = 内存里的序列已经和库里那份不一样了（改过但没更新）。
     * `editing=true` = 正在「直接编辑」这一条（每次编辑自动写回库里）。
     */
    recordingLibCurrent: null as {
      id: string;
      name: string;
      dirty: boolean;
      editing?: boolean;
    } | null,
    /** 库目录路径，面板上显示出来方便用户自己去备份/拷走。 */
    recordingLibDir: "" as string,
    /**
     * 工作流库列表（**不含 nodes/edges**，见后端 `workflow_lib.list_meta`）。
     *
     * 与 `recordingLib` 同理由放在 store：菜单的未保存标记、库弹层、定时运行里
     * 选工作流都读同一份，各拉各的会出现「刚存的那份在下拉里选不到」。
     */
    workflowLib: [] as WorkflowMeta[],
    /** 当前这份对应库里哪一条（后端 `id_of_path`）。空串 = 还没入库。 */
    workflowLibCurrent: "" as string,
    /** 工作流库目录，弹层里显示出来方便用户自己备份/拷走。 */
    workflowLibDir: "" as string,
    /** 当前工作流在磁盘上的路径（空 = 还没落盘过）。 */
    workflowPath: "" as string,
    /**
     * 有没有未保存的改动。**真源在后端**（`workflow_dirty()`，判据是版本号），
     * 这里只是镜像——与 `running` / `recording` 同样的处理。
     * 前端自己记「我改过没有」是不行的：定时任务会直接在后台换掉整棵工作流树，
     * 那条路上前端什么都没发过。
     */
    workflowDirty: false as boolean,
    runProgress: { done: 0, total: 0 } as { done: number; total: number },
    runNodeType: "" as string,
    /** 当前节点的自定义名与参数摘要（后端 run.node 带过来） */
    runNodeName: "" as string,
    runNodeDetail: "" as string,
    /** 「跑满 N 轮」进度（后端 run.round 带过来；total=0 表示没设上限） */
    runRound: { round: 0, total: 0 } as { round: number; total: number },
    /** 本次运行的节点轨迹（时间正序，最多 RUN_TRACE_LIMIT 条；跑完保留供回看） */
    runTrace: [] as RunTraceItem[],
    banner: "" as string,
    bannerKind: "ok" as "ok" | "warn" | "error",
    // key.captured 订阅（ParamsPanel 捕获键时接收回填）
    _captured: new Set<CapturedKeyHandler>(),
    // 合并并发握手（首连与 rpc_up 事件可能同时触发）
    _handshaking: false,
    /**
     * 最近一次**同步给托盘**的录制态。
     *
     * 托盘菜单的「开始/停止录制」文案与菜单栏角标由 Rust 持有，前端每次状态变化
     * 都推一次会变成高频 IPC（录制中事件流每秒几十条状态变更）。用这个值做去重：
     * 只有真的翻转了才推。
     */
    trayRecording: null as boolean | null,
    /**
     * 置顶日志窗是否显示。**真源在 Rust**（窗口由它创建/隐藏），这里只镜像：
     * 前端自己记的话，日志窗的「×」和系统 ⌘W 这两条路会让按钮永远停在「已打开」。
     */
    logWinVisible: false as boolean,
  }),
  getters: {
    selectedNode(state) {
      return state.selectedIndex >= 0 ? state.workflow.nodes[state.selectedIndex] : null;
    },
    nodeDefsByName(state): Record<string, NodeDefinition> {
      const m: Record<string, NodeDefinition> = {};
      for (const d of state.definitions) m[d.type] = d;
      return m;
    },
    /** 起始节点 uid，见 `resolveEntryUid`。 */
    entryUid(state): string {
      return resolveEntryUid(state.workflow);
    },
    /**
     * 诊断信息汇总（§18 P4「诊断面板」）。
     *
     * 刻意返回**纯数据**而非让组件各取各的：一是组件只负责渲染，二是这里能直接
     * 单测（组件渲染在无 WebView 的环境里测不到）。
     * 不在这里做任何 RPC——诊断面板必须能在「后端已挂」时打开。
     */
    diagnostics(state) {
      const p = state.permissions ?? ({} as Permissions);
      const nodes = state.workflow?.nodes ?? [];
      return {
        appVersion: state.appVersion || "(未知)",
        protocolExpected: PROTOCOL_VERSION,
        protocolOk: state.protocolOk,
        connected: state.connected,
        rpcDownCount: state.rpcDownCount,
        rpcDownAt: state.rpcDownAt,
        lastRecoveredAt: state.lastRecoveredAt,
        // 优先给「最近一次断连」的详情：面板通常在已恢复时才被打开
        rpcDownDetail: state.lastRpcDownDetail || state.rpcDownDetail,
        lastError: state.lastError,
        backendNotes: state.backendNotes,
        permissions: {
          accessibility: !!p.accessibility,
          inputMonitoring: !!p.inputMonitoring,
          screenRecording: !!p.screenRecording,
        },
        workflowName: state.workflow?.name ?? "",
        nodeCount: nodes.length,
        enabledNodeCount: nodes.filter((n) => n?.enabled).length,
        edgeCount: state.workflow?.edges?.length ?? 0,
        startUid: resolveEntryUid(state.workflow),
        migratedFromList: !!state.workflowMigrated,
        definitionCount: state.definitions.length,
        base: { x: state.base?.x ?? 0, y: state.base?.y ?? 0 },
        scheduleMode: state.schedule?.mode ?? "",
        nextFire: state.nextFire,
        recordBufferCount: state.recordBuffer.length,
        recordBufferLimit: RECORD_BUFFER_LIMIT,
        lastRecordInfo: state.lastRecordInfo,
        running: state.running,
        recording: state.recording,
      };
    },
  },
  actions: {
    /**
     * 推一行给置顶日志窗：进历史缓冲 + 经 Tauri 事件送过去。
     *
     * 传 `null` 是合法的（`notificationToLine` 对不关心的通知返回 null），
     * 这样调用点不必先判空。
     */
    pushLog(line: LogLine | null) {
      if (!line) return;
      logRing.push(line);
      try {
        void emit(LOG_EVENT, line).catch(() => {});
      } catch {
        /* 无 Tauri 环境：日志窗本来也不存在，忽略 */
      }
    },
    /** 前端自己产生的一行（开始运行 / 断连 / 恢复…），与后端通知同一个缓冲。 */
    noteLog(level: LogLevel, text: string, kind = "front") {
      this.pushLog({ t: Date.now(), level, kind, text });
    },
    /**
     * 开/关置顶日志窗。窗口是 Rust 建的，这里只拿结果镜像状态。
     * 失败要报出来：以前这种「点了没反应」的入口只能靠猜。
     */
    async toggleLogWin() {
      try {
        this.logWinVisible = !!(await invoke("af_logwin_toggle"));
      } catch (e) {
        this.setBanner("打开日志窗失败：" + errMessage(e), "error");
      }
    },

    setBanner(msg: string, kind: "ok" | "warn" | "error" = "error") {
      this.banner = msg;
      this.bannerKind = kind;
      // 诊断面板要能看到「最近一次出错」，而 banner 会被后续的 ok/warn 覆盖掉。
      if (kind === "error") this.lastError = msg;
    },
    clearBanner() {
      this.banner = "";
    },
    dismissReconnect() {
      this.reconnectNotice = "";
    },
    dismissMigrated() {
      this.migratedNotice = "";
    },
    isInputFocused(): boolean {
      const el = document.activeElement as HTMLElement | null;
      return (
        !!el &&
        (el.tagName === "INPUT" ||
          el.tagName === "TEXTAREA" ||
          el.isContentEditable)
      );
    },

    async init() {
      await rpc.connect();
      rpc.onNotify((n) => this.handleNotification(n));
      // sidecar 上下线由 Rust 以 Tauri 事件广播（rpc_up / rpc_down），与 stdout 上的
      // NDJSON 通知是两条通道——此前只监听 rpc_event，故断连时前端毫无感知。
      rpc.onStatus((up, detail) => {
        const wasConnected = this.connected;
        this.connected = up;
        if (up) {
          this.rpcDownDetail = "";
          this.clearBanner();
          // Rust 会在 sidecar 崩溃后重启它，而新进程的工作流是空的。
          // 不重新握手的话界面会继续显示旧数据——看起来一切正常，实际后端已换人。
          if (!wasConnected) {
            // 判据用 `rpcDownAt` 而不是 `wasConnected`：这里**永远**处在
            // `!wasConnected` 分支里，用它只会得到「已连接」。
            this.noteLog("ok", this.rpcDownAt ? "后端已恢复" : "后端已连接");
            // 断线过又回来：明确告诉用户「已恢复」。此前只是把横幅清掉，
            // 用户可能根本没察觉后端崩过一次（而它其实已经重启、状态被重置）。
            if (this.rpcDownAt) {
              this.lastRecoveredAt = Date.now();
              this.reconnectNotice =
                `后端 sidecar 已重新连接（本次会话累计断连 ${this.rpcDownCount} 次）`;
            }
            // Rust 会在 sidecar 崩溃后重启它，而新进程的工作流是空的。
            // 不重新握手的话界面会继续显示旧数据——看起来一切正常，实际后端已换人。
            void this.handshake();
          }
        } else {
          this.rpcDownDetail = detail || "";
          if (detail) this.lastRpcDownDetail = detail;
          this.rpcDownAt = Date.now();
          this.rpcDownCount += 1;
          // detail 可能含查找路径 + sidecar 最近 stderr：横幅只显示首行，全文留给
          // console 与诊断面板（横幅放不下，且换行会撑坏布局）。
          const first = (detail || "").split("\n")[0].trim();
          this.noteLog("error", "后端断开：" + (first || "原因未知（正在重连）"));
          this.setBanner(
            first
              ? `后端 sidecar 断开，正在重连…（${first}）`
              : "后端 sidecar 断开，正在重连…",
            "warn"
          );
          if (detail) console.error("[rpc_down]", detail);
        }
      });
      // 托盘菜单的「开始/停止录制」：Rust 只发事件，状态仍由这里翻转
      // （否则会出现「托盘说在录、界面说停了」这种两边各存一份的分叉）。
      // listen 失败（非 Tauri 环境、capability 缺失）不该影响启动——托盘是辅助入口。
      try {
        await listen<string>("tray_action", (e) => {
          if (e.payload !== "toggle_record") return;
          // `by: "hotkey"` 而不是 "button"：托盘点击发生在**菜单栏**，
          // 永远不会落在主窗矩形内，所以没有"点停止按钮"需要从序列尾部裁掉。
          // 传 "button" 会让后端去找最后一个窗口内的按下——那可能是用户的真实操作，
          // 从它处截掉后半段（2026-09-12 那次事故就是这么来的）。
          void this.toggleRecord({ by: "hotkey" });
        });
      } catch (e) {
        console.warn("[tray] 托盘事件监听未装定:", errMessage(e));
      }
      // 置顶日志窗的两条接线：
      //   ① 它挂载时会喊一声 `af_log_hello`，我们把历史日志一次性补给它——Tauri 事件
      //      不缓存、不重放，晚开的窗口否则只能从"下一行"开始看；
      //   ② 开/关状态由 Rust 广播（三个入口：主窗按钮、日志窗的 ×、⌘W），
      //      再加一次主动查询兜住"事件发在前端注册之前"的竞态。
      try {
        await listen(LOG_HELLO_EVENT, () => {
          try {
            void emit(LOG_BACKLOG_EVENT, logRing.snapshot()).catch(() => {});
          } catch {
            /* 无 Tauri 环境：忽略 */
          }
        });
        await listen<boolean>("af_logwin_visible", (e) => {
          this.logWinVisible = !!e.payload;
        });
        this.logWinVisible = !!(await invoke("af_logwin_is_visible"));
      } catch (e) {
        console.warn("[logwin] 置顶日志窗接线未装定:", errMessage(e));
      }
      // 录制态变化就推给托盘。用 `$subscribe` 而不是在六七个改 `recording` 的
      // 地方各调一次——漏一处就是「托盘角标不消失」，而这种漏很难被注意到。
      this.$subscribe(() => this.syncTray(), { detached: true });
      await this.handshake();
      this.syncTray();
    },

    /**
     * 把录制态推给托盘（菜单项文案 + 工具提示 + 菜单栏角标）。
     *
     * 去重是必须的：`$subscribe` 在任何状态变更后都会触发，而录制中事件流每秒
     * 会造成几十次变更——每次都发一次 IPC 纯属浪费。
     */
    syncTray() {
      if (this.trayRecording === this.recording) return;
      this.trayRecording = this.recording;
      // invoke 在没有 Tauri 环境时**同步抛错**（window 未定义），`.catch()` 接不住，
      // 所以两层都要兜（同 setClickThrough）。
      try {
        void invoke("af_tray_state", { recording: this.recording }).catch(() => {});
      } catch {
        /* 无 Tauri 环境：忽略 */
      }
    },
    /** 把窗口藏进托盘（界面上的入口；关窗那条路在 Rust 的 on_window_event 里）。 */
    hideToTray() {
      try {
        void invoke("af_hide_window").catch(() => {});
      } catch {
        /* 无 Tauri 环境：忽略 */
      }
    },

    /**
     * 与后端对齐全部状态（首次连接与重连后共用）。
     *
     * 每一步独立 try/catch：后端某个方法失败不应拖垮其余状态同步。
     * 并发调用会被合并，避免首连与 rpc_up 事件同时触发两次握手。
     */
    async handshake() {
      if (this._handshaking) return;
      this._handshaking = true;
      try {
        try {
          const info = await rpc.request("app.info");
          this.appVersion = info.appVersion ?? "";
          this.protocolOk = info.protocolVersion === PROTOCOL_VERSION;
          if (info.permissions) this.permissions = info.permissions;
          this.connected = true;
          if (!this.protocolOk) {
            this.setBanner(
              `协议版本不匹配（前端期望 ${PROTOCOL_VERSION}，后端 ${info.protocolVersion}）`,
              "warn"
            );
          }
        } catch (e) {
          this.connected = false;
          // 优先用 Rust 侧带查找路径/stderr 的详情，比 send_rpc 的裸错误（"sidecar 未连接"）可读。
          this.setBanner(
            "无法连接后端 sidecar：" + (this.rpcDownDetail || errMessage(e))
          );
        }
        // 拉取定义 + 当前工作流 + 定时配置（三者均来自后端真源）
        try {
          this.definitions = (await rpc.request("nodes.definitions")) ?? [];
        } catch {
          /* ignore */
        }
        try {
          const cur = await rpc.request("workflow.current");
          // 走 applyCurrent 而不是 applyWorkflow：后者不认 path / lib_id，
          // 于是重连之后菜单会把「当前这份是哪一条」当成「还没入库」。
          this.applyCurrent(cur);
        } catch {
          /* ignore */
        }
        try {
          const sc = await rpc.request("schedule.get");
          this.schedule = sc.schedule ?? null;
          this.nextFire = sc.nextFire ?? "";
        } catch {
          /* ignore */
        }
        // 录制库列表：参数面板的「选择录制」下拉在没选节点前就可能要渲染，
        // 所以握手时就要有一份（而不是等用户切到录制页）。
        try {
          await this.refreshRecordingLib();
        } catch {
          /* ignore */
        }
        // 工作流库列表：菜单上的未保存标记、库弹层、定时运行里选工作流都要它。
        // 握手时就备好，省得用户点开弹层先看到一片空白。
        try {
          await this.refreshWorkflowLib();
        } catch {
          /* ignore */
        }
        // 默认热键：F9=录制开/停、F10=运行开/停、F11=取点。此前前端从未调用 hotkey.set，
        // 后端热键监听从未装定，表现为「没有录制/结束运行快捷键」。启动与重连都要装定。
        try {
          await rpc.request("hotkey.set", { actions: ["record", "run", "pick"] });
        } catch {
          /* ignore */
        }
      } finally {
        this._handshaking = false;
      }
    },

    applyWorkflow(wf: Workflow, running?: boolean, recording?: boolean) {
      // 校验必须在写状态之前；损坏通知不能把工作流置为 undefined。
      if (!wf || !Array.isArray(wf.nodes)) {
        throw new Error("工作流数据无效：缺少 nodes 数组");
      }
      // v4 的 edges / start 缺失时补默认值。旧帧（重连前缓存的）或旧版后端
      // 都不带这两个字段，画布拿到 undefined 会在 computed 里直接抛。
      // 注意**不改传入对象**：它可能是 store 里已有的引用。
      const next: Workflow = {
        ...wf,
        edges: Array.isArray(wf.edges) ? wf.edges : [],
        start: typeof wf.start === "string" ? wf.start : "",
      };
      this.workflow = next;

      // 迁移提示只在「从否变是」时给一次。每次都设的话，后续任何一次
      // workflow.changed（改个参数就会有）都会把提示重新弹出来。
      const migrated = !!next.migrated_from_list;
      if (migrated && !this.workflowMigrated) {
        this.migratedNotice =
          "这个工作流来自旧版「有序列表」，已按原顺序自动连线。" +
          "条件节点不再自动门控后续节点——请把「成立 / 不成立」分别接到各自的分支上。";
      } else if (!migrated) {
        // 换成了非迁移工作流（新建 / 打开 v4 文件）：提示必须撤掉，
        // 否则它会一直挂在界面上，说的却是上一个工作流的事。
        this.migratedNotice = "";
      }
      this.workflowMigrated = migrated;

      // 未保存标记。只在后端**明确给了布尔值**时才覆盖：重连前缓存的旧帧、
      // 或旧版后端都不带这个字段，那时 `!!undefined` 会把标记清掉，表现为
      // 「改了一堆东西，标记却忽然没了」。
      if (typeof (wf as any).dirty === "boolean") this.workflowDirty = (wf as any).dirty;

      if (typeof running === "boolean") this.running = running;
      if (typeof recording === "boolean") this.recording = recording;
      if (this.selectedIndex >= wf.nodes.length) this.selectedIndex = wf.nodes.length - 1;
    },

    handleNotification(n: JsonRpcNotification) {
      // 单条通知处理异常不能打死后续通知
      try {
        this._handleNotification(n);
      } catch (e) {
        console.error("[rpc] 通知处理失败:", n.method, e);
      }
    },
    _handleNotification(n: JsonRpcNotification) {
      // 顺带推一行给置顶日志窗。**只在这一处**做转换（见 logwin/format.ts）：
      // 每个 case 里各写一遍的话，漏一处就是「日志窗少一条」，而这种漏很难被注意到。
      this.pushLog(notificationToLine(n as any));
      switch (n.method) {
        case "workflow.changed":
          // 与 workflow.current 响应不同：通知 params 直接是工作流摘要。
          this.applyWorkflow(n.params);
          break;
        case "permission.changed":
          this.permissions = n.params;
          break;
        case "hotkey.triggered": {
          // 输入框聚焦时屏蔽热键（§14 顺带修复）
          if (this.isInputFocused()) break;
          const a = n.params.action;
          if (a === "record") this.toggleRecord({ by: "hotkey" });
          else if (a === "run") this.toggleRun();
          else if (a === "pick") {
            // 后端事件自带光标坐标（CLI 进程读不到全局光标），直接回填
            if (typeof n.params.x === "number") {
              this.base = { x: n.params.x, y: n.params.y };
            }
          }
          break;
        }
        case "base.picked":
          // 「取点」按钮 armed 后的回填：下一个按键事件自带坐标
          this.base = { x: n.params.x, y: n.params.y };
          this.setBanner(`基点已取：${n.params.x}, ${n.params.y}`, "ok");
          break;
        case "key.captured":
          this._captured.forEach((h) => h(n.params.name));
          break;
        case "record.event":
          this.recordBuffer.push(...(n.params.events ?? []));
          if (this.recordBuffer.length > RECORD_BUFFER_LIMIT) {
            this.recordBuffer.splice(0, this.recordBuffer.length - RECORD_BUFFER_LIMIT);
          }
          break;
        case "record.stopped":
          this.recording = false;
          this.lastRecordCount = n.params.count ?? 0;
          this.lastRecordInfo = n.params;
          break;
        case "run.node": {
          // 从「没在跑」跳到「在跑」= 新一轮开始（含定时任务在后端直接触发的那些，
          // 那条路上前端没发过 run.start，只能靠这个判据清空轨迹）。
          if (!this.running) this.runTrace = [];
          const t = Date.now();
          const prev = this.runTrace.length ? this.runTrace[this.runTrace.length - 1].t : 0;
          this.running = true;
          this.runNodeType = n.params.type ?? "";
          this.runNodeName = n.params.name ?? "";
          this.runNodeDetail = n.params.detail ?? "";
          this.runTrace.push({
            t,
            name: this.runNodeName || this.runNodeType || "?",
            detail: this.runNodeDetail,
            deltaMs: prev ? t - prev : 0,
          });
          if (this.runTrace.length > RUN_TRACE_LIMIT) {
            this.runTrace.splice(0, this.runTrace.length - RUN_TRACE_LIMIT);
          }
          break;
        }
        case "run.progress":
          this.runProgress = { done: n.params.done, total: n.params.total };
          break;
        case "run.round":
          // 「跑满 N 轮」的进度。单独一条事件，不与 run.progress（节点进度）混用。
          this.runRound = { round: n.params.round ?? 0, total: n.params.total ?? 0 };
          break;
        case "run.finished":
          this.running = false;
          this.runNodeType = "";
          this.runNodeName = "";
          this.runNodeDetail = "";
          this.runProgress = { done: 0, total: 0 };
          this.setClickThrough(false);
          break;
        case "run.error":
          this.running = false;
          this.runNodeName = "";
          this.runNodeDetail = "";
          this.setClickThrough(false);
          this.setBanner("运行出错：" + (n.params.message ?? ""), "error");
          break;
        case "log.warning": {
          const msg = String(n.params?.message ?? "");
          if (!msg) break;
          // 新的排前面（面板直接顺序渲染，不用再倒序）
          this.backendNotes = [
            {
              ts: Date.now(),
              level: String(n.params?.level ?? "WARNING"),
              logger: String(n.params?.logger ?? ""),
              message: msg,
            },
            ...this.backendNotes,
          ].slice(0, BACKEND_NOTE_LIMIT);
          // 顺带在横幅上露一次脸——否则用户得先想到去开诊断面板。
          // 只在横幅空着时写：不能把正在显示的运行错误顶掉。
          if (!this.banner) this.setBanner("后端告警：" + msg, "warn");
          break;
        }
        case "schedule.fired":
          if (n.params?.ran) {
            // 仅表示触发成功，可能晚于 run.finished；运行态以 run.* 为准。
            this.setBanner("定时触发：" + (n.params.path ?? ""), "ok");
          } else {
            this.setBanner(
              "定时触发跳过：" + (n.params?.reason ?? "忙"), "warn"
            );
          }
          break;
        // 注：sidecar 上下线走 Tauri 事件（见 init() 里的 rpc.onStatus），
        // 不再作为 NDJSON 通知处理——Rust 从未在 stdout 上发过这两个 method。
      }
    },

    onCapturedKey(h: CapturedKeyHandler): () => void {
      this._captured.add(h);
      return () => this._captured.delete(h);
    },
    /** 组件级原始通知订阅（如 template.snipped 回填）。 */
    onNotifyRaw(h: (n: JsonRpcNotification) => void): () => void {
      return rpc.onNotify(h);
    },
    async snipTemplate() {
      await rpc.request("template.snip");
    },
    async probeInput() {
      // alive: tap 收得到合成事件；text_alive: 收得到 Unicode 文本提交。
      // input_source: 当前输入法，以及它是否**已实测确认**不经过事件层
      //   （系统拼音就是这种：上屏走 insertText:，事件通道原理上覆盖不到）。
      // 三者要分开看——见 RecordPanel 的「文本链路自检」。
      return (await rpc.request("input.probe")) as {
        alive: boolean;
        text_alive?: boolean;
        input_source?: {
          id: string;
          name: string;
          event_channel_unsupported: boolean | null;
        };
      };
    },

    // ---- 节点 CRUD（均调后端，结构变更经 workflow.changed 回写） ----
    // 结构操作一律以 RPC 响应为准刷新（workflow.current）：
    // workflow.changed 通知经 Tauri 事件转发偶发丢失，会让页面停在旧状态
    // （「删除节点后页面不刷新」的根因）；通知仅作广播冗余。
    applyCurrent(cur: { workflow: Workflow; running?: boolean; recording?: boolean }) {
      this.applyWorkflow(cur.workflow, cur.running, cur.recording);
      this.applyLocation(cur);
      if (this.selectedIndex >= this.workflow.nodes.length)
        this.selectedIndex = this.workflow.nodes.length - 1;
    },
    /**
     * 把「当前这份在哪儿、存过没有」抄进 store。
     *
     * 判据是 `"path" in cur` 而不是 `typeof cur.path === "string"`：`workflow.new`
     * 之后后端给的是 `path: null`（还没落盘）。用 typeof 判的话这一帧会被整个跳过，
     * 于是刚新建完的界面仍显示着**上一份**的库 id——用户按「保存」时以为存的是
     * 新那份，实际覆盖掉了刚才打开的那份流程。
     */
    applyLocation(cur: any) {
      if (!cur || typeof cur !== "object") return;
      if ("path" in cur) this.workflowPath = String(cur.path ?? "");
      if ("lib_id" in cur) this.workflowLibCurrent = String(cur.lib_id ?? "");
      if (typeof cur.dirty === "boolean") this.workflowDirty = cur.dirty;
    },
    /**
     * uid → 当前 nodes 下标；找不到返回 -1。
     *
     * 画布上一切交互都以 uid 为单位（边的两端、Vue Flow 的节点 id 都是 uid），
     * 而后端 node.* 系列仍以 index 为单位，所以这里集中做一次换算。
     * 不要在组件里各写一份 `findIndex`——下标会随增删漂移，散着写迟早有一处忘记重算。
     */
    indexOfUid(uid: string): number {
      return this.workflow.nodes.findIndex((n) => n.uid === uid);
    },
    async addNode(type: string, x?: number, y?: number) {
      const r = await rpc.request("node.add", { type, x, y });
      this.applyCurrent(r);
      return r.index as number;
    },
    // 以下四个都是**按 uid 寻址**。别改回传下标：下标只在「送出那一刻、那一份
    // 列表」里成立，而画布上一次删多个节点会给每个 remove 各发一次请求，它们
    // 都按同一份旧状态算下标 → 第二笔起指向别的节点，**删错/改错且不报错**。
    // uid 在节点真被删掉之前一直有效。
    async removeNode(uid: string) {
      const selUid = this.workflow.nodes[this.selectedIndex]?.uid;
      const cur = await rpc.request("node.remove", { uid });
      this.applyCurrent(cur);
      // 选中要**跟着 uid 走**：删掉的是别的节点时下标会整体前移，
      // 只按 index 判断的话选中会悄悄挪到相邻的另一个节点上，
      // 而参数面板这时显示的是另一个节点的参数——用户一改就改错了对象。
      if (!selUid) return;
      if (!this.workflow.nodes.some((n) => n.uid === selUid)) this.selectNode(-1);
      else this.selectedIndex = this.indexOfUid(selUid);
    },
    async renameNode(uid: string, name: string) {
      const cur = await rpc.request("node.rename", { uid, name });
      this.applyCurrent(cur);
    },
    async toggleNode(uid: string, enabled: boolean) {
      const cur = await rpc.request("node.toggle", { uid, enabled });
      this.applyCurrent(cur);
    },
    async setParam(uid: string, key: string, value: any) {
      const cur = await rpc.request("node.params.set", { uid, key, value });
      this.applyCurrent(cur);
    },

    // ---- 画布：坐标与边（后端这几个方法本身就是 uid 为单位） ----
    /**
     * 保存节点坐标。**只在拖拽结束时调一次**——逐帧上报会把整份工作流
     * 广播几十次，workflow.changed 刷满通知队列，把运行状态类通知挤掉
     * （丢一条界面就可能永久停在旧状态）。
     */
    async setNodePos(uid: string, x: number, y: number) {
      const cur = await rpc.request("node.setPos", { uid, x, y });
      this.applyCurrent(cur);
    },
    /**
     * 连一条边。同一个 (源节点, 出口) 后端只保留一条——**后连的替换先连的**。
     * 返回这次连线对图的影响，调用方据此决定提示什么：
     * - `replaced`：旧连线被顶掉了。不说一声的话用户会以为「连了两条」；
     * - `cycleEdges`：这条边让图里出现了环，值是**环上的全部边**（含刚连的这条）。
     *   **环是合法的**（回边是循环的唯一表达方式），所以只提示、不拦——但
     *   「故意做循环」和「连错成环」在画布上长得一模一样，区别要到运行时撞
     *   `MAX_STEPS` 才显现，值得当场提一句。
     *   **返回边而不是 `true`**：节点一多，光给两个节点名用户照样找不到那条回路，
     *   把「找环」原样推回去等于没帮上忙。画布拿这些边直接标出来。
     *
     * `dstSide` 是画布上的落点侧（用户在哪一侧松手），**只影响线画在目标节点的
     * 哪条边上，不参与执行**；后端会把脏值收敛到默认侧。
     */
    async addEdge(
      src: string,
      dst: string,
      port: string = PORT_OUT,
      dstSide: string = DEFAULT_TARGET_SIDE
    ): Promise<{ replaced: boolean; cycleEdges: GraphEdge[] | null }> {
      // 两个判断都用**连线前**的图：
      // - `replaced` 只看「这个出口原来有没有边」，与图结构无关；
      //   注意这里用**全部**边而不是活边——后端按 (src, port) 替换时不看出口是否
      //   合法，被调小 case_count 后残留的那条同样会被顶掉，漏判会让用户以为连了两条。
      // - 环检测问的是「dst 能否走到 src」，旧边从 src 出发，用不到它。
      //   但必须用**活边**：残留的 `case:4` 边画布上不显示、执行器也不会走，
      //   拿它判环会提示一个用户根本看不见的环。
      const live = liveEdges(this.workflow.nodes, this.workflow.edges);
      const replaced = this.workflow.edges.some((e) => e.src === src && e.port === port);
      const loop = cycleEdges(live, { src, port, dst });
      const cur = await rpc.request("edge.add", { src, dst, port, dst_side: dstSide });
      this.applyCurrent(cur);
      return { replaced, cycleEdges: loop };
    },
    async removeEdge(src: string, port: string = PORT_OUT) {
      const cur = await rpc.request("edge.remove", { src, port });
      this.applyCurrent(cur);
    },
    /** 指定起始节点；传空串回到「第一个 start 节点 / 第一个节点」的默认规则。 */
    async setStart(uid: string) {
      const cur = await rpc.request("workflow.setStart", { uid });
      this.applyCurrent(cur);
    },
    /** 指定「哪个节点每次执行算一轮」（空串 = 取消） */
    async setRoundUid(uid: string) {
      await this.updateWorkflow({ round_uid: uid });
    },
    setClickThrough(enabled: boolean) {
      // invoke 在没有 Tauri 环境时会**同步抛错**（ReferenceError: window is
      // not defined），`.catch()` 只接得到异步拒绝、接不住同步抛——
      // 必须两层都兜，否则一个纯辅助功能能把调用方的流程带崩。
      try {
        void invoke("set_click_through", { enabled }).catch(() => {});
      } catch {
        /* 无 Tauri 环境：忽略 */
      }
    },
    clearRecord() {
      this.recordBuffer = [];
      this.lastRecordCount = 0;
      this.lastRecordInfo = null;
      this.recordCanUndo = false;
    },
    selectNode(index: number) {
      this.selectedIndex = index;
    },

    // ---- 工作流 ----
    /**
     * 新建一份空工作流。
     *
     * `force` 只在用户**已经确认过**「丢弃未保存的改动」时才是 true。后端对未保存
     * 的改动会报 -32006，界面据此弹确认框——这个判断刻意留在后端：前端漏了就会
     * 静默丢掉用户刚连好的一整张图，而那之后没有任何地方能找回来。
     */
    async newWorkflow(force = false) {
      const cur = await rpc.request("workflow.new", { force });
      this.applyCurrent(cur);
      this.selectedIndex = -1;
    },
    async updateWorkflow(patch: Partial<Workflow>) {
      const cur = await rpc.request("workflow.update", { patch });
      this.applyCurrent(cur);
    },
    /** 保存。**没入库过就自动入库**（后端负责），所以「新建 → 保存」不会失败。 */
    async saveWorkflow() {
      const r = await rpc.request("workflow.save");
      this.applyLocation(r);
      // 首次保存会在库里**新建**一条。不重拉的话用户接着打开「工作流库」
      // 会发现刚存的那份不在列表里，然后以为没存上、再存一遍。
      await this.refreshWorkflowLib();
    },
    async loadWorkflow() {
      const p = await open({ title: "从文件打开工作流", filters: [{ name: "工作流", extensions: ["json"] }] });
      if (typeof p === "string") {
        const cur = await rpc.request("workflow.load", { path: p });
        this.applyCurrent(cur);
        this.selectedIndex = -1;
        await this.refreshWorkflowLib();
      }
    },

    // ---- 工作流库（后端 core/workflow_lib.py）----
    /**
     * 拉一次库列表 + 当前这份的位置。
     *
     * `workflow.lib.list` 是**唯一的列表来源**（菜单标记、库弹层、定时运行里选
     * 工作流都读它），所以每次存/改/删/导入之后都要重拉——只改本地数组的话，
     * 另一处那份还是旧的。
     */
    async refreshWorkflowLib() {
      const r = await rpc.request("workflow.lib.list");
      this.workflowLib = normalizeMetas(r?.items);
      this.workflowLibCurrent = String(r?.current ?? "");
      this.workflowLibDir = String(r?.dir ?? "");
      if (typeof r?.dirty === "boolean") this.workflowDirty = r.dirty;
    },
    /** 从磁盘重新读取当前这份（改完 JSON 不用另存新的一条）。 */
    async reloadWorkflow(force = false) {
      const cur = await rpc.request("workflow.reload", { force });
      this.applyCurrent(cur);
      this.selectedIndex = -1;
    },

    /** 打开库里的一条。未保存的改动会被后端拦下（-32006），`force` 才丢弃。 */
    async openWorkflowFromLib(id: string, force = false) {
      const cur = await rpc.request("workflow.lib.open", { id, force });
      this.applyCurrent(cur);
      this.selectedIndex = -1;
    },
    /**
     * 打开库里的一条并**立刻开跑**。
     *
     * 不复用 `toggleRun()` 单独作为入口：它按 `running` 决定是「启动」还是
     * 「停止」，于是在运行中按这一条会**把正在跑的流程停掉**——而用户以为
     * 自己在启动另一份。这里显式拒掉，让用户自己决定先停谁。
     */
    async runWorkflowFromLib(id: string, force = false) {
      if (this.running) throw new Error("正在运行中，请先停止再运行另一份工作流");
      await this.openWorkflowFromLib(id, force);
      await this.toggleRun();
    },
    async renameWorkflowInLib(id: string, name: string) {
      const r = await rpc.request("workflow.lib.rename", { id, name });
      // 改的可能正是当前这份：后端已经把标题栏那份名字改了，这里同步一下
      if (String(r?.current ?? "") === id) {
        this.workflow = { ...this.workflow, name: String(r?.item?.name ?? name) };
      }
      await this.refreshWorkflowLib();
    },
    async deleteWorkflowFromLib(id: string) {
      const r = await rpc.request("workflow.lib.delete", { id });
      this.applyLocation(r);
      await this.refreshWorkflowLib();
    },
    /** 从文件导入一份进库。返回新条目的 id（用户取消则空串）。**不切换当前工作流**。 */
    async importWorkflowFromFile(): Promise<string> {
      const p = await open({ title: "导入工作流", filters: [{ name: "工作流", extensions: ["json"] }] });
      if (typeof p !== "string") return "";
      const r = await rpc.request("workflow.lib.import", { path: p });
      await this.refreshWorkflowLib();
      return String(r?.item?.id ?? "");
    },
    /** 导出成普通工作流文件。`wid` 空 = 导出当前这份。返回写出的路径（取消则空串）。 */
    async exportWorkflow(wid = ""): Promise<string> {
      const name = wid
        ? (this.workflowLib.find((m) => m.id === wid)?.name ?? "")
        : this.workflow.name;
      const p = await save({
        title: "导出工作流",
        // 默认文件名由工作流名推出来，但必须**洗过**：名字里带 `/` 时对话框会把
        // 文件悄悄写进一个子目录，而用户以为自己只是沿用了原来的名字。
        defaultPath: wfExportName(name),
        filters: [{ name: "工作流", extensions: ["json"] }],
      });
      if (typeof p !== "string") return "";
      await rpc.request("workflow.lib.export", { path: p, id: wid });
      return p;
    },

    // ---- 运行 / 录制 ----
    async toggleRun() {
      if (this.running) {
        // 停止是异步请求；正常等 run.finished 再退出运行态。
        this.noteLog("warn", "■ 请求停止运行");
        const r = await rpc.request("run.stop");
        // 兜底：后端已结束（通知可能已丢）时不会有 run.finished，用响应直接收敛。
        if (r && r.running === false) this.running = false;
      } else {
        // 发请求前置位，连续点击/F10 会走停止；失败时回滚。
        // 不在 await 后写 true：快速工作流可能已先发 run.finished。
        this.running = true;
        this.runNodeType = "";
        this.runNodeName = "";
        this.runNodeDetail = "";
        this.runTrace = [];
        this.runProgress = { done: 0, total: 0 };
        this.noteLog("info", "▶ 开始运行");
        try {
          await rpc.request("run.start", { base_x: this.base.x, base_y: this.base.y });
        } catch (e) {
          this.running = false;
          throw e;
        }
        // 回放防误触：主窗开启鼠标穿透，回放的点击不会被自己吃掉。
        // 停止只能用 F10（穿透期间窗口不接收点击）。
        //
        // 穿透是**辅助**行为，必须与 run.start 的成败解耦：并入上面那个 try 的话，
        // 一旦 invoke 不可用（无 Tauri 环境、命令未注册），已经把工作流跑起来的
        // 启动会被回滚成"启动失败"——运行明明起来了却显示失败。
        this.setClickThrough(true);
      }
    },
    /**
     * 开始/停止录制。
     *
     * `by` 必须由调用方显式给出（不给默认值）：**按钮停止**才有"停止点击"需要从
     * 序列尾部裁掉；**快捷键停止**的 F9 已由后端 skip_keys 排除，序列里没有停止
     * 动作，裁任何东西都只会误删用户内容。
     *
     * 这两个入口曾共用同一个默认 `trim: true`，于是按 F9 停止时后端会去找"最后一个
     * 落在窗口矩形内的按下"——那是个真实操作——并从它处把后面整段截掉（2026-09-12
     * 事故：一次 7.8 秒的移动录制被裁成 0 条）。
     */
    async toggleRecord(opts: { by: "button" | "hotkey" }) {
      if (this.recording) {
        // 窗口边界在**停止这一刻**取，避免用录制开始时的旧边界裁错位置。
        const r = await rpc.request("record.stop", {
          trim: opts.by === "button",
          window_bounds: await this.windowBounds(),
        });
        this.recording = false;
        this.lastRecordInfo = r;
        this.lastRecordCount = r.count ?? 0;
        // 后端在 record.stop 里把这次录制**自动入库**了（`library` 字段是那条的元信息）。
        // 不重拉列表的话，用户得切走再切回来才看得见刚录的那条。
        if (r?.library) {
          try {
            await this.refreshRecordingLib();
          } catch {
            /* 库列表是辅助信息，拉不到不影响"停止录制"本身已成功 */
          }
        }
        await this.recordSubscribe(false);
        await this.refreshRecordBuffer();
      } else {
        this.recordBuffer = [];
        // 边界仅用于停止时定位停止点击；drop_in_window 保持关闭——按窗口矩形
        // 丢弃事件会在边界过期时成片吞掉真实操作。
        await rpc.request("record.start", { window_bounds: await this.windowBounds() });
        this.recording = true;
        await this.recordSubscribe(true);
      }
    },
    async windowBounds(): Promise<[number, number, number, number] | null> {
      // 走 Rust 命令：前端 window API 需要额外 capability，缺权限时静默失败
      try {
        const b = (await invoke("window_bounds")) as [
          number, number, number, number,
        ];
        return [Math.round(b[0]), Math.round(b[1]), Math.round(b[2]), Math.round(b[3])];
      } catch {
        return null;
      }
    },
    /** 用后端返回的权威序列覆盖本地缓冲（录制结束、每次编辑后调用）。 */
    _applyRecord(r: any) {
      this.recordBuffer = r?.events ?? [];
      this.lastRecordCount = r?.count ?? 0;
      this.recordCanUndo = !!r?.can_undo;
      if (Array.isArray(r?.origin)) this.recordOrigin = r.origin;
      // `lib` 只在"这份数据来自录制库相关接口"时才带。用 `in` 判而不是 `??`——
      // 后端**故意**用 `lib: null` 表示"当前这段还没入库"，把它当"字段缺失"忽略掉
      // 会让界面一直显示上一次的"已同步"。
      if (r && typeof r === "object" && "lib" in r) {
        this.recordingLibCurrent = r.lib ?? null;
      }
    },
    /** 拉取后端权威事件序列覆盖本地缓冲，消除"看到的和写入的不一致"。 */
    async refreshRecordBuffer() {
      try {
        this._applyRecord(await rpc.request("record.current"));
      } catch {
        /* 保留增量缓冲 */
      }
    },
    /**
     * 事件编辑的统一入口：`record.remove` / `removeMovesBefore` / `setOrigin` /
     * `setText` / `keysToText` / `patch` / `insert` / `duplicate` / `moveEvent` /
     * `undo`。
     *
     * 后端返回的是**编辑后的完整权威序列**，直接覆盖本地缓冲——不在这里自己
     * 改数组：编辑种类一多，「前端算一遍、后端算一遍」迟早对不上。
     */
    async recordEdit(method: string, params: Record<string, unknown> = {}) {
      this._applyRecord(await rpc.request(method, params));
    },
    async recordSubscribe(on: boolean) {
      // §3.2：录制面板打开/录制开始时订阅事件流，关闭时退订，避免高频事件打爆管道。
      await rpc.request("record.subscribe", { on });
    },
    async recordToNode() {
      await rpc.request("record.toNode");
      // 主动拉一次真源：写入录制回放节点时 workflow.changed 是含全部事件的大帧
      // （实测 40KB+），WebView 在录制事件流刚结束的高负载下可能丢掉这条通知，
      // 表现为「节点写入了但列表不出现，刷新页面才恢复」。此处以响应为准刷新状态。
      const cur = await rpc.request("workflow.current");
      this.applyWorkflow(cur.workflow, cur.running, cur.recording);
    },
    async pickBase() {
      // arm 一次性取点：用户按任意键（通常 F11）后经 base.picked 通知回填
      await rpc.request("base.pick");
    },

    // ---- 录制库（后端 core/recording_lib.py）----
    /**
     * 拉一次库列表 + 当前条目状态。
     *
     * `record.lib.list` 是**唯一的列表来源**（参数面板的下拉也读它），所以每次
     * 存/改/删之后都要重拉——只更新本地数组的话，参数面板那份还是旧的，
     * 用户会看到「刚存的录制在下拉里选不到」。
     */
    async refreshRecordingLib() {
      const r = await rpc.request("record.lib.list");
      this.recordingLib = (r?.items ?? []) as RecMeta[];
      this.recordingLibCurrent = r?.current ?? null;
      this.recordingLibDir = r?.dir ?? "";
    },
    /** 把当前编辑缓冲存进库。`rid` 空 = 新建一条，给了 = **覆盖**那一条。 */
    async recordingLibSave(name: string, rid = "") {
      const r = await rpc.request("record.lib.save", { name, id: rid });
      this.recordingLibCurrent = r?.current ?? null;
      await this.refreshRecordingLib();
    },
    async recordingLibRename(rid: string, name: string) {
      const r = await rpc.request("record.lib.rename", { id: rid, name });
      this.recordingLibCurrent = r?.current ?? null;
      await this.refreshRecordingLib();
    },
    async recordingLibDelete(rid: string) {
      const r = await rpc.request("record.lib.delete", { id: rid });
      this.recordingLibCurrent = r?.current ?? null;
      await this.refreshRecordingLib();
    },
    /** 把库里一条**载入编辑缓冲**（替换当前录制），之后可继续编辑 / 更新 / 写入节点。 */
    async recordingLoadFromLib(rid: string) {
      this._applyRecord(await rpc.request("record.fromLib", { id: rid }));
    },
    /**
     * 进入「直接编辑库里这条」：载入它，之后**每次编辑自动写回**库里那条。
     *
     * 与 `recordingLoadFromLib`（「载入」按钮）的差别只在收尾——载入之后编辑只改
     * 内存、界面提示「改动未更新」，要再点「更新到库」；本动作把落盘也接管了，
     * 于是库里点开就能改。写回在**后端**统一做，前端不记账（编辑接口有十几个，
     * 漏一个就会「库里还是旧的、界面却显示已同步」）。
     */
    async recordingLibEdit(rid: string) {
      this._applyRecord(await rpc.request("record.lib.edit", { id: rid }));
    },
    /** 退出「直接编辑」模式，回到「改了要手动更新到库」的语义。 */
    async recordingLibEndEdit() {
      this._applyRecord(await rpc.request("record.lib.endEdit"));
    },
    /** 库里一条直接落成画布节点（按 id 引用），不用先载入缓冲。 */
    async recordingLibToNode(rid: string) {
      const cur = await rpc.request("record.lib.toNode", { id: rid });
      this.applyCurrent(cur);
    },

    // ---- 热键 / 捕获 / 权限 / 定时 ----
    async setHotkeys(actions: (string | null)[]) {
      await rpc.request("hotkey.set", { actions });
    },
    async clearHotkeys() {
      await rpc.request("hotkey.clear");
    },
    async startCapture() {
      await rpc.request("key.capture");
    },
    async stopCapture() {
      await rpc.request("key.capture.stop");
    },
    async requestPermissions() {
      await rpc.request("app.requestPermissions");
    },
    async openSettings(panel: string) {
      await rpc.request("app.openPermissionSettings", { panel });
    },
    async configureSchedule(cfg: any) {
      const r = await rpc.request("schedule.configure", cfg);
      this.schedule = r.schedule;
      this.nextFire = r.nextFire;
    },
    async refreshSchedule() {
      const r = await rpc.request("schedule.get");
      this.schedule = r.schedule;
      this.nextFire = r.nextFire;
    },
  },
});
