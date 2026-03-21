# Pipeline 异步 Stage 支持设计记录

## 背景
- 当前 `pipeline` 模块运行稳定，但 `@stage_func(id=..., order=...)` 只支持同步函数。
- 现有实现默认 Stage 是“调用后立即完成”的同步单元：函数体执行结束、返回 `None`、写入 finished marker 与 stage 快照，这三件事发生在同一个同步调用链中。
- 新需求是：**不引入新 API**，让同一个 `stage_func` 同时兼容同步 `def` 与异步 `async def`，并保持已有同步语义不变。

## 约束前提
- 若某个 Stage 是异步函数，则其 Pipeline 入口方法也应是异步函数。
- 异步入口方法中按 Python 原生语义调用 Stage：
  - `await self.stage1()`
  - `await self.stage2()`
- 本次设计**不**尝试支持以下模式：
  - 在同步 `run()` 中直接调用异步 Stage
  - 自动在 `stage_func` 内部隐式创建或接管事件循环
  - 为异步场景新增第二套装饰器（如 `async_stage_func`）

## 设计目标
1. 保持同步 Pipeline 的用户写法、运行行为、类型行为不变。
2. 在不新增公开 API 的前提下，使 `@stage_func` 可装饰 `async def stage(self) -> None`。
3. 保持以下核心语义不变：
   - Stage 调用顺序仍然严格按 `order` 递增。
   - cache hit 仍然跳过 Stage 函数体。
   - 只有 Stage 真正成功完成后，才写 finished marker 与 `<stage_id>.pkl`。
   - `state/output` 从快照恢复，`input/transient` 保留当前实例值。
   - 异常、中断、取消都不应把当前 Stage 视为完成。

## 非目标
- 不为一个 Pipeline 实例提供并发执行多个 Stage 的能力。
- 不改变 cache key、spec 校验、field schema 校验、连续性校验等既有机制。
- 不解决“用户忘记 `await self.stage()`”这类逻辑错误之外的全部异步误用。

## 推荐方案

### A. `stage_func` 采用双路径包装
- `stage_func` 在装饰阶段识别原函数是否为 coroutine function。
- 若原函数为同步函数：
  - 保持当前同步包装逻辑。
  - 返回同步方法，调用结果仍为 `None`。
- 若原函数为异步函数：
  - 返回异步方法。
  - 调用结果为 awaitable；只有在 `await` 完成后，才等价于同步 Stage 的“成功返回 `None`”。

### B. 运行时拆分为同步/异步两条执行路径
- 保留现有同步 `_run_stage(...)` 逻辑给同步 Stage 使用。
- 新增 `_run_stage_async(...)` 给异步 Stage 使用。
- 两条路径共享相同的语义步骤：
  1. 读取 pipeline meta 与 runtime state。
  2. 执行 bootstrap。
  3. 校验“只能调用下一阶段”。
  4. 若存在 stage 快照，则先恢复 payload。
  5. 若快照内已包含该 Stage 的 finished marker，则直接返回。
  6. 否则执行 Stage 函数体。
  7. 仅在函数体成功完成后，写入 finished marker 与快照。

### C. 异步 Stage 的完成语义
- 对同步 Stage：
  - “`fn(self)` 返回且返回值为 `None`”即视为完成。
- 对异步 Stage：
  - “`await fn(self)` 成功结束且结果为 `None`”即视为完成。
- 这一定义保证“完成后才持久化”的事务边界不变，只是把完成时点从“同步返回”扩展为“await 完成”。

## 推荐的语义解释

### 1. 顺序约束
- 同步与异步统一使用同一套顺序守卫。
- 若前一 Stage 尚未完成，则后一 Stage 无论同步/异步都不得启动。
- 因此合法写法是：

```python
async def run(self) -> None:
    await self.stage1()
    await self.stage2()
```

- 非法写法包括：
  - 未执行 `stage1` 就 `await self.stage2()`
  - 已完成 `stage1` 后再次调用 `await self.stage1()`
  - 在同一实例上并发触发多个 Stage

### 2. cache hit 语义
- 同步场景维持现状：调用时若命中 finished snapshot，函数体不执行，直接返回 `None`。
- 异步场景下：
  - `await self.stage()` 仍然可能命中缓存。
  - 命中时函数体不执行，await 会快速完成。
- 对用户而言，差异仅在 Python 调用约定，不在 pipeline 语义。

### 3. 异常与取消
- 同步 Stage 抛异常时，当前 Stage 不写快照。
- 异步 Stage 在 `await` 过程中抛异常或收到取消时，也不写 finished marker，不保存该 Stage 的完成态。
- 恢复粒度仍然是 Stage，而不是 Stage 内部的 await 点。

### 4. 恢复与覆盖
- 从快照恢复时，规则保持不变：
  - 恢复 `state/output`
  - 恢复 finished markers
  - 不覆盖 `input/transient`
- 这条规则与同步/异步无关，不需要改变数据格式。

## 不推荐方案与原因

### 方案一：在同步包装器里隐式执行协程
- 例如内部尝试 `asyncio.run(...)` 或类似桥接。
- 问题：
  - 在已有事件循环中会直接失败或语义混乱。
  - 会把“同步方法”和“异步方法”的调用约定混在一起，难以解释。
  - 很难保持已有同步语义与错误边界的一致性。

### 方案二：为异步场景引入新 API
- 例如新增 `async_stage_func`。
- 问题：
  - 与“不引入新 API”的目标冲突。
  - 会让同步/异步 Pipeline 拥有两套近似规则，增加文档和学习成本。

