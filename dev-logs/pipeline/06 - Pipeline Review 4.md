# Pipeline 四轮审计补充（IDE/静态检查 `kw_only` 提示问题）

## 背景
- 用户在 IDE / 静态检查中发现：
  - 使用 `@define_pipeline(..., kw_only=True)` 时，仍报
  - `Fields without default values cannot appear after fields with default values`
- 同样结构在 `attrs.define(kw_only=True)` 下不会报错。
- 后续追问进一步确认：
  - 当改为 `kw_only=False` 时，必须保留该报错。
  - 不能通过修改字段默认行为“消音”来规避静态错误。

## 根因
- `define_pipeline` 的 `kw_only` 在类型签名中不够明确，导致 pyright 在自定义 dataclass-transform 场景下无法稳定按 `kw_only=True` 推断字段规则。
- 同时，字段 helper（`Spec/Input/Output/Transient/...`）的 `kw_only` 类型若不是与 attrs 一致的“可继承类级配置”，静态分析会把字段级语义误判为固定值。
- 结果是 `save_path: str = Transient()` 仍可能被当作“非默认必填字段”，触发“默认字段后不允许非默认字段”的提示。

## 修复
1. 将 `define_pipeline` 的 `kw_only` 提升为显式关键字参数（默认 `False`）：
   - `src/alpenstock/pipeline/_decorators.py`
   - `src/alpenstock/pipeline/_decorators.pyi`
   - `src/alpenstock/pipeline/__init__.pyi`
2. 运行时调用 `attrs.define(kw_only=kw_only, **attrs_define_kwargs)`，确保运行行为与声明一致。
3. 字段 helper 的类型签名将 `kw_only` 调整为 `bool | None = None`（与 attrs 语义对齐）：
   - `src/alpenstock/pipeline/_fields.pyi`
   - 未显式传 `kw_only` 时，静态检查会按类级 `kw_only` 规则推断，而不是强制当作 `False`。
4. 回退错误方向的临时策略：
   - 不再在 `Transient` 运行时 helper 内部默认注入 `kw_only=True`。
   - 保证 `kw_only=False` 场景下仍会暴露真实字段顺序错误。

## 新增测试
- 运行时回归：
  - `tests/pipeline/test_pipeline.py`
  - 新增 `test_define_pipeline_kw_only_allows_required_field_after_defaults`
  - 新增 `test_spec_file_uses_block_style_for_nested_mapping`（宽松断言 spec 文件已使用换行缩进而非内联字典）
- 静态检查回归：
  - 新增 `tests/typecheck/cases/pass_kw_only_required_after_defaults.py`
  - 新增 `tests/typecheck/cases/fail_kw_only_false_required_after_defaults.py`
  - `tests/typecheck/test_pyright_contracts.py` 同步新增正/反向 case

## 补充修复：spec.yaml 可读性格式
- 问题：
  - spec 文件在部分嵌套字典场景会写成 inline（如 `{k: 2}`），不利于人工阅读。
- 修复：
  - 将 YAML dumper 统一设置为 block style（`default_flow_style = False`），确保嵌套映射默认换行+缩进输出。
  - 涉及文件：
    - `src/alpenstock/pipeline/_decorators.py`
    - `src/alpenstock/pipeline/_spec_io.py`
- 验证：
  - 新增单测仅做宽松检查：包含 `spec_b:` 换行块，并排除 `spec_b: {k: 2}` 内联写法。

## 结论
- `define_pipeline(kw_only=True)` 场景下，不再触发字段默认值顺序误报。
- `define_pipeline(kw_only=False)` 场景下，会正确报出字段默认值顺序错误（不再被掩盖）。
- 该问题已通过运行时单测与 pyright 合同测试双重覆盖。
