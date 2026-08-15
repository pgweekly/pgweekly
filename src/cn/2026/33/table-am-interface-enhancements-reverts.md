# Table AM 接口增强：扩展性与设计边界

## 引言

2023 年 11 月，Alexander Korotkov 在 [pgsql-hackers](https://www.postgresql.org/message-id/flat/CAPpHfdurb9ycV8udYqM%3Do0sPS66PJ4RCBM1g-bBpvzUfogY0EA%40mail.gmail.com) 发起了一组包含十二个补丁的讨论，希望让 PostgreSQL 的**表访问方法（Table Access Method，Table AM）**不再只适合与 heap 相近的存储。直接动机来自 OrioleDB，但目标更广：支持索引组织表、不同的 MVCC 方案、自定义行标识，以及自行协调索引维护的表访问方法。

这条线程后来又多了一层意义。若干补丁在 PostgreSQL 17 特性冻结前不久进入主分支，却在更深入的提交后评审中几乎全部被回退。它因此成为一个很有代表性的案例：技术上，边界横跨执行器、Table AM、Index AM 与 relcache；工程治理上，则要回答「一个很有价值的可扩展性想法，何时才成熟到足以冻结为外部 API」。

## 现有 Table AM 边界为何不够

PostgreSQL 允许关系选择表访问方法，但外围逻辑仍带有大量 heap 假设：heap 元组用块号加偏移量组成 `ctid`；ANALYZE 对物理块采样；`INSERT ... ON CONFLICT` 依赖推测式插入；执行器通常在调用 Table AM 后再插入二级索引元组。

对索引组织或基于 undo 的引擎，这些假设并不自然：

- 并发更新后重新定位一行，代价可能远高于沿 heap TID 查找。
- 行标识可能是主键或其他变长值，而不是 48 位的块号/偏移量组合。
- 对物理块采样可能没有意义。
- 主索引可能就是表本身，先做 heap 式推测插入、再由执行器维护索引的拆分方式并不合适。
- AM 专属元数据与 reloptions 可能需要比核心现有接口更复杂的布局和生命周期。

因此，这不是一个单独特性，而是一次把多项决策同时推过 API 边界的尝试。

## 最初的十二个补丁

v1 系列大致分为四组。

### 元组标识、生命周期与 MVCC

第一组试图减少元组操作中的 heap 假设：

- 让 `tuple_update()` / `tuple_delete()` 锁定已经重新找到的并发更新元组，避免再次查找。
- 为 `DELETE ... RETURNING` 的 EvalPlanQual 路径增加隔离测试。
- 允许 AM 显式释放 `rd_amcache` 后面由多次分配构成的复杂数据结构。
- 在不假设 PostgreSQL 事务 ID 的情况下判断元组是否属于当前事务。
- 允许 `tuple_insert()` 返回一个能理解该 AM 专属系统属性的原生 slot。

评审让其中一项设计找到了更合适的位置：判断「是否为当前事务元组」的操作从 `TableAmRoutine` 移到了 `TupleTableSlotOps`。解释元组表示的对象自己回答这个问题，比在关系级 AM 上再增加一层间接调用更准确。

### ANALYZE 与采样

原接口提供 `scan_analyze_next_block` 与 `scan_analyze_next_tuple`。新提议用 `relation_analyze` 回调让 AM 选择元组采样函数，以支持不以物理块为中心的算法。

抽象方向合理，但补丁也移动或复制了 `acquire_sample_rows()` 的大段逻辑，由此产生兼容性问题：块式外部 AM 原本只需实现两个回调就能复用 PostgreSQL 的采样算法；新接口却可能迫使它复制更多 `analyze.c` 代码。

### INSERT、索引与 reloptions

另一组补丁希望让 Table AM 控制当前跨越多个子系统的操作：

- 用完整的 `tuple_insert_with_arbiter()` 接口替代推测式插入回调，从而封装 `INSERT ... ON CONFLICT`。
- 允许插入方法通知执行器「索引元组已由 AM 处理，不要重复插入」。
- 允许 Table AM 定义表 reloptions，并影响建在该表上的索引 reloptions。
- 创建索引时通知 Table AM。

它们暴露出全系列最难的架构问题：执行器或 Index AM 的多少知识可以安全地进入 Table AM？直接传递 `EState` 会把扩展绑定到执行器内部结构；让 Table AM 静默替换或重新解释用户指定的索引，会违背用户明确选择的 Index AM；若 Table AM 自行插入索引，还要说明执行器何时准备索引状态、删除索引项又由谁负责。

### RowRefType 与变长 RowID

最后两个补丁把「行引用的种类」与「行锁强度」分开，并引入类似 `bytea` 的 `RowID`，作为 `ctid` 之外的变长行标识。这使索引组织表有机会使用主键或更复杂的值定位行。

但评审指出，只改 Table AM 仍无法让普通索引使用它：现有 Index AM API 继续传递 heap 风格 TID。也就是说，这个方案只设计了跨 API 交互的一侧。

## 补丁如何演进

下载归档包含线程引用的全部版本：主系列 v1–v8、custom reloptions v9–v11、三版 ANALYZE 后续方案，以及最终的回退补丁。

在 v2–v4 中，补丁经过变基和重排；当前事务判断移入 tuple slot；`rd_amcache` 清理增加断言；GCC 11 测试发现的缺失头文件告警得到修复。评审也明确指出了 slot 所有权问题：允许 `tuple_insert()` 返回另一种 slot，不只是改变返回类型，调用方还必须知道输入 slot 和返回 slot 应该 clear、复用还是 release。

到 v5 时，若干较小补丁已经提交，余下系列缩减为八个。行引用方案因为 `ROW_MARK_COPY`、FDW 行为与设计共识尚未解决，被明确推迟；`INSERT ... ON CONFLICT` 方案也被保留到以后，因为让 AM 直接接触 `EState` 会破坏封装，而在发布周期末重新设计不透明回调上下文风险太高。

custom reloptions 随后单独演进。首次提交的版本对 `StdRdOptions` 作了过强假设，并在 `RelationParseRelOptions()` 触发 Coverity 告警。该提交回退后，v9–v11 将选项拆成「核心可见的固定形状公共值」和「仅 AM 解释的不透明值」，并加入 `test_tam_options` 测试模块。不过，relcache 是否应知道各类默认值、为何存在多条解析路径、文档是否充分等问题仍未完全解决。

## ANALYZE 的兼容性陷阱

影响最大的评审集中在 ANALYZE。Andres Freund 指出，至少有四个投入生产使用的外部 AM 实现了某种 ANALYZE 行为。接口改造提交后，块式 AM 无法再低成本复用 `acquire_sample_rows()`。

线程迅速提出了三种修补方式：

1. 在新关系级回调之外恢复旧的块级回调。
2. 不保留两套接口，改为泛化 `acquire_sample_rows()`，让 heap 式和非块式 AM 都能复用。
3. 在代码不变的情况下继续修订泛化回调的注释。

问题又与同期的 ANALYZE 流式读取和预取工作交织。heap 式预取假定逻辑块号与物理位置对应，而通用 Table AM 恰恰不能默认这一点。因此，回退 Table AM 补丁不能简单地撤销一个提交，还必须把后来依赖该形状的流式读取改动安全地拆开。

这说明扩展兼容性不只是「还能不能编译」。如果原本可复用的核心算法突然变成每个扩展都要复制和长期维护的代码，同样是实质性兼容回退。

## 提交后评审与回退

2024 年 3 月下旬至 4 月初，复杂 `rd_amcache` 清理、插入返回新 slot、slot 当前事务判断、并发更新元组锁定、泛化 ANALYZE、custom reloptions，以及由 AM 控制索引插入等改动先后进入主分支。

提交后评审提出了具体问题：潜在数据损坏风险、对象所有权不清、执行器索引状态准备时机、外部 AM 兼容性、文档不足，以及只完成一半的跨 API 设计。

时点也很关键：这些是接近特性冻结的扩展 API。一旦随发布版交付，一个不成熟的回调就会成为长期兼容承诺。讨论重点因此从继续打磨转为判断哪些提交已有足够共识，哪些不应进入 PostgreSQL 17。

4 月 11 日，`rd_amcache`、返回新 slot、并发更新元组锁定、custom reloptions，以及索引插入控制等受质疑提交被回退。ANALYZE 重构及其复用辅助方案在处理同期流式读取依赖后，于 4 月 16 日回退。

## 最终留下了什么

这组工作中有两项小改动仍存在于当前 PostgreSQL 源码：

- `TupleTableSlotOps.is_current_xact_tuple()`（提交 `0997e0af273`）：由元组表示自身判断是否由当前事务创建。这正是评审过程中找到更准确语义归属的方案。
- `TableAmRoutine.relation_copy_for_cluster()` 参数顺序修正（提交 `97ce821e3e1`）：一个直接的 API 一致性修复。

更大的提案——Table AM 自定义 reloptions、自行控制索引插入、插入返回原生 slot、泛化 ANALYZE、`RowRefType`、变长 `RowID`、索引创建通知，以及完整的 `INSERT ... ON CONFLICT` 回调——都没有通过这条线程进入 PostgreSQL。它们不应被描述为 PostgreSQL 17 特性。

本文刻意不提供可执行 SQL 示例：线程讨论的是内部扩展回调，用户可见效果要么依赖假想 AM，要么已被回退；把相关 SQL 写成当前可用行为会造成误导。

## 社区讨论带来的设计原则

技术评审最终集中到几条原则：

- **让行为靠近真正拥有语义的对象。** 当前事务判断放到 tuple slot，比继续扩大关系级接口更精确。
- **明确写出所有权。** 返回替代 slot 或保存复杂缓存对象的 API，必须定义分配、清理、失效和复用规则。
- **跨边界设计必须覆盖两侧。** 变长行标识和索引组织表需要 Table AM 与 Index AM 协同改变。
- **执行器内部状态应保持不透明。** 依赖 `EState` 的回调会把无关的执行器实现细节冻结成扩展契约。
- **保留可复用的默认实现。** 如果每个 heap 式扩展都要复制一大段核心算法，再灵活的回调也会变得昂贵。
- **把扩展 API 当作长期承诺。** 文档、代表性测试模块和明确评审共识都是设计的一部分，临近特性冻结时尤其如此。

## 结语

这条线程没有交付最初设想的完整 Table AM 重构，却更清晰地揭示了下一次设计必须补齐的内容。PostgreSQL 的确需要更好地支持并非「heap 加少量改动」的存储引擎，但每个新回调都要定义所有权、默认行为、与索引及执行器的交互，以及现有外部 AM 的兼容路径。

最终留下的 tuple-slot 方法看似只是一个小成果，却很能说明问题：最耐久的可扩展性改动，往往不是单纯扩大接口，而是把语义边界收得更准确。

## 参考资料

- [邮件列表线程：Table AM Interface Enhancements](https://www.postgresql.org/message-id/flat/CAPpHfdurb9ycV8udYqM%3Do0sPS66PJ4RCBM1g-bBpvzUfogY0EA%40mail.gmail.com)
- [PostgreSQL 文档：Table Access Method Interface Definition](https://www.postgresql.org/docs/current/tableam.html)
- [PGCon 2023：Future of Table Access Methods](https://www.pgcon.org/events/pgcon_2023/schedule/session/470-future-of-table-access-methods/)