### 方案三：统一把所有 Stage 都改成 awaitable
- 问题：
  - 会破坏现有同步 Pipeline 的调用方式。
  - 与“保持已有语义不变”冲突。

## 实现层面的关键改动

### 1. 装饰器层
- `stage_func` 需要区分同步函数和 coroutine function。
- 对异步函数返回 `async def wrapped(...)`。
- 对同步函数保留 `def wrapped(...)`。
- 两种包装器都继续保留：
  - stage id 元数据
  - stage order 元数据
  - original fn 元数据
  - “不接受额外参数”的运行时保护

### 2. Meta 与签名校验
- 现有签名校验只验证“只能接收 `self`”。
- 这条规则可以继续保留。
- 需要放宽“返回 `None`”的静态与文档表述：
  - 同步 Stage：返回 `None`
  - 异步 Stage：await 后结果为 `None`

### 3. RuntimeState 增强
- 当前 runtime 只记录：
  - `bootstrapped`
  - `cache_enabled`
  - `save_path`
  - `finished_stages`
- 支持异步后，建议新增“in-flight stage”状态，用于防止：
  - 同一实例上并发进入两个 Stage
  - 同一 Stage 在未完成前被重复启动
- 该状态不参与持久化，仅用于实例内运行时守卫。

### 4. 阻塞 IO 与事件循环兼容
- 现有缓存校验、spec 读写、payload 读写、自定义 `__saver/__loader` 调用，本质上都依赖同步磁盘 IO。
- 若在异步 Stage 的执行路径中直接调用这些同步 helper，会阻塞事件循环，破坏异步框架的正常调度。
- 推荐方案是：
  - **保留现有同步 helper 作为唯一实现**
  - 在异步 Stage runner 中，使用 `await asyncio.to_thread(sync_helper, ...)` 调用这些同步 helper
- 该策略的优点：
  - 不引入新依赖
  - 不复制一套 `*_async` helper，避免逻辑漂移
  - 最大程度复用现有稳定的同步语义与原子写实现
- 因此，本设计**不推荐**系统性新增异步版 helper，如：
  - `_bootstrap_if_needed_async`
  - `_load_payload_async`
  - `_save_payload_async`
- 更推荐的写法是：

```python
await asyncio.to_thread(_bootstrap_if_needed, self, meta=meta, runtime=runtime)
payload = await asyncio.to_thread(_load_payload, self, stage_file)
await asyncio.to_thread(_save_payload, self, stage_file, payload)
```

- 只有当某个同步 helper 的职责边界过大，导致“线程内阻塞 IO”和“事件循环线程内状态更新”难以区分时，才考虑做**小范围重构**。
- 这种重构的目标应是澄清边界，而不是为 async 再复制一套完整实现。

### 5. 类型桩
- `stage_func` 的 `.pyi` 需要从“只接受 `Callable[..., None]`”扩展为同时接受：
  - `Callable[..., None]`
  - `Callable[..., Awaitable[None]]`
- 否则运行时已经支持 async，但 pyright 仍会把异步 Stage 声明判错。

### 6. 文档与测试
- 正式用户文档需要补充同步/异步双态说明。
- 测试需要新增异步运行时合同与类型合同。

## 必须保持不变的既有语义
1. `save_to=None` 时仍禁用持久化，但顺序守卫继续生效。
2. `spec.yaml` 校验与 field schema 校验逻辑不变。
3. 连续性校验仍然按 `order` 进行。
4. finished markers 仍是 stage payload 的一部分。
5. cache hit 时，Stage 函数体仍然不得执行。
6. 自定义 `__saver/__loader` 语义不变。

## 需要新增的测试

### 运行时测试
1. 异步两阶段 Pipeline 首次运行会生成 `spec.yaml` 和各 stage 快照。
2. 异步 Pipeline cache hit 会跳过函数体。
3. 异步 Pipeline 缺少最后一个快照时，只重跑缺失 Stage。
4. 异步 Stage 抛异常时，不写 finished marker 与快照。
5. 异步 Stage 被取消时，不写 finished marker 与快照。
6. 异步 Pipeline 仍然遵守顺序守卫。
7. 同一实例并发启动多个 Stage 时，应明确失败。
8. 同步 Pipeline 既有测试应全部继续通过，证明无回归。

### 类型检查测试
1. `@stage_func` 装饰同步 `def stage(self) -> None` 继续通过。
2. `@stage_func` 装饰 `async def stage(self) -> None` 通过。
3. `async def run(self) -> None: await self.stage()` 通过。
4. 异步 Stage 若声明了额外参数，仍应报错。
5. 若忘记 `await self.stage()`，应尽可能通过类型合同捕获明显误用。

## 建议涉及文件
- `src/alpenstock/pipeline/_decorators.py`
- `src/alpenstock/pipeline/_decorators.pyi`
- `src/alpenstock/pipeline/__init__.pyi`
- `src/alpenstock/pipeline/_meta.py`
- `tests/pipeline/`
- `tests/typecheck/`
- `docs/guides/pipeline.md`

## 结论
- 在“异步 Stage 必须由异步入口方法按 `await self.stage()` 调用”的前提下，`stage_func` 采用双路径设计是可行且推荐的。
- 该方案不需要新增公开 API，可以最大程度保持同步语义、缓存语义、恢复语义与错误语义稳定。
- 真正需要新增的不是另一套用户接口，而是：
  - 一条异步执行路径
  - 一组异步合同测试
  - 一层实例内 in-flight 守卫

## 状态
- 记录时间：2026-03-21
- 状态：设计方向已确认，待进入实现阶段。
