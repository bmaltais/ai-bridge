import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Inbound: Anthropic Messages API (what Claude Code sends)
# ---------------------------------------------------------------------------


class ContentBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(b.text for b in self.content if b.type == "text")


class MessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[Message]
    max_tokens: int = 4096
    stream: bool = False
    # Claude Code sends system as a list of content blocks; also accept plain string
    system: str | list[ContentBlock] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    metadata: dict[str, Any] | None = None

    def system_text(self) -> str | None:
        if self.system is None:
            return None
        if isinstance(self.system, str):
            return self.system
        return "\n".join(b.text for b in self.system if b.type == "text") or None


# ---------------------------------------------------------------------------
# Outbound: Anthropic Messages API (non-streaming)
# ---------------------------------------------------------------------------


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class MessagesResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock]
    model: str
    stop_reason: Literal["end_turn"] = "end_turn"
    stop_sequence: None = None
    usage: Usage

    @classmethod
    def from_text(cls, text: str, model: str, input_tokens: int) -> "MessagesResponse":
        output_tokens = max(1, len(text) // 4)
        return cls(
            content=[ContentBlock(text=text)],
            model=model,
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        )


# ---------------------------------------------------------------------------
# Outbound: Anthropic streaming SSE events
# ---------------------------------------------------------------------------


class MessageStartMessage(BaseModel):
    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list = []
    model: str
    stop_reason: None = None
    stop_sequence: None = None
    usage: Usage


class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: MessageStartMessage


class ContentBlockStartEvent(BaseModel):
    type: Literal["content_block_start"] = "content_block_start"
    index: int = 0
    content_block: ContentBlock = Field(default_factory=lambda: ContentBlock(text=""))


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ContentBlockDeltaEvent(BaseModel):
    type: Literal["content_block_delta"] = "content_block_delta"
    index: int = 0
    delta: TextDelta


class ContentBlockStopEvent(BaseModel):
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int = 0


class MessageDeltaData(BaseModel):
    stop_reason: Literal["end_turn"] = "end_turn"
    stop_sequence: None = None


class MessageDeltaUsage(BaseModel):
    output_tokens: int


class MessageDeltaEvent(BaseModel):
    type: Literal["message_delta"] = "message_delta"
    delta: MessageDeltaData = Field(default_factory=MessageDeltaData)
    usage: MessageDeltaUsage


class MessageStopEvent(BaseModel):
    type: Literal["message_stop"] = "message_stop"


# ---------------------------------------------------------------------------
# /v1/models response
# ---------------------------------------------------------------------------


class ModelObject(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "use-ai-proxy"


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelObject]
