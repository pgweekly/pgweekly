# PostgreSQL 存储 I/O 转换 Hook：将核心存储与加密逻辑解耦

## 引言

在这条 [pgsql-hackers 线程](https://www.postgresql.org/message-id/flat/CAAAe_zCHNVKxETAWBZg3YWPdmXsf-G2OcQtQOYQn4AeZaES4tQ%40mail.gmail.com) 中，作者提出了一套面向 PostgreSQL 存储路径的数据转换 Hook 协议，并以 TDE（透明数据加密）作为参考实现。核心思路是：加密/解密转换交给扩展实现，PostgreSQL 核心仍负责存储编排、校验和与持久化语义。

## 技术分析

### 补丁集引入了什么

该系列补丁在 I/O 与 WAL 路径增加了 5 个 Hook 点：

- `mdread_post_hook`
- `mdwrite_pre_hook`
- `mdextend_pre_hook`
- `xlog_insert_pre_hook`
- `xlog_decode_pre_hook`

同时在页面 `pd_flags` 中加入转换标记，并为 WAL 增加标识（`XLR_BLOCK_ID_TRANSFORMED = 251`），用于在缺少对应扩展时快速失败，避免把已转换载荷误当作普通 WAL 记录。

### 版本演进（v1 到 v4）

- **v1**：初始基础设施（`0001`）+ `contrib/test_tde` 参考扩展（`0002`）。
- **v2**：补齐 `test_tde` 的构建系统集成（Meson 与 contrib 构建入口）。
- **v3**：修复 `mdread_post_hook` 调用处的指针类型兼容问题（增加 `(void **)` 转换）。
- **v4**：新增 `0003`，修复 tablespace 回归测试在跨平台场景下的兼容性问题。

### SQL 示例

下面示例来自补丁中的 `contrib/test_tde/sql/basic.sql`。它们用于说明设计思路，需要使用包含该补丁且配置 `shared_preload_libraries = 'test_tde'` 的构建；在正式发布版 PostgreSQL 中不可直接使用。

```sql
SHOW test_tde.key;

CREATE TABLE test_encrypt (
  id int,
  secret_data text,
  secret_number int
);

INSERT INTO test_encrypt VALUES
  (1, 'sensitive data 1', 12345),
  (2, 'sensitive data 2', 67890),
  (3, NULL, 11111);

SELECT COUNT(*) FROM test_encrypt;
SELECT secret_data FROM test_encrypt WHERE secret_number = 12345;
```

```sql
CREATE TABLE test_set_tablespace (id int, data text);
INSERT INTO test_set_tablespace
SELECT g, 'data ' || g FROM generate_series(1, 50) g;

ALTER TABLE test_set_tablespace SET TABLESPACE regress_tde_tblspc;
SELECT COUNT(*) FROM test_set_tablespace;
```

## 社区观点

评审讨论的重点并不在加密算法本身，而在架构边界：

- WAL 与 buffer manager 的 Hook 是否应拆分为两条独立讨论线。
- 新增 Hook 与正在推进的可扩展 SMGR 方案是否存在职责重叠。
- 在 critical section 中启用真实加密插件后，性能评估是否充分。

作者认为细粒度转换 Hook 相比“替换整套存储实现”更易维护；评审方则更关注 API 重叠与长期可维护性。

## 技术细节

参考扩展 `test_tde` 展示了几条关键安全约束：

- 对加密页先做校验和验证，再执行解密。
- 逆转换后清除转换标记，并对明文重新计算校验和。
- 在允许 critical section 分配的内存上下文中预分配和管理加密上下文。
- 通过 hook chaining 保持与其他扩展的可组合性。

在 WAL 路径中，转换标识与解码 Hook 的组合用于保证：缺少所需转换逻辑时不会错误解释记录内容。

## 当前状态

该线程已迭代到 `v4`，但整体仍处于 RFC/评审阶段。关于其与 SMGR 可扩展性的关系、以及 WAL 与 heap/buffer Hook 的边界划分仍在讨论中，因此更适合将其视为一项持续演进的基础设施提案，而非已合入主干的功能。

## 结语

这个讨论很好地体现了 PostgreSQL 在安全需求下的扩展哲学：核心保持通用，暴露边界清晰的 Hook，并通过显式协议标记保障安全。方向上有潜力，但能否进入上游，仍取决于与更广泛存储扩展方案的协同，以及真实工作负载下的性能验证。
