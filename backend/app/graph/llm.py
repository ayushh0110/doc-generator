from typing import Any, Type

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.config import get_settings

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMNotConfigured(Exception):
    pass


def get_chat_model() -> BaseChatModel:
    settings = get_settings()
    if settings.llm_provider == "groq":
        if not settings.groq_api_key:
            raise LLMNotConfigured("GROQ_API_KEY is not set, so the agents can't run. Add it to backend/.env.")
        # Free-tier limits are per minute, so waiting and retrying usually gets through.
        return ChatOpenAI(
            model=settings.groq_model,
            base_url=settings.groq_base_url,
            api_key=settings.groq_api_key,
            max_tokens=settings.groq_max_tokens,
            temperature=0.2,
            timeout=300,
            max_retries=6,
        )
    if settings.llm_provider == "llamacpp":
        # llama-server speaks the OpenAI protocol. It doesn't check the key, but the client wants one.
        return ChatOpenAI(
            model=settings.llamacpp_model,
            base_url=settings.llamacpp_base_url,
            api_key="not-needed",
            max_tokens=settings.llamacpp_max_tokens,
            temperature=0.2,
            timeout=900,
            max_retries=0,
        )
    if settings.llm_provider != "anthropic":
        raise LLMNotConfigured(f'Unknown LLM_PROVIDER "{settings.llm_provider}". Use anthropic, groq or llamacpp.')
    if not settings.anthropic_api_key:
        raise LLMNotConfigured("ANTHROPIC_API_KEY is not set, so the agents can't run. Add it to backend/.env.")
    return ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        max_tokens=16000,
        reasoning_effort=settings.llm_effort,
        betas=[REFUSAL_FALLBACK_BETA],
        model_kwargs={"fallbacks": "default"},
    )


def structured(schema: Type[BaseModel]) -> Any:
    # json_schema, not the default function_calling. Current Claude models reject
    # forced tool choice, and llama-server turns a schema into a grammar it can enforce.
    return get_chat_model().with_structured_output(schema, method="json_schema")
