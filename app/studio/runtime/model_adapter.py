"""LangChain model adapter for Studio's OpenAI-compatible provider gateway."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    convert_to_openai_messages,
    message_chunk_to_message,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from app.studio.runtime.llm import ModelStreamEvent, stream_model_turn
from app.studio.models import BusinessRecord


ModelTurn = Callable[
    [BusinessRecord, list[dict[str, Any]], str | None, list[dict[str, Any]] | None],
    Iterator[ModelStreamEvent],
]


class StudioChatModel(BaseChatModel):
    """Expose Studio's provider-compatible stream as a LangChain chat model."""

    _record: BusinessRecord = PrivateAttr()
    _requested_model: str | None = PrivateAttr(default=None)
    _model_turn: ModelTurn = PrivateAttr()

    def __init__(
        self,
        *,
        record: BusinessRecord,
        requested_model: str | None = None,
        model_turn: ModelTurn = stream_model_turn,
    ) -> None:
        super().__init__()
        self._record = record
        self._requested_model = requested_model
        self._model_turn = model_turn

    @property
    def _llm_type(self) -> str:
        return "studio-openai-compatible"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"requested_model": self._requested_model or "active"}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=formatted, **kwargs)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del stop, run_manager
        converted = convert_to_openai_messages(
            messages,
            text_format="string",
            include_id=True,
            pass_through_unknown_blocks=True,
        )
        openai_messages = converted if isinstance(converted, list) else [converted]
        tools = kwargs.get("tools")
        if not isinstance(tools, list):
            tools = None

        final_calls = []
        streamed_tool_calls = False
        for event in self._model_turn(
            self._record,
            openai_messages,
            self._requested_model,
            tools,
        ):
            if event.kind == "reasoning" and event.content:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        additional_kwargs={"reasoning_content": event.content},
                    )
                )
            elif event.kind == "content" and event.content:
                yield ChatGenerationChunk(message=AIMessageChunk(content=event.content))
            elif event.kind == "tool_call_delta" and event.tool_call_chunks:
                streamed_tool_calls = True
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": chunk.name,
                                "args": chunk.arguments,
                                "id": chunk.id,
                                "index": chunk.index,
                            }
                            for chunk in event.tool_call_chunks
                        ],
                    )
                )
            elif event.kind == "completed":
                final_calls = event.tool_calls

        tool_chunks = []
        if not streamed_tool_calls:
            tool_chunks = [
                {
                    "name": call.name,
                    "args": json.dumps(call.arguments, ensure_ascii=False),
                    "id": call.id,
                    "index": index,
                }
                for index, call in enumerate(final_calls)
            ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=tool_chunks,
                chunk_position="last",
            )
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        combined: AIMessageChunk | None = None
        for generation in self._stream(messages, stop, run_manager, **kwargs):
            chunk = generation.message
            combined = chunk if combined is None else combined + chunk
        message = (
            message_chunk_to_message(combined)
            if combined is not None
            else AIMessage(content="")
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
