# 轻量级 Pipeline 草案

本项目实现了一套**轻量级 Pipeline**（流水线）框架，其目标是在科研、优化等场景下提供一种简单而可靠的方式来组织分阶段执行的任务。框架强调分阶段可打断、可恢复、可缓存，并把责任边界清晰地交给用户：你控制输入和代码的稳定性，框架负责保存与恢复。

> 本规范使用了一些保留的英文术语（Pipeline、Stage、Spec、State、Input、Output、Transient）来描述核心概念，以便避免翻译造成的歧义。

## 目标

* **分阶段执行**：一个 Pipeline 由若干 Stage 组成，每个 Stage 完成后都会持久化一次。
* **可中断**：执行过程中随时可以被 `Ctrl+C` 等信号打断。
* **可恢复**：再次启动时自动从最近完成的 Stage 恢复运行，无需用户干预。
* **可缓存**：对于相同的 `save_to` 目录和 Stage 标识，Stage 的第二次调用会直接复用磁盘缓存，而不是重新计算。
* **一致的开发语义**：使用者只需编写“正常运行”的入口方法（如 `run/fit/execute`），框架会根据需要自动进行缓存和恢复。

## 设计边界

为了保持简单，这个框架**刻意**不做以下事情：

* **缓存失效检测**：不检测 Input 值的变化，也不检测 Stage 代码的修改导致的缓存失效。只要 Spec 与保存的一致，Stage 缓存就被视为有效；否则由使用者负责。
* **异常处理**：框架不会捕获或改写异常。只有 Stage 正常结束并持久化，才能恢复。
* **并发支持**：默认假设单机单进程语义。若需要并发运行，请自行保证每个进程的 `save_to` 目录独立，避免竞争。
* **版本迁移**：如果字段集合或 Spec 改变，将直接报错，不尝试向下兼容。

## 核心概念（Concepts）

* **Pipeline**：一个 Pipeline 对象的行为由**代码**和**Spec**（不可变配置）共同决定。Pipeline 持有多个属性（字段），它们被分为不同的语义类别。
* **Stage**：Pipeline 的最小执行与缓存单元。每个 Stage 由 `@stage_func(id="...", order=<int>)` 装饰的方法定义，必须提供唯一的 `stage_id` 与唯一的 `order`。`order` 越小越早执行，Stage 在 Pipeline 入口方法中必须按此顺序调用。
* **Spec**：决定 Pipeline 行为的不可变配置。Spec 会被序列化为 `spec.yaml`，在恢复时与当前传入的 Spec 做深度比较，不一致则报错。Spec 字段在对象构造后不能修改。
* **State**：持久化的中间状态，在 Stage 之间传递，并随 Stage 快照一起保存与恢复。
* **Output**：持久化的输出，供 Pipeline 运行结束后读取，与 State 类似但语义上用于向外暴露结果。
* **Input**：输入数据，不会被保存。多个 Stage 可以读取 Input，但更改 Input 内容不会导致缓存失效。
* **Transient**：仅在运行时使用的字段，如 `save_to`（缓存目录）、`log_dir` 等，不会持久化。

## 快速示例

下面的示例展示了一个简单的训练 Pipeline，它包含两个 Stage：初始化权重和训练过程。通过指定 `save_to` 路径，Stage 会在完成时保存快照；再次运行时会自动从最近完成的 Stage 恢复，不会重复计算。

```python
from attrs import define
from pathlib import Path
import numpy as np

from alpenstock.pipeline import (
    define_pipeline, Spec, State, Input, Output, Transient, stage_func
)

@define
class TrainSpec:
    lr: float
    total_iter: int = 100

@define_pipeline(save_path_field="save_to", kw_only=True)
class ToyTrain:
    # 使用 Spec()/State()/Input()/Output()/Transient() 来标注字段的语义
    spec: TrainSpec = Spec()
    x: np.ndarray = Input()  # 输入数据，不保存
    w: np.ndarray = State()  # 持久化的权重
    loss_curve: float = Output()  # 持久化输出
    save_to: str | Path | None = Transient(default=None)  # 缓存目录

    def run(self) -> None:
        self.init_stage()
        self.train_stage()

    @stage_func(id="init", order=0)
    def init_stage(self) -> None:
        # 初始化权重
        self.w = self.x.mean(axis=0)

    @stage_func(id="train", order=1)
    def train_stage(self) -> None:
        # 训练过程，这里为了演示简化为对 w 做一次更新
        self.w = self.w - self.spec.lr * self.x.sum(axis=0)
        self.loss_curve = (self.x @ self.w).sum()

# 第一次运行：执行两个 Stage，并将结果保存到 ./cache/ 中
p = ToyTrain(spec=TrainSpec(lr=0.1), x=np.random.randn(10, 3), save_to="./cache/")
p.run()

# 第二次运行：自动从 Stage "train" 的快照恢复，跳过 init_stage 和 train_stage
p2 = ToyTrain(spec=TrainSpec(lr=0.1), x=np.random.randn(10, 3), save_to="./cache/")
p2.run()  # 不会重新计算

print(p2.loss_curve)  # 从缓存中获取
```

