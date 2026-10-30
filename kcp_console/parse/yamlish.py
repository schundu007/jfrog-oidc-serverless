"""A small YAML-subset reader, enough for kubelet's CredentialProviderConfig,
Helm values (`helm get values -a`) and rendered manifests.

Stdlib only, so no PyYAML. Supports block mappings and sequences, `- key: v`
items, flow sequences of scalars (`["*.azurecr.io"]`), `{}`/`[]`, quoted and
plain scalars, booleans/ints/null, `|`/`>` block scalars, comments, and `---`
document separators (load_all). Anything fancier (anchors, tags, multi-line
flow) is out of scope and read as a plain string.
"""

from __future__ import annotations

import re

_KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'[^']*'|[^\s"'#\[{-][^:#]*?|-[^\s:#][^:#]*?)\s*:(?:\s+|$)(.*)$""")


def _strip_comment(s: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or s[i - 1] in " \t[{,:-"):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def scalar(s: str):
    s = s.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        body = s[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\") if s[0] == '"' else body.replace("''", "'")
    if s == "{}":
        return {}
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [scalar(p) for p in _split_flow(inner)] if inner else []
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _split_flow(s: str) -> list:
    parts, cur, quote = [], [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _lines(text: str) -> list:
    out = []
    for raw in text.replace("\t", "  ").splitlines():
        if not raw.strip():
            out.append((None, "", raw))
            continue
        body = _strip_comment(raw)
        if not body.strip():
            continue
        out.append((len(body) - len(body.lstrip(" ")), body.strip(), raw))
    return out


class _Reader:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def peek(self):
        while self.i < len(self.lines) and self.lines[self.i][0] is None:
            self.i += 1
        return self.lines[self.i] if self.i < len(self.lines) else None

    def block(self, indent: int):
        line = self.peek()
        if line is None or line[0] < indent:
            return None
        if line[1].startswith("- ") or line[1] == "-":
            return self.seq(line[0])
        return self.mapping(line[0])

    def seq(self, indent: int) -> list:
        out = []
        while True:
            line = self.peek()
            if line is None or line[0] != indent or not (line[1].startswith("- ") or line[1] == "-"):
                return out
            rest = line[1][1:].lstrip(" ")
            if not rest:
                self.i += 1
                out.append(self.block(indent + 1))
            elif _KEY.match(rest):
                # "- key: v" starts a mapping whose keys sit at the column after "- "
                col = indent + (len(line[1]) - len(rest))
                self.lines[self.i] = (col, rest, line[2])
                out.append(self.mapping(col))
            else:
                self.i += 1
                out.append(scalar(rest))

    def mapping(self, indent: int) -> dict:
        out = {}
        while True:
            line = self.peek()
            if line is None or line[0] != indent:
                return out
            m = _KEY.match(line[1])
            if not m:
                return out
            key = scalar(m.group(1))
            val = m.group(2).strip()
            self.i += 1
            if val in ("|", "|-", "|+", ">", ">-", ">+"):
                out[key] = self.block_scalar(indent, folded=val.startswith(">"))
            elif val == "":
                nxt = self.peek()
                if nxt is not None and (nxt[0] > indent or (nxt[0] == indent and nxt[1].startswith("- "))):
                    out[key] = self.block(nxt[0])
                else:
                    out[key] = None
            else:
                out[key] = scalar(val)

    def block_scalar(self, indent: int, folded: bool) -> str:
        body, base = [], None
        while self.i < len(self.lines):
            ind, _, raw = self.lines[self.i]
            if ind is None:
                body.append("")
                self.i += 1
                continue
            raw_ind = len(raw) - len(raw.lstrip(" "))
            if raw_ind <= indent:
                break
            base = raw_ind if base is None else min(base, raw_ind)
            body.append(raw)
            self.i += 1
        while body and body[-1] == "":
            body.pop()
        text = "\n".join(line[base:] if base is not None and line else line for line in body)
        return " ".join(text.split("\n")) if folded else text + "\n"


def load(text: str):
    """Parse the first document."""
    docs = load_all(text)
    return docs[0] if docs else None


def load_all(text: str) -> list:
    docs, cur = [], []
    for raw in (text or "").splitlines():
        if raw.strip() == "---" or raw.startswith("--- "):
            docs.append("\n".join(cur))
            cur = []
        else:
            cur.append(raw)
    docs.append("\n".join(cur))
    out = []
    for d in docs:
        lines = _lines(d)
        if not any(ind is not None for ind, _, _ in lines):
            continue
        r = _Reader(lines)
        first = r.peek()
        out.append(r.block(first[0]))
    return out
