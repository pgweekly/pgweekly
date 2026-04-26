# 面向 TDE 的 post-special 页面存储：一个加密提案如何演进为页面空间抽象重构

## 引言

这条 [pgsql-hackers 讨论线程](https://www.postgresql.org/message-id/flat/CAOxo6XKWHHUr1agOZxEHuL-UW8Me3YndUsJ%3D09tcDiw%2BLd8YEw%40mail.gmail.com) 最初以透明数据加密（TDE）基础设施为目标，但很快扩展为对页面布局归属、存储管理器边界与长期可维护性的系统性讨论。核心思路是：在页面 special 区域之后预留可选空间，让校验和、加密元数据及未来页面特性可以统一接入。

## 技术分析

### 这组补丁试图解决什么

跨多个版本，补丁主要引入了：

- `reserved_page_size` 及相关机制，用于支持 "post-special" 页面空间。
- `PageUsableSpace` 抽象，用来替代 `BLCKSZ - SizeOfPageHeaderData` 这类硬编码假设。
- 可选页面特性标记（包括扩展校验和方向的尝试）。
- 将页面容量相关上限改为动态计算（如 `MaxHeapTupleSize`、`MaxHeapTuplesPerPage` 以及 TOAST 相关阈值）。
- 集群初始化与元数据链路改造，包括 `initdb`、`pg_resetwal`，以及后续对 SQL 可见性的支持。

### 版本演进（v1/无前缀 到 v4）

- **早期补丁（无前缀、v1/v2 风格）：**重点是预留页面空间与页面特性试验。
- **v2：**开始拆分基础设施与性能类改动（如 fast div/mod 与 visibility map 路径），同时继续调整页面特性方案。
- **v3（28 个补丁）：**演进为更完整的分层重构，包含 `PageUsableSpace`、动态上限计算、reloptions hook，以及 TOAST 与 control file 路径联动。
- **v4：**继续做拆分与收敛，补齐 control file/GUC/bootstrap 侧逻辑，并增加了通过 `pg_control_system()` 暴露信息的 SQL 路径。

### SQL 示例

下面示例对应线程后期版本中讨论到的 SQL 可见行为（尤其是 control file 信息暴露）。这些语句依赖线程中的补丁构建，在正式发布版 PostgreSQL 中通常不可直接运行。

```sql
-- 通过 SQL 查看 control file 相关配置（v4 提案）
SELECT *
FROM pg_control_system();
```

```sql
-- 观察在页面可用空间不再是常量后，
-- toast 相关参数与行为的可配置性。
CREATE TABLE t_reserved (
  id bigint PRIMARY KEY,
  payload text
) WITH (toast_tuple_target = 2048);

INSERT INTO t_reserved
SELECT g, repeat('x', 5000)
FROM generate_series(1, 1000) AS g;

SELECT relname, reloptions
FROM pg_class
WHERE relname = 't_reserved';
```

## 社区观点

评审意见反复聚焦在架构边界问题：

- 这类能力应放在通用存储管理器层，还是更贴近访问方法（AM）各自实现？
- 为存储元数据占用 "黄金页面空间" 是否合理，是否会挤压 AM 的性能潜力？
- 在缺少充分基准数据时，运行时成本与复杂度是否可接受？
- 若要推动 TDE 进入社区主线，是否必须先通过 API 设计减少侵入式改动？

线程后期还出现了更宏观的讨论：TDE 的合规需求价值很高，但社区是否接纳，仍取决于技术收益、实现复杂度与维护成本之间的平衡。

## 技术细节

这条线程中最值得关注的技术轨迹，是从 "针对 TDE 的定制方案" 逐步转向 "通用页面空间模型"：

- 把固定宏替换为可计算形式。
- 让 heap/index/visibility/TOAST 等多个子系统共享统一的 "可用空间" 抽象。
- 通过初始化与元数据工具链保持集群级配置的一致可观测性。
- 将性能优化（如 fast division）拆分为独立补丁，避免与核心语义改动耦合评审。

这种拆分提高了可审阅性，但也暴露出 PostgreSQL 内核中大量依赖固定页面几何假设的历史包袱。

## 当前状态

该线程跨越较长时间并经历多轮重写，说明设计与评审都很深入；但它仍处于持续讨论阶段，而非已合入主干的功能。关于职责边界、复杂度控制与性能收益的核心问题仍未完全收敛。

## 结语

这场讨论的价值不止于 TDE。本质上，它展示了安全需求如何倒逼 PostgreSQL 重新审视底层存储抽象：很多时候，要想让一个功能可被上游接受，不是 "塞进一个能力"，而是先把底层接口和边界定义得更清晰。无论该系列最终是否落地，`PageUsableSpace` 与边界分层思路都可能持续影响后续存储与加密方向。
