"""Base Agent with tool-use loop — the heart of the agent-native architecture.

Each agent is NOT a simple prompt→response function. Instead:
1. Agent receives a task + system prompt + available tools
2. Agent calls LLM → may get text OR tool_calls
3. If tool_calls: execute them, append results, call LLM again (loop)
4. If text: agent has finished reasoning → return result
5. Self-correction: if output fails schema validation, feed error back to LLM

This loop pattern is what makes the system "agent-native" rather than a chain.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import TypeAdapter

from script_weaver.core.config import get_settings
from script_weaver.core.types import (
    DecisionRecord,
    ProjectState,
    UserAction,
)
from script_weaver.llm.client import LLMClient
from script_weaver.llm.providers import ChatResponse, ToolCall
from script_weaver.tools.definitions import (
    BUILTIN_TOOLS,
    execute_tool_call,
    get_builtin_tool_schemas,
)

logger = logging.getLogger(__name__)


class BaseAgent:
    """Base class for all pipeline agents.

    Subclasses define:
    - `name`: Agent identifier
    - `stage`: Which pipeline stage this agent operates in
    - `system_prompt`: The system prompt template
    - `output_artifact_type`: What artifact this agent produces
    """

    name: str = "base_agent"
    stage: str = ""                                   # PipelineStage value
    system_prompt: str = ""
    output_artifact_type: str = ""                     # e.g., "outline", "script"

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        extra_tools: list[dict[str, Any]] | None = None,
        max_iterations: int | None = None,
        max_retries: int | None = None,
    ):
        settings = get_settings()
        self._llm = llm_client or LLMClient()
        self._max_iterations = max_iterations or settings.agent_max_tool_iterations
        self._max_retries = max_retries or settings.agent_max_self_correction_retries

        # Combine built-in tools with any extra tools
        self._tool_schemas = get_builtin_tool_schemas() + (extra_tools or [])
        for tool in self._tool_schemas:
            if tool["name"] in ("read_state", "read_artifact", "write_artifact"):
                parameters = tool["parameters"]
                parameters["properties"].pop("project_state_json", None)
                parameters["required"] = [
                    name for name in parameters["required"] if name != "project_state_json"
                ]
            if tool["name"] == "write_artifact" and self.output_artifact_type:
                properties = tool["parameters"]["properties"]
                properties["artifact_type"]["enum"] = [self.output_artifact_type]
                field = ProjectState.model_fields.get(self.output_artifact_type)
                # refined_idea has a richer generation shape than its stored string.
                if field is not None and self.output_artifact_type != "refined_idea":
                    schema = TypeAdapter(field.annotation).json_schema()
                    properties["content"]["description"] = (
                        "JSON string matching this schema; generated content must not be empty:\n"
                        + json.dumps(schema, ensure_ascii=False)
                    )

    async def execute(self, state: ProjectState, user_message: str = "") -> Any:
        """Main entry point: run the agent loop to produce an artifact.

        Args:
            state: Current project state (read-only context for the agent)
            user_message: Optional additional instruction from user

        Returns:
            The produced artifact (type depends on the agent)
        """
        logger.info(f"[{self.name}] Starting execution. Stage: {self.stage}")

        state_json = state.model_dump_json(ensure_ascii=False)

        # Build initial messages
        messages = [
            {"role": "system", "content": self._build_system_prompt(state)},
            {"role": "user", "content": self._build_constrained_user_prompt(state, user_message)},
        ]

        # Run the agent loop
        result = await self._agent_loop(messages, state_json)

        logger.info(f"[{self.name}] Execution complete.")
        return result

    def _build_system_prompt(self, state: ProjectState) -> str:
        """Build the full system prompt. Override in subclasses for skill injection."""
        return self.system_prompt

    def _build_constrained_user_prompt(self, state: ProjectState, user_message: str) -> str:
        prompt = self._build_user_prompt(state, user_message)
        if not state.user_input:
            return prompt
        return (
            f"=== 原始创作要求 ===\n{state.user_input}\n"
            "原始创作要求优先于下游扩写资料、示例和默认创作模板；"
            "保留明确指定的总时长、人物数量、地点数量、文字及其呈现载体。"
            "总时长不是故事中的倒计时；不要因节拍或灯光状态变化新增地点。"
            "若已有资料与原始要求冲突，应收敛到原始要求。\n\n"
            + prompt
        )

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        """Build the initial user message. Override in subclasses."""
        if user_message:
            return user_message
        return f"Please complete your task for this project. Project title: {state.meta.title or 'Untitled'}"

    async def _agent_loop(
        self,
        messages: list[dict[str, Any]],
        state_json: str,
    ) -> dict[str, Any]:
        """Core agent loop: LLM call → tool execution → repeat until done."""
        iteration = 0
        last_content = ""
        last_error = "No artifact was submitted"

        while iteration < self._max_iterations:
            iteration += 1
            logger.debug(f"[{self.name}] Agent loop iteration {iteration}/{self._max_iterations}")

            try:
                response: ChatResponse = await self._llm.chat(
                    messages=messages,
                    tools=self._tool_schemas if self._tool_schemas else None,
                )
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                logger.error(f"[{self.name}] LLM call failed: {detail}")
                return {"error": f"LLM call failed: {detail}", "raw_response": last_content,
                        "last_error": last_error}

            logger.debug(
                "[%s] iteration=%s stop_reason=%s tools=%s",
                self.name, iteration, response.stop_reason,
                [call.name for call in response.tool_calls],
            )
            if response.stop_reason == "max_tokens":
                last_error = (
                    "max_tokens: output was truncated "
                    f"(completion_tokens={response.usage.completion_tokens}); "
                    "reduce the generation scope or explicitly increase SCRIPTWEAVER_LLM_MAX_TOKENS"
                )
                return {"error": "output_token_limit", "last_error": last_error}

            # Build assistant message from response
            assistant_msg: dict[str, Any] = {}
            if response.content:
                last_content = response.content
            if response.tool_calls:
                # Providers adapt this shared OpenAI-format history as needed.
                assistant_msg["content"] = response.content
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (
                                json.dumps(tc.arguments, ensure_ascii=False)
                                if isinstance(tc.arguments, dict)
                                else str(tc.arguments)
                            ),
                        },
                    }
                    for tc in response.tool_calls
                ]
            else:
                assistant_msg["content"] = response.content or "(no response)"
            messages.append({"role": "assistant", **assistant_msg})

            # Branch: tool calls vs. final response
            if response.tool_calls:
                # Execute each tool call and append results as tool-role messages
                for tool_call in response.tool_calls:
                    logger.debug(
                        f"[{self.name}] Tool call: {tool_call.name}({tool_call.arguments})"
                    )
                    result = await self._execute_tool(
                        tool_name=tool_call.name, arguments=tool_call.arguments,
                        state_json=state_json,
                    )
                    if tool_call.name == "write_artifact":
                        try:
                            written = json.loads(result)
                            if written.get("status") == "success":
                                if written["artifact_type"] != self.output_artifact_type:
                                    raise ValueError(
                                        f"Expected artifact_type {self.output_artifact_type}"
                                    )
                                self._validate_artifact(written["data"])
                                return {"status": "success", "data": written["data"], "raw": result}
                        except json.JSONDecodeError:
                            pass  # Keep the tool's plain-text validation error for the model.
                        except (ValueError, TypeError, KeyError) as e:
                            result = json.dumps({"error": str(e)})
                        last_error = result
                        logger.debug("[%s] Artifact rejected: %s", self.name, last_error)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
                # Loop continues — LLM will see tool results and decide next action
            else:
                # No tool calls — agent has finished its reasoning
                # Try to extract structured output from the content
                try:
                    parsed = self._parse_final_output(response.content, state_json)
                    if parsed.get("status") != "success":
                        messages.append({
                            "role": "user",
                            "content": "Return a JSON artifact or call write_artifact. "
                                       f"Last failure: {last_error}",
                        })
                        continue
                    self._validate_artifact(parsed["data"])
                    return parsed
                except (ValueError, TypeError) as e:
                    last_error = str(e)
                    logger.debug("[%s] Artifact rejected: %s", self.name, last_error)
                    messages.append({
                        "role": "user", "content": f"Invalid artifact: {e}. Please correct it.",
                    })

        # Exceeded max iterations — force return whatever we have
        logger.warning(f"[{self.name}] Max iterations ({self._max_iterations}) reached.")
        return {"error": "max_iterations_exceeded", "raw_response": last_content,
                "last_error": last_error}

    def _validate_artifact(self, data: Any) -> None:
        """Validate generated output without weakening the saved project schema."""
        kind = self.output_artifact_type
        if kind == "refined_idea":
            if not isinstance(data, dict):
                raise ValueError("refined_idea must be an object")
            for key in ("logline", "title", "core_theme"):
                if key in data and not isinstance(data[key], str):
                    raise ValueError(f"{key} must be text")
            if not (data.get("logline", "").strip() or data.get("core_theme", "").strip()):
                raise ValueError("refined_idea needs a logline or core_theme")
            return
        if kind in ("characters", "scenes") and isinstance(data, dict):
            data = [data]
        if kind not in ProjectState.model_fields:
            raise ValueError(f"Unsupported artifact type: {kind}")
        artifact = getattr(ProjectState.model_validate({kind: data}), kind)
        if artifact is None:
            raise ValueError(f"{kind} must not be null")
        required_content = {
            "outline": "plot_outline", "script": "scenes",
            "storyboard": "shots", "art_style": "overall_style",
        }
        content = getattr(artifact, required_content[kind]) if kind in required_content else artifact
        if not content:
            raise ValueError(f"{kind} must not be empty")

    async def _execute_tool(
        self, tool_name: str, arguments: dict[str, Any] | str,
        *, state_json: str | None = None,
    ) -> str:
        """Execute a single tool call with error handling."""
        # Arguments might be a JSON string (Anthropic format) or already a dict
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw_input": arguments}

        if (state_json is not None and isinstance(arguments, dict)
                and tool_name in ("read_state", "read_artifact", "write_artifact")):
            arguments = {**arguments, "project_state_json": state_json}

        try:
            return await execute_tool_call(tool_name, arguments)
        except Exception as e:
            logger.error(f"[{self.name}] Tool '{tool_name}' error: {e}")
            return json.dumps({"error": str(e)})

    def _parse_final_output(
        self,
        content: str | None,
        state_json: str,
    ) -> dict[str, Any]:
        """Parse the agent's final text response into structured output.

        Agents should ideally use write_artifact tool to produce output.
        This method handles cases where they return JSON directly in text.
        """
        if not content:
            return {"error": "empty_response", "raw": ""}

        # Try to extract JSON from the response
        # Handle markdown code blocks
        json_str = content.strip()
        if "```json" in json_str:
            start = json_str.index("```json") + 7
            end = json_str.index("```", start)
            json_str = json_str[start:end].strip()
        elif "```" in json_str:
            start = json_str.index("```") + 3
            end = json_str.index("```", start)
            # Skip language identifier line
            newline_pos = json_str.find("\n", start)
            if newline_pos != -1 and newline_pos < end:
                start = newline_pos + 1
            json_str = json_str[start:end].strip()

        try:
            data = json.loads(json_str)
            return {"status": "success", "data": data, "raw": content}
        except json.JSONDecodeError:
            # Not pure JSON — return as-is with raw text
            return {
                "status": "text_response",
                "data": {"text": content},
                "raw": content,
            }

    def record_decision(
        self,
        state: ProjectState,
        context: str,
        action: UserAction,
        modification: str | None = None,
        reasoning: str | None = None,
    ) -> None:
        """Record a decision to project memory (for Grows With User)."""
        decision = DecisionRecord(
            stage=self.stage,
            context=context,
            action=action,
            modification=modification,
            reasoning=reasoning,
        )
        state.memory.record_decision(decision)
        state.touch()


class SimpleAgent(BaseAgent):
    """Simplified agent that doesn't use tool loop — just one-shot LLM call.

    Useful for agents that produce straightforward output without needing
    to read/write artifacts iteratively (e.g., IdeaRefiner).
    """

    async def execute(self, state: ProjectState, user_message: str = "") -> Any:
        """One-shot LLM call without tool-use loop."""
        logger.info(f"[{self.name}] Starting simple execution.")

        messages = [
            {"role": "system", "content": self._build_system_prompt(state)},
            {"role": "user", "content": self._build_constrained_user_prompt(state, user_message)},
        ]

        try:
            response = await self._llm.chat(messages)
            parsed = self._parse_final_output(response.content, state.model_dump_json())
            return parsed
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            logger.error(f"[{self.name}] Execution error: {detail}")
            return {"error": detail}
