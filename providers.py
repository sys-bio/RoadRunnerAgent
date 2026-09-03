"""Model providers, behind one small interface.

The agent loop in `agent.py` does not care which vendor answers, only that a
turn yields text, tool calls, a stop reason and token counts. Two providers
implement that:

  * `AnthropicProvider` - the Messages API, with adaptive thinking, effort,
    prompt caching and strict tool schemas.
  * `OpenAICompatibleProvider` - anything speaking the OpenAI chat-completions
    dialect, which is how DeepSeek is reached.

The differences that matter are documented on each class, because they change
what the comparison between the two actually means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class TurnResult:
    texts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    refusal_details: Any = None


class AnthropicProvider:
    """Claude via the Messages API."""

    name = "anthropic"

    def __init__(self, model: str, effort: str, system: str,
                 tools: list[dict], max_tokens: int, client=None) -> None:
        import anthropic
        import agent  # for make_client / request_shape, kept in one place

        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or agent.make_client()
        self.request_shape = agent.request_shape(model, effort)
        # One cache breakpoint on the system prompt: it carries the Antimony
        # reference and is byte-identical between handoffs.
        self.system = [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}]
        self.tools = tools
        self.messages: list[dict[str, Any]] = []
        self._anthropic = anthropic

    def start(self, user_text: str) -> None:
        self.messages = [{"role": "user", "content": user_text}]

    def resume(self, messages: list, follow_up: str) -> None:
        """Continue a finished conversation with a new question."""
        self.messages = list(messages)
        self.append_user(follow_up)

    def turn(self, tools: list[dict] | None = None) -> TurnResult:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            tools=self.tools if tools is None else tools,
            messages=self.messages,
            **self.request_shape,
        ) as stream:
            response = stream.get_final_message()

        usage = response.usage
        result = TurnResult(
            stop_reason=response.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(
                usage, "cache_creation_input_tokens", 0) or 0,
        )
        if response.stop_reason == "refusal":
            result.refusal_details = getattr(response, "stop_details", None)
            return result

        # Thinking blocks must be echoed back unchanged on the next turn.
        self.messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "text" and block.text.strip():
                result.texts.append(block.text)
            elif block.type == "tool_use":
                result.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=block.input))
        return result

    def append_user(self, text: str) -> None:
        """Add a user turn, merging if the conversation already ends with one.

        A finished run ends with the user message carrying the tool results,
        so a follow-up appended as a new message would put two user messages
        back to back - malformed for this API.
        """
        if self.messages and self.messages[-1]["role"] == "user"                 and isinstance(self.messages[-1]["content"], list):
            self.messages[-1]["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": text})

    def append_results(self, results: list[dict], extra_text: str = "") -> None:
        content = [{"type": "tool_result", "tool_use_id": r["id"],
                    "content": r["content"], "is_error": r["is_error"]}
                   for r in results]
        if extra_text:
            # Same user message as the tool results: two consecutive user
            # messages would be a malformed conversation.
            content.append({"type": "text", "text": extra_text})
        self.messages.append({"role": "user", "content": content})


class OpenAICompatibleProvider:
    """Any provider speaking the OpenAI chat-completions dialect.

    Used for DeepSeek. Three differences from the Anthropic path change what a
    like-for-like comparison means, and are reported rather than hidden:

      * **No prompt caching breakpoint.** DeepSeek caches automatically and
        reports `prompt_cache_hit_tokens`, but there is no `cache_control` to
        place, so the system prompt is not deliberately pinned.
      * **Effort has a different name and a shorter ladder.** It is sent as
        `reasoning_effort`, whose documented values are none/low/high/max -
        no "medium", no "xhigh". `map_effort` translates and says so.
      * **Tool arguments arrive as a JSON string** and are parsed here. A
        model that emits malformed JSON produces a tool call this layer
        rejects, which the loop reports back as a tool error rather than
        crashing.
    """

    name = "openai-compatible"

    def __init__(self, model: str, effort: str, system: str,
                 tools: list[dict], max_tokens: int, client=None,
                 base_url: str = "", api_key_env: str = "") -> None:
        import os

        from openai import OpenAI

        self.model = model
        self.effort = effort
        self.reasoning_effort, self.effort_note = map_effort(model, effort)
        self.max_tokens = max_tokens
        self.system = system
        self.messages: list[dict[str, Any]] = []
        self.tools = [self._translate_tool(t) for t in tools]
        if client is not None:
            self.client = client
        else:
            key = os.environ.get(api_key_env, "").strip()
            if not key:
                raise RuntimeError(
                    f"{api_key_env} is not set in this process. If you used "
                    "setx, open a new terminal - setx only affects processes "
                    "started afterwards.")
            self.client = OpenAI(api_key=key, base_url=base_url)

    @staticmethod
    def _translate_tool(tool: dict) -> dict:
        """Anthropic tool definition -> OpenAI function definition."""
        function = {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        }
        if tool.get("strict"):
            function["strict"] = True
        return {"type": "function", "function": function}

    def start(self, user_text: str) -> None:
        self.messages = [{"role": "system", "content": self.system},
                         {"role": "user", "content": user_text}]

    def resume(self, messages: list, follow_up: str) -> None:
        """Continue a finished conversation with a new question."""
        self.messages = list(messages)
        self.append_user(follow_up)

    def turn(self, tools: list[dict] | None = None) -> TurnResult:
        extra = {}
        if self.reasoning_effort:
            extra["reasoning_effort"] = self.reasoning_effort
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self.messages,
            tools=(self.tools if tools is None
                   else [self._translate_tool(t) for t in tools]),
            **({"extra_body": extra} if extra else {}),
        )
        choice = response.choices[0]
        message = choice.message
        usage = response.usage

        # DeepSeek reports cache hits/misses; both are part of prompt_tokens,
        # unlike the Anthropic API where the three counters are disjoint.
        hit = getattr(usage, "prompt_cache_hit_tokens", None) or 0
        total_prompt = getattr(usage, "prompt_tokens", 0) or 0
        result = TurnResult(
            stop_reason={"tool_calls": "tool_use", "stop": "end_turn",
                         "length": "max_tokens"}.get(
                             choice.finish_reason, choice.finish_reason),
            input_tokens=max(total_prompt - hit, 0),
            cache_read_tokens=hit,
            cache_write_tokens=0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

        self.messages.append(message.model_dump(exclude_none=True))
        if message.content and message.content.strip():
            result.texts.append(message.content)
        for call in (message.tool_calls or []):
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                arguments = {"__parse_error__": f"{exc}"}
            result.tool_calls.append(
                ToolCall(id=call.id, name=call.function.name, input=arguments))
        return result

    def append_user(self, text: str) -> None:
        """A finished run ends with `tool` messages here, so this is simple."""
        self.messages.append({"role": "user", "content": text})

    def append_results(self, results: list[dict], extra_text: str = "") -> None:
        for entry in results:
            self.messages.append({
                "role": "tool",
                "tool_call_id": entry["id"],
                "content": entry["content"],
            })
        if extra_text:
            self.messages.append({"role": "user", "content": extra_text})


# Which provider serves which model, and how to reach it.
REGISTRY = {
    "anthropic": {
        "cls": AnthropicProvider,
        "prefixes": ("claude-",),
        "key_env": "ANTHROPIC_API_KEY",
        "supports_effort": True,
        "effort_levels": ("low", "medium", "high", "xhigh", "max"),
        "effort_map": {},
        "kwargs": {},
    },
    "deepseek": {
        "cls": OpenAICompatibleProvider,
        "prefixes": ("deepseek-",),
        "key_env": "DEEPSEEK_API_KEY",
        # DeepSeek takes effort under the OpenAI-dialect name
        # `reasoning_effort`. The published docs list none/low/high/max, but
        # the API was probed directly and accepts more than that: none,
        # minimal, low, medium, high, xhigh, max - while rejecting 'ultra',
        # 'banana' and '' with a 400. So the parameter is genuinely
        # validated, the documentation is merely incomplete, and every level
        # this application offers passes through unmapped.
        #
        # Accepted is not the same as distinct: whether 'medium' and 'xhigh'
        # produce different behaviour from their neighbours is unmeasured -
        # both depth probes hit the max_tokens cap before the model finished.
        "supports_effort": True,
        "effort_levels": ("none", "minimal", "low", "medium", "high",
                          "xhigh", "max"),
        "effort_map": {},
        "kwargs": {"base_url": "https://api.deepseek.com/v1",
                   "api_key_env": "DEEPSEEK_API_KEY"},
    },
}


def key_env_for(model: str) -> str:
    """Which environment variable holds the key for this model."""
    return REGISTRY[provider_for(model)]["key_env"]


def supports_effort(model: str) -> bool:
    """Whether `effort` reaches this model, or is silently discarded."""
    try:
        return REGISTRY[provider_for(model)]["supports_effort"]
    except ValueError:
        return False


def effort_levels(model: str) -> tuple[str, ...]:
    """The levels this model actually distinguishes."""
    try:
        return REGISTRY[provider_for(model)]["effort_levels"]
    except ValueError:
        return ()


def map_effort(model: str, effort: str) -> tuple[str, str]:
    """(level to send, note) - the note is empty when nothing was changed."""
    try:
        spec = REGISTRY[provider_for(model)]
    except ValueError:
        return effort, ""
    if effort in spec["effort_levels"]:
        return effort, ""
    mapped = spec["effort_map"].get(effort)
    if mapped is None:
        native = ", ".join(spec["effort_levels"])
        return spec["effort_levels"][0], (
            f"{model} has no effort level {effort!r} (it has {native}); "
            f"using {spec['effort_levels'][0]!r}")
    return mapped, (f"{model} has no {effort!r} level, so it was sent as "
                    f"{mapped!r} - two of our levels can collapse onto one "
                    f"here, giving identical runs")


def provider_for(model: str) -> str:
    for name, spec in REGISTRY.items():
        if model.startswith(spec["prefixes"]):
            return name
    raise ValueError(f"no provider knows how to serve {model!r}")


def make_provider(model: str, effort: str, system: str, tools: list[dict],
                  max_tokens: int, client=None):
    spec = REGISTRY[provider_for(model)]
    return spec["cls"](model=model, effort=effort, system=system, tools=tools,
                       max_tokens=max_tokens, client=client, **spec["kwargs"])
