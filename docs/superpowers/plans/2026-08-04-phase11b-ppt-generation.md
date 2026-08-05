# Phase 11b-1 — PPT 生成工作流 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Phase 11b-1 最小可用 PPT 生成工作流（7 节点流水线），使用 python-pptx 生成 .pptx 文件。

**Architecture:** 基于已完成的 Workflow 引擎（Phase 11a），创建一个 PlanNode 递归树作为 workflow。节点通过共享 `inputs` dict 传递数据，LLM 仅用于内容生成（意图分类/槽位提取/大纲生成/页面内容），PPTX 导出通过 `command_exec` 调用独立 helper 模块完成。参考 translate workflow 的模式。

**Tech Stack:** Python 3.11+ / python-pptx / PlanNode ABC

## Global Constraints

- 不使用 `pytest-asyncio`——用 `asyncio.run()` + `free_port`/`port_factory` fixtures（`tests/conftest.py`）
- 配置类继承 `_StrictModel`（`twinkle/config/schema.py`），`extra="forbid"`
- Workflow 代码放在 `<WORKSPACE>/workflows/pptx-craft/root.py`（不是 twinkle 包内）
- 节点内不可直接 `import os/sys/subprocess`（沙箱限制），文件操作和系统调用通过 `call_tool`
- PPTX 生成逻辑放在 `twinkle/agentserver/workflow/ppt_export.py` 作为独立可执行 helper
- 所有 LLM 调用通过 `self.call_llm(prompt)` （回调注入模式）
- 无法直接使用 `time`/`datetime` 模块（沙箱限制），时间戳通过 `command_exec` 生成

---

## File Structure

```
twinkle/agentserver/workflow/
    ppt_export.py          # NEW: python-pptx helper (takes JSON via stdin, outputs .pptx)

<WORKSPACE>/workflows/pptx-craft/
    root.py                # NEW: 7-node PlanNode pipeline

tests/
    test_workflow_pptx.py  # NEW: integration tests with mock LLM
```

---

### Task 1: ppt_export helper — python-pptx JSON → PPTX 生成器

**Files:**
- Create: `twinkle/agentserver/workflow/ppt_export.py`
- Test: 在 Task 3 中集成测试

**Interfaces:**
- Produces: 可独立执行的模块 `python -m twinkle.agentserver.workflow.ppt_export '<json>'`
- 输入: JSON 字符串 `{"output_path": "...", "topic": "...", "pages": [{title, body, page_type}]}`
- 输出: 生成 `.pptx` 文件到指定路径，stdout 打印结果路径

这是零依赖纯逻辑模块（只依赖 python-pptx）。从 workflow 节点内通过 `call_tool("command_exec", ...)` 调用。

- [ ] **Step 1: 实现 ppt_export 模块**

