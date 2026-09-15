/**
 * 「首次拿到非空工作流时适应一次视野」的状态机。
 *
 * 抽成纯模块是为了**能被测**。这段逻辑错过两次，而它出错时其它测试全是绿的：
 * 画布不报错、节点一个不少，只是视野没跟过去——用户打开一份已有工作流，
 * 只看到左上角一两个节点，会以为工作流是空的或者坏了。
 *
 * 关键是两个时机必须分开，不能合成一步：
 *   request(hasNodes) —— 数据到位（store 里已经有节点了）。**这里只能标记意图。**
 *   commit()          —— Vue Flow 量完节点尺寸（`nodesInitialized` 事件）。**这时才 fit。**
 *
 * 合起来就是原来那个 bug：在 request 里直接 fit。此刻节点刚进 DOM、尺寸还是 0，
 * 包围盒是退化的，fitView 等于没调；更糟的是意图已被消费掉，等尺寸真量完时
 * 反而不会补第二次——表现就是「视野永远不adapt」。
 */
export interface FitOnce {
  /** 数据到位，申请一次适应。已经有非空数据了就不重复申请（避免每次编辑都把视野拉走）。 */
  request(hasNodes: boolean): void;
  /** 尺寸量完，执行。没有待办、或已经适应过 → 空转（幂等，可被重复调用）。 */
  commit(): void;
  /** 工作流被清空：允许下一份重新适应。 */
  reset(): void;
  /** 是否已经适应过（诊断/测试用）。 */
  isFitted(): boolean;
}

export function createFitOnce(fit: () => void): FitOnce {
  let fitted = false;
  let pending = false;
  return {
    request(hasNodes: boolean): void {
      if (fitted || !hasNodes) return;
      pending = true;
    },
    commit(): void {
      if (!pending) return;
      pending = false;
      fitted = true;
      fit();
    },
    reset(): void {
      fitted = false;
      pending = false;
    },
    isFitted(): boolean {
      return fitted;
    },
  };
}
