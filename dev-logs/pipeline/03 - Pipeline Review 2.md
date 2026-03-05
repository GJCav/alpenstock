# Pipeline 二轮审计报告

## 审计范围
- 历史审计报告：`02 - Pipeline Review 1.md`
- 设计草案：`01 - Pipeline Draft.md`
- 用户文档：`docs/guides/pipeline.md`
- 核心实现：`src/alpenstock/pipeline/*.py`
- 测试：`tests/pipeline/*`、`tests/typecheck/*`

## 基线验证
- `pixi run pytest -q tests/pipeline tests/typecheck/test_pyright_contracts.py`：`39 passed`

## 上轮问题复检结论
1. `__saver/__loader` name mangling 失效：已修复。
2. `stage_id` 路径安全：已修复（仅允许字母/数字/下划线）。
3. 草案“入口前恢复最后 stage”与实现不一致：已对齐为“调用 stage 时恢复”。
4. “前缺后有”混合行为：已修复（bootstrap 连续性校验）。
5. `Spec` 不可变契约可绕过：已修复（运行时 + 类型层禁止 `on_setattr` 覆盖）。
6. 草案示例与 API 不一致：大部分已修复。
7. 测试覆盖缺口：已补齐主要风险点。

---

## 新发现（按严重度排序）

### 1. [高] `spec.yaml` 缺失但 stage 快照仍在时，会静默复用旧缓存并写入新 spec

**证据**
- 当 `spec.yaml` 不存在时，当前实现直接写入新的 spec：
  - `src/alpenstock/pipeline/_decorators.py:160-176`
- 写完后不会校验“这些已有 stage 快照是否与新 spec 匹配”，后续 stage 调用仍可命中旧快照：
  - `src/alpenstock/pipeline/_decorators.py:121-127`

**复现实验（本地临时目录）**
- 第一次运行：`spec_k=1`，产物 `y=30`。
- 手动删除 `spec.yaml`，保留 `s1.pkl/s2.pkl`。
- 第二次运行：`spec_k=999`，结果仍为旧值 `y=30`，且 stage 被跳过（`log=[]`），同时新的 `spec.yaml` 被写入为当前 spec。

**风险**
- 产生“新 spec + 旧 stage 数据”的静默不一致。
- 用户无法从异常中感知缓存已污染，容易得到错误实验结论。

**建议**
- 当 `spec.yaml` 缺失且检测到任意 `<stage_id>.pkl` 存在时，直接报错并要求手动清理缓存目录。
- 同步增加对应回归测试。

---

### 2. [中] 草案仍存在一处与实现不一致描述：`spec.yaml` 缺失是否报错

**证据**
- 草案实现检查清单写的是“缺失或不一致都会报错”：
  - `Pipeline Draft.md:195`
- 实现实际行为是“缺失时创建新 `spec.yaml`”：
  - `src/alpenstock/pipeline/_decorators.py:171-176`

**风险**
- 文档承诺与实现不一致，增加用户误判恢复语义的概率。

**建议**
- 统一草案该条描述，明确“缺失时仅在空目录/无 stage 快照时允许初始化；否则报错”。

---

## 测试覆盖建议（增量）
- 增加用例：删除 `spec.yaml` 但保留 stage 快照，预期抛错。
- 增加用例：`spec.yaml` 缺失且无 stage 快照，预期允许初始化（若该语义保留）。

## 结论
本轮复检显示：上次报告中的核心问题已基本修复，整体质量显著提升。
当前最关键剩余风险是“`spec.yaml` 丢失后的静默旧缓存复用”，建议优先修复并补回归测试。