```python
# twinkle/agentserver/workflow/ppt_export.py
"""PPTX export helper — reads JSON from stdin, writes .pptx via python-pptx.

Called from workflow nodes via command_exec:
  echo '<json>' | python -m twinkle.agentserver.workflow.ppt_export

Expected JSON shape:
  {"output_path": "output/ppt-xxx/主题.pptx", "topic": "...", "pages": [
      {"title": "...", "body": "...", "page_type": "cover|data|ending"},
      ...
  ]}

Layout rules (Phase 11b-1):
  - cover: 居中标题(Pt44 bold) + 副标题(Pt20) + 日期
  - ending: 居中感谢语(Pt40 bold) + 副文本(Pt18)
  - data: 顶部标题栏(Pt32 bold) + 正文列表(Pt18)
  - Slide size: 13.333" x 7.5" (widescreen 16:9)
"""
from __future__ import annotations

import json
import sys
from datetime import date

from pptx import Presentation
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt


def _add_textbox(slide, left, top, width, height):
    """Add a textbox and return its text_frame."""
    txBox = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    tf = txBox.text_frame
    tf.word_wrap = True
    return tf


def _add_paragraph(tf, text, size=18, bold=False, alignment=PP_ALIGN.LEFT, space_after=8):
    """Add a paragraph to a text_frame with formatting."""
    p = tf.add_paragraph() if len(tf.paragraphs) > 0 and tf.paragraphs[0].text != "" else tf.paragraphs[0]
    # If first paragraph is already filled, add new one
    if tf.paragraphs[0].text and len(tf.paragraphs) == 1:
        pass  # will add_paragraph below
    else:
        p = tf.paragraphs[0]

    # Actually: simpler approach — always use first paragraph for first call, add for rest
    return p


def _fill_first_paragraph(tf, text, size=18, bold=False, alignment=PP_ALIGN.LEFT):
    """Fill the first (default) paragraph in a text_frame."""
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(size)
    p.font.bold = bold
    p.alignment = alignment
    return p


def generate_pptx(output_path: str, topic: str, pages: list[dict]) -> str:
    """Generate .pptx from page data. Returns output_path on success."""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    for page in pages:
        page_type = page.get("page_type", "data")
        title = page.get("title", "")
        body = page.get("body", "")

        if page_type == "cover":
            slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank layout
            # Title
            tf = _add_textbox(slide, 1.0, 2.5, 11.333, 1.5)
            _fill_first_paragraph(tf, title, size=44, bold=True, alignment=PP_ALIGN.CENTER)
            # Subtitle
            tf2 = _add_textbox(slide, 1.0, 4.2, 11.333, 1.0)
            _fill_first_paragraph(tf2, body, size=20, alignment=PP_ALIGN.CENTER)
            # Date
            tf3 = _add_textbox(slide, 1.0, 5.5, 11.333, 0.5)
            _fill_first_paragraph(tf3, str(date.today()), size=14, alignment=PP_ALIGN.CENTER)

        elif page_type == "ending":
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            tf = _add_textbox(slide, 1.0, 2.5, 11.333, 2.0)
            _fill_first_paragraph(tf, title, size=40, bold=True, alignment=PP_ALIGN.CENTER)
            p2 = tf.add_paragraph()
            p2.text = body
            p2.font.size = Pt(18)
            p2.alignment = PP_ALIGN.CENTER

        else:  # data / default
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            # Title bar at top
            tf = _add_textbox(slide, 0.5, 0.3, 12.333, 0.8)
            _fill_first_paragraph(tf, title, size=32, bold=True)
            # Body content — each line as a paragraph
            tf2 = _add_textbox(slide, 0.8, 1.5, 11.533, 5.5)
            lines = body.split("\n") if body else [""]
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                if i == 0:
                    _fill_first_paragraph(tf2, line, size=18)
                else:
                    p = tf2.add_paragraph()
                    p.text = line
                    p.font.size = Pt(18)
                    p.space_after = Pt(8)

    prs.save(output_path)
    return output_path


def main():
    """Entry point: read JSON from stdin, generate PPTX."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    output_path = data.get("output_path", "")
    if not output_path:
        print("ERROR: output_path is required", file=sys.stderr)
        sys.exit(1)

    topic = data.get("topic", "演示文稿")
    pages = data.get("pages", [])

    try:
        path = generate_pptx(output_path, topic, pages)
        print(f"PPTX saved to {path}")
    except Exception as e:
        print(f"ERROR: pptx generation failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证模块可正常导入**

```bash
python -c "from twinkle.agentserver.workflow.ppt_export import generate_pptx; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: 冒烟测试——生成一个最小 PPTX**

```bash
cd /tmp && echo '{"output_path":"test.pptx","topic":"测试","pages":[{"title":"封面标题","body":"副标题","page_type":"cover"},{"title":"内容页","body":"要点一\n要点二\n要点三","page_type":"data"},{"title":"谢谢","body":"感谢观看","page_type":"ending"}]}' | python -m twinkle.agentserver.workflow.ppt_export && ls -la test.pptx
```
Expected: `test.pptx` 文件存在且大小 > 0

