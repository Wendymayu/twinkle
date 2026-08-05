# Phase 11b — PPT 生成能力 设计文档

> 参考：jiuwenswarm `skill_codes/ppt/`（14 阶段流水线）+ `pptx-craft`（Node.js CLI 工具）
> 依赖：Phase 11a Workflow 引擎
> 详细分析：`docs/design/ppt-generation-architecture.md`

## 一、jiuwenswarm 的 PPT 生成架构

### 1.1 两个组件

PPT 生成系统由 **两个独立组件** 协同工作：

1. **SkillTurbo 流水线**（Python）—— 编排层，决定"做什么"和"怎么做"
2. **pptx-craft**（Node.js）—— 执行层，提供"能力"（CLI 工具、模板、风格定义）

```
SkillTurbo 流水线 (Python)                    pptx-craft (Node.js)
  14 个 PlanNode 节点                           CLI 工具 + 静态资源
  定义流程、决策、内容生成                        HTML→PPTX 转换、风格、检查修复
       │                                              │
       └── 通过 bash 工具调用 pptx-craft CLI ──────────┘
```

**关键发现**：pptx-craft **不是** python-pptx。它是一个 Node.js 工具链，通过 Playwright 渲染 HTML → 截图/提取矢量 → JSZip 组装 PPTX。选择 HTML→PPTX 的核心原因：CSS 自由布局、ECharts 图表、Tailwind 样式、所见即所得。

**另一个关键发现**：pptx-craft 虽然以 Skill 目录结构存在（`office-claw-skills/pptx-craft/`），但 SkillTurbo **从未读取过它的 SKILL.md**。它只是被当作一个外部工具箱来用——通过 bash 调用 CLI、读取风格文件、调用 npm 脚本。真正承担"Skill"角色的是 SkillTurbo 流水线的 14 个 PlanNode。

### 1.2 完整流水线（14 阶段）

```
P0 PipelineInit ──→ P1 IntentClassify ──→ P3 DocumentParse ──→ P2 RequirementCollect
                                                                  │
                                                                  ▼
P4 ContentPlan ──→ P5 OutlineReview ──→ P6 DeepResearch ──→ P7 StylePrepare
                                                          │
                                                          ▼
P6.5 ImagePrepare ──→ P8 PPTPageGen ──→ P9 PPTExport ──→ P11 SpeakerNotes ──→ P10 Delivery
```

### 1.3 核心设计理念

| 理念 | 实现 |
|------|------|
| 纯代码编排 | 不依赖 LLM 做流程决策，每个节点用 Python 代码显式编排 |
| 共享上下文 | 所有节点通过 `inputs: dict[str, Any]` 读写共享状态，节点间无需直接通信 |
| LLM 仅用于内容生成 | LLM 只负责文本/代码生成，不决定流程走向 |
| 条件执行 | P3（文档解析）、P6.2（搜索）、P11（演讲备注）均为条件执行 |
| 重试与兜底 | P4 内容策划最多 2 次，P6.1 搜索最多 2 轮重搜，用户超时走 LLM 兜底 |
| best-effort | P11 演讲备注、P6.5 图片准备等失败不阻塞主流程 |

### 1.4 各阶段概要

| 阶段 | 职责 | LLM 用途 | 外部工具 |
|------|------|----------|----------|
| P0 PipelineInit | 环境检测、npm install、创建工作区 | 无 | pptx-craft CLI |
| P1 IntentClassify | 意图分类（有附件/有路径/纯主题） | 提取槽位 | 无 |
| P3 DocumentParse | 解析附件文档 | 推断主题 | read_file / image_ocr |
| P2 RequirementCollect | 收集需求槽位 | 提取/生成候选 | ask_user_question |
| P3.5 TemplateContext | 模板叙事上下文 | 无 | 读取 template-spec.json |
| P4 ContentPlan | 素材评估 + 搜索 + 大纲生成 + 校验 | 生成搜索查询、撰写大纲 | web_search |
| P5 OutlineReview | 大纲审阅 | 修订大纲 | ask_user_question |
| P6 DeepResearch | 深度研究（per-page 并发） | 搜索查询、撰写研究报告 | web_search / fetch_webpage |
| P7 StylePrepare | 风格准备 | 生成自定义风格 | 读取风格文件 |
| P6.5 ImagePrepare | 图片准备 | 分析图片需求 | text_to_image / pptx-craft CLI |
| P8 PPTPageGen | HTML 幻灯片生成（最复杂） | 生成 HTML 页面 | pptx-craft CLI check/fix |
| P9 PPTExport | 导出 PPTX | 无 | pptx-craft CLI convert |
| P11 SpeakerNotes | 演讲备注 | 生成备注 | pptx-craft CLI notes |
| P10 Delivery | 交付 | 无 | send_file_to_user |

