"""A small token-based clone detector for one Python file (type-1 and type-2 clones).

Identifiers become ID, literals become LIT, comments and layout are dropped. Every window of MIN_TOKENS consecutive
normalized tokens is hashed; a window seen twice at non-overlapping positions is a clone. Lines covered by any clone
instance count as duplicated. This mirrors what jscpd does with its defaults (50 tokens), restricted to one file.
Validated in the self-test: a seeded copy-pasted function is found, distinct functions are not.
"""
import io
import keyword
import token
import tokenize

MIN_TOKENS = 50
MIN_LINES = 5
SKIP = {token.COMMENT, token.NL, token.NEWLINE, token.INDENT, token.DEDENT, token.ENCODING, token.ENDMARKER}


def normalized_tokens(source):
    out = []
    for t in tokenize.generate_tokens(io.StringIO(source).readline):
        if t.type in SKIP:
            continue
        if t.type == token.NAME and not keyword.iskeyword(t.string) and t.string not in ("self", "cls"):
            val = "ID"
        elif t.type in (token.STRING, token.NUMBER) or t.type == getattr(token, "FSTRING_START", -1):
            val = "LIT"
        elif t.type in (getattr(token, "FSTRING_MIDDLE", -1), getattr(token, "FSTRING_END", -1)):
            continue
        else:
            val = t.string
        out.append((val, t.start[0]))
    return out


def duplication(source, min_tokens=MIN_TOKENS, min_lines=MIN_LINES):
    """Returns {dup_lines, code_lines, dup_ratio, clones}. code_lines counts lines that carry at least one token."""
    toks = normalized_tokens(source)
    code_lines = {ln for _, ln in toks}
    seen = {}
    pairs = []
    for i in range(len(toks) - min_tokens + 1):
        key = tuple(v for v, _ in toks[i:i + min_tokens])
        j = seen.get(key)
        if j is None:
            seen[key] = i
        elif i - j >= min_tokens:          # non-overlapping second occurrence
            pairs.append((j, i))
    dup_lines = set()
    clones = 0
    last = None
    for j, i in pairs:
        a = {ln for _, ln in toks[j:j + min_tokens]}
        b = {ln for _, ln in toks[i:i + min_tokens]}
        if len(a) < min_lines or len(b) < min_lines:
            continue
        if last is None or i != last[1] + 1 or j != last[0] + 1:
            clones += 1                    # a new clone region starts; consecutive windows extend the same one
        last = (j, i)
        dup_lines |= a | b
    n = len(code_lines)
    return {"dup_lines": len(dup_lines), "code_lines": n, "dup_ratio": round(len(dup_lines) / n, 4) if n else 0.0,
            "clones": clones}