## 语义规范

### 执行模型

* Pipeline 的入口方法名称没有硬性规定，可以是 `run`、`fit`、`execute` 等。用户需要在入口方法中按 `order` 升序调用 Stage。
* Stage 必须使用 `@stage_func(id=..., order=...)` 装饰：`id` 在同一 Pipeline 内必须唯一且只允许字母、数字、下划线；`order` 必须是非负整数，且在同一 Pipeline 内唯一。Stage 调用时不接受额外参数。
* 运行时只允许调用“下一阶段”（按 `order`）；调用未来阶段或已完成阶段会立即报错。
* 当启用 `save_to` 时，Stage 被调用的第二次及之后都会直接读取磁盘缓存，不再执行函数体。

### 缓存键

缓存命中只依赖两部分：

1. ``save_to`` 目录
2. ``stage_id``

因此，不同的 Pipeline **不得使用同一个 save_to 目录**，否则会出现缓存污染。推荐的做法是用实验名称或时间戳生成独立目录，如 `./runs/MyPipeline/experiment1/`。

### Stage 依赖边界

为了保证缓存语义正确，每个 Stage 必须视为纯函数：

> Stage 的输出只能依赖于当前的 Spec、persistent (State/Output) 与 Input。不得依赖于 Transient 字段、环境变量、时间等外部状态。

框架不会检测依赖边界是否被破坏；如果 Stage 依赖了未持久化的随机性或修改了 Input，那么责任由用户自行承担。

### Stage 调用与返回值

* 若 Stage 已完成并有缓存，函数体不会被执行，直接返回 `None`。
* Stage 方法必须返回 `None`；跨 Stage 的数据传递只能通过 State/Output。

### 恢复流程与覆盖顺序

当 `save_to` 非空时，Pipeline 在**首次调用任意 Stage**时执行初始化校验：

1. **Spec 校验**：若 `spec.yaml` 存在，则读取并与当前 Spec 做深比较，不一致则报错。
2. **缺失 spec 的损坏判定**：若 `spec.yaml` 缺失但目录中存在任意 `<stage_id>.pkl`（或任意 `*.pkl` 快照），视为缓存损坏，直接报错并要求用户手动清理目录。
3. **首次初始化**：仅当 `spec.yaml` 缺失且没有 stage 快照时，写入新的 `spec.yaml`。
4. **字段集合校验**：当 `spec.yaml` 存在时，当前类的字段名与种类必须与上次运行一致，否则报错。
5. **连续性校验**：按 `order` 升序检查 `<stage_id>.pkl` 文件。若出现“前阶段缺失、后阶段存在”，直接报错并要求用户手动清理缓存目录。

随后，在每次调用某个 Stage 时：

1. 若该 Stage 的 `<stage_id>.pkl` 存在，则加载该快照并恢复 persistent (State/Output) 与完成标记。
2. 若快照包含该 Stage 的完成标记，则跳过函数体；否则执行函数体并覆盖写回该 Stage 快照。
3. Input 与 Transient 始终使用当前实例传入的值，不从快照覆盖。

### 中断与长 Stage

框架的恢复粒度是 Stage。如果在一个 Stage 内发生异常或被打断，则该 Stage 的进度不会保存，重启后从上一个完成的 Stage 重新开始。如果 Stage 很长，可以采用两种办法：

* 在 Stage 内部将进度写入某个 State，并手动调用自己的保存函数。
* 拆分为更小的 Stage，或将复杂部分外移到单独的 Pipeline 组合执行。

## 序列化约定

框架持久化的是一个字典（state_dict），其中包含 persistent (State/Output) 字段以及 Stage 完成标记。默认使用 `pickle` 序列化；如果要自定义保存格式，可以在 Pipeline 子类中实现 `__saver(path, obj)` 和 `__loader(path)` 方法。

