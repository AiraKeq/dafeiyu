"""Route-classification request controls for the pinned native provider.
AstrBot4.29.4 forwards kwargs to _prepare_chat_payload but that implementation
ignores them. Use its image/context assembler, then explicitly set request controls
on the existing zero-retry SDK client. No provider settings or credentials mutate.
"""
import json


async def classify(context, provider_id, text, images, system_prompt):
    prompt = json.dumps({'text': text, 'image_count': len(images)}, ensure_ascii=False)
    getter = getattr(context, 'get_provider_by_id', None)
    provider = getter(provider_id) if getter is not None else None
    if (provider is not None and hasattr(provider, '_prepare_chat_payload')
            and getattr(provider, 'client', None) is not None):
        payload, _ = await provider._prepare_chat_payload(
            prompt=prompt, image_urls=images or None, contexts=[], system_prompt=system_prompt)
        extra = {}
        model = str(payload.get('model', '')).lower()
        base = getattr(provider.client, 'base_url', None)
        if 'deepseek' in model or getattr(base, 'host', '') == 'api.deepseek.com':
            extra['thinking'] = {'type': 'disabled'}
        # Existing client construction has max_retries=0. Per-call timeout/control
        # is explicit instead of being silently dropped by the native adapter.
        completion = await provider.client.chat.completions.create(
            model=payload['model'], messages=payload['messages'], stream=False,
            max_tokens=512, response_format={'type':'json_object'}, extra_body=extra, timeout=6.0)
        choices = getattr(completion, 'choices', None)
        if not choices:
            return ''
        return str(getattr(choices[0].message, 'content', '') or '')
    # Explicit public interface for other providers/isolated fake contexts. This
    # branch does not claim support if their adapter drops controls as well.
    response = await context.llm_generate(
        chat_provider_id=provider_id, prompt=prompt, image_urls=images or None,
        system_prompt=system_prompt, contexts=[], max_tokens=512,
        response_format={'type':'json_object'}, thinking={'type':'disabled'}, request_max_retries=1)
    return str(response.completion_text or '')
