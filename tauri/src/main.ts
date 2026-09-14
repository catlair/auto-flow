import { createApp } from "vue";
import { createPinia } from "pinia";
import TDesign from "tdesign-vue-next";
import "tdesign-vue-next/es/style/index.css";
// 流程图画布的基础样式。**必须全局引入**：节点/边/手柄的定位全靠它们，
// 少一份会让手柄挤在节点左上角、边画不出来，而 SFC 的 scoped 样式管不到
// 库自己生成的元素（它们不带本组件的 scope id）。
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";
import App from "./App.vue";
import "./style.css";

const app = createApp(App);
app.use(createPinia());
app.use(TDesign);
app.mount("#app");
