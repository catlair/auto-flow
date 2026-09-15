<script setup lang="ts">
// 流程图画布（v4）。替代原来的线性节点列表 NodeList.vue。
//
// 为什么必须是画布而不是列表：v3 的「有序列表 + run_when 门控」只能表达
// 「某个全局条件成立/不成立时，后续节点跑不跑」——同一条件下所有节点共用
// 一个标志，无法表达「成立走 A 组、不成立走 B 组、两组再汇合」。
// 换成 节点 + 有向边 之后，这些都是一等公民。
//
// 后端是唯一真源：这里只把 store 里的工作流投影成画布的 nodes/edges，
// 任何交互都发 RPC、等响应回来再刷新。**不在本地乐观改边**——
// 乐观改完之后如果后端拒绝（自环、节点已删），画布会停在和后端不一致的状态。
import { computed, nextTick, ref, shallowRef, watch } from "vue";
import {
  VueFlow,
  Handle,
  Position,
  ConnectionMode,
  MarkerType,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type NodeDragEvent,
  type NodeMouseEvent,
  type VueFlowStore,
} from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
import { MessagePlugin } from "tdesign-vue-next";
import { useAppStore } from "@/stores/app";
import { errMessage } from "@/rpc/client";
import { PORT_OUT, portLabel, exitPorts, canConnectFrom, canConnectTo } from "@/flow/ports";
import { nodeSummary } from "@/flow/summary";
import { createFitOnce } from "@/flow/fitOnce";

const store = useAppStore();

/** 自定义节点类型名。用常量而不是到处写字面量，改名时不会漏掉一处。 */
const NODE_TYPE = "af";
/** 目标手柄的 id。必须与模板里 `<Handle type="target" :id>` 一致，否则边渲染不出来。 */
const TARGET_HANDLE = "in";

const flow = shallowRef<VueFlowStore | null>(null);
const wrapEl = ref<HTMLElement | null>(null);

const defs = computed(() => store.nodeDefsByName);

/** 节点投影。每次重建新对象：Vue Flow 会往收到的节点上挂 dimensions/selected 等字段，
 *  直接传 store 里的对象等于让库改真源。 */
const vNodes = computed(() =>
  store.workflow.nodes.map((n) => ({
    id: n.uid,
    type: NODE_TYPE,
    position: { x: n.x ?? 0, y: n.y ?? 0 },
    data: {
      type: n.type,
      enabled: n.enabled,
      typeName: defs.value[n.type]?.name ?? n.type,
      customName: (n.name || "").trim(),
      summary: nodeSummary(n, defs.value),
      isEntry: store.entryUid === n.uid,
      exits: exitPorts(n.type, n.params?.case_count),
      canIn: canConnectTo(n.type),
      canOut: canConnectFrom(n.type),
    },
  }))
);

/**
 * 边投影。两类边不渲染：
 *
 * - `dst` 为空：后端允许存「这个出口留空」，它与「这条边不存在」在执行上等价，
 *   画布上画一条没有终点的线只会让人以为连上了。
 * - 源节点/目标节点已不在：手工编辑的 json 才可能出现，画出来是悬空的。
 *
 * 出口名对源节点已不合法（如 case_count 从 5 调回 3）时同样跳过：那个手柄
 * 不存在，Vue Flow 找不到锚点，会在控制台刷告警且画在奇怪的位置。
 * 边本身留着——把 case_count 调回去它还在，只是执行器不会走到（分支不会发出
 * 那个出口），这正是「不删数据、只不显示」的预期行为。
 */
