# Pipeline 审查报告

## 审查范围
- 设计草案：[01 - Pipeline Draft.md](./01%20-%20Pipeline%20Draft.md)
- 用户文档：`docs/guides/pipeline.md`
- 核心实现：`src/alpenstock/pipeline/*.py`
- 测试脚本：`tests/pipeline/*`、`tests/typecheck/*`

## 基线验证
- `pixi run pytest -q tests/pipeline`：26 passed
- `pixi run pytest -q tests/typecheck/test_pyright_contracts.py`：5 passed

> 现有测试全部通过，但以下问题属于“设计-实现不一致”或“未覆盖风险”，不一定会在当前测试中暴露。

## 发现（按严重度排序）

### 1. [严重] 文档承诺的 `__saver/__loader` 自定义序列化按草案写法无法生效
**证据**
- 草案要求：`Pipeline Draft.md:132`（建议在子类实现 `__saver(path, obj)`、`__loader(path)`）
- 实现查找：`src/alpenstock/pipeline/_decorators.py:215,225`
  - `getattr(type(self), "__saver", None)`
  - `getattr(type(self), "__loader", None)`

**问题说明**
- Python 双下划线方法会发生 name mangling，类里写 `def __saver(...)` 后，真实属性名是 `_ClassName__saver`，不是 `__saver`。
- 因此按草案写法实现后，运行时查找不到，最终静默回退到默认 `pickle`。

**复现实验（本地临时脚本）**
- 定义 `def __saver(self, path, obj): ...` 后执行 stage，结果：`step.pkl=True`，`step.custom=False`（自定义方法未被调用）。

**风险**
- 用户以为已切换序列化格式，实际仍在使用默认 `pickle`。
- 这会导致性能、兼容性或合规预期落空，且问题较隐蔽。

---

### 2. [严重] `stage_id` 未做路径安全校验，可写出 `save_to` 目录外
**证据**
- `stage_id` 只校验“非空字符串”：`src/alpenstock/pipeline/_decorators.py:88-90`
- 文件路径直接拼接：`src/alpenstock/pipeline/_decorators.py:115,128`
  - `runtime.save_path / f"{stage_id}.pkl"`

**问题说明**
- 若 `stage_id="../escape"`，最终会把快照写到 `save_to` 上级目录。

**复现实验（本地临时目录）**
- 缓存目录仅预期出现 `cache/spec.yaml`。
- 实际还生成了 `escape.pkl`（位于缓存目录外）。

**风险**
- 缓存污染范围超出预期目录。
- 可能覆盖同级文件，增加误写风险。

---

### 3. [高] 恢复策略与草案不一致：实现没有“入口前恢复最后完成 Stage”
**证据**
- 草案恢复流程：`Pipeline Draft.md:116-121`（入口前恢复最后完成 Stage）
- 实现：`_bootstrap_if_needed` 仅做 spec/schema 校验与写入，未扫描 stage 快照
  - `src/alpenstock/pipeline/_decorators.py:136-170`
- `stage_ids` 被收集但未参与恢复逻辑
  - 定义：`src/alpenstock/pipeline/_meta.py:23,66-73`
  - 全局搜索仅定义处命中，无消费点

**问题说明**
- 当前逻辑是“每次调用某个 stage 时，才尝试读取该 stage 的 `<id>.pkl`”。
- 这与草案“先统一恢复到最近完成状态，再进入入口函数”不同。

**可观察影响（本地临时脚本）**
- 仅有 `stage1.pkl` 时，直接调用 `stage2()` 不会自动先恢复 `stage1` 状态，结果可错误（示例输出：`v=0 out=1`）。

**风险**
- 对入口函数调用顺序更敏感；一旦出现条件分支或非标准调用，可能得到与草案描述不一致的结果。

---

### 4. [高] “前阶段文件缺失、后阶段文件存在”时会出现“执行 + 覆盖回滚”的混合行为
**证据**
- 缓存命中判定依赖“对应 stage 文件存在 + 完成标记存在”：`src/alpenstock/pipeline/_decorators.py:114-120`
- 当先执行了前阶段，再加载后阶段快照时，`_apply_payload` 会覆盖 state/output：`src/alpenstock/pipeline/_decorators.py:194-210`

**问题说明**
- 若 `stage1.pkl` 缺失但 `stage2.pkl` 仍在：
  1. 调用 `stage1()` 会真正执行；
  2. 随后调用 `stage2()` 会从旧 `stage2.pkl` 恢复并跳过函数体；
  3. 刚算出的 `stage1` 结果可能被旧快照覆盖。

**复现实验（本地临时脚本）**
- 输出示例：`log=['s1'] a=2 b=3`（`s1`执行了，但最终状态回到旧快照值）。

**风险**
- 产生“部分执行 + 旧状态覆盖”的混合语义。
- 若 stage 含外部副作用（I/O、计数、远程调用），会出现副作用已发生但状态回滚的前后不一致。

---

### 5. [中] Spec“构造后不可修改”的契约可被绕过
**证据**
- `Spec()` 仅 `setdefault("on_setattr", attrs.setters.frozen)`：`src/alpenstock/pipeline/_fields.py:42-44`

**问题说明**
- 用户可显式传入 `Spec(on_setattr=None)` 覆盖冻结策略，直接改写 Spec 字段。
- 即使默认冻结，Spec 若为可变对象（如 `dict/list`），仍可做原地修改（冻结只限制重绑定，不限制深层变更）。

**风险**
- 与草案 `Pipeline Draft.md:28`（Spec 构造后不可修改）存在偏差。
- 运行期若修改 Spec，缓存语义可变得不可预测。

---

### 6. [中] 草案示例与真实 API 存在多处不一致，易误导落地使用
**证据（草案）**
- `Pipeline Draft.md:40`：示例 `from pipeline import ...`（实际包路径是 `alpenstock.pipeline`）
- `Pipeline Draft.md:49`：示例 `@define_pipeline(kw_only=True)`（实现要求显式 `save_path_field`）
- `Pipeline Draft.md:50`：示例继承 `PipelineBase`（当前实现是 decorator-only，无 base class 要求）

**风险**
- 新使用者照抄示例会直接报错或产生错误心智模型。

## 测试覆盖缺口
当前测试覆盖了主流程和多数基础校验，但以下高风险点无直接测试：
- 自定义序列化钩子 `__saver/__loader` 的生效性与签名约束。
- `stage_id` 的路径合法性（禁止 `..`、路径分隔符等）。
- “入口前恢复最后完成 stage”语义（草案承诺）与当前实现差异。
- “前阶段文件缺失、后阶段文件存在”的恢复一致性。
- Spec 不可变契约被覆盖/深层可变对象原地修改的行为边界。

## 结论
本次实现在“基础缓存/恢复 + 主要校验 + 常规路径单测”上完成度较高，但存在几项关键风险：
1. 自定义序列化接口按草案写法不可用（严重）。
2. `stage_id` 缺乏路径安全约束（严重）。
3. 恢复语义与草案描述不一致，且在异常文件组合下出现混合行为（高）。

这些问题会在复杂/真实运行场景下暴露，建议优先处理上述 1-4 项。
