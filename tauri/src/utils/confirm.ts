/**
 * 应用内确认框。
 *
 * 刻意**不用 `window.confirm()`**——在 Tauri 下它不只是「不可靠」，而是
 * **会让危险操作无条件执行**。两条原因叠加：
 *
 * 1. Tauri 会往 WebView 里注入
 *    `window.confirm = async function(i){ return await invoke("plugin:dialog|confirm", …) }`
 *    ——它返回的是 **Promise**。Promise 恒为 truthy，所以两种惯用写法都拦不住：
 *    `if (!window.confirm(...)) return;`（`!Promise` 恒 false ⇒ `return` 永不执行）
 *    和 `window.confirm(...) && doIt()`（Promise 恒真 ⇒ 右侧无条件执行）。
 * 2. 本项目**没有授予 `dialog:allow-confirm`**（`tauri-plugin-dialog` 的 `default`
 *    权限集只有 allow-message / allow-open / allow-save），所以那个 invoke
 *    会**被权限拒绝**——**连框都不弹**，用户什么也看不到。
 *
 * 合起来：`window.confirm` 的效果是「没有任何提示，直接做」。
 * 对「移到废纸篓」「永久删除」这类操作就是静默的数据丢失。
 *
 * TDesign 的 DialogPlugin 渲染在应用窗口内，不依赖宿主，也不吃 Tauri 的权限，
 * 还能自定义按钮文案——「移到废纸篓」「丢弃并打开」比一个光秃秃的「确定」清楚得多。
 *
 * ⚠️ 调用方**必须 `await`**；不 await 等于没拦。
 */
import { DialogPlugin } from "tdesign-vue-next";

export function confirmDanger(opts: {
  header: string;
  body: string;
  confirmBtn?: string;
  cancelBtn?: string;
}): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (v: boolean) => {
      if (!settled) {
        settled = true;
        resolve(v);
      }
    };
    const d = DialogPlugin.confirm({
      header: opts.header,
      body: opts.body,
      theme: "warning",
      confirmBtn: opts.confirmBtn ?? "确定",
      cancelBtn: opts.cancelBtn ?? "取消",
      // settle 必须排在 destroy() **前面**：destroy 会触发 onClose，
      // 反过来写的话 onClose 先结算成 false，「确定」就永远不生效。
      onConfirm: () => {
        settle(true);
        d.destroy();
      },
      onCancel: () => {
        settle(false);
        d.destroy();
      },
      onClose: () => {
        settle(false);
        d.destroy();
      },
    });
  });
}
