"""Pure contiguous UTF-8 windows shared by public vector projections."""

MAX_BYTES = 2400


def windows(body):
    """Complete consecutive text coverage; prefer line boundaries, never truncate."""
    start = 0
    while start < len(body):
        end, size = start, 0
        while end < len(body) and size + len(body[end].encode("utf-8")) <= MAX_BYTES:
            size += len(body[end].encode("utf-8"))
            end += 1
        if end < len(body):
            newline = body.rfind("\n", start, end)
            if newline >= start + (end - start) // 2:
                end = newline + 1
        text = body[start:end]
        if text.strip():
            yield start, end, text
        start = end
