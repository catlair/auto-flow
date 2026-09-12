<script setup lang="ts">
import { ref, watch } from "vue";
import draggable from "vuedraggable";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

import { errMessage } from "@/rpc/client";
const store = useAppStore();

// 本地镜像，避免 vuedraggable 直接改 store 真源；后端广播 workflow.changed 时再同步回来。
const items = ref(store.workflow.nodes.map((n) => ({ ...n, uid: n.uid })));
watch(
  () => store.workflow.nodes,
  (n) => {
    items.value = n.map((x) => ({ ...x, uid: x.uid }));
  },
  { deep: true }
);

function displayName(n: any): string {
  return (n.name && n.name.trim()) || store.nodeDefsByName[n.type]?.name || n.type;
}

const renaming = ref(-1);
const renameText = ref("");
function startRename(i: number) {
  renaming.value = i;
  renameText.value = store.workflow.nodes[i]?.name || "";
  const el = document.getElementById(`af-rename-${i}`) as HTMLInputElement | null;
  if (el) { el.focus(); el.select(); }
}
async function commitRename(i: number) {
  const name = renameText.value.trim();
  renaming.value = -1;
  if ((store.workflow.nodes[i]?.name || "") !== name)
    await store.renameNode(i, name);
}

/**
 * vuedraggable 的 `newIndex` 是「拖完之后该项在列表里的下标」，
 * 与后端 `node.move(index, to)` 的 `to`（`pop(index)` 后再 `insert(to)`）语义一致，
 * 所以这里直接透传，不需要 ±1。
 *
 * 失败必须把本地镜像拉回真源：vuedraggable 已经乐观地改过 `items`，
 * 若不回滚，界面会停在一个后端并不认可的顺序上，直到下一次 store 变更才纠正。
 */
async function onEnd(e: { oldIndex?: number; newIndex?: number }) {
  if (e.oldIndex === undefined || e.newIndex === undefined) return;
  if (e.oldIndex === e.newIndex) return;
  try {
    await store.moveNode(e.oldIndex, e.newIndex);
  } catch (err) {
    items.value = store.workflow.nodes.map((x) => ({ ...x, uid: x.uid }));
    MessagePlugin.error("移动失败：" + errMessage(err));
  }
}

async function onAdd(type: string) {
  try {
    const idx = await store.addNode(type);
    store.selectNode(idx);
  } catch (e) {
    MessagePlugin.error("添加失败：" + errMessage(e));
  }
}
async function onToggle(index: number, val: boolean) {
  await store.toggleNode(index, val);
}
async function onRemove(index: number) {
  await store.removeNode(index);
  if (store.selectedIndex === index) store.selectNode(-1);
}
</script>

<template>
  <div>
    <t-select
      placeholder="添加节点…"
      :value="null"
      @change="onAdd"
      size="small"
      style="margin-bottom: 8px"
    >
      <t-option
        v-for="d in store.definitions"
        :key="d.type"
        :value="d.type"
        :label="d.name"
      />
    </t-select>

    <!-- 拖拽手柄用独立的 .af-grip，不用整行：整行当手柄时，在开关/删除按钮上按下
         也会启动拖拽（@click.stop 只拦 click，拦不住 mousedown）。 -->
    <draggable v-model="items" item-key="uid" @end="onEnd" handle=".af-grip" ghost-class="af-ghost">
      <template #item="{ element, index }">
        <div
          class="af-node"
          :class="{ sel: index === store.selectedIndex }"
          @click="store.selectNode(index)"
        >
          <span class="af-grip" title="拖拽排序">⋮⋮</span>
          <span class="af-node-idx">{{ index + 1 }}</span>
          <t-switch
            :value="element.enabled"
            size="small"
            @change="(v: boolean) => onToggle(index, v)"
            @click.stop
          />
          <span
            v-if="renaming !== index"
            class="af-node-name"
            :title="displayName(element) + '（双击改名）'"
            @dblclick.stop="startRename(index)"
            >{{ displayName(element) }}</span
          >
          <input
            v-else
            :id="`af-rename-${index}`"
            v-model="renameText"
            class="af-rename-input"
            @click.stop
            @keydown.enter.prevent="commitRename(index)"
            @keydown.esc="renaming = -1"
            @blur="commitRename(index)"
          />
          <t-button
            size="small"
            variant="text"
            theme="danger"
            @click.stop="onRemove(index)"
            >删</t-button
          >
        </div>
      </template>
    </draggable>

    <div v-if="!items.length" class="af-empty">暂无节点，从上方添加。</div>
  </div>
</template>

<style scoped>
.af-node {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid #e7e7e7;
  border-radius: 6px;
  margin-bottom: 6px;
  cursor: pointer;
  background: #fff;
}
.af-node.sel {
  border-color: #0052d9;
  background: #f2f7ff;
}
.af-grip {
  flex: none;
  cursor: grab;
  color: #c0c4cc;
  font-size: 11px;
  line-height: 1;
  letter-spacing: -1px;
  padding: 2px 1px;
  user-select: none;
}
.af-grip:active {
  cursor: grabbing;
  color: #0052d9;
}
.af-node-idx {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #eef2f8;
  color: #666;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  flex: none;
}
.af-node-name {
  flex: 1;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.af-rename-input {
  flex: 1;
  min-width: 0;
  border: 1px solid #0052d9;
  border-radius: 4px;
  padding: 2px 6px;
  font-size: 13px;
  outline: none;
}
.af-empty {
  color: #999;
  font-size: 12px;
  padding: 8px;
}
.af-ghost {
  opacity: 0.4;
}
</style>