### 1.5 数据流

```
P0 → inputs[pptx_root, output_dir, pages_dir]
P1 → inputs[has_documents, doc_paths, image_paths, need_speaker_notes]
P3 → inputs[doc_raw_path, doc_parse_ok, topic]
P2 → inputs[topic, page_count, audience, style_id, search_mode, source_type, research_depth]
P4 → inputs[outline_path, material_richness, search_results]
P6 → inputs[research_paths]
P7 → inputs[style_file_path]
P6.5 → inputs[image_map_path]
P8 → pages_dir 下 HTML 文件
P9 → inputs[pptx_path, pptx_filename]
P10 → inputs[delivery_status, __artifact__]
```

### 1.6 用户交互点

| 位置 | 交互内容 | 超时兜底 |
|------|---------|---------|
| P2.1 主题选择 | 4 个候选主题让用户选择 | LLM 从候选中挑选 |
| P2.2 批量询问 | 页数/受众/目的 | LLM 推断默认值 |
| P2.3 风格选择 | 5 种预设风格 | LLM 选 business-classic |
| P5 大纲审阅 | 确认/编辑/修改 | 跳过审阅 |

### 1.7 pptx-craft 的能力

| 能力 | 入口 | 调用节点 |
|------|------|---------|
| HTML→PPTX 转换 | `cli.js convert` | P9 |
| 环境检测 | `cli.js check-env` | P0 |
| 工作区管理 | `cli.js generate-timestamp-dir` / `ensure-output-dir` | P0 |
| 页面质量检查 | `cli.js check` | P8/P9 |
| 页面自动修复 | `cli.js fix` | P8/P9 |
| 研究质量校验 | `cli.js validate-research` | P6 |
| 演讲备注 | `cli.js notes extract-text` / `inject` | P11 |
| 风格定义 | `references/styles/{style_id}/style.md` | P7 |
| 设计规范 | `references/designer.md` | P8 |

## 二、Twinkle 的 PPT 生成方案

### 2.1 PPTX 生成方式决策

| 方案 | 优势 | 劣势 |
|------|------|------|
| **python-pptx**（先期） | 纯 Python，无外部依赖，先跑通流水线 | 布局能力有限，图表需手动构建，视觉设计不如 HTML 灵活 |
| **HTML→PPTX**（后期） | CSS 自由布局、ECharts 图表、Tailwind 样式、所见即所得 | 需要 Node.js + Playwright，依赖链长；HTML→PPTX 转换有边界限制 |

**策略**：先 python-pptx 跑通流水线，再切 HTML→PPTX。切换时只需替换 P8（页面生成）和 P9（导出）两个节点，其他节点不变。

### 2.2 分阶段演进

| Phase | 节点数 | 能力 | PPTX 方式 |
|-------|--------|------|-----------|
| **11b-1（最小可用）** | 7 | 无文档、无搜索、无风格的简单 PPT | python-pptx |
| **11b-2** | +3 | 加入文档解析 + 搜索 + 风格 | python-pptx |
| **11b-3** | +3 | 加入大纲审阅 + 图片 + 演讲备注 | python-pptx |
| **11b-4** | 14 | 完整功能 | HTML→PPTX |

### 2.3 Phase 11b-1：最小可用流水线

```
PipelineInit ──→ IntentClassify ──→ RequirementCollect ──→ ContentPlan ──→ PPTPageGen ──→ PPTExport ──→ Delivery
```

#### PipelineInit

- 创建输出目录（`<workspace>/output/ppt-{timestamp}/`）
- 产出：`inputs["output_dir"]`, `inputs["pages_dir"]`

#### IntentClassify

- LLM 从用户消息判断意图：有附件？有路径？纯主题？
- 提取：`topic`, `has_documents`, `image_paths`
- Phase 11b-1 只处理 `has_documents=False`（纯主题模式）

#### RequirementCollect

