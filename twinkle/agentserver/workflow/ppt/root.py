"""PPT Generation Pipeline — 7 节点流水线，基于 LLM 内容生成 + python-pptx 导出。

所有参数（主题、页数、受众、风格）均由 workflow 内部 LLM 从用户原文中提取。
Agent 只需传入用户原话即可。

节点：
  1. PipelineInit        — 创建输出目录
  2. IntentClassify      — LLM 提取主题
  3. RequirementCollect  — LLM 提取槽位（页数/受众/风格/目的）
  4. ContentPlan         — LLM 生成大纲
  5. PPTPageGen          — LLM 逐页生成内容
  6. PPTExport           — python-pptx 导出 .pptx
  7. Delivery            — 验证并返回文件路径

使用方式：
  execute_workflow(workflow_name="ppt", inputs='{"text": "帮我做一个关于人工智能的PPT"}')
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_user_text(inputs: dict) -> str:
    """从 inputs 中收集用户原文，支持多种 key 名。

    优先识别自然语言 key（text/task/user_request/user_message/query）。
    若 agent 直接传入结构化 spec（topic/audience/page_count/outline 等）而无原文，
    则用 topic 合成一段文本，避免下游 LLM 提取节点收到空 prompt。
    """
    for key in ("text", "task", "user_request", "user_message", "query"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Spec-mode fallback: synthesize from structured fields so LLM 节点有上下文
    topic = inputs.get("topic", "")
    if isinstance(topic, str) and topic.strip():
        return topic.strip()
    return ""

def _split_title_body(entry: str) -> tuple[str, str]:
    """在第一个冒号（全角：或半角:）处拆分大纲条目为 (title, body)。"""
    for sep in ("：", ":"):
        idx = entry.find(sep)
        if idx >= 0:
            return entry[:idx].strip(), entry[idx + len(sep):].strip()
    return entry.strip(), ""

def _outline_to_pages(outline: list, topic: str) -> list[dict]:
    """将 agent 提供的大纲（str|dict 列表）转换为页面 dict 列表。

    首页→cover，末页→ending，中间→data。每页保证有 title/body/page_type。
    """
    raw = [e for e in outline if isinstance(e, (str, dict))]
    n = len(raw)
    pages: list[dict] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, dict):
            title = str(entry.get("title", "")).strip()
            body = str(entry.get("body", "")).strip()
            page_type = str(entry.get("page_type", "")).strip()
        else:
            title, body = _split_title_body(str(entry))
            page_type = ""
        if i == 0:
            page_type = page_type or "cover"
            title = title or topic or "演示文稿"
        elif i == n - 1:
            page_type = page_type or "ending"
            title = title or "谢谢"
            body = body or "感谢观看"
        else:
            page_type = page_type or "data"
            body = body or title
        pages.append({"title": title, "body": body, "page_type": page_type})
    return pages

# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

class PipelineInitNode(PlanNode):
    """创建输出目录，产出 output_dir / pages_dir。"""

    async def _execute(self, inputs: dict):
        print(f"[PipelineInit] creating output directory...")

        result = await self.call_tool(
            "command_exec",
            command="python -c \"import time; print(time.strftime('%Y%m%d-%H%M%S'))\"",
        )
        ts = result.get("stdout", "").strip() if isinstance(result, dict) else "default"
        if not ts:
            ts = "default"

        output_dir = "output/ppt-" + ts
        pages_dir = output_dir + "/pages"

        await self.call_tool(
            "command_exec",
            command=(
                "python -c \"import pathlib; "
                "pathlib.Path('" + output_dir + "').mkdir(parents=True, exist_ok=True); "
                "pathlib.Path('" + pages_dir + "').mkdir(parents=True, exist_ok=True)\""
            ),
        )

        print(f"[PipelineInit] output_dir={output_dir}")
        return {"output_dir": output_dir, "pages_dir": pages_dir}


class IntentClassifyNode(PlanNode):
    """LLM 从用户消息中提取演示主题。"""

    async def _execute(self, inputs: dict):
        text = _collect_user_text(inputs)
        # Spec-mode: agent 已提供 topic，直接采用，跳过 LLM 提取
        existing_topic = inputs.get("topic", "")
        if isinstance(existing_topic, str) and existing_topic.strip():
            topic = existing_topic.strip()
            print(f"[IntentClassify] using provided topic: {topic[:60]}")
            return {"topic": topic, "has_documents": False}

        print(f"[IntentClassify] extracting topic from: {text[:60]}...")

        prompt = (
            "分析以下用户消息，提取PPT演示主题。\n"
            "\n"
            "用户消息：\"" + text + "\"\n"
            "\n"
            "返回 JSON 格式（只返回 JSON，不要其他内容）：\n"
            '{"intent": "ppt" 或 "other", "topic": "提取的主题", "has_documents": false}'
        )
        result = await self.call_llm(prompt)
        data = self.extract_json(result)

        topic = data.get("topic", "") or "Presentation"
        print(f"[IntentClassify] topic={topic}")
        return {"topic": topic, "has_documents": data.get("has_documents", False)}


class RequirementCollectNode(PlanNode):
    """LLM 从用户消息中提取槽位：page_count / audience / style_id / presentation_purpose。"""

    async def _execute(self, inputs: dict):
        text = _collect_user_text(inputs)
        topic = inputs.get("topic", "")

        # Spec-mode: agent 已提供槽位，直接采用，跳过 LLM
        provided_page_count = inputs.get("page_count")
        provided_audience = inputs.get("audience")
        provided_style = inputs.get("style_id") or inputs.get("style")
        provided_purpose = inputs.get("presentation_purpose")
        if any(v for v in (provided_page_count, provided_audience, provided_style, provided_purpose)):
            try:
                page_count = int(provided_page_count) if provided_page_count is not None else 8
            except (TypeError, ValueError):
                page_count = 8
            page_count = max(3, min(page_count, 20))
            style_id = provided_style or "business-classic"
            print(f"[RequirementCollect] using provided slots: page_count={page_count}, audience={provided_audience}")
            return {
                "topic": topic,
                "page_count": page_count,
                "audience": provided_audience or "通用",
                "style_id": style_id,
                "presentation_purpose": provided_purpose or "汇报",
            }

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

        try:
            page_count = int(data.get("page_count", 8))
        except (TypeError, ValueError):
            page_count = 8
        page_count = max(3, min(page_count, 20))
        print(f"[RequirementCollect] page_count={page_count}, audience={data.get('audience')}")
        return {
            "topic": data.get("topic", topic) or topic,
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
        total_pages = page_count + 2

        # Spec-mode: agent 已提供大纲，直接转换为 pages_plan，跳过 LLM
        agent_outline = inputs.get("outline")
        if isinstance(agent_outline, list) and agent_outline:
            pages = _outline_to_pages(agent_outline, topic)
            print(f"[ContentPlan] using provided outline: {len(pages)} pages")
            return {
                "outline": {"topic": topic, "pages": pages},
                "pages_plan": pages,
                "topic": topic,
            }

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
                    first_type = pages[0].get("page_type", "")
                    last_type = pages[-1].get("page_type", "")
                    if first_type == "cover" and last_type == "ending":
                        print(f"[ContentPlan] outline OK: {len(pages)} pages (attempt {attempt + 1})")
                        resolved_topic = data.get("topic", topic) or topic
                        return {"outline": data, "pages_plan": pages, "topic": resolved_topic}
                print(f"[ContentPlan] validation failed (attempt {attempt + 1}): "
                      f"expected {total_pages} pages, got {len(pages)}")
            except Exception as e:
                print(f"[ContentPlan] parse failed (attempt {attempt + 1}): {e}")

        # Fallback
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
        return {
            "outline": {"topic": topic, "pages": fallback_pages},
            "pages_plan": fallback_pages,
            "topic": topic,
        }


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
                generated_pages.append(plan)
                print(f"[PPTPageGen] page {i + 1}: {page_type} — {plan_title}")
                continue

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
        topic = inputs.get("topic", "演示文稿") or "演示文稿"
        output_dir = inputs.get("output_dir", "output/ppt-default")
        pages = inputs.get("pages", [])

        # Sanitize topic for filename — never produce an empty/hidden basename
        safe_topic = "".join(c for c in topic if c.isalnum() or c in ("_", "-", " ", "."))[:50].strip()
        if not safe_topic:
            safe_topic = "presentation"
        pptx_path = output_dir + "/" + safe_topic + ".pptx"

        print(f"[PPTExport] generating {pptx_path} with {len(pages)} pages...")

        # Build JSON payload manually (sandbox has no json module)
        def _esc(s):
            return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        page_parts = []
        for p in pages:
            t = _esc(p.get("title", ""))
            b = _esc(p.get("body", ""))
            pt = _esc(p.get("page_type", "data"))
            page_parts.append(
                '{"title": "' + t + '", "body": "' + b + '", "page_type": "' + pt + '"}'
            )
        payload = (
            '{"output_path": "' + _esc(pptx_path) + '", '
            '"topic": "' + _esc(topic) + '", '
            '"pages": [' + ", ".join(page_parts) + ']}'
        )

        payload_path = output_dir + "/_payload.json"
        await self.call_tool("write_file", file_path=payload_path, content=payload)

        await self.call_tool(
            "command_exec",
            command=(
                "python -c \""
                "import json; "
                "from twinkle.agentserver.workflow.ppt_export import generate_pptx; "
                "data = json.load(open('" + payload_path + "', encoding='utf-8')); "
                "path = generate_pptx(data['output_path'], data['topic'], data['pages']); "
                "print('PPTX saved to ' + path)"
                "\""
            ),
        )

        print(f"[PPTExport] done: {pptx_path}")
        return {"node": "export", "status": "ok", "pptx_path": pptx_path}


class DeliveryNode(PlanNode):
    """验证 PPTX 文件存在并返回路径。"""

    async def _execute(self, inputs: dict):
        pptx_path = inputs.get("pptx_path", "")
        topic = inputs.get("topic", "")
        page_count = len(inputs.get("pages", []))
        print(f"[Delivery] verifying: {pptx_path}")

        try:
            read_result = await self.call_tool("read_file", file_path=pptx_path)
            if "not found" in str(read_result).lower():
                print(f"[Delivery] file not found: {pptx_path}")
                return {"node": "delivery", "status": "error", "error": "PPTX file not found"}
        except Exception:
            pass  # binary file — expected

        print(f"[Delivery] done: {pptx_path}")
        return {
            "node": "delivery",
            "status": "ok",
            "pptx_path": pptx_path,
            "file_path": pptx_path,
            "topic": topic,
            "page_count": page_count,
        }


# ---------------------------------------------------------------------------
# Root pipeline
# ---------------------------------------------------------------------------

class PPTCraftPipeline(PlanNode):
    """7 节点 PPT 生成流水线：从用户文本中提取参数 → 生成大纲 → 生成内容 → 导出 PPTX。"""

    async def _execute(self, inputs: dict):
        text = _collect_user_text(inputs)
        print(f"[PPTCraftPipeline] === Starting PPT generation ===")
        print(f"[PPTCraftPipeline] text={text[:80] if text else '(empty)'}")

        for sub in self.sub_plans:
            result = await self.execute_subplan(sub, inputs)
            if isinstance(result, dict):
                inputs.update(result)

        page_count = len(inputs.get("pages", []))
        print(f"[PPTCraftPipeline] === Complete: {page_count} pages ===")
        return {
            "node": "delivery",
            "status": "ok",
            "pptx_path": inputs.get("pptx_path", ""),
            "file_path": inputs.get("file_path", ""),
            "topic": inputs.get("topic", ""),
            "page_count": page_count,
        }


root = PPTCraftPipeline(
    plan_name="ppt",
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