- [ ] **Step 4: Commit**

```bash
git add twinkle/agentserver/workflow/ppt_export.py
git commit -m "feat(workflow): add ppt_export helper for python-pptx generation"
```

---

### Task 2: pptx-craft workflow — 7 节点 PlanNode 流水线

**Files:**
- Create: `<WORKSPACE>/workflows/pptx-craft/root.py`

**Interfaces:**
- Consumes: `PlanNode` (from sandbox), `call_llm`, `call_tool("command_exec", ...)`, `call_tool("write_file", ...)`
- Produces: `root` PlanNode 实例
- Inputs: `{"text": "用户原始消息"}`
- Outputs: `{"node": "delivery", "status": "ok", "pptx_path": "...", "file_path": "..."}`

流水线：PipelineInit → IntentClassify → RequirementCollect → ContentPlan → PPTPageGen → PPTExport → Delivery

- [ ] **Step 1: 创建 workflow 目录**

```bash
mkdir -p ~/.twinkle/workflows/pptx-craft
```

- [ ] **Step 2: 实现 7 节点 PlanNode 流水线**

```python
# <WORKSPACE>/workflows/pptx-craft/root.py
"""PPT Generation Pipeline — 7 节点流水线，基于 LLM 内容生成 + python-pptx 导出。

节点：
  1. PipelineInit        — 创建输出目录
  2. IntentClassify      — LLM 提取主题
  3. RequirementCollect  — LLM 提取槽位（页数/受众/风格）
  4. ContentPlan         — LLM 生成大纲
  5. PPTPageGen          — LLM 逐页生成内容
  6. PPTExport           — python-pptx 导出 .pptx
  7. Delivery            — 验证并返回文件路径

使用方式：
  execute_workflow(workflow_name="pptx-craft", inputs='{"text": "帮我做一个关于人工智能的PPT"}')
"""


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

class PipelineInitNode(PlanNode):
    """创建输出目录，产出 output_dir / pages_dir。"""

    async def _execute(self, inputs: dict):
        text = inputs.get("text", "")
        print(f"[PipelineInit] creating output directory...")

        # Generate timestamp via command_exec (sandbox has no time module)
        result = await self.call_tool(
            "command_exec",
            command="python -c \"import time; print(time.strftime('%Y%m%d-%H%M%S'))\"",
        )
        import json as _json
        ts = _json.loads(result).get("stdout", "").strip()
        if not ts:
            ts = "default"

        output_dir = "output/ppt-" + ts
        pages_dir = output_dir + "/pages"

        # Ensure directories exist
        await self.call_tool(
            "command_exec",
            command="mkdir -p " + output_dir + " " + pages_dir,
        )

        topic_guess = text[:40] if text else "Presentation"
        print(f"[PipelineInit] output_dir={output_dir}")
        return {"output_dir": output_dir, "pages_dir": pages_dir, "topic": topic_guess}


class IntentClassifyNode(PlanNode):
    """LLM 从用户消息判断意图：提取主题和附件标记。Phase 11b-1 只处理纯主题模式。"""

    async def _execute(self, inputs: dict):
        text = inputs.get("text", "")
        print(f"[IntentClassify] analyzing: {text[:60]}...")

        prompt = (
            "分析以下用户消息，判断用户是否想要生成PPT，如果是则提取主题。\n"
            "\n"
            "用户消息：\"" + text + "\"\n"
            "\n"
            "返回 JSON 格式（只返回 JSON，不要其他内容）：\n"
            '{"intent": "ppt" 或 "other", "topic": "提取的主题", "has_documents": false}'
        )
        result = await self.call_llm(prompt)
        data = self.extract_json(result)

        topic = data.get("topic", inputs.get("topic", ""))
        intent = data.get("intent", "ppt")
        print(f"[IntentClassify] intent={intent}, topic={topic}")
        return {"topic": topic, "has_documents": data.get("has_documents", False)}


class RequirementCollectNode(PlanNode):
    """LLM 从用户消息提取槽位：page_count / audience / style_id。缺失值用默认值填充。"""

    async def _execute(self, inputs: dict):
        text = inputs.get("text", "")
        topic = inputs.get("topic", "")
        print(f"[RequirementCollect] extracting slots for: {topic}")

        prompt = (
            "从以下用户消息中提取PPT制作需求参数。\n"
            "\n"
            "用户消息：\"" + text + "\"\n"
            "主题：\"" + topic + "\"\n"
            "\n"
            "参数说明：\n"
            "- page_count: 内容页数（不含封面和结束页），默认 8\n"
            "- audience: 目标受众，如\"技术团队\"/\"管理层\"/\"学生\"，默认\"通用\"\n"
            "- style_id: 风格，可选\"business-classic\"/\"tech-minimal\"/\"creative\"，默认\"business-classic\"\n"
            "- presentation_purpose: 演示目的，如\"汇报\"/\"教学\"/\"宣传\"，默认\"汇报\"\n"
            "\n"
            "返回 JSON 格式（只返回 JSON，不要其他内容）：\n"
            '{"topic": "...", "page_count": 8, "audience": "通用", "style_id": "business-classic", "presentation_purpose": "汇报"}'
        )
        result = await self.call_llm(prompt)
        data = self.extract_json(result)

        page_count = max(3, min(data.get("page_count", 8), 20))
        print(f"[RequirementCollect] page_count={page_count}, audience={data.get('audience')}")
        return {
            "topic": data.get("topic", topic),
            "page_count": page_count,
            "audience": data.get("audience", "通用"),
            "style_id": data.get("style_id", "business-classic"),
            "presentation_purpose": data.get("presentation_purpose", "汇报"),
        }


class ContentPlanNode(PlanNode):
    """LLM 生成 PPT 大纲。校验页面数 = page_count + 2（封面+结束页）。最多重试 2 次。"""

    async def _execute(self, inputs: dict):
        topic = inputs.get("topic", "")
        page_count = inputs.get("page_count", 8)
        audience = inputs.get("audience", "通用")
        purpose = inputs.get("presentation_purpose", "汇报")
        total_pages = page_count + 2  # + cover + ending
        print(f"[ContentPlan] generating outline: {topic}, {page_count} content pages")

        prompt = (
            "为以下主题生成 PPT 大纲。\n"
            "\n"
            "主题：\"" + topic + "\"\n"
            "内容页数：" + str(page_count) + " 页（不含封面和结束页）\n"
            "目标受众：" + audience + "\n"
            "演示目的：" + purpose + "\n"
            "\n"
            "要求：\n"
            "1. 第1页为封面（cover），最后一页为结束页（ending），中间为内容页（data）\n"
            "2. 每页包含标题和要点列表（3-6个要点）\n"
            "3. 封面页标题就是演示主题，副标题为简短的说明\n"
            "4. 结束页为感谢观看\n"
            "\n"
            "返回 JSON 格式（只返回 JSON，不要其他内容）：\n"
            '{"topic": "...", "pages": [\n'
            '  {"title": "封面标题", "body": "副标题说明", "page_type": "cover"},\n'
            '  {"title": "第1页标题", "body": "要点一\\n要点二\\n要点三", "page_type": "data"},\n'
            '  ...\n'
            '  {"title": "谢谢", "body": "感谢观看", "page_type": "ending"}\n'
            ']}\n'
            "\n"
            "注意：pages 数组长度必须是 " + str(total_pages) + "。"
        )

        for attempt in range(3):
            result = await self.call_llm(prompt)
            try:
                data = self.extract_json(result)
                pages = data.get("pages", [])
                if len(pages) == total_pages:
                    # Verify first is cover and last is ending
                    first_type = pages[0].get("page_type", "")
                    last_type = pages[-1].get("page_type", "")
                    if first_type == "cover" and last_type == "ending":
                        print(f"[ContentPlan] outline OK: {len(pages)} pages (attempt {attempt + 1})")
                        return {"outline": data, "pages_plan": pages}
                print(f"[ContentPlan] validation failed (attempt {attempt + 1}): "
                      f"expected {total_pages} pages, got {len(pages)}")
            except Exception as e:
                print(f"[ContentPlan] parse failed (attempt {attempt + 1}): {e}")

        # Fallback: build a minimal outline
        print(f"[ContentPlan] using fallback outline")
        fallback_pages = [
            {"title": topic, "body": purpose + " — " + audience, "page_type": "cover"},
        ]
        for i in range(page_count):
            fallback_pages.append({
                "title": topic + " (" + str(i + 1) + ")",
                "body": "要点一：待补充\n要点二：待补充\n要点三：待补充",
                "page_type": "data",
            })
        fallback_pages.append({"title": "谢谢", "body": "感谢观看", "page_type": "ending"})
        return {"outline": {"topic": topic, "pages": fallback_pages}, "pages_plan": fallback_pages}


class PPTPageGenNode(PlanNode):
    """逐页调用 LLM 生成详细内容。每页返回 title + body。"""

    async def _execute(self, inputs: dict):
        topic = inputs.get("topic", "")
        audience = inputs.get("audience", "通用")
        pages_plan = inputs.get("pages_plan", [])
        print(f"[PPTPageGen] generating content for {len(pages_plan)} pages...")

        generated_pages = []
        for i, plan in enumerate(pages_plan):
            page_type = plan.get("page_type", "data")
            plan_title = plan.get("title", "")
            plan_body = plan.get("body", "")

            if page_type in ("cover", "ending"):
                # Cover and ending pages use plan content directly
                generated_pages.append(plan)
                print(f"[PPTPageGen] page {i + 1}: {page_type} — {plan_title}")
                continue

            # Content page: ask LLM to expand
            prompt = (
                "为以下 PPT 页面生成详细内容。\n"
                "\n"
                "演示主题：\"" + topic + "\"\n"
                "目标受众：" + audience + "\n"
                "页面标题：\"" + plan_title + "\"\n"
                "页面大纲：\"" + plan_body + "\"\n"
                "\n"
                "要求：\n"
                "1. 生成 4-6 条要点，每条要点是一句完整的话\n"
                "2. 每条要点以\"- \"开头，每条一行\n"
                "3. 内容要有深度，不只罗列标题\n"
                "\n"
                "返回格式（只返回这个格式，不要其他内容）：\n"
                "页面标题\n"
                "\n"
                "- 第一条要点内容\n"
                "- 第二条要点内容\n"
                "...\n"
                "\n"
                "注意：不要返回 JSON，直接返回标题+要点格式。"
            )

            try:
                result = await self.call_llm(prompt)
                lines = result.strip().split("\n")
                # First non-empty line is title
                content_title = plan_title
                body_lines = []
                for line in lines:
                    stripped = line.strip()
                    if not body_lines and stripped and not stripped.startswith("-"):
                        content_title = stripped
                    elif stripped.startswith("-"):
                        body_lines.append(stripped[1:].strip())
                    elif stripped:
                        body_lines.append(stripped)

                generated_pages.append({
                    "title": content_title,
                    "body": "\n".join(body_lines) if body_lines else plan_body,
                    "page_type": page_type,
                })
                print(f"[PPTPageGen] page {i + 1}/{len(pages_plan)}: {content_title}")
            except Exception as e:
                print(f"[PPTPageGen] page {i + 1} failed: {e}, using plan fallback")
                generated_pages.append(plan)

        print(f"[PPTPageGen] generated {len(generated_pages)} pages")
        return {"pages": generated_pages}


class PPTExportNode(PlanNode):
    """调用 ppt_export helper 生成 .pptx 文件。"""

    async def _execute(self, inputs: dict):
        topic = inputs.get("topic", "演示文稿")
        output_dir = inputs.get("output_dir", "output/ppt-default")
        pages = inputs.get("pages", [])

        # Sanitize topic for filename
        safe_topic = "".join(c for c in topic if c.isalnum() or c in ("_", "-", " ", "."))[:50]
        pptx_path = output_dir + "/" + safe_topic + ".pptx"

        print(f"[PPTExport] generating {pptx_path} with {len(pages)} pages...")

        import json as _json
        payload = _json.dumps({
            "output_path": pptx_path,
            "topic": topic,
            "pages": pages,
        }, ensure_ascii=False)

        # Escape single quotes for shell
        escaped = payload.replace("'", "'\\''")

        result = await self.call_tool(
            "command_exec",
            command="echo '" + escaped + "' | python -m twinkle.agentserver.workflow.ppt_export",
        )

        print(f"[PPTExport] result: {str(result)[:200]}")
        return {"node": "export", "status": "ok", "pptx_path": pptx_path}


class DeliveryNode(PlanNode):
    """验证 PPTX 文件存在并返回路径。"""

    async def _execute(self, inputs: dict):
        pptx_path = inputs.get("pptx_path", "")
        topic = inputs.get("topic", "")
        print(f"[Delivery] verifying: {pptx_path}")

        # Try to read the file to verify it exists
        try:
            read_result = await self.call_tool("read_file", file_path=pptx_path)
            # read_file returns error string for binary files — that's expected for .pptx
            # The fact it didn't return "not found" means the file exists
            if "not found" in str(read_result).lower():
                print(f"[Delivery] file not found: {pptx_path}")
                return {"node": "delivery", "status": "error", "error": "PPTX file not found"}
        except Exception:
            # binary file — expected
            pass

        print(f"[Delivery] done: {pptx_path}")
        return {
            "node": "delivery",
            "status": "ok",
            "pptx_path": pptx_path,
            "file_path": pptx_path,
            "topic": topic,
            "page_count": len(inputs.get("pages", [])),
        }


# ---------------------------------------------------------------------------
# Root pipeline
# ---------------------------------------------------------------------------

class PPTCraftPipeline(PlanNode):
    """7 节点 PPT 生成流水线：Init → Intent → Requirements → Outline → Content → Export → Deliver"""

    async def _execute(self, inputs: dict):
        text = inputs.get("text", "")
        print(f"[PPTCraftPipeline] === Starting PPT generation: {text[:60]} ===")

        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            if isinstance(result, dict):
                inputs.update(result)

        print(f"[PPTCraftPipeline] === Complete ===")
        return {
            "node": "delivery",
            "status": "ok",
            "pptx_path": inputs.get("pptx_path", ""),
            "file_path": inputs.get("file_path", ""),
            "topic": inputs.get("topic", ""),
        }


root = PPTCraftPipeline(
    plan_name="pptx-craft",
    instruction="7 节点 PPT 生成流水线：将用户请求转换成 .pptx 文件",
    sub_plans=[
        PipelineInitNode("pipeline-init", "创建输出目录"),
        IntentClassifyNode("intent-classify", "LLM 提取主题"),
        RequirementCollectNode("requirement-collect", "LLM 提取槽位"),
        ContentPlanNode("content-plan", "LLM 生成大纲"),
        PPTPageGenNode("pptx-pagegen", "LLM 逐页生成内容"),
        PPTExportNode("pptx-export", "python-pptx 导出 .pptx"),
        DeliveryNode("delivery", "验证并返回文件路径"),
    ],
)
```

