// 全局状态：后端是唯一真源（§10），前端近乎无状态——只持有视图态、表单草稿、运行/录制态镜像。
import { defineStore } from "pinia";
import { errMessage, rpc } from "@/rpc/client";
import type {
  JsonRpcNotification,
  NodeDefinition,
  Permissions,
  Workflow,
} from "@/rpc/types";
import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";

export const PROTOCOL_VERSION = 1;

/**
 * 录制事件缓冲上限。超出后丢最旧的——完整数据在后端内存树里，
 * 前端这份只用于实时预览，不参与回放/保存。
 */
export const RECORD_BUFFER_LIMIT = 5000;

type CapturedKeyHandler = (name: string) => void;

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
    protocolOk: true,
    appVersion: "",
    running: false,
    recording: false,
    lastRecordCount: 0,
    lastRecordInfo: null as any,
    permissions: { accessibility: false, inputMonitoring: false, screenRecording: false } as Permissions,
    workflow: { name: "未命名", speed: 1.0, repeat: 1, nodes: [] } as Workflow,
    definitions: [] as NodeDefinition[],
    selectedIndex: -1,
    base: { x: 0, y: 0 },
    schedule: null as any,
    nextFire: "",
    recordBuffer: [] as any[],
    runProgress: { done: 0, total: 0 } as { done: number; total: number },
    runNodeType: "" as string,
    banner: "" as string,
    bannerKind: "ok" as "ok" | "warn" | "error",
    // key.captured 订阅（ParamsPanel 捕获键时接收回填）
    _captured: new Set<CapturedKeyHandler>(),
    // 合并并发握手（首连与 rpc_up 事件可能同时触发）
    _handshaking: false,
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
        permissions: {
          accessibility: !!p.accessibility,
          inputMonitoring: !!p.inputMonitoring,
          screenRecording: !!p.screenRecording,
        },
        workflowName: state.workflow?.name ?? "",
        nodeCount: nodes.length,
        enabledNodeCount: nodes.filter((n) => n?.enabled).length,
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
          this.setBanner(
            first
              ? `后端 sidecar 断开，正在重连…（${first}）`
              : "后端 sidecar 断开，正在重连…",
            "warn"
          );
          if (detail) console.error("[rpc_down]", detail);
        }
      });
      await this.handshake();
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
          this.applyWorkflow(cur.workflow, cur.running, cur.recording);
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
      this.workflow = wf;
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
          if (a === "record") this.toggleRecord();
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
        case "run.node":
          this.running = true;
          this.runNodeType = n.params.type ?? "";
          break;
        case "run.progress":
          this.runProgress = { done: n.params.done, total: n.params.total };
          break;
        case "run.finished":
          this.running = false;
          this.runNodeType = "";
          this.runProgress = { done: 0, total: 0 };
          this.setClickThrough(false);
          break;
        case "run.error":
          this.running = false;
          this.setClickThrough(false);
          this.setBanner("运行出错：" + (n.params.message ?? ""), "error");
          break;
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
      return (await rpc.request("input.probe")) as { alive: boolean };
    },

    // ---- 节点 CRUD（均调后端，结构变更经 workflow.changed 回写） ----
    // 结构操作一律以 RPC 响应为准刷新（workflow.current）：
    // workflow.changed 通知经 Tauri 事件转发偶发丢失，会让页面停在旧状态
    // （「删除节点后页面不刷新」的根因）；通知仅作广播冗余。
    applyCurrent(cur: { workflow: Workflow; running?: boolean; recording?: boolean }) {
      this.applyWorkflow(cur.workflow, cur.running, cur.recording);
      if (this.selectedIndex >= this.workflow.nodes.length)
        this.selectedIndex = this.workflow.nodes.length - 1;
    },
    async addNode(type: string, index?: number) {
      const r = await rpc.request("node.add", { type, index });
      this.applyCurrent(r);
      return r.index as number;
    },
    async removeNode(index: number) {
      const cur = await rpc.request("node.remove", { index });
      this.applyCurrent(cur);
      if (this.selectedIndex === index) this.selectNode(-1);
    },
    async moveNode(index: number, to: number) {
      const cur = await rpc.request("node.move", { index, to });
      this.applyCurrent(cur);
    },
    async renameNode(index: number, name: string) {
      const cur = await rpc.request("node.rename", { index, name });
      this.applyCurrent(cur);
    },
    async toggleNode(index: number, enabled: boolean) {
      const cur = await rpc.request("node.toggle", { index, enabled });
      this.applyCurrent(cur);
    },
    async setParam(index: number, key: string, value: any) {
      const cur = await rpc.request("node.params.set", { index, key, value });
      this.applyCurrent(cur);
    },
    setClickThrough(enabled: boolean) {
      invoke("set_click_through", { enabled }).catch(() => {});
    },
    clearRecord() {
      this.recordBuffer = [];
      this.lastRecordCount = 0;
      this.lastRecordInfo = null;
    },
    selectNode(index: number) {
      this.selectedIndex = index;
    },

    // ---- 工作流 ----
    async newWorkflow() {
      const cur = await rpc.request("workflow.new");
      this.applyCurrent(cur);
      this.selectedIndex = -1;
    },
    async updateWorkflow(patch: Partial<Workflow>) {
      const cur = await rpc.request("workflow.update", { patch });
      this.applyCurrent(cur);
    },
    async loadWorkflow() {
      const p = await open({ title: "打开工作流", filters: [{ name: "工作流", extensions: ["json"] }] });
      if (typeof p === "string") {
        const cur = await rpc.request("workflow.load", { path: p });
        this.applyWorkflow(cur.workflow, cur.running, cur.recording);
        this.selectedIndex = -1;
      }
    },
    async saveWorkflow() {
      await rpc.request("workflow.save");
    },
    async saveWorkflowAs() {
      const p = await save({ title: "另存为", defaultPath: "workflow.json", filters: [{ name: "工作流", extensions: ["json"] }] });
      if (typeof p === "string") await rpc.request("workflow.save", { path: p });
    },

    // ---- 运行 / 录制 ----
    async toggleRun() {
      if (this.running) {
        // 停止是异步请求；正常等 run.finished 再退出运行态。
        const r = await rpc.request("run.stop");
        // 兜底：后端已结束（通知可能已丢）时不会有 run.finished，用响应直接收敛。
        if (r && r.running === false) this.running = false;
      } else {
        // 发请求前置位，连续点击/F10 会走停止；失败时回滚。
        // 不在 await 后写 true：快速工作流可能已先发 run.finished。
        this.running = true;
        this.runNodeType = "";
        this.runProgress = { done: 0, total: 0 };
        try {
          await rpc.request("run.start", { base_x: this.base.x, base_y: this.base.y });
          // 回放防误触：主窗开启鼠标穿透，回放的点击不会被自己吃掉。
          // 停止只能用 F10（穿透期间窗口不接收点击）。
          await invoke("set_click_through", { enabled: true });
        } catch (e) {
          this.running = false;
          this.setClickThrough(false);
          throw e;
        }
      }
    },
    async toggleRecord(via: "button" | "hotkey" = "hotkey") {
      if (this.recording) {
        // via=button：点「停止录制」按钮的那次点击本身会被录进事件序列，
        // 必须让后端裁掉停止交互（含移向按钮的移动），否则回放会复现它。
        const r = await rpc.request("record.stop", { trim: via === "button" });
        this.recording = false;
        this.lastRecordInfo = r;
        this.lastRecordCount = r.count ?? 0;
        await this.recordSubscribe(false);
      } else {
        this.recordBuffer = [];
        await rpc.request("record.start");
        this.recording = true;
        await this.recordSubscribe(true);
      }
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