const vEdges = computed(() => {
  const byUid = new Map(store.workflow.nodes.map((n) => [n.uid, n]));
  const out = [];
  for (const e of store.workflow.edges) {
    const src = byUid.get(e.src);
    const dst = byUid.get(e.dst);
    if (!src || !dst) continue;
    if (!exitPorts(src.type, src.params?.case_count).includes(e.port)) continue;
    out.push({
      id: `${e.src}|${e.port}`,
      source: e.src,
      target: e.dst,
      sourceHandle: e.port,
      targetHandle: TARGET_HANDLE,
      label: portLabel(e.port),
      labelBgStyle: { fill: "#ffffff", fillOpacity: 0.85 },
      labelStyle: { fontSize: "10px", fill: "#64748b" },
      style: { stroke: "#94a3b8", strokeWidth: 1.4 },
      markerEnd: MarkerType.ArrowClosed,
    });
  }
  return out;
});

// ---- 交互 ----

/**
 * 自环在这里就拦掉。
 * 后端也会拒（`edge.add` 里判 src == dst），但在那里拒是「先连上、再被弹回」，
 * 画布会闪一下；这里直接不让连，手感干净。
 * 其余合法性（出口名、目标是否存在）由手柄本身保证——结束节点没有源手柄、
 * 开始节点没有目标手柄，连不出来。
 */
function isValidConnection(c: Connection): boolean {
  return c.source !== c.target;
}

function onConnect(c: Connection) {
  const port = c.sourceHandle || PORT_OUT;
  void store
    .addEdge(c.source, c.target, port)
    .then((replaced) => {
      // 一个出口只允许一条边，后连的替换先连的。不说一声的话，
      // 用户会以为「连了两条」，实际旧的那条已经没了。
      if (replaced) MessagePlugin.info(`已替换「${portLabel(port)}」出口原有的连线`);
    })
    .catch((e) => MessagePlugin.error("连线失败：" + errMessage(e)));
}

/**
 * 拖拽**结束**才落库。逐帧上报会把整份工作流广播几十次，
 * workflow.changed 刷满通知队列，把运行状态类通知挤掉。
 */
function onNodeDragStop(e: NodeDragEvent) {
  void store
    .setNodePos(e.node.id, e.node.position.x, e.node.position.y)
    .catch((err) => MessagePlugin.error("保存位置失败：" + errMessage(err)));
}

function onNodeClick(e: NodeMouseEvent) {
  store.selectNode(store.indexOfUid(e.node.id));
}

function onPaneClick() {
  store.selectNode(-1);
}

/**
 * 画布内的删除（选中后按 Delete）走和后端一样的两条路径：删节点 / 删边。
 *
 * 一次可能来**多个** remove（Shift 多选 / 框选后按 Delete）。这里必须**串行
 * await**：早先写成 `void removeByUid(ch.id)` 并发发出，每个请求都按同一份
 * 「删除前」的节点列表把 uid 换算成下标 → 第二笔起下标错位，后端删掉的是
 * **别的节点**且不报错。现在寻址已改成 uid（后端不再依赖下标），但**仍然串行**：
 * 并发的 `workflow.changed` 会互相覆盖前端状态，画布会闪一下。
 */
async function onNodesChange(changes: NodeChange[]) {
  for (const ch of changes) {
    if (ch.type === "remove") await removeByUid(ch.id);
  }
}

function onEdgesChange(changes: EdgeChange[]) {
  for (const ch of changes) {
    if (ch.type !== "remove") continue;
    const port = ch.sourceHandle || PORT_OUT;
    void store
      .removeEdge(ch.source, port)
      .catch((e) => MessagePlugin.error("删除连线失败：" + errMessage(e)));
  }
}

async function removeByUid(uid: string) {
  // 前端不认识这个 uid 就静默跳过：重复的 remove 事件（或别处已经删掉）不该
  // 给用户弹一个「删除失败」。注意这里只用来判「有没有这个节点」，
  // **不再把 uid 换算成下标送给后端**——见 store.removeNode 的说明。
  if (store.indexOfUid(uid) < 0) return;
  try {
    await store.removeNode(uid);
  } catch (e) {
    MessagePlugin.error("删除失败：" + errMessage(e));
  }
}

async function toggleByUid(uid: string, v: boolean) {
  if (store.indexOfUid(uid) < 0) return;
  try {
    await store.toggleNode(uid, v);
  } catch (e) {
    MessagePlugin.error("切换失败：" + errMessage(e));
  }
}