* **Spec 序列化**：Spec 会被序列化为 `spec.yaml`。键必须是字符串；值应为标量、列表或字典。如果尝试序列化复杂对象，将抛出异常。
* **state_dict 结构**：允许字典、列表和元组嵌套；叶子节点可以是标量类型（包括 `numpy` 标量）、`numpy.ndarray` 或其他可被 `pickle` 处理的对象。
* **Stage 文件**：每个 Stage 的快照文件名为 `<stage_id>.pkl`。每次 Stage 成功完成都会覆盖该文件。
* **原子写入**：写入 `spec.yaml` 和 Stage 文件时，使用临时文件 + 覆盖的方式保证原子性。
* **Hook 查找规则**：自定义 `__saver/__loader` 支持 Python name mangling，框架会按类层级解析（兼容 `def __saver(...)` 的写法）。

## 持久化目录结构

当提供了 ``save_to`` 目录时，框架将在该目录中创建以下文件：

* **spec.yaml**：记录 Spec 的序列化内容。
* **`<stage_id>.pkl`**：每个 Stage 对应一个全量快照文件，保存 persistent 状态和 Stage 完成标记。

框架不生成额外的 manifest；Stage 完成标记作为特殊的 persistent 字段存储在快照中，其名称格式为 ``__stage_finished_<stage_id>``。

## 字段分类规则

字段的语义完全由 ``attrs.field`` 的 ``metadata`` 决定。为了简化使用，本项目提供了五个辅助构造函数：

* **Spec()** – 标记字段为 Spec。Spec 字段构造完成后不可修改，且不允许覆盖 `on_setattr`。
* **State()** – 标记字段为 State。随 Stage 快照保存与恢复。
* **Output()** – 标记字段为 Output。随 Stage 快照保存与恢复，语义上用于输出。
* **Input()** – 标记字段为 Input。不会保存，可在多个 Stage 中读取。
* **Transient()** – 标记字段为 Transient。运行时临时使用，不持久化。

每个 Pipeline 必须显式使用这些构造函数来声明各字段的类别。未标注类别的字段会被视为 Transient。

## 最佳实践与常见陷阱

### 建议做法

* **记录随机性**：如果算法依赖随机性，请将随机种子或随机状态放入 State，以获得可重复的结果。
* **记录重要参数**：将影响行为的重要超参数放入 Spec 中，而不是放在 Transient 中。
* **合理拆分 Stage**：长时间运行且需要中途恢复的工作应拆分成多个 Stage，或在 Stage 内自行管理进度。
* **将路径等配置标记为 Transient**：例如 `save_to`、日志目录等，不必保存。

### 常见陷阱（由用户负责）

* **修改 Input**：框架无法阻止对 Input 对象的原地修改（例如改变数组内容），这可能导致缓存结果与预期不符。
* **依赖未持久化状态**：Stage 函数中读取了环境变量、全局随机数或系统时间，这些变化不会触发缓存失效检测。
* **共用同一目录**：不同 Pipeline 不应使用相同的 `save_to` 目录，以免互相污染缓存文件。
* **Spec 变化**：若 Spec 字段增删或值不同，恢复时会抛出错误；此行为不做迁移。
* **Spec 深层可变对象原地修改**：`Spec` 的冻结只阻止字段重绑定，不阻止 `dict/list` 等对象的原地修改；此类行为由用户自行约束。

## 实现检查清单

下面列出了实现层面需要遵守的一些硬规则，便于开发者编写或调试 Pipeline：

1. 每个 Stage 必须显式提供唯一的 `stage_id` 与唯一的 `order`；`stage_id` 只能包含字母、数字和下划线；`order` 必须是非负整数。
2. 缓存键只由 `save_to` 和 `stage_id` 决定；不要在不同 Pipeline 间复用 `save_to` 目录。
3. Stage 的输出只能依赖 Spec、persistent 和 Input；不做失效检测。
4. Stage 方法必须返回 `None`，跨 Stage 只能通过 State/Output 传递数据。
5. 每个 Stage 保存全量快照，文件名为 `<stage_id>.pkl`，使用原子覆盖策略。
6. `spec.yaml` 存在时必须通过 spec 与字段集合校验；`spec.yaml` 缺失且存在 `*.pkl` 快照时必须报错。
7. `spec.yaml` 缺失且不存在 stage 快照时，视为新缓存并写入 `spec.yaml`。
8. 若缓存目录出现“前 stage 缺失、后 stage 存在”，初始化阶段必须报错并要求手动清理。
9. Stage 调用必须严格按 `order` 递增执行；不允许跳调或重调已完成阶段。
10. 恢复时，persistent/output 从磁盘覆盖；Input 与 Transient 使用当前实例的值。

遵循以上规范，就可以构建出可打断、可恢复且行为确定的 Pipeline。
