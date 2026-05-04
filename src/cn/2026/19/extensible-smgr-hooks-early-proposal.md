# 可扩展 SMGR Hook（2021–2022）：Anastasia 的提案、表空间语义与 redo 壁垒

## 引言

这场 [pgsql-hackers 归档](https://www.postgresql.org/message-id/flat/CAP4vRV6JKXyFfEOf%3Dn%2Bv5RGsZywAQ3CTM8ESWvgq%2BS87Tmgx_g%40mail.gmail.com)是后来「可插拔存储管理器」讨论的较早一章。Anastasia Lubennikova（2021 年 6 月）提出以 **Hook** 为核心的改法：用解析 `f_smgr` 虚表的入口取代静态 `smgrsw[]` 分发，默认仍落到现有的 `md` 实现（`smgr_standard()`）。Yura Sokolov 随即提出贯穿后续多年的问题——**具体实现如何按关系选择**——并倾向把选择做成**表空间上的不可变属性**，而非单一全局 Hook。沉寂多月后，Andres Freund 以 **CommitFest 节奏**将该条目标为带反馈退回。Kirill Reshke（2022 年 6 月）则贴出更大胆的原型：**目录 `pg_smgr`**、`CREATE STORAGE MANAGER`、表级语法、**WAL 携带 SMGR 标识**，并坦承 **redo** 与检查点处的失败。Andrey Borodin 补充了 **Greenplum** 侧将冷段落到对象存储的实践，询问 PostgreSQL 社区是否需要类似能力。

## 技术分析

### 以 Hook 为先的 API（0001-smgr_api.patch）

首版补丁引入 `smgr_hook`：扩展实现 `const f_smgr smgr_custom = { … }`，再提供 `const f_smgr *smgr_custom(BackendId backend, RelFileNode rnode)` 以按后端与 `RelFileNode` 分支，最后 `smgr_hook = smgr_custom`。核心把对 `smgrsw[]` 的直接下标访问改为可返回内置 `md` 或自定义虚表的解析路径。

首帖动机覆盖**存储层压缩/加密**、**磁盘配额**、**SLRU 存储改造**，以及替换文件层的其他设想（并引用当时列表线程与 PGCon 2021 的 lazy restore 资料）。

### 选择语义：全局 Hook 与表空间属性

Yura 认同需要不止一个 `md` 实现，但指出补丁未说明**不同关系如何落到不同实现**。他主张选择应是**表空间的不可变属性**，并在 `smgropen` 时传入；实现上倾向**更小侵入**：让 `smgrsw` 指向可增长数组、保留 `reln->smgr_which` 语义，辅以类似 `char smgr_name[NAMEDATALEN]` 的字段，在小注册表里线性查找。

这条反馈与后来「注册表 / 命名 / redo」路线一脉相承：**光有函数指针不够，还要有绑定模型**。

### CommitFest 层面的收束

Andres 在 2022 年 3 月说明线程**超过半年无更新**，将条目标为 **returned with feedback**，并明确**不可能进入 PostgreSQL 15**。讨论并未终止：数月后 Kirill 在同一主题下继续实验。

### Kirill 原型：`pg_smgr`、DDL 与 WAL 携带的身份

Kirill 的邮件把设计摆到接近**自定义表访问方法**的形态：

- 系统目录 **`pg_smgr`** 保存处理器 OID；
- 扩展脚本中 `CREATE FUNCTION … RETURNS table_smgr_handler` 与  
  `CREATE STORAGE MANAGER proxy_smgr HANDLER proxy_smgr_handler;`
- `CREATE TABLE … STORAGE MANAGER …`（邮件中还记录了最初把 handler 名与 manager 名混淆的试错）。

他称回归**接近通过**，但在**首次检查点**或**崩溃恢复**处失败。难点仍是 **redo**：崩溃恢复阶段 **syscache 不能像普通后端那样依赖**，无法仅靠目录解析「这个 `RelFileNode` 该用哪个 `smgr`」。他尝试在携带 `RelFileNode` 的 WAL 记录里加入 **SMGR OID**，让重放不查目录即可选实现——代价是 WAL 形态大改，集成仍脆弱。

附件 `v1-0001-Add-create-storage-manager-ddl-and-routines.patch` 实现了大量表面语法（解析 `CREATE STORAGE MANAGER`、`OBJECT_STORAGE_MANAGER`、`CREATE TABLE` 可选 `STORAGE MANAGER name` 等）。

### 工业界对照

Andrey Borodin 介绍 **Greenplum** 中相关能力（并给出 2022 年的上游 PR 链接）：扩展在段长期不访问时把文件同步到类 S3 备份，访问时再拉回——面向分析型、**冷且大体追加**的数据。他询问：若 PostgreSQL 核心提供可扩展 `smgr`，这类扩展对社区是否有价值。

### SQL 示例

下列片段来自 **Kirill 的列表实验**与 **`v1-0001-Add-create-storage-manager-ddl-and-routines.patch`** 原型，**不能**在已发布版本上执行，仅用于说明拟议 DDL 与目录形态。

```sql
CREATE EXTENSION proxy_smgr;

SELECT * FROM pg_smgr;

CREATE TABLE tt(i int) STORAGE MANAGER proxy_smgr;
```

扩展脚本形态（线程与补丁中的一致写法）：

```sql
CREATE FUNCTION proxy_smgr_handler(internal)
RETURNS table_smgr_handler
AS 'MODULE_PATHNAME'
LANGUAGE C;

CREATE STORAGE MANAGER proxy_smgr HANDLER proxy_smgr_handler;
```

## 社区观点摘要

- **Yura Sokolov**：把绑定拉到**表空间级不可变配置**，并控制核心改动面。
- **Andres Freund**：用 **CommitFest 规则**收束长期停滞的补丁预期。
- **Kirill Reshke**：用可运行原型证明 **按关系选择 SMGR** 会迅速撞上 **redo / syscache**；仅靠目录解析不够，需要额外持久化元数据或 WAL 标记（代价高昂）。
- **Andrey Borodin**：提供 **冷数据卸载** 的真实需求侧叙事。

## 技术细节

- **全局 Hook** 便于描述「运行时决策」，但未定义该决策在**重放路径**上如何无损复现。
- **表空间绑定**与现有「文件放哪里」的管理模型一致，但若表空间本身声明自定义管理器，仍要解决**恢复早期**如何读取该声明的问题。
- 在 WAL 中携带 **SMGR 身份**能力强、侵入面广：凡带 `RelFileNode` 的记录类都可能受影响，必须与替代性的**表空间侧映射文件**等方案权衡。

## 现状

该线程在 **2022 年 8 月**后未再在归档中延续到合并结论；后续 Neon/Percona 等线程以**注册表、链式 SMGR、测试模块**等方向继续演进。本文档适合作为「Hook 起点 + `CREATE STORAGE MANAGER` 草图 + redo 壁垒」的**历史锚点**阅读。

## 结语

若后来的「SMGR Redux」讨论的是注册、链式与工具链，这条 2021–2022 的线程则讲清**起点问题**：Hook 好写，**绑定与恢复**难做。Yura 的表空间视角与 Kirill 的 WAL 方案是同一枚硬币的两面——在系统目录尚不可靠的时刻，**必须另有渠道**告诉重放引擎该调用哪套存储实现。