- LLM 从用户消息提取槽位：`topic`, `page_count`, `audience`, `style_id`
- 缺失 `topic` → LLM 生成候选 → 询问用户（超时 120s → LLM 兜底）
- 缺失 `page_count` → 默认 8 页
- `page_count` 是内容页数，总页数 = page_count + 2（封面 + 结束页）

#### ContentPlan

- LLM 生成大纲（`outline.md`）：`# 大纲：{topic}` + `## 页面规划` + `### P{N}:` 页面块
- 校验大纲格式：页面数 = page_count + 2，首页 cover，末页 ending
- 校验失败 → 重试（最多 2 次）
- 产出：`inputs["outline_path"]`, `inputs["outline"]`

#### PPTPageGen

- 读取大纲，逐页调用 `call_llm` 生成内容
- 每页返回 JSON：`{"title": "...", "body": "...", "page_type": "data/cover/ending/..."}`
- 产出：`inputs["pages"]`（所有页面的 JSON 列表）

#### PPTExport

- 读取 `pages` 列表，用 python-pptx 生成 .pptx 文件
- 基础布局规则：cover（居中标题+副标题+日期）、ending（居中感谢语）、data（标题+正文列表）
- 产出：`inputs["pptx_path"]`

#### Delivery

- 验证 PPTX 文件存在 + 大小 > 0
- 返回文件路径

### 2.4 数据流

```
PipelineInit   → inputs[output_dir, pages_dir]
IntentClassify → inputs[has_documents, topic, image_paths]
RequirementCollect → inputs[topic, page_count, audience, style_id]
ContentPlan    → inputs[outline_path, outline]
PPTPageGen     → inputs[pages]
PPTExport      → inputs[pptx_path]
Delivery       → inputs[file_path]
```

### 2.5 用户交互

| 位置 | 交互内容 | 超时兜底 |
|------|---------|---------|
| RequirementCollect | 缺失 topic → 4 个候选让用户选择 | LLM 从候选中挑选 |
| RequirementCollect | 缺失 page_count/audience → 询问 | LLM 推断默认值 |

### 2.6 失败处理

| 场景 | 处理 |
|------|------|
| ContentPlan 大纲校验失败 | 重试最多 2 次，仍失败 → fallback |
| PPTPageGen 单页生成失败 | 跳过该页，生成占位内容（best-effort） |
| PPTExport 导出失败 | fallback |
| 用户超时未回复 | LLM 兜底选默认值 |
| Workflow 整体失败 | `execute_workflow` 返回 error，LLM 自主降级 |

### 2.7 后续 Phase 演进

**Phase 11b-2**（+3 节点）：
- P3 DocumentParse：解析附件文档，提取内容
- P6 DeepResearch：搜索 + 深度研究（per-page 并发）
- P7 StylePrepare：风格准备（预设风格 + 自定义风格）

**Phase 11b-3**（+3 节点）：
- P5 OutlineReview：大纲审阅，用户确认/编辑
- P6.5 ImagePrepare：图片准备（本地图片 + AI 生图）
- P11 SpeakerNotes：演讲备注（best-effort）

**Phase 11b-4**（切换到 HTML→PPTX）：
- P8 PPTPageGen：改为生成 HTML 页面（1280×720px，Tailwind + ECharts）
- P9 PPTExport：改为 `cli.js convert`（引入 pptx-craft）
- P0 PipelineInit：加入 npm install + playwright install
- 密度检查 17 项 + CLI check/fix 自动修复

## 三、与 jiuwenswarm 的映射

| jiuwenswarm | Twinkle | 说明 |
|---|---|---|
| `skill_codes/ppt/`（14 节点） | `<WORKFLOWS_DIR>/pptx-craft/root.py` | Phase 11b-1 先做 7 节点 |
| `pptx-craft` (Node.js CLI) | Phase 11b-1~3: python-pptx / Phase 11b-4: 引入 pptx-craft | 渐进式切换 |
| `ask_user_question` | HITL 机制 | 超时兜底一致 |
| `stream_llm_collect` | `call_llm` 回调 | 不走 Hook |
| `inputs["pptx_root"]` 定位 pptx-craft | Phase 11b-1~3: 不需要 / Phase 11b-4: 需要类似机制 | 通过 bash 调用 CLI |
| `web_search` + `fetch_webpage` | Phase 11b-2: 需集成搜索工具 | 待设计 |
| `send_file_to_user` | 返回文件路径 | 后续可加文件下载工具 |
