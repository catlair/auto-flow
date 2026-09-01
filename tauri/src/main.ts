import { createApp } from "vue";
import { createPinia } from "pinia";
import TDesign from "tdesign-vue-next";
import "tdesign-vue-next/es/style/index.css";
import App from "./App.vue";
import "./style.css";

const app = createApp(App);
app.use(createPinia());
app.use(TDesign);
app.mount("#app");