// 改名：画布上双击节点名就地编辑
const renamingUid = ref("");
const renameText = ref("");
const renameEl = ref<HTMLInputElement | null>(null);

function startRename(uid: string, current: string) {
  renamingUid.value = uid;
  renameText.value = current;
  void nextTick(() => {
    renameEl.value?.focus();
    renameEl.value?.select();
  });
}

async function commitRename(uid: string, fallback: string) {
  if (renamingUid.value !== uid) return;
  renamingUid.value = "";
  const name = renameText.value.trim();
  if (name === fallback) return;
  if (store.indexOfUid(uid) < 0) return;
  try {
    await store.renameNode(uid, name);
  } catch (e) {
    MessagePlugin.error("改名失败：" + errMessage(e));
  }
}

// ---- 添加节点 ----

/**
 * 新节点落在**当前视口中心**，而不是固定的 (80, 80)。
 *
 * 画布可以平移缩放，固定坐标在用户把视野挪走之后会落在屏幕外——
 * 表现就是「点了添加，什么都没发生」，而节点其实加上了。
 */
function newPos(): { x: number; y: number } {
  const vp = flow.value?.getViewport?.() ?? { x: 0, y: 0, zoom: 1 };
  const zoom = Number(vp.zoom) || 1;
  const el = wrapEl.value;
  const w = el?.clientWidth || 640;
  const h = el?.clientHeight || 420;
  // 屏幕坐标 → 画布坐标：先减去视口平移，再除以缩放
  const cx = (w / 2 - (Number(vp.x) || 0)) / zoom;
  const cy = (h / 2 - (Number(vp.y) || 0)) / zoom;
  // 错开一点，连点添加时不会叠成一个点
  const j = (store.workflow.nodes.length % 5) * 26;
  return { x: Math.round(cx - 70 + j), y: Math.round(cy - 18 + j) };
}

async function onAdd(type: string) {
  try {
    const p = newPos();
    const idx = await store.addNode(type, p.x, p.y);
    store.selectNode(idx);
  } catch (e) {
    MessagePlugin.error("添加失败：" + errMessage(e));
  }
}

async function markStart() {
  const uid = store.selectedNode?.uid;
  if (!uid) return;
  try {
    await store.setStart(uid);
  } catch (e) {
    MessagePlugin.error("设置起点失败：" + errMessage(e));
  }
}

async function clearStart() {
  try {
    await store.setStart("");
  } catch (e) {
    MessagePlugin.error("恢复默认起点失败：" + errMessage(e));
  }
}

// ---- 视口 ----

// 首次拿到非空工作流时自动适应一下视野。只在「从空变非空」时做，
// 否则每次编辑都会把用户刚调整好的视野拉走。
//
// 状态机本体在 @/flow/fitOnce（纯模块，可被单测钉住）。这里只负责把两个
// **必须分开**的时机接上：
//   maybeFit() —— store 里有节点了（数据到位）
//   tryFit()   —— Vue Flow 发 nodes-initialized（尺寸量完，这时 fit 才有意义）
// 早先把它写成「数据一到就 nextTick(fitView)」，节点尺寸还没量，等于没调——
// 表现是打开已有工作流后只看到左上角一两个节点，其余在视野外。
const fitOnce = createFitOnce(() => void flow.value?.fitView({ padding: 0.2 }));

function maybeFit() {
  fitOnce.request(store.workflow.nodes.length > 0);
}

/**
 * 真正执行 fitView 的时机。**必须等 Vue Flow 量完节点尺寸**才能调。
 *
 * 正确信号是 `nodesInitialized`：它在**所有节点都有非零尺寸**时才发；新增节点会
 * 先把它落回 false、量完再变 true，所以每次拿到新节点都能等到一次。
 */
function tryFit() {
  if (!flow.value) return;
  fitOnce.commit();
}

