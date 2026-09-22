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
            {"role": "user", "content": self._build_user_prompt(state, user_message)},
        ]

        # Run the agent loop
        result = await self._agent_loop(messages, state_json)

        logger.info(f"[{self.name}] Execution complete.")
        return result

    def _build_system_prompt(self, state: ProjectState) -> str:
        """Build the full system prompt. Override in subclasses for skill injection."""
        return self.system_prompt

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        """Build the initial user message. Override in subclasses."""
        if user_message:
            return user_message
        return f"Please complete your task for this project. Project title: {state.meta.title or 'Untitled'}"

    async def _agent_loop(
        self,
        messages: list[dict[str, str]],
        state_json: str,
    ) -> dict[str, Any]:
        """Core agent loop: LLM call → tool execution → repeat until done."""
        iteration = 0
        last_content = ""
        last_artifact: dict[str, Any] | list[Any] | None = None

        while iteration < self._max_iterations:
            iteration += 1
            logger.debug(f"[{self.name}] Agent loop iteration {iteration}/{self._max_iterations}")

            try:
                response: ChatResponse = await self._llm.chat(
                    messages=messages,
                    tools=self._tool_schemas if self._tool_schemas else None,
                )
            except Exception as e:
                logger.error(f"[{self.name}] LLM call failed: {e}")
                return {"error": f"LLM call failed: {e}", "raw_response": last_content}

            # Build assistant message from response
            assistant_msg: dict[str, Any] = {}
            if response.content:
                last_content = response.content
            if response.tool_calls:
                # OpenAI tool-call format: content must be null, and each call
                # needs the type/function wrapper with arguments as a JSON string.
                assistant_msg["content"] = None
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
                    # Capture write_artifact payload so it can be integrated into state
                    if tool_call.name == "write_artifact":
                        args = tool_call.arguments
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        if isinstance(args, dict):
                            content = args.get("content")
                            parsed: dict[str, Any] | list[Any] | None = None
                            if isinstance(content, str):
                                try:
                                    parsed = json.loads(content)
                                except json.JSONDecodeError:
                                    parsed = None
                            elif isinstance(content, (dict, list)):
                                parsed = content
                            if parsed is not None:
                                # Artifact written — return immediately. The LLM
                                # often keeps calling read_state/request_review
                                # after writing and never emits a final text, which
                                # previously made the loop spin to max iterations.
                                logger.info(
                                    f"[{self.name}] Artifact captured via write_artifact "
                                    f"on iteration {iteration}."
                                )
                                return {
                                    "status": "success",
                                    "data": parsed,
                                    "raw": (
                                        content if isinstance(content, str)
                                        else json.dumps(content, ensure_ascii=False)
                                    ),
                                }
                    result = await self._execute_tool(tool_name=tool_call.name, arguments=tool_call.arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })
                # Loop continues — LLM will see tool results and decide next action
            else:
                # No tool calls — agent has finished its reasoning
                # Try to extract structured output from the content
                parsed = self._parse_final_output(response.content, state_json)
                if last_artifact is not None and parsed.get("status") != "success":
                    parsed = {
                        "status": "success",
                        "data": last_artifact,
                        "raw": response.content,
                    }
                return parsed

        # Exceeded max iterations — force return whatever we have
        logger.warning(f"[{self.name}] Max iterations ({self._max_iterations}) reached.")
        if last_artifact is not None:
            return {"status": "success", "data": last_artifact, "raw": last_content}
        return {"error": "max_iterations_exceeded", "raw_response": last_content}

    async def _execute_tool(self, tool_name: str, arguments: dict[str, Any] | str) -> str:
        """Execute a single tool call with error handling."""
        # Arguments might be a JSON string (Anthropic format) or already a dict
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw_input": arguments}

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
            {"role": "user", "content": self._build_user_prompt(state, user_message)},
        ]

        try:
            response = await self._llm.chat(messages)
            parsed = self._parse_final_output(response.content, state.model_dump_json())
            return parsed
        except Exception as e:
            logger.error(f"[{self.name}] Execution error: {e}")
            return {"error": str(e)}
