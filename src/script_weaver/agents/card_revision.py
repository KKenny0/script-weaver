"""One-card revision using the shared tool loop, with no project writes."""
import json

from script_weaver.agents.base import BaseAgent
from script_weaver.core.card_edit import WRITABLE_FIELDS, card_snapshot, validate_candidate_output


class CardRevisionAgent(BaseAgent):
    name = "card_revision"
    output_artifact_type = "card_candidate"
    system_prompt = (
        "只修订指定的一张卡片。保留身份、归属和只读字段。"
        "调用一次 write_artifact，content 为 JSON，恰好包含 kind、target_id、changes。"
        "changes 仅包含需要改变的可编辑字段；枚举使用原始规范值。"
    )

    def __init__(self, kind, target_id, *, llm_client=None):
        super().__init__(llm_client=llm_client, max_iterations=1)
        self.kind, self.target_id = kind, target_id
        self._tool_schemas = [tool for tool in self._tool_schemas if tool["name"] == "write_artifact"]
        self._tool_schemas[0]["parameters"]["properties"]["content"]["description"] = (
            "JSON object with exactly kind, target_id and changes. Writable fields: "
            + ", ".join(WRITABLE_FIELDS[kind])
        )

    async def execute(self, state, user_message=""):
        self._basis = state.model_copy(deep=True)
        return await super().execute(state, user_message)

    def _build_user_prompt(self, state, user_message):
        return json.dumps({"kind": self.kind, "target_id": self.target_id,
                           "target": card_snapshot(state, self.kind, self.target_id),
                           "field_contract": {name: ([member.value for member in spec] if isinstance(spec, type) else spec) for name, spec in WRITABLE_FIELDS[self.kind].items()},
                           "request": user_message}, ensure_ascii=False)

    def _validate_tool_calls(self, calls):
        if len(calls) != 1 or calls[0].name != "write_artifact":
            raise ValueError("定向修改只接受一次 write_artifact，不能提交多个目标。")

    def _validate_artifact(self, data):
        validate_candidate_output(self._basis.model_copy(deep=True), self.kind, self.target_id, data)

    async def _execute_tool(self, tool_name, arguments, *, state_json=None):
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict) or set(arguments) != {"artifact_type", "content"}:
            raise ValueError("候选工具参数越界。")
        if tool_name != "write_artifact" or arguments["artifact_type"] != self.output_artifact_type:
            raise ValueError("候选工具类型错误。")
        data = json.loads(arguments["content"]) if isinstance(arguments["content"], str) else arguments["content"]
        self._validate_artifact(data)
        return json.dumps({"status": "success", "artifact_type": self.output_artifact_type, "data": data})

    def _parse_final_output(self, content, state_json):
        # A plain JSON reply must be the complete payload, never a selected
        # object from prose or a larger artifact.
        return {"status": "success", "data": json.loads(content or "")}
