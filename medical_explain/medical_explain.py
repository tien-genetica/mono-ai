from __future__ import annotations

from typing import List, Optional, Dict, Any, Protocol
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from datetime import datetime
import os
import asyncio
import aiohttp
from utils import measure_time, with_retry
from openai import AsyncOpenAI
import time


load_dotenv()


class Reference(BaseModel):
    title: str
    url: str
    source: str
    snippet: Optional[str] = None
    published_at: Optional[datetime] = None
    score: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExplainedResult(BaseModel):
    provider: str
    explanation: str
    references: List[Reference] = Field(default_factory=list)
    confidence: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MedicalExplainOptions(BaseModel):
    language: str = "en"
    audience: str = "clinician"  # "clinician" | "patient"
    style: str = "paragraph"  # "bullets" | "paragraph"
    max_chars: int = 1200
    top_k_refs: int = 6


class LlmOptions(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None


class LLMExplainProvider:
    name = "llm"

    def __init__(self, client: Optional[AsyncOpenAI] = None) -> None:
        self.client = client or AsyncOpenAI()

    @measure_time
    async def explain(
        self,
        query: str,
        options: MedicalExplainOptions,
        llm_options: Optional[LlmOptions] = None,
        context: Optional[str] = None,
    ) -> List[ExplainedResult]:
        model = (llm_options.model if llm_options else None) or os.getenv(
            "OPENAI_CHAT_MODEL"
        )
        if model:
            os.environ.setdefault("OPENAI_CHAT_MODEL", model)

        client = (
            self.client
            if not (llm_options and llm_options.base_url)
            else AsyncOpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=llm_options.base_url,
            )
        )
        sys = (
            "You are a medical explainer. Provide a concise, evidence-based explanation for the given medical term or phrase. "
            "Audience: {aud}. Language: {lang}. Style: {style}. "
            "Strictly limit output to {maxc} characters. "
            "Prioritize clinical guidelines, randomized controlled trials, and systematic reviews. "
            "If the evidence is uncertain or lacking, clearly state the uncertainty. "
            "Do not use markdown headings."
        )

        usr = (
            f"Context: {context}\n" if context else ""
        ) + f"Question: {query}\nProvide a concise answer."
        try:
            resp = await client.chat.completions.create(
                model=os.getenv("OPENAI_CHAT_MODEL", "gpt-5-nano"),
                temperature=0.0,
                messages=[
                    {
                        "role": "system",
                        "content": sys.format(
                            aud=options.audience,
                            lang=options.language,
                            style=(
                                "bullet points with short sentences"
                                if options.style == "bullets"
                                else "one concise paragraph"
                            ),
                            maxc=options.max_chars,
                        ),
                    },
                    {"role": "user", "content": usr},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            result = ExplainedResult(
                provider=self.name,
                explanation=text or "",
                references=[],
                confidence=0.5 if text else 0.0,
                metadata={
                    "llm": (llm_options.provider if llm_options else None) or "auto",
                    "model": model,
                },
            )
            return [result]
        except Exception as e:
            err_result = ExplainedResult(
                provider=self.name,
                explanation="",
                references=[],
                confidence=0.0,
                metadata={
                    "llm": (llm_options.provider if llm_options else None) or "auto",
                    "model": model,
                    "error": str(e)[:200],
                },
            )
            return [err_result]


class PerplexityExplainProvider:
    name = "perplexity"

    def __init__(
        self, api_key: Optional[str] = None, model: Optional[str] = None
    ) -> None:
        self.api_key = api_key or os.getenv("PERPLEXITY_API_KEY")
        self.model = model or os.getenv("PERPLEXITY_MODEL") or "sonar"

    @measure_time
    async def explain(
        self, query: str, options: MedicalExplainOptions, context: Optional[str] = None
    ) -> ExplainedResult:
        if not self.api_key:
            return ExplainedResult(
                provider=self.name,
                explanation="",
                references=[],
                confidence=0.0,
                metadata={"model": self.model, "error": "Missing PERPLEXITY_API_KEY"},
            )

        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        system_prompt = (
            "Answer like a careful clinician. Use current knowledge. Provide a concise answer (<= {maxc} chars). "
            "No preambles."
        )
        user_prompt = (
            f"Context: {context}\n" if context else ""
        ) + f"Question: {query}\nLanguage: {options.language}"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt.format(maxc=options.max_chars),
                },
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as r:
                    if r.status >= 400:
                        detail = await r.text()
                        raise RuntimeError(f"Perplexity API error {r.status}: {detail}")
                    data = await r.json()
        except Exception as e:
            return ExplainedResult(
                provider=self.name,
                explanation="",
                references=[],
                confidence=0.0,
                metadata={"model": self.model, "error": str(e)[:200]},
            )

        msg = (
            (data.get("choices") or [{}]).get("message", {})
            if isinstance(data, dict)
            else {}
        )
        if not msg and isinstance(data, dict):
            # Some responses include choices list
            first_choice = (data.get("choices") or [{}])[0]
            msg = first_choice.get("message", {})
        content = (msg.get("content") or "").strip()
        citations = (
            (data.get("citations") if isinstance(data, dict) else None)
            or msg.get("citations")
            or []
        )
        refs: List[Reference] = []
        for c in citations[: options.top_k_refs]:
            if isinstance(c, str):
                refs.append(Reference(title=c, url=c, source="Perplexity"))
            else:
                url_c = c.get("url") or c.get("source") or ""
                title_c = c.get("title") or url_c
                refs.append(
                    Reference(
                        title=title_c,
                        url=url_c,
                        source="Perplexity",
                        snippet="(Perplexity citation)",
                    )
                )

        return ExplainedResult(
            provider=self.name,
            explanation=content,
            references=refs,
            confidence=0.7 if content else 0.0,
            metadata={"model": self.model},
        )


async def main() -> None:
    query = "ambroxol"
    num_queries = 30
    opts = MedicalExplainOptions(
        language="en",
        audience="clinician",
        style="paragraph",
        max_chars=1000,
        top_k_refs=5,
    )

    llm_provider = LLMExplainProvider()
    px_provider = PerplexityExplainProvider()

    tasks = []
    for _ in range(num_queries):
        tasks.append(
            with_retry(
                llm_provider.explain,
                query=query,
                options=opts,
                llm_options=LlmOptions(
                    provider=os.getenv("LLM_PROVIDER", "openai"),
                    model=os.getenv("OPENAI_CHAT_MODEL", "gpt-5-nano"),
                ),
                retries=3,
                initial_delay_s=0.5,
                backoff=2.0,
            )
        )
        tasks.append(
            with_retry(
                px_provider.explain,
                query=query,
                options=opts,
                retries=3,
                initial_delay_s=0.5,
                backoff=2.0,
            )
        )

    start_ts = time.perf_counter()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_elapsed_ms = int((time.perf_counter() - start_ts) * 1000)

    success_count = sum(1 for r in results if not isinstance(r, Exception))
    error_count = len(results) - success_count

    print(
        f"Ran {num_queries} queries on each provider ({len(results)} tasks) concurrently."
    )
    print(f"Total wall time: {total_elapsed_ms} ms")
    if error_count:
        print(f"Errors: {error_count} task(s) failed")


if __name__ == "__main__":
    asyncio.run(main())
