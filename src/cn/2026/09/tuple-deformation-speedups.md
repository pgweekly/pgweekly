# 元组解构的进一步加速：预计算 attcacheoff

## 引言

**元组解构（tuple deformation）** 是将 PostgreSQL heap 元组的原始字节表示解包为 `TupleTableSlot` 中各个属性值的过程。它在查询执行中无时无刻不发生——每次顺序扫描、索引扫描或连接产生一行时，执行器都必须对元组进行解构才能访问列值。对于处理数百万行的负载，即便对解构热路径做小幅优化，也能带来可观的性能提升。

David Rowley 一直在持续优化元组解构。在 PostgreSQL 18 中，他已合并多项补丁：`CompactAttribute`（5983a4cff）、更快的偏移对齐（db448ce5a）以及内联解构循环（58a359e58）。在此基础上，他提出**预计算 `attcacheoff`**，而不是在每次属性访问时计算。讨论已演进至 v10（2026 年 2 月），**Andres Freund** 贡献了 NULL 位图转 isnull 的方案，使 Apple M2 在部分场景下加速达 **63%**。补丁集仍在积极审查中。

## 为什么重要

当执行器需要从元组中读取某列值时，必须：

1. **对齐**：按属性对齐要求对齐当前偏移
2. **获取**：通过 `fetch_att()` 读取值
3. **前移**：跳过当前属性到下一个

这些步骤形成依赖链：每个偏移都依赖前一个。指令级并行空间有限。对于定长属性，PostgreSQL 可以缓存偏移（`attcacheoff`）以避免重复计算对齐和长度——但此前缓存是在解构循环*内部*完成的。David 的想法是：在 `TupleDesc` 初始化完成时**一次性**计算，而不是对每个元组都算一遍。

## 技术方案

### TupleDescFinalize()

核心改动是引入 `TupleDescFinalize()`，必须在 `TupleDesc` 创建或修改后调用。该函数会：

1. **预计算**所有定长属性的 `attcacheoff`
2. **记录** `firstNonCachedOffAttr`——第一个无法缓存偏移的属性的 `attnum`（即首个 varlena 或 cstring 属性）
3. **启用**一个紧凑循环，先处理所有有缓存偏移的属性，再进入需要手动计算偏移的属性

如果元组在最后一个有缓存偏移的属性之前存在 NULL，则只能使用 `attcacheoff` 到该 NULL 为止——但对于没有早期 NULL 的元组，快速路径可以在一个紧凑循环中处理大量属性，而无需任何按属性的偏移运算。

### 专用解构循环

补丁添加了一个专用循环，先处理所有有预计算 `attcacheoff` 的属性，再进入处理 varlena/cstring 属性的循环。对于设置了 `HEAP_HASNULL` 的元组，当前代码会对每个属性调用 `att_isnull()`。进一步优化是：在遇到第一个 NULL 之前，持续解构而不调用 `att_isnull()`。基准测试中的场景 #5（首列 int not null、末列 int null）最能体现这一点——常表现为最大加速。

### 可选的 OPTIMIZE_BYVAL 循环

可选变体针对所有被解构属性均为 `attbyval == true` 的元组增加一个循环。此时可以内联 `fetch_att()`，而无需处理指针类型的分支，从而减少分支、获得更紧凑的循环。代价是：当该优化不适用时，需要额外检查 `attnum < firstByRefAttr`。基准测试中，不同硬件和编译器对是否启用该优化效果不一。

## 基准测试设计

为最大化解构工作占总 CPU 的比例，David 设计了如下基准查询：

```sql
SELECT sum(a) FROM t1;
```

其中 `a` 列几乎在最后，因此必须先解构前面的所有属性才能读取 `a`。八种表结构涵盖首列（int/text、null/not null）和末列（int null/not null）的组合。对每种表结构，分别在 0、10、20、30、40 个额外 `INT NOT NULL` 列下运行——每次基准运行包含 40 个场景，每个场景 100 万行。

## 基准测试结果

结果因硬件和编译器而异：

- **AMD Zen 2（3990x）+ GCC**：启用 `OPTIMIZE_BYVAL` 时平均加速达 **21%**；部分测试超过 **44%**；无回退。
- **AMD Zen 2 + Clang**：0 额外列场景下存在小幅回退。
- **Apple M2**：场景 #1 和 #5 提升明显；其余提升较小；部分补丁有轻微回退。
- **Intel（Azure）**：在共享、少核实例上运行，因与其他负载共享 L3，结果噪声较大。