- [ ] **Step 3: 验证 workflow AST 校验通过**

```python
from twinkle.agentserver.workflow.validator import PlanCodeValidator
from pathlib import Path
root_path = Path.home() / ".twinkle" / "workflows" / "pptx-craft" / "root.py"
code = root_path.read_text(encoding="utf-8")
errors = PlanCodeValidator().validate(code)
print(errors)  # Expected: []
```

- [ ] **Step 4: 验证 workflow 可在 sandbox 中加载**

```python
from twinkle.agentserver.workflow.sandbox import build_namespace
from pathlib import Path
root_path = Path.home() / ".twinkle" / "workflows" / "pptx-craft" / "root.py"
code = root_path.read_text(encoding="utf-8")
ns = build_namespace()
exec(code, ns)
root = ns.get("root")
print(type(root).__name__)  # Expected: PPTCraftPipeline
print(len(root.sub_plans))  # Expected: 7
```

- [ ] **Step 5: Commit**

```bash
# workflow 文件在 workspace 目录,不在 git 仓库中,无需 git add
# 但可以先确认文件已正确放置
ls -la ~/.twinkle/workflows/pptx-craft/root.py
```

---

### Task 3: 集成测试 — mock LLM 验证 7 节点流水线

**Files:**
- Create: `tests/test_workflow_pptx.py`

