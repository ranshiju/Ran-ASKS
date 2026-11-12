"""Pure, bounded literal relevance signals shared across discovery channels.

These describe matching text, never semantic identity, entailment or confidence.
"""
import re
from bisect import bisect_left

STOPWORDS = set("a an the and or of for to in on with from by as is are be this that these those what which how why does do can could would should please explain describe".split())


def parts(text):
    return re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]{2,}", text.casefold())


def terms(text):
    found = []
    for part in parts(text):
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            found.extend(part[i:i+2] for i in range(max(1, len(part)-1)))
        elif part not in STOPWORDS:
            found.append(part)
    return set(found)


def match(query, title, body):
    chunks = list(dict.fromkeys(p for p in parts(query) if p not in STOPWORDS))[:48]
    query_terms = terms(" ".join(chunks))
    title_terms = terms(title)
    text = (title + "\n" + body).casefold()
    text_terms = title_terms | terms(body)
    # Whole English words and intact CJK phrases prevent scattered bigram noise
    # from outranking the requested name. Short CJK terms still count as coverage.
    anchors = [p for p in chunks if len(p) >= 3]
    matched = [p for p in anchors if (p in text if re.fullmatch(r"[\u3400-\u9fff]+", p) else p in text_terms)]
    return {"basis": "literal-query-v1",
            "phrase_coverage": sum(map(len, matched)) / max(1, sum(map(len, anchors))),
            "term_coverage": len(query_terms & text_terms) / max(1, len(query_terms)),
            "title_coverage": len(query_terms & title_terms) / max(1, len(query_terms)),
            "matched_phrases": matched}


def rank_key(features):
    return tuple(-features.get(k, 0) for k in ("phrase_coverage", "term_coverage", "title_coverage"))


def subject_match(subjects, labels, body):
    """Literal caller-supplied names, not inferred entities or semantic identity."""
    label_text = "\n".join(labels).casefold()
    text = label_text + "\n" + body.casefold()
    matched, label_matches = [], []
    for subject in subjects:
        pattern = re.escape(subject.casefold()).replace(r"\ ", r"\s+")
        pattern = r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])"
        if re.search(pattern, text):
            matched.append(subject)
        if re.search(pattern, label_text):
            label_matches.append(subject)
    return {"matched": matched, "labels": label_matches}


def excerpt(query, body, limit=700):
    """Select one literal body slice; offsets never refer to normalized text.

    Distinct matches guide display only, not ranking or evidence validation.
    Keeping some preceding context helps, but cannot guarantee all conditions fit.
    """
    if limit < 1:
        raise ValueError("excerpt limit must be positive")
    start = 0
    if len(body) > limit:
        chunks = list(dict.fromkeys(p for p in parts(query) if p not in STOPWORDS))[:48]
        query_terms = terms(" ".join(chunks))
        phrases = {p for p in chunks if len(p) >= 3}
        positions = {}
        for needle in sorted(query_terms | phrases):
            pattern = re.escape(needle)
            if not re.fullmatch(r"[\u3400-\u9fff]+", needle):
                pattern = r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])"
            hits = [m.start() for m in re.finditer(pattern, body, re.I)]
            if hits:
                positions[needle] = hits
        starts = {0}
        for hits in positions.values():
            starts.update(max(0, min(pos - inset, len(body) - limit))
                          for pos in hits for inset in (limit // 3, limit // 2))

        def coverage(offset):
            found = set()
            matched_starts, matched_ends = [], []
            for needle, hits in positions.items():
                index = bisect_left(hits, offset)
                if index < len(hits) and hits[index] + len(needle) <= offset + limit:
                    found.add(needle)
                    matched_starts.append(hits[index])
                    matched_ends.append(hits[index] + len(needle))
            margin = min(min(matched_starts) - offset, offset + limit - max(matched_ends)) if found else 0
            return sum(len(p) for p in found & phrases), len(found & query_terms), margin, -offset

        start = max(starts, key=coverage)
    end = min(len(body), start + limit)
    return {"excerpt": body[start:end], "excerpt_truncated": start > 0 or end < len(body),
            "excerpt_span": {"start": start, "end": end}}
