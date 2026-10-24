"""Provider-neutral chat types. Messages are plain dicts in OpenAI chat format."""

from pydantic import BaseModel, Field


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it


class AssistantMessage(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    def to_dict(self) -> dict:
        """The message as it goes back into the conversation history."""
        msg: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return msg


class ChatResult(BaseModel):
    message: AssistantMessage
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON schema of the arguments object

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def cost_usd(usage: Usage, price_in: float, price_out: float) -> float:
    """Cost of one call; prices are USD per million tokens."""
    return (usage.prompt_tokens * price_in + usage.completion_tokens * price_out) / 1e6
