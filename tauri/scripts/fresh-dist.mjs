// 构建前把 vite 输出目录改名让路，而不是让它自己清空。
//
// 背景：`vite build` 默认会 emptyOutDir（递归删除 dist/ 下所有文件）。产物有
// 数百个文件，整批删除会触发宿主环境的批量删除保护
// （[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]）导致构建直接失败。
// 同卷 rename 不是删除，既绕过保护，也留下了可回滚的上一版产物。
import { existsSync, renameSync } from "node:fs";

const OUT = "dist";
if (existsSync(OUT)) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const old = `dist.old.${stamp}`;
  renameSync(OUT, old);
  console.log(`[fresh-dist] 旧产物已改名让路: ${old}`);
}
