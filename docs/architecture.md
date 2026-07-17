# CSP 架构说明

本文档记录当前运行时架构。README 面向使用者；本文面向维护者，重点是并发边界、资源生命周期和测试不变量。

## 目标

运行时采用 CSP 风格的异步流水线：

- `S_Worker` 生产 `T`。
- `P_Worker` 发起外部请求，等待并生产 `Q`。
- `C_Worker` 原子获取一组 `T + Q` 并执行最终消费。

架构目标是资源所有权闭合、背压边界清晰、取消语义可测试。当前设计不包含中心调度器、动态角色选择或运行时动态并发分配。

## 核心组件

`Physical_Sem` 限制本地浏览器重操作并发。`P_Worker` 发出请求后，在等待 `Q` 返回期间不得持有该许可。

`T_Slot_Sem` 限制已入库 `T` 的容量。`T` 生成成功后才允许获取 slot。

`Q_Slot_Sem` 限制已返回并入库 `Q` 的容量。`Q` 真正返回前不得获取 slot。

`Q_Pending_Sem` 限制已发出但尚未终态的外部 `Q` 请求数量。

`Inventory` 是唯一库存门面。worker 不直接访问底层队列，只能调用：

```text
put_t(env)
put_q(env)
claim_pair()
```

`ResourceEnvelope` 把资源实体和库存 slot 绑定在一起。slot 只能释放一次。

`PairLease` 是 `claim_pair()` 返回的异步上下文管理器。pair 一旦 claim 成功，两个 envelope 的所有权转移给 lease，直到 consumer 退出上下文。

`AdmissionGate` 是局部生产准入门控，只根据库存深度和静态水位决定是否允许继续生产。它不选择 worker 角色，不搬运资源，不调整并发容量。

## Worker 流程

### S_Worker

1. 等待 `AdmissionGate` 允许生产 `T`。
2. 获取 `Physical_Sem`。
3. 生产 `T`。
4. 释放 `Physical_Sem`。
5. 调用 `ResourceEnvelope.create_with_slot(...)`，创建 envelope 并获取 `T_Slot_Sem`。
6. 调用 `Inventory.put_t(...)`，把所有权转移给 `Inventory`。
7. 所有权转移前如果异常或取消，释放已获取的 slot。

slot 获取和 envelope 创建必须绑定，避免取消落在“已获取 slot、尚未创建 envelope”的窗口。

### P_Worker

1. 等待 `AdmissionGate` 允许生产 `Q`。
2. 获取 `Q_Pending_Sem`。
3. 获取 `Physical_Sem`。
4. 创建请求并发送。
5. 释放 `Physical_Sem`。
6. 在不持有 `Physical_Sem` 的情况下等待 `Q` 返回。
7. `Q` 返回后调用 `ResourceEnvelope.create_with_slot(...)`，创建 envelope 并获取 `Q_Slot_Sem`。
8. 调用 `Inventory.put_q(...)`，把所有权转移给 `Inventory`。
9. 请求进入终态后释放 `Q_Pending_Sem`。

`Q_Pending_Sem` 表达外部在途上限；`Q_Slot_Sem` 只表达已返回库存容量。两者不能合并。

### C_Worker

1. 进入 `async with inventory.claim_pair() as pair`。
2. 获取 `Physical_Sem`。
3. 消费 pair。
4. 释放 `Physical_Sem`。
5. 退出 `PairLease`，释放两个库存 slot。

`C_Worker` 不允许先取单边资源再等待另一边。对 consumer 来说，pair claim 必须是原子的。

## Inventory 语义

第一版 `Inventory` 使用一把 lock 和一个 condition。lock 保护等待、复查、过期清理和弹出操作。

必须保持以下语义：

- 等待中的 consumer 不移除资源。
- claim 成功时同时移除一个有效 `T` 和一个有效 `Q`。
- 等待 pair 时被取消，不影响库存。
- claim 成功后被取消，由 `PairLease` 核销两个 envelope。
- worker 永远不能直接操作底层 `T` / `Q` 队列。

只要锁内逻辑保持很小，除 lazy expiry cleanup 外基本是 O(1)，单锁在当前版本可以接受。只有 profile 证明锁竞争成为真实瓶颈时，才考虑拆锁或分片。

## 过期模型

`ResourceEnvelope` 可以携带 `created_at` 和 `expires_at`。`Inventory` 在配对前可以丢弃已过期资源。

当前采用 lazy cleanup：

- `put_t`、`put_q`、`claim_pair` 被触发时顺手清理。
- 清理会释放它看到的过期 envelope 对应 slot。
- 系统完全静默时不会主动扫库。
- 单边长期故障和静默停摆由监控暴露，不靠后台 sweeper 修复。

当前不做 worker 级回队。消费失败后，已 claim 的 `T` 和 `Q` 都由 `PairLease` 核销。

## 容量策略

容量边界由 Semaphore 表达。启动期容量优先级：

```text
显式 PHYSICAL_CAP > CAPACITY_PROFILE > CPU/内存自动派生
```

`CAPACITY_PROFILE` 只在启动时读取，是静态 profile，不是运行时调度器。

默认 worker 数量由容量派生：

```text
S_WORKERS = Physical_Sem + 2
P_WORKERS = Q_Pending_Sem + 2
C_WORKERS = Physical_Sem + 2
```

worker 数量不是主要调参入口。主要并发边界是各类容量许可，而不是 coroutine 循环数量。

## 当前不做

当前版本明确不包含：

- 中心调度器；
- 运行时角色选择；
- 动态打分；
- 动态并发控制；
- worker 级回队；
- 高价值资源抢救策略；
- 后台过期清扫；
- 自动切换高风险浏览器模式。

这些方向可以单独实验，但不能混入基础所有权模型。

## 必须保持的不变量

实现和测试必须维持以下不变量：

- 每个已获取的库存 slot 最终释放一次且只释放一次。
- 每个已准入的 pending 请求最终释放一次 `Q_Pending_Sem`。
- `P_Worker` 等待 `Q` 返回时不持有 `Physical_Sem`。
- `Q_Slot_Sem` 只在 `Q` 返回后获取。
- `C_Worker` 只能通过 `Inventory.claim_pair()` 获取 `T` 和 `Q`。
- `claim_pair()` 要么返回一个受 `PairLease` 保护的完整 pair，要么不返回资源。
- 等待 pair 时取消，不移除库存资源。
- claim pair 后取消，两个 envelope 由 `PairLease` 释放。
- 触发清理时，过期资源不能被配对。
- 监控只读，不修改 Semaphore、队列或 worker 状态。

## 测试

```bash
.venv/bin/pip install -r tests/requirements.txt
```

快速检查：

```bash
python3 -m unittest tests.test_admission_gate tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_runtime_log_analyzer -v
```

完整测试：

```bash
python3 -m pytest tests -q
```

重点测试文件：

- `tests.test_inventory_unittest`：库存、过期和 lease 行为。
- `tests.test_register_runtime_unittest`：worker 运行语义和监控行。
- `tests.test_admission_gate`：局部门控水位。
- `tests.test_runtime_log_analyzer`：日志解析兼容性。
- `tests/test_cancel.py`：取消边界。
- `tests/test_property.py`：随机化不变量检查。
- `tests/test_stress.py`：更高并发 fake-service 压测。