function onFlowInit(instance: VueFlowStore) {
  flow.value = instance;
  maybeFit();
}

watch(
  () => store.workflow.nodes.length,
  (n) => {
    if (!n) {
      fitOnce.reset();
      return;
    }
    maybeFit();
  }
);
</script>

<template>
  <div ref="wrapEl" class="af-flow-wrap">
    <div class="af-flow-bar">
      <t-select
        placeholder="添加节点…"
        :value="null"
        size="small"
        style="width: 132px"
        @change="onAdd"
      >
        <t-option v-for="d in store.definitions" :key="d.type" :value="d.type" :label="d.name" />
      </t-select>
      <t-button
        size="small"
        variant="outline"
        :disabled="store.selectedIndex < 0"
        @click="markStart"
        >设为起点</t-button
      >
      <t-button
        size="small"
        variant="outline"
        :disabled="!store.workflow.start"
        @click="clearStart"
        >默认起点</t-button
      >
    </div>

    <div class="af-canvas">
      <VueFlow
        :nodes="vNodes"
        :edges="vEdges"
        :connection-mode="ConnectionMode.Strict"
        :is-valid-connection="isValidConnection"
        :delete-key-code="'Delete'"
        :min-zoom="0.2"
        :max-zoom="2.5"
        :default-viewport="{ x: 40, y: 40, zoom: 1 }"
        @init="onFlowInit"
        @connect="onConnect"
        @node-drag-stop="onNodeDragStop"
        @node-click="onNodeClick"
        @pane-click="onPaneClick"
        @nodes-change="onNodesChange"
        @edges-change="onEdgesChange"
        @nodes-initialized="tryFit"
      >
        <Background :gap="16" />
        <Controls position="bottom-right" />

        <template #node-af="p">
          <div
            class="af-gnode"
            :class="{ sel: p.selected, off: !p.data.enabled, entry: p.data.isEntry }"
          >
            <!-- 开始节点没有入边：它本身就是入口，能连进来就意味着「别处还能跳进来」，
                 而执行器只从 start 字段进——画布看着连通、实际不会执行。 -->
            <Handle
              v-if="p.data.canIn"
              type="target"
              :id="TARGET_HANDLE"
              :position="Position.Left"
              class="af-handle af-handle-in"
            />

            <div class="af-gnode-head">
              <span v-if="p.data.isEntry" class="af-badge" title="起始节点">起</span>
              <span
                v-if="renamingUid !== p.id"
                class="af-gnode-name"
                :title="(p.data.customName || p.data.typeName) + '（双击改名）'"
                @dblclick.stop="startRename(p.id, p.data.customName)"
                >{{ p.data.customName || p.data.typeName }}</span
              >
              <input
                v-else
                ref="renameEl"
                v-model="renameText"
                class="af-rename nodrag"
                @click.stop
                @keydown.enter.prevent="commitRename(p.id, p.data.customName)"
                @keydown.esc="renamingUid = ''"
                @blur="commitRename(p.id, p.data.customName)"
              />
              <span class="af-gnode-actions nodrag">
                <t-switch
                  :value="p.data.enabled"
                  size="small"
                  @change="(v: boolean) => toggleByUid(p.id, v)"
                />
                <button class="af-del" title="删除节点" @click.stop="removeByUid(p.id)">×</button>
              </span>
            </div>

            <div v-if="p.data.summary" class="af-gnode-sum" :title="p.data.summary">
              {{ p.data.summary }}
            </div>
            <div v-if="!p.data.enabled" class="af-gnode-off">已停用（执行时跳过）</div>

            <!-- 出口。多出口时一行一个，手柄挂在行右端——都在节点中线上会叠成一坨。 -->
            <div v-if="p.data.exits.length > 1" class="af-exits">
              <div v-for="port in p.data.exits" :key="port" class="af-exit">
                <span class="af-exit-label">{{ portLabel(port) }}</span>
                <Handle
                  type="source"
                  :id="port"
                  :position="Position.Right"
                  class="af-handle af-handle-out"
                />
              </div>
            </div>
            <Handle
              v-else-if="p.data.canOut"
              type="source"
              :id="p.data.exits[0] || PORT_OUT"
              :position="Position.Right"
              class="af-handle af-handle-out"
            />
          </div>
        </template>
      </VueFlow>

      <div v-if="!store.workflow.nodes.length" class="af-flow-empty">
        画布是空的。从左上角「添加节点」开始，或打开一个已有工作流。
      </div>
    </div>
  </div>