## 补丁演进

### v1 → v3（2025 年 12 月 – 2026 年 1 月）

- **v1**：三个补丁——0001（预计算 attcacheoff）、0002（实验性 NULL 位图前瞻）、0003（移除专用 hasnulls 循环）
- **v2**：代码库同步、修复 0003 中 NULL 位图 Assert、JIT 修复（移除 `TTS_FLAG_SLOW`）、更多基准
- **v3**：代码库同步、放弃 0002 和 0003（基准收益有限）、仅保留 0001

### v4（2026 年 1 月）

回应 Chao Li 的代码审查：

- **NULL 位图 mask**（tupmacs.h）：补充注释——当 `natts & 7 == 0` 时 mask 为 0，代码会正确返回 `natts`
- **未初始化 TupleDesc**：`firstNonCachedOffAttr == 0` 表示无缓存属性；`-1` 表示未初始化。添加 Assert，失败时提示调用 `TupleDescFinalize()`
- **拼写**："possibily" → "possibly"
- **LLVM**：修复编译警告

### v5–v8（2026 年 1–2 月）：Andres Freund 的 NULL 位图优化

**Andres Freund** 加入讨论并提出关键改进：不再对每列调用 `att_isnull()`，而是用 SWAR（SIMD Within A Register）技术直接从 NULL 位图计算 `isnull[]` 数组。思路是：将位图的一个字节乘以精心选定的值（如 `0x204081`），使每位扩散到独立字节，再掩码。这样无需 2KB 查找表，在多数硬件上效果良好。

David 在补丁 0004（“Various experimental changes”）中实现了该方案。0004 的其他改动包括：

- **`populate_isnull_array()`**：用乘法技巧批量将 NULL 位图转换为 `tts_isnull`
- **`tts_isnull` 大小**：向上取整到 8 的倍数，使循环可一次写 8 字节（避免 `memset` 内联问题）
- **`t_hoff`**：对 `!hasnulls` 元组，使用 `MAXALIGN(offsetof(HeapTupleHeaderData, t_bits))` 替代 `t_hoff`
- **`fetch_att_noerr()`**：新增无 `elog` 的变体，用于常见的 `attlen == 8` 情况

**John Naylor** 指出当字节为 255 时 `__builtin_ctz(~bits[bytenum])` 未定义；David 通过强制转换修复：`pg_rightmost_one_pos32(~((uint32) bits[bytenum]))`。

启用 0004 后，Apple M2 平均比 master 快 **53%**（排除 0 额外列测试约 **63%**）。Andres 建议使用 `pg_nounroll` 和 `pg_novector` pragma 防止 GCC 对 `populate_isnull_array()` 过度向量化，该函数曾生成低效代码。

### v9（2026 年 2 月 24 日）

- **补丁重排**：`deform_bench` 移至 0001，便于在 master 上做基准测试
- **0004（新）**：`slot_getsomeattrs` 的 sibling-call 优化——将 `slot_getmissingattrs()` 移入 `getsomeattrs()`，使编译器可应用尾调用优化，降低开销并改善 0 额外列测试
- **0005（新）**：将 `CompactAttribute` 从 16 字节缩小到 8 字节——`attcacheoff` 改为 `int16`（最大 2^15），布尔用位标志。Andres 指出 8 字节便于编译器使用 scale factor 8 的单条 LEA；6 字节则需两条 LEA

### v10（2026 年 2 月 25 日）— 最新补丁集

基于实际 v10 补丁内容：

**0003（优化元组解构）**：
- `firstNonCachedOffsetAttr`：首个无缓存偏移的属性的索引
- `firstNonGuaranteedAttr`：首个可为 NULL、缺失或 `!attbyval` 的属性的索引。仅解构到此属性时，无需访问 `HeapTupleHeaderGetNatts(tup)`，减少 CPU 流水线依赖
- `TTS_FLAG_OBEYS_NOT_NULL_CONSTRAINTS`：保证属性优化的可选标志（部分代码在 NOT NULL 校验前即解构元组）
- `populate_isnull_array()`：使用 `SPREAD_BITS_MULTIPLIER_32`（0x204081）将反转的 NULL 位图每位扩散到独立字节；分低 4 位和高 4 位处理以避免 uint64 溢出
- `fetch_att_noerr()`：无 `elog` 的 `fetch_att()` 变体；当 attlen 来自 `CompactAttribute` 时安全
- `first_null_attr()`：用 `pg_rightmost_one_pos32` 或 `__builtin_ctz` 查找位图中首个 NULL

