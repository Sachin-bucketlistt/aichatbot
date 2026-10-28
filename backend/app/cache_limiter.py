import copy

def _enforce_cache_limits(messages: list[dict]) -> list[dict]:
    """Ensure no more than 4 cache_control blocks exist.
    Anthropic API throws 400 Bad Request if there are >4 cache breakpoints.
    Prioritize the system prompt, then the most recent messages.
    """
    msgs = copy.deepcopy(messages)
    
    cache_allowance = 4
    if msgs and msgs[0].get("role") == "system":
        content = msgs[0].get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    cache_allowance -= 1

    for msg in reversed(msgs[1:] if msgs and msgs[0].get("role") == "system" else msgs):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    if cache_allowance > 0:
                        cache_allowance -= 1
                    else:
                        del block["cache_control"]
    return msgs
