import asyncio
import time
from typing import List, Optional
from fastapi import HTTPException
import httpx
import random
from openai import AsyncOpenAI, OpenAIError

# === CONFIG ===
from email2ai.config import OPENAI_API_KEY, OPENAI_MODEL

MAX_TEXT_TOKENS = 272_000  
MAX_OUTPUT_TOKENS = 128_000

# Custom exception for failed summaries
class SummaryFailedException(Exception):
    """Raised when email2ai summary generation fails"""
    pass


MODEL_PRICING = {
    "gpt-5.2-2025-12-11": {
        "input": 1.75 / 1_000_000,
        "output": 14.00 / 1_000_000,
    }
}

async def calculate_cost(model, prompt_tokens, completion_tokens):
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return None

    return {
        "input_cost": prompt_tokens * pricing["input"],
        "output_cost": completion_tokens * pricing["output"],
    }

# Retry function
async def retry_async_with_exponential_backoff(
    func,
    *args,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retry_on_exceptions=(Exception,),
    retry_on_status_codes=(429, 500, 502, 503, 504),
    **kwargs
):
    delay = initial_delay

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)

        except retry_on_exceptions as e:
            status_code = None
            if isinstance(e, (httpx.HTTPStatusError, OpenAIError)):
                if hasattr(e, "response") and hasattr(e.response, "status_code"):
                    status_code = e.response.status_code
                elif hasattr(e, "status_code"):
                    status_code = e.status_code

            if status_code and status_code not in retry_on_status_codes:
                print(f"Non-retryable status {status_code} — giving up")
                raise

            if attempt == max_retries:
                print(f"Gave up after {max_retries + 1} attempts")
                error_msg = f"Operation failed after {max_retries + 1} retries: {e}"
                raise Exception(error_msg) from e

            # Backoff logic
            if status_code == 429:
                delay = min(max_delay, delay * backoff_factor * 2.5)
            else:
                if jitter:
                    delay = min(max_delay, delay * backoff_factor * (0.5 + random.random()))
                else:
                    delay = min(max_delay, delay * backoff_factor)

            print(f"Retry {attempt + 1}/{max_retries + 1} after {delay:.1f}s  (reason: {str(e)[:120]})")
            await asyncio.sleep(delay)


# OpenAI Async Client
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def query_openai_raw(
    context: str,
    prompt: str,
    skip_validation: bool = False,
    image_b64: Optional[str] = None
) -> str:

    full_prompt = prompt.replace("{document_text}", context)

    # Rough early check (useful before sending)
    estimated_input = len(full_prompt) // 3 + 1

    if not skip_validation and estimated_input > MAX_TEXT_TOKENS:
        msg = f"Input (~{estimated_input:,} tokens) exceeds hard limit {MAX_TEXT_TOKENS:,}"
        print(msg)
        raise SummaryFailedException(msg)
    elif estimated_input > MAX_TEXT_TOKENS:
        print(f"Input (~{estimated_input:,}) exceeds limit — proceeding anyway (skip_validation=True)")
    
    has_image = image_b64 is not None and len(image_b64) > 100  # rough sanity check

    if has_image:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": full_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "high"                        }
                    }
                ]
            }
        ]
    else:
        messages = [{"role": "user", "content": full_prompt}]

    async def _make_request():
        start = time.time()
        print(f"→ Sending request to {OPENAI_MODEL} (~{estimated_input:,} est. input tokens)")

        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_completion_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.1
        )

        choice = response.choices[0]
        content = choice.message.content or ""

        # Openai token usage 
        if response.usage:
            usage = response.usage
            tokens_cost_usd = await calculate_cost(
                model=response.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens
            )
            usage_dict = {
                "prompt_tokens":     usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens":      usage.total_tokens,
                "input_cost": round(tokens_cost_usd["input_cost"], 6) if tokens_cost_usd else None,
                "output_cost": round(tokens_cost_usd["output_cost"], 6) if tokens_cost_usd else None,
            }
        else:
            usage_dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "input_cost": 0, "output_cost": 0}

        return content, usage_dict

    try:
        return await retry_async_with_exponential_backoff(
            _make_request,
            max_retries=3,
            initial_delay=10.0,
            max_delay=180.0,
            backoff_factor=2.0,
            retry_on_exceptions=(OpenAIError, httpx.HTTPError, Exception),
            retry_on_status_codes=(429, 500, 502, 503, 504)
        )
    except Exception as e:
        msg = f"OpenAI call failed after retries: {str(e)}"
        print(msg)
        raise SummaryFailedException(msg) from e


async def run_staged_summary(
    context_chunks: List[str],
    prompt_template: str,
    filename: str,
    image_b64: Optional[str] = None,
# ) -> str:
) -> tuple[str, dict]:

    # Only require non-empty chunks when there's NO image
    if not image_b64:
        if not context_chunks or all(not chunk.strip() for chunk in context_chunks):
            raise HTTPException(
                status_code=404,
                detail="No context chunks provided for summary generation."
            )
    else:
        # For images: allow empty list or list with empty/whitespace strings
        if not context_chunks:
            context_chunks = [""]   # normalize

    if "{document_text}" not in prompt_template:
        raise HTTPException(
        status_code=404,
        detail=f"Prompt template missing '{{document_text}}' placeholder."
    )

    context = "\n\n".join(context_chunks)

    final_prompt = prompt_template

    final_summary, usage_dict = await query_openai_raw(
        context=context,
        prompt=final_prompt,
        skip_validation=True,
        image_b64 = image_b64, 
    )

    if final_summary:
        return final_summary, usage_dict

    raise SummaryFailedException("Summary generation returned empty result")