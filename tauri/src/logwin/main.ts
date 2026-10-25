import { createApp } from "vue";
import LogWin from "./LogWin.vue";
import "./logwin.css";

// 置顶日志窗的独立入口（`log.html`）。
//
// 刻意**不**装 Pinia / TDesign / 画布库：这个窗口只做一件事——把主窗推来的行
// 滚出来。整包依赖都拉进来只会让它在运行期间更容易卡（它常年浮在最上层，
// 卡一下就直接表现为"日志不走了"），而它存在的意义正是"运行卡住时还能看"。
createApp(LogWin).mount("#logapp");
