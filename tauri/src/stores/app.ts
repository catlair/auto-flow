// 全局状态：后端是唯一真源（§10），前端近乎无状态——只持有视图态、表单草稿、运行/录制态镜像。
import { defineStore } from "pinia";
import { rpc } from "@/rpc/client";
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
    protocolOk: true,
    appVersion: "",
    running: false,
    recording: false,
    lastRecordCount: 0,
    permissions: { accessibility: false, inputMonitoring: false, screenRecording: false } as Permissions,
    workflow: { name: "未命名", speed: 1.0, repeat: 1, nodes: [] } as Workflow,
    definitions: [] as NodeDefinition[],
    selectedIndex: -1,
    base: { x: 0, y: 0 },
    schedule: null as any,
    nextFire: "",
    recordBuffer: [] as any[],
    runProgress: { done: 0, total: 0 } as { done: number; total: number },
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
        this.setBanner("无法连接后端 sidecar：" + String((e as Error).message));
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
    },

    applyWorkflow(wf: Workflow, running?: boolean, recording?: boolean) {
      this.workflow = wf;
      if (typeof running === "boolean") this.running = running;
      if (typeof recording === "boolean") this.recording = recording;
      if (this.selectedIndex >= wf.nodes.length) this.selectedIndex = wf.nodes.length - 1;
    },

    handleNotification(n: JsonRpcNotification) {
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
          else if (a === "pick") this.pickBase();
          break;
        }
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
          break;
        case "run.node":
          break;
        case "run.progress":
          this.runProgress = { done: n.params.done, total: n.params.total };
          break;
        case "run.finished":
          this.running = false;
          this.runProgress = { done: 0, total: 0 };
          break;
        case "run.error":
          this.running = false;
          this.setBanner("运行出错：" + (n.params.message ?? ""), "error");
          break;
        case "schedule.fired":
          this.setBanner("定时触发：" + (n.params.path ?? ""), "ok");
          break;
        case "rpc_down":
          this.connected = false;
          this.setBanner("后端 sidecar 断开，正在重连…", "warn");
          break;
        case "rpc_up":
          this.connected = true;
          this.clearBanner();
          break;
      }
    },

    onCapturedKey(h: CapturedKeyHandler): () => void {
      this._captured.add(h);
      return () => this._captured.delete(h);
    },

    // ---- 节点 CRUD（均调后端，结构变更经 workflow.changed 回写） ----
    async addNode(type: string, index?: number) {
      const r = await rpc.request("node.add", { type, index });
      return r.index as number;
    },
    async removeNode(index: number) {
      await rpc.request("node.remove", { index });
    },
    async moveNode(index: number, to: number) {
      await rpc.request("node.move", { index, to });
    },
    async toggleNode(index: number, enabled: boolean) {
      await rpc.request("node.toggle", { index, enabled });
    },
    async setParam(index: number, key: string, value: any) {
      await rpc.request("node.params.set", { index, key, value });
    },
    selectNode(index: number) {
      this.selectedIndex = index;
    },

    // ---- 工作流 ----
    async newWorkflow() {
      await rpc.request("workflow.new");
      this.selectedIndex = -1;
    },
    async updateWorkflow(patch: Partial<Workflow>) {
      await rpc.request("workflow.update", { patch });
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
        await rpc.request("record.stop");
        await this.recordSubscribe(false);
      } else {
        this.recordBuffer = [];
        await rpc.request("record.start");
        await this.recordSubscribe(true);
      }
    },
    async recordSubscribe(on: boolean) {
      // §3.2：录制面板打开/录制开始时订阅事件流，关闭时退订，避免高频事件打爆管道。
      await rpc.request("record.subscribe", { on });
    },
    async recordToNode() {
      await rpc.request("record.toNode");
    },
    async pickBase() {
      const r = await rpc.request("base.pick");
      this.base = { x: r.x, y: r.y };
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