**0004（sibling-call 优化）**：
- `getsomeattrs()` 现负责调用 `slot_getmissingattrs()`
- `slot_getmissingattrs()`：用 for 循环替代 `memset`（基准显示循环更快）
- `slot_deform_heap_tuple()`：在 `attnum < reqnatts` 时于末尾调用 `slot_getmissingattrs()`；参数由 `natts` 改为 `reqnatts`

**0005（8 字节 CompactAttribute）**：
- `attcacheoff` 改为 `int16`；大于 `PG_INT16_MAX` 的偏移不缓存
- `attispackable`、`atthasmissing`、`attisdropped`、`attgenerated` 使用位标志
- 保存 `cattrs = tupleDesc->compact_attrs` 以帮助 GCC 生成更优代码（避免重复 `TupleDescCompactAttr()` 调用）

**审查修复**：
- **Amit Langote**：修复 rebase 噪声（重复的 `attcacheoff` 检查）
- **Zsolt Parragi**：大端序修复——在 `populate_isnull_array()` 的 `memcpy` 前加入 `pg_bswap64()`
- **拼写**："benchmaring" → "benchmarking"，"to info" → "into"
- **Andres**：在 `slot_getmissingattrs` 前设置 `*offp` 以减少栈溢出；将 `attnum` 改为 `size_t` 以修复 GCC `-fwrapv` 下的代码生成

### deform_bench 与基准基础设施

**Andres** 与 **Álvaro Herrera** 讨论了 `deform_bench` 的放置：`src/test/modules/benchmark_tools`、`src/benchmark/tuple_deform`，或单一微基准扩展。Andres 主张逐步合并有用工具，而非等待完整套件。David 倾向于先完成解构优化补丁；deform_bench 可能单独提交。

## 代码审查：Chao Li 的反馈

Chao Li 审查了补丁并提出几点：

1. **NULL 位图 mask**：补充注释说明 `natts & 7 == 0` 时无溢出/OOB 风险
2. **未初始化 TupleDesc**：在 TupleDesc 创建时将 `firstNonCachedOffAttr` 初始化为 `-1`；在 `nocachegetattr()` 中断言 `>= 0`
3. **语义一致性**：用 0 表示“无缓存属性”，>0 表示“有缓存”
4. **拼写**："possibily" → "possibly"

David 在 v4 中均已处理。

## 当前状态

- **v10**（2026 年 2 月）为最新补丁集：0001（deform_bench）、0002（TupleDescFinalize 桩）、0003（主优化）、0004（sibling-call + NULL 位图→isnull）、0005（8 字节 CompactAttribute）
- **Andres Freund** 支持合并 0004，认为收益明显；0005 的收益较不确定（在解构少量列时有助于 LEA 寻址）
- **Zsolt Parragi**（Percona）、**Álvaro Herrera**、**John Naylor**、**Amit Langote** 持续参与审查
- deform_bench 的放置（src/test/modules 或 src/benchmark）仍在讨论；David 希望先落地优化补丁

## 结论

在 `TupleDescFinalize()` 中预计算 `attcacheoff`，并为有缓存偏移的属性使用专用紧凑循环，可为现代 CPU 上的元组解构带来可观的加速。当元组具有大量定长列且 NULL 较少或较晚出现时，优化效果最佳。结合 Andres Freund 的 NULL 位图转 isnull 方案（“0x204081” SWAR 技巧），Apple M2 在排除边缘情况后可达 **63%** 加速。`slot_getsomeattrs` 的 sibling-call 优化进一步降低开销。结果因硬件和编译器而异；GCC 可能对部分循环过度向量化，可通过 pragma 或将循环索引改为 `size_t` 缓解。补丁集（v10）经 Andres、John Naylor、Zsolt Parragi、Álvaro Herrera、Amit Langote 等多轮审查，正在向集成推进。

## 参考资料

- [讨论：More speedups for tuple deformation](https://www.postgresql.org/message-id/flat/CAApHDvpoFjaj3%2Bw_jD5uPnGazaw41A71tVJokLDJg2zfcigpMQ%40mail.gmail.com)
- 相关 v18 工作：5983a4cff（CompactAttribute）、db448ce5a（更快偏移对齐）、58a359e58（内联解构循环）
