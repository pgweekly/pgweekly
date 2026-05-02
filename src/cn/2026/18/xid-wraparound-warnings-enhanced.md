# 增强 XID / MultiXact 回卷告警：百分比详情与 1 亿事务预警余量

## 引言

事务 ID 回卷是少数几种、集群可能在磁盘未满的情况下逐步走向数据不可用场景之一。PostgreSQL 早已在数据库中最老的普通 XID 逼近回卷边界时发出 `WARNING`，但纯数字的“还剩多少事务”不够直观；同时有反馈认为旧的默认预警窗口对如今的事务吞吐来说偏紧。

这篇基于 [pgsql-hackers 讨论串](https://www.postgresql.org/message-id/flat/aRdhSSFb9zZH_0zc%40nathan) 的文章梳理 Nathan Bossart 的补丁：**让告警更易读**，并把**首次大声告警**的时机**提前**；经评审后，第三块在热路径上周期性打 `LOG` 的方案被放弃，更早的发现仍交给 DBA 的监控与 `age(datfrozenxid)` 等常规手段。

## 技术分析

### 在 `DETAIL` 中补充百分比

第一处改动在 `varsup.c` 以及 `multixact.c` 中对应路径上，为既有回卷相关 `WARNING` 增加 `errdetail()`，例如：

```text
DETAIL:  Approximately 1.86% of transaction IDs are available for use.
```

百分比按“剩余空间 / 标识空间的一半”计算，与同文件里 `xidWrapLimit` 相对 `MaxTransactionId` 的定义方式一致；MultiXact 侧则使用 `MultiXactIds` 的平行表述。

`maintenance.sgml` 中的示例告警块同步更新，使文档与真实输出一致。

### 将告警阈值从 4000 万提高到 1 亿

原先在距离“停止分配”约 **4000 万**事务时开始强烈告警；该常数对普通 XID 与 MultiXact 一并改为 **1 亿**。注释里的“油表”比喻也随比例调整（约从“还剩 2%”改为“还剩 5%”量级），与新的数值自洽。

这是行为变化：在仍可能发生分配失败之前，**会更早**发出 `WARNING`，为执行库级 `VACUUM`、处理长期未决的两阶段事务、清理陈旧复制槽等争取时间。

### 已放弃的补丁：在 `GetNewTransactionId()` 上周期性早期 `LOG`

初版系列中的第三块补丁设想：当剩余 XID 少于 5 亿时，每 100 万事务在**仅服务器日志**中打一条 `LOG`，既比 `WARNING` 更早提醒，又尽量不打扰应用侧。

评审人 Shinya Kato 认为性价比不高：要在 `TransamVariablesData` 中增加字段，并在 **`GetNewTransactionId()`** 热路径上做取模判断；而需要更早信号的 DBA 完全可以用 `age(datfrozenxid)`（及相关目录信息）自定义阈值监控。

Nathan 接受该意见并**删除**该部分；最终提交为 **v4 的两补丁**（0001 + 0002），不再包含 0003。

### SQL 示例

下列语句本身不会触发回卷告警；它们属于文档与实践中常用的“距离回卷还有多远”的检查方式，也与评审中“用监控代替热路径日志”的思路一致。

```sql
-- 各数据库的事务“年龄”（维护手册中的典型查询）
SELECT datname, age(datfrozenxid) FROM pg_database;
```

```sql
-- 更细粒度：哪些表 relfrozenxid 最老（常见 DBA 巡检）
SELECT relname, age(relfrozenxid) AS rel_age
FROM pg_class
WHERE relkind = 'r'
ORDER BY age(relfrozenxid) DESC
LIMIT 20;
```

具体 `WARNING` 文案与阈值取决于 PostgreSQL 版本与构建；增强后的 `DETAIL` 与 1 亿事务余量需待包含该提交的版本/分支。

## 社区观点

**Chao Li** 对 v2 做了较细评审：建议百分比分母与回卷计算一致，使用 `(MaxTransactionId / 2)` 而非 `PG_INT32_MAX`（作者采用与周边代码一致的直接写法）；指出 `%.2f` 在余量仍很大时可能显示 `0.00%`，而 `errmsg` 中仍保留精确剩余事务数；修正拼写与主题行笔误；并讨论早期 `LOG` 阈值是否应联系 `autovacuum_freeze_max_age`。Nathan 认为即便 GUC 设得极不谨慎，回卷告警仍应保持有意义。

**Shinya Kato** 认可百分比信息对 DBA 有帮助；指出 `maintenance.sgml` 中的示例数字需随补丁更新（后续版本已修）；并推动拿掉热路径上的周期性日志方案。

**wenhui qiu** 表示整体 LGTM，并另提 CommitFest 上关于“无法降低表年龄时向用户解释原因”的补丁，与本系列正交但方向互补。

## 技术细节

- **最终涉及文件：** `varsup.c`、`multixact.c`、`doc/src/sgml/maintenance.sgml`。
- **百分比计算：** `(wrapLimit - current) / (MaxTransactionId / 2) * 100`（MultiXact 为 `MaxMultiXactId / 2`），与“最老可能可见 ID + 半环”的极限定义对齐。
- **未新增用户级 GUC：** 代码注释仍明确该阈值不会做成可配置项；本次是对默认余量与文案的保守调整。

## 现状

讨论串末尾 Nathan 说明相关工作已 **committed**（2026 年 3 月）。落地形态为：**保留**百分比 `DETAIL` 与 **100M** 预警门槛；**不包含**在 `GetNewTransactionId()` 上周期性打日志的实现。

## 结语

化解回卷风险仍依赖 vacuum 节奏与消除冻结障碍，但本串在**可读性**（百分比配合原有精确计数）与**预警提前量**（40M → 100M）上做了务实改进；放弃热路径日志则是在**可观测性**与**每事务开销**之间选择了后者，把“更早知道”交给显式的 `datfrozenxid` 监控——这也是评审共识下的合理取舍。
