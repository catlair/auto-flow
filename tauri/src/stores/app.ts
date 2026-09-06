// 全局状态：后端是唯一真源（§10），前端近乎无状态——只持有视图态、表单草稿、运行/录制态镜像。
import { defineStore } from "pinia";
import { errMessage, rpc } from "@/rpc/client";
import type {
  JsonRpcNotification,
  NodeDefinition,
  Permissions,
  Workflow,
} from "@/rpc/types";
import { open, save } from "@tauri-apps/plugin-dialog";

export const PROTOCOL_VERSION = 1;

type CapturedKeyHandler = (name: string) => void;

export const useAppStore = defineStore("app", {
  state: () => ({
    connected: false,
    // Rust 侧最近一次断连原因（含查找路径 / sidecar stderr），用于给出可读提示。
    rpcDownDetail: "",
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
  },
  actions: {
    setBanner(msg: string, kind: "ok" | "warn" | "error" = "error") {
      this.banner = msg;
      this.bannerKind = kind;
    },
    clearBanner() {
      this.banner = "";
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
        this.connected = up;
        if (up) {
          this.rpcDownDetail = "";
          this.clearBanner();
        } else {
          this.rpcDownDetail = detail || "";
          // detail 可能含查找路径 + sidecar 最近 stderr：横幅只显示首行，全文进 console。
          const first =
            (detail || "").split("\n")[0] || "后端 sidecar 断开，正在重连…";
          this.setBanner(first, "warn");
          if (detail) console.error("[rpc_down]", detail);
        }
      });
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
      // 后端热键监听从未装定，表现为「没有录制/结束运行快捷键」。启动时统一装定默认绑定。
      try {
        await rpc.request("hotkey.set", { actions: ["record", "run", "pick"] });
      } catch {
        /* ignore */
      }
    },

    applyWorkflow(wf: Workflow, running?: boolean, recording?: boolean) {
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
          this.applyWorkflow(n.params.workflow);
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
          if (this.recordBuffer.length > 5000) {
            this.recordBuffer.splice(0, this.recordBuffer.length - 5000);
          }
          break;
        case "record.stopped":
          this.recording = false;
          this.lastRecordCount = n.params.count ?? 0;
          this.lastRecordInfo = n.params;
          break;
        case "run.node":
          this.runNodeType = n.params.type ?? "";
          break;
        case "run.progress":
          this.runProgress = { done: n.params.done, total: n.params.total };
          break;
        case "run.finished":
          this.running = false;
          this.runNodeType = "";
          this.runProgress = { done: 0, total: 0 };
          break;
        case "run.error":
          this.running = false;
          this.setBanner("运行出错：" + (n.params.message ?? ""), "error");
          break;
        case "schedule.fired":
          if (n.params?.ran) {
            this.running = true;
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
        await rpc.request("run.stop");
      } else {
        await rpc.request("run.start", { base_x: this.base.x, base_y: this.base.y });
      }
    },
    async toggleRecord() {
      if (this.recording) {
        // 统计直接取响应回填：record.stopped 通知在大帧/高频事件流下可能被
        // WebView 丢弃，若只依赖通知，写入节点按钮会因 lastRecordCount=0 永远禁用。
        const r = await rpc.request("record.stop");
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