</template>

<style scoped>
.af-flow-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1;
  min-height: 0;
}
.af-flow-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.af-canvas {
  position: relative;
  flex: 1;
  min-height: 320px;
  border: 1px solid #eceef1;
  border-radius: 8px;
  background: #fbfcfd;
  overflow: hidden;
}
.af-flow-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #aaa;
  font-size: 12px;
  pointer-events: none;
}

/* ---- 节点卡片 ---- */
.af-gnode {
  min-width: 148px;
  max-width: 220px;
  /* 必须是定位元素：Vue Flow 的手柄是 position:absolute + right:0 + translate(50%),
     锚在**最近的定位祖先**的右边缘。不写的话手柄会锚到 .vue-flow__node 上，
     而写了之后锚到卡片本身——两种出口（单出口/多出口）才落在同一条竖线上。 */
  position: relative;
  background: #fff;
  border: 1px solid #dfe3e8;
  border-radius: 8px;
  padding: 6px 10px 8px;
  font-size: 12px;
  color: #1f2937;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}
.af-gnode.sel {
  border-color: #0052d9;
  box-shadow: 0 0 0 2px rgba(0, 82, 217, 0.14);
}
.af-gnode.entry {
  border-left: 3px solid #059669;
}
.af-gnode.off {
  background: #fafafa;
  color: #9aa0a6;
}
.af-gnode-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.af-badge {
  flex: none;
  background: #e6f6ee;
  color: #057a4a;
  border-radius: 4px;
  padding: 0 4px;
  font-size: 10px;
  line-height: 16px;
}
.af-gnode-name {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: text;
}
.af-gnode-actions {
  flex: none;
  display: flex;
  align-items: center;
  gap: 4px;
}
.af-del {
  border: none;
  background: none;
  color: #c0c4cc;
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
  padding: 0 2px;
}
.af-del:hover {
  color: #e34d59;
}
.af-gnode-sum {
  margin-top: 3px;
  color: #6b7280;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.af-gnode-off {
  margin-top: 3px;
  color: #ed7b2f;
  font-size: 10px;
}
.af-rename {
  flex: 1;
  min-width: 0;
  border: 1px solid #0052d9;
  border-radius: 4px;
  padding: 1px 4px;
  font-size: 12px;
  outline: none;
}

/* 多出口：一行一个。手柄用绝对定位挂在行右端——
   `.af-exit` 是 position:relative，库自带的 .vue-flow__handle-right
   （top:50%; right:0; translate(50%,-50%)）于是落在这一行的中线上。 */
.af-exits {
  margin-top: 4px;
  border-top: 1px dashed #eceef1;
  padding-top: 2px;
}
.af-exit {
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  /* 给手柄让位。**不能**在这里写 position: relative——那会把手柄的锚点从卡片
     挪到这一行上（手柄会内缩 10px 并**正好压住标签最后一个字**：「情形 1」的
     数字被圆点盖掉，用户分不清哪条边是情形几）。留出的宽度 = 手柄半径 + 余量。 */
  padding-right: 8px;
}
.af-exit-label {
  color: #64748b;
  font-size: 11px;
}

/* 手柄本身：库默认 5px、灰底，在画布上几乎看不见，这里放大加色。
   注意库对非 connectable 的手柄给 pointer-events: none。 */
.af-handle {
  width: 9px;
  height: 9px;
  border: 1.5px solid #fff;
  background: #0052d9;
}
.af-handle-in {
  background: #94a3b8;
}
</style>