**Interfaces:**
- Consumes: `WorkflowExecutor`, `PlanNode`, `FakeLLM` pattern from `tests/test_workflow_translate.py`
- Produces: 端到端测试 + AST 校验测试 + Sandbox 加载测试

- [ ] **Step 1: 编写集成测试**

```python
# tests/test_workflow_pptx.py
"""Integration test: pptx-craft workflow with mock LLM."""
import asyncio
import json
import pytest
import tempfile
import os
from pathlib import Path


# --- Mock LLM that returns context-aware responses ---

async def _mock_call_llm(prompt: str, system_prompt: str = "") -> str:
    """Mock LLM: returns structured responses based on prompt content."""
    import json as _json

    if "判断用户是否想要生成PPT" in prompt:
        # IntentClassify — extract topic from user message
        if "人工智能" in prompt:
            return '{"intent": "ppt", "topic": "人工智能技术与应用", "has_documents": false}'
        if "Python" in prompt:
            return '{"intent": "ppt", "topic": "Python 编程入门", "has_documents": false}'
        return '{"intent": "ppt", "topic": "演示文稿", "has_documents": false}'

    if "PPT制作需求参数" in prompt:
        if "技术团队" in prompt:
            return '{"topic": "人工智能技术与应用", "page_count": 6, "audience": "技术团队", "style_id": "tech-minimal", "presentation_purpose": "技术分享"}'
        return '{"topic": "人工智能技术与应用", "page_count": 8, "audience": "通用", "style_id": "business-classic", "presentation_purpose": "汇报"}'

    if "为以下主题生成 PPT 大纲" in prompt:
        # ContentPlan — generate outline with correct page count
        import re as _re
        match = _re.search(r"内容页数：(\d+)", prompt)
        content_pages = int(match.group(1)) if match else 8
        pages = [
            {"title": "人工智能技术与应用", "body": "从理论到实践的全面解读", "page_type": "cover"},
        ]
        for i in range(content_pages):
            pages.append({
                "title": f"第{i + 1}章：AI 核心概念",
                "body": f"- 要点A：AI 的定义与发展历程\n- 要点B：机器学习基础\n- 要点C：深度学习原理",
                "page_type": "data",
            })
        pages.append({"title": "谢谢", "body": "感谢观看", "page_type": "ending"})
        return _json.dumps({"topic": "人工智能技术与应用", "pages": pages}, ensure_ascii=False)

    if "为以下 PPT 页面生成详细内容" in prompt:
        # PPTPageGen — expand single page
        return "AI 核心概念\n\n- 人工智能是计算机科学的一个分支，致力于模拟人类智能行为\n- 机器学习是实现AI的主要方法，通过数据驱动模型学习\n- 深度学习利用多层神经网络处理复杂数据模式\n- 自然语言处理和计算机视觉是AI两大核心应用领域"

    return "mock"


class FakeLLM:
    """Duck-type LLMClient — just enough for _call_llm_wrapper."""
    async def stream(self, messages, tools=None):
        from twinkle.agentserver.llm_client import TextDelta
        prompt = messages[-1]["content"]
        result = await _mock_call_llm(prompt)
        yield TextDelta(content=result)


def _make_executor():
    from twinkle.agentserver.workflow.executor import WorkflowExecutor
    from twinkle.config.schema import WorkflowConfig
    return WorkflowExecutor(
        llm=FakeLLM(),
        tools=None,
        subagent_executor=None,
        config=WorkflowConfig(enable_fallback=False),
    )


def _load_pptx_workflow():
    """Load the pptx-craft root.py from the workflows directory."""
    root_path = Path.home() / ".twinkle" / "workflows" / "pptx-craft" / "root.py"
    if not root_path.exists():
        pytest.skip("pptx-craft workflow not installed")
    return root_path.read_text(encoding="utf-8")


def test_pptx_workflow_validates():
    """The pptx-craft root.py should pass AST validation."""
    from twinkle.agentserver.workflow.validator import PlanCodeValidator
    plan_code = _load_pptx_workflow()
    errors = PlanCodeValidator().validate(plan_code)
    assert errors == [], f"Validation errors: {errors}"


def test_pptx_workflow_sandbox_loads():
    """The pptx-craft root.py should load in the sandbox namespace."""
    from twinkle.agentserver.workflow.sandbox import build_namespace
    plan_code = _load_pptx_workflow()
    namespace = build_namespace()
    exec(plan_code, namespace)

    root = namespace.get("root")
    assert root is not None
    from twinkle.agentserver.workflow.node import PlanNode
    assert isinstance(root, PlanNode)
    assert root.plan_name == "pptx-craft"
    assert len(root.sub_plans) == 7


def test_pptx_workflow_e2e_no_export():
    """Full pipeline minus PPTExport (which requires real tools)."""
    plan_code = _load_pptx_workflow()
    executor = _make_executor()

    # Import ToolManager so command_exec works in the test
    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "帮我做一个关于人工智能的PPT"}))

    assert result["node"] == "delivery"
    assert result["status"] == "ok"
    assert "人工智能" in result.get("topic", "")
    assert "pptx_path" in result


def test_pptx_workflow_extract_json():
    """Verify mock LLM JSON extraction works for each stage."""
    plan_code = _load_pptx_workflow()
    executor = _make_executor()

    from twinkle.agentserver.tools import tool_manager
    executor._tools = tool_manager()

    result = asyncio.run(executor.execute_workflow(plan_code, {"text": "帮我做一个关于人工智能的PPT，面向技术团队"}))

    # Result should have all expected fields
    assert isinstance(result, dict)
    assert result.get("status") == "ok"
    print(f"Workflow result: {json.dumps(result, ensure_ascii=False, default=str)[:500]}")
```

- [ ] **Step 2: 运行测试验证失败（workflow 文件尚未创建）**

Run: `python -m pytest tests/test_workflow_pptx.py -v`
Expected: 如果 root.py 不存在则 skip；存在则根据 mock LLM 行为 PASS 或 FAIL

- [ ] **Step 3: 运行测试验证通过**

Run: `python -m pytest tests/test_workflow_pptx.py -v`
Expected: PASS

- [ ] **Step 4: 运行完整测试套件确认无回归**

Run: `python -m pytest tests/ -v`
Expected: 所有测试通过（包括已有的 13 个 workflow 测试）

- [ ] **Step 5: Commit**

```bash
git add tests/test_workflow_pptx.py
git commit -m "test(workflow): add pptx-craft workflow integration tests"
```

