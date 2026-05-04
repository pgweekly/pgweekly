# 可扩展存储管理器 API：从 Hook 到注册、链式 SMGR 与长期讨论

## 引言

这场自 2023 年中延续至今的 [pgsql-hackers 讨论](https://www.postgresql.org/message-id/flat/CAEze2WgMySu2suO_TLvFyGY3URa4mAx22WeoEicnK%3DPCNWEMrA%40mail.gmail.com)触及 PostgreSQL 中最难动的边界之一：**存储管理器（`smgr`）**，也就是把关系分叉（fork）落实为具体文件 I/O 的那一层。Neon 方面希望减少「整表替换 `smgr` 实现」式的 Hook，改为**基于注册表**的设计，让扩展提供存储管理器而不去劫持核心符号。讨论很快吸引了核心开发者评审、基于新 API 的 **`fsync_checker` 概念验证**、Percona 对透明数据加密（TDE）的兴趣，以及多轮重贴补丁——直至 2026 年仍在演进，涉及**链式 SMGR**、测试代码安放位置以及 AIO 回调可扩展性等。

为何重要：云厂商与扩展作者希望在尽可能低的 I/O 层实现压缩、加密、配额、远程对象存储和可观测性；要做到安全，就必须厘清 API、恢复语义，以及配置如何送达**并不总能读系统目录**的后端进程。

## 技术分析

### 从 Hook 走向注册表

Matthias van de Meent 在 2023 年 6 月的首帖把方案放在既有提案（含 Anastasia Lubennikova 的工作）与 Yura Sokolov 在列表上的偏好之下：相比粗暴替换，更倾向**注册**与命名空间集成。扩展不再原地换掉 `smgr` 的虚表，而是由核心维护一张**注册表**。

首版补丁草图包括：

- 在加载 `shared_preload_libraries` 时动态增长的 `smgrsw`。
- 为 `CREATE TABLESPACE` 增加可选的 `USING smgrname (…)` 语法，使表空间可命名存储管理器（随后被指出与**恢复路径**强相关）。
- 待解问题：执行 `smgr` 操作的**并非所有后端都能依赖 catcache**、第二份原型补丁中 **redo 仍损坏**、以及 `pg_dump` 与测试等工具链。

### 早期评审：间接指针、初始化与类型

Andres Freund 对在 `PostmasterMain` 初始化动态管理器存疑，建议改到 **`BaseInit()`**；对把静态 `smgrsw[]` 再变成一层指针间接寻址有保留，更希望**限制**可注册数量；追问注册路径里的 `pg_compiler_barrier()`；并质疑若 `smgrrelation_size` 全局已定，为何每个关系对象仍要携带一份。

Tristan Partin 的评审则涉及 API 清理（`md*` 可改为 `md.c` 内 `static`）、`SMgrId` 类型加宽、`InvalidSmgrId` 校验、结构体字段命名、`mdexists` 中疑似**变基失误**，以及 `mddroptsp` 空桩的格式一致性。

### 恢复与目录访问

Kirill Reshke（2023 年 12 月）问：若 `smgr_redo` 走 `get_tablespace` 查目录，**崩溃恢复**阶段系统目录是否可用——Matthias 承认原型错误地假设了目录可读，应考虑类似 **`pg_filenode.map`** 的旁路映射文件，或在表空间元数据位置（例如 `/pg_tblspc/` 符号链接旁）记录表空间到 `smgr` 的对应关系，从而在 replay 时不必依赖 syscache。

关于**按关系**还是**按表空间**选择管理器：Matthias 认为表空间已经抽象「存储池／盘」，与底层放置策略更契合；若坚持每表一种实现，只能通过大量表空间近似实现，运维成本很高。

### 以 fsync_checker 压测 API 形状

Tristan 在 2024 年 1 月的跟进把 Matthias 的补丁变基，使管理器可**继承/委托**；增加全局覆盖钩子（自承「偏 hack」，缺少「唯一胜者」规则）；加入 **`checkpoint_create` 钩子**以便在真正刷检查点前观察；并基于 Andres 早年想法实现需预加载的 **`fsync_checker`**。他也坦诚该工具**并不完备**：若关系只经 WAL 落盘而未走 `fsync`（例如与 `log_smgrcreate()` / 类 `createdb` 流程相关），简单「检查点前未 fsync」会**误报**。

Aleksander Alekseev（2024 年 3 月）提醒 **cfbot** 仍不满意，需要更新版本。

### 范围之争：基础设施与捆绑示例

Nitin Jadhav（2024 年 9 月）认为系列把**核心基础设施**与 `fsync_checker` 等附加功能绑在一起，后者宜拆分。他还概括战略分歧：**Hook 式**全局替换简单，但**只能二选一**；**注册式**可多管理器并存，灵活但改动面大——只有社区真打算长期支持多管理器时才划算。

Xun Gong（2024 年 12 月）认同注册式可扩展性，并类比 Greenplum 为 AO 表使用独立 `smgr` 以适配不同文件布局。

Andreas Karlsson（Percona，2025 年 2 月）重贴并整理 Tristan 的补丁以适配 TDE：为 **`mdcreate` 传入旧 `RelFileLocator`**，使加密元数据在 relfile 变更时得以延续；引入 **`smgr_chain`** GUC：逗号分隔链中，**最后一个为 tail**（如 `md`），前面为 **modifier**（TDE、`fsync_checker` 或假想的远程后端），从而让加密逻辑既能包 `md` 也能包未来的非本地实现。他也重复质疑 **barrier** 的必要性，并关心配置方式与**基准测试/额外开销**。

### 推动 v5、v6 的评审意见

Kirill 在 2025 年 3 月追问：**prefetch** 是否应对所有扩展作者强制、断言是否覆盖更多回调、小补丁是否应合并、`fsync_checker` 更适合 **`contrib` 还是 `src/test/modules`**，以及 `RelFileLocator` 相关改动的提交说明是否充分。CI 仍红直至处理。

Vignesh C 将 CommitFest 状态标为**等待作者**，要求先回应 Kirill 的意见。

Zsolt Parragi（2026 年 1 月）发布 **v6**：变基、补齐断言（含 **`startreadv`**）、按反馈合并补丁、认为 **prefetch 可实现为空函数**、将 **`fsync_checker` 挪到测试模块**、充实提交说明，并增加 **AIO 句柄回调可扩展** 的补丁，显示存储改造与新异步 I/O 方向的交汇。

### SQL 示例

以下片段对应邮件中的 **WIP 补丁（至 v6）**，并非任何已发布版本的行为。补丁中 `smgr_chain` 被标为 **`PGC_POSTMASTER`**：应在 `postgresql.conf` 中设置并**重启**；只有打了相应补丁的服务器上 `SHOW` 才有意义。

```sql
-- 在服务器级配置有效链并重启之后，例如
-- smgr_chain = 'fsync_checker, md'   -- 若干 modifier，最后是 tail 管理器
SHOW smgr_chain;
```

线程里更早的原型曾探索在 **`CREATE TABLESPACE`** 上通过 DDL 选择管理器；该语法与 redo 的交互在讨论中被判为棘手，**勿**将其视为已承诺的语言特性，仅作历史提案阅读。

## 社区观点摘要

- **Andres Freund**：初始化位置、避免无谓间接层、对内存屏障与结构布局的尖锐提问。
- **Tristan Partin**：API 打磨、通过 `smgr.diff` 修正 `mdexists` / `mdcreate` 中 `MdSMgrRelation` 相关告警，并以 `fsync_checker` 暴露真实 API 痛点。
- **Kirill Reshke**：恢复阶段能否读目录、CI、API 完备性（prefetch/断言）、调试工具在代码树中的**安放位置**。
- **Nitin Jadhav / Xun Gong**：产品化视角——拆分关注点；若生态真要「多管理器」，注册式更有价值。
- **Andreas Karlsson / Zsolt Parragi**：持续变基、TDE 驱动的 `RelFileLocator` 传递、可组合加密的链式模型，以及 CI 卫生。

## 技术细节

- **动态注册**带来灵活性，也在热路径上引入额外间接开销——正是 Andres 所警惕的折衷。
- **WAL/redo** 必须在不依赖 syscache 的前提下解析应调用哪个 `smgr`；文件化映射或表空间本地元数据是自然选项。
- **链式模型**把加密、校验等横切关注点做成 **modifier**，最终落到 **tail** 的具体存储上，与用户态「装饰器栈」直觉相近。
- **`fsync_checker`** 说明 **fd 层记账** 很微妙：耐久性可能经 WAL 达成而非 `fsync`，朴素告警易误报。

## 现状

截至 Zsolt **2026 年 1 月的 v6** 帖子，分支已变基并处理评审意见（`fsync_checker` 迁移、断言补齐、补丁合并）。就本文所涉范围而言，该 API **尚未**作为稳定特性进入已发布版本；配置面、开销与恢复元数据等设计问题仍在列表上讨论。

## 结语

这条线程说明：深层存储可扩展性会同时撞上**初始化、类型标识、redo、基准与代码树政策**，远不止是导出一张函数表。注册表与链式结构分别回答「如何发现实现」与「如何组合横切层」，但仍需对恢复与性能给出严谨方案。关注 TDE、远程存储或解耦存储后端的读者，可把这串归档当作上游落地前的设计文档持续跟踪。
