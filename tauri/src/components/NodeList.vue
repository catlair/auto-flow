<script setup lang="ts">
import { ref, watch } from "vue";
import draggable from "vuedraggable";
import { useAppStore } from "@/stores/app";
import { MessagePlugin } from "tdesign-vue-next";

const store = useAppStore();

// 本地镜像，避免 vuedraggable 直接改 store 真源；后端广播 workflow.changed 时再同步回来。
const items = ref(store.workflow.nodes.map((n) => ({ ...n })));
watch(
  () => store.workflow.nodes,
  (n) => {
    items.value = n.map((x) => ({ ...x }));
  },
  { deep: true }
);

function defName(type: string): string {
  return store.nodeDefsByName[type]?.name ?? type;
}

function onEnd(e: { oldIndex?: number; newIndex?: number }) {
  if (e.oldIndex === undefined || e.newIndex === undefined) return;
  if (e.oldIndex !== e.newIndex) store.moveNode(e.oldIndex, e.newIndex);
}

async function onAdd(type: string) {
  try {
    const idx = await store.addNode(type);
    store.selectNode(idx);
  } catch (e) {
    MessagePlugin.error("添加失败：" + (e as Error).message);
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
    <div class="af-panel-title">节点列表</div>
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

    <draggable v-model="items" item-key="type" @end="onEnd" handle=".af-node" ghost-class="af-ghost">
      <template #item="{ element, index }">
        <div
          class="af-node"
          :class="{ sel: index === store.selectedIndex }"
          @click="store.selectNode(index)"
        >
          <t-switch
            :value="element.enabled"
            size="small"
            @change="(v: boolean) => onToggle(index, v)"
            @click.stop
          />
          <span class="af-node-name">{{ defName(element.type) }}</span>
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
.af-node-name {
  flex: 1;
  font-size: 13px;
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
