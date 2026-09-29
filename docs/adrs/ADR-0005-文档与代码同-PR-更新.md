# ADR-0005：文档与代码同 PR 更新（关键资产管理方法）

- 状态：accepted
- 日期：2026-09-18
- 相关：`docs/` 全目录、`CHANGELOG.md`

## 背景

本项目交付物不只是代码，还包括**架构图、节点规格、七项方案细则、ADR**。
这类资产最容易腐烂：代码改了图没改，文档变成"看起来很美但没人信"的摆设。

## 决策

### 1. 目录分层（唯一真源）

```
docs/design/    架构与方案（architecture / nodes / retrieval / knowledge_update /
                routing / error_handling / resilience / fallback）
docs/dev/       开发与运维（setup / how-to-add-node / how-to-add-provider /
                testing / observability）
docs/api/       接口契约（rest_api）
docs/adrs/      架构决策记录（只增不改，废弃标 deprecated）
CHANGELOG.md    每次 PR 追加条目
README.md       分层：快速开始 / 开发 / 运维
```

### 2. 命名规范

| 类型 | 规范 | 示例 |
| --- | --- | --- |
| 设计文档 | 小写中划线 | `knowledge_update.md` |
| 开发文档 | 小写中划线 / `how-to-` 前缀 | `how-to-add-node.md` |
| ADR | `ADR-XXXX-简述.md`，四位序号 | `ADR-0002-三路召回与-RRF-融合.md` |
| 状态 | `proposed` / `accepted` / `deprecated` | ADR 头部字段 |

### 3. 强制同步矩阵

| 改了什么 | 必须同步 |
| --- | --- |
| 节点增删 / 状态字段变更 | `docs/design/nodes.md` 的规格表 |
| 图结构变更（边、条件分支） | `docs/design/architecture.md` 的 Mermaid + ASCII |
| 检索参数 / 阈值变更 | `docs/design/retrieval.md` 与 `testing.md` 基线 |
| 错误码新增 | `docs/design/error_handling.md` 错误码表 |
| Provider 新增 | 新增 ADR + `setup.md` + `resilience.md` 降级矩阵 |
| 接口变更 | `docs/api/rest_api.md` |
| 任何变更 | `CHANGELOG.md` 追加一行 |

### 4. 图与代码同源

Mermaid 源码**直接内嵌**在 Markdown 中，不单独存图片文件，
保证"改文档即改图"，且能在 GitHub / Typora / Cursor 中渲染。ASCII 简图保留一份，供终端与 Code Review 场景。

### 5. 版本对齐

- ADR 只增不改：决策变更写新 ADR，旧 ADR 标 `deprecated` 并指向新编号；
- 文档版本跟随代码：release tag 与 `doc_version` 语义化对齐；
- 评测基线变更（换 embedding / 换模型）必须更新 `docs/dev/testing.md` 的基线表与日期。

## 后果

- 正面：文档成为可执行的契约，Code Review 有明确检查表；
- 负面：每次改动有额外文档成本；通过"同步矩阵"把成本限定在**必须改的那一份**，避免全量重写。
