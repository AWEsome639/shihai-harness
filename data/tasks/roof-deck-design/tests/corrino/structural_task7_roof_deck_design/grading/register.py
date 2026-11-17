"""Constraint-register matcher.

A register row MATCHES a requirement when its document names one of the requirement's document aliases, its clause
names one of the clause aliases, and its value agrees with the requirement: a number within tolerance of the
requirement's value or of one of its stated equivalents (`alt_values`, unit-scaled by 1, 1000 or 1/1000); a
requirement without a numeric value needs one of its keywords in the row's value or applies_to text.

A row is WRONG when it cites a superseded or distractor document, or when it cites the document and clause of one or
more requirements, matches none of them, and CONTRADICTS them: it states a number of the same magnitude as a cited
requirement's value (ratio 0.5-2 after unit scaling) that is not a value the cited clause prints (`clause_values`).
Numbers that are part of identifiers (S01, RB05, DN250, M12, C1) are ignored (a requirement may list `value_patterns`
such as a rod designation to accept instead); rows without numbers, or with numbers the clause prints, are neutral.
A row carrying more than MAX_NUMS numbers matches nothing and counts as wrong when it cites a requirement (number
sweeps are not registers).  A row that cites a numeric requirement is wrong when ANY of its numbers contradicts (even
if another number matches), and a wrong row credits nothing: neither a keyword match nor one correct number can shield
guessed numbers.  A row cites ONE document and at most three clauses (a combined table): a row whose
citation resolves to requirements of more than one document, or to more than three distinct clauses, matches nothing
and counts as wrong (concatenated citations are not registers).  Unit scaling is limited to a requirement's `scales` where x1000 makes no sense.  Extra correct rows are neutral.  Precision = 1 - wrong / filed (rows beyond MAX_ROWS are
ignored); below the disclosed floor of 0.60 the register credits nothing.
"""
from __future__ import annotations

import re

PRECISION_FLOOR = 0.60
MAX_ROWS = 150
MAX_NUMS = 8
_SCALES = (1.0, 1000.0, 0.001)
_NUM = re.compile(r"(?<![A-Za-z0-9.])[-+]?\d*\.?\d+")


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", str(s).lower()).strip()


def _nums(s: str):
    out = []
    s = re.sub(r"(?<=[A-Za-z])-(?=\d)", "", str(s).replace(",", ""))     # SBF-16, RB08-4, BC-30: identifiers
    for m in _NUM.finditer(s):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            pass
    return out


def _doc_match(row_doc: str, aliases) -> bool:
    d = _n(row_doc)
    return any(_n(a) in d for a in aliases if _n(a))


def _clause_match(row_clause: str, aliases) -> bool:
    c = " " + _n(row_clause) + " "
    for a in aliases:
        a = _n(a)
        if not a:
            continue
        if re.search(r"(?<![0-9a-z.])" + re.escape(a) + r"(?![0-9])", c):
            return True
    return False


def _close(v: float, target: float, tol: float, abs_tol: float, scales=_SCALES) -> bool:
    return any(abs(v * scale - target) <= max(abs(target) * tol, abs_tol) for scale in scales)


def _targets(req):
    return [] if req.get("value") is None else [float(req["value"])] + [float(x) for x in req.get("alt_values", [])]


def _value_ok(row_value: str, req, text: str = "") -> bool:
    nums = _nums(row_value)
    if len(nums) > MAX_NUMS:
        return False
    kws = req.get("keywords") or []
    blob = f"{row_value} {text}".lower()
    if req.get("value") is None:
        return not kws or any(k in blob for k in kws)
    # A row that cites the governing clause and states its substance in words, with no number of its
    # own, evidences that the clause was found, so it counts towards overall recall - but it cannot
    # satisfy a requirement whose substance IS a number: a register with no numbers in it would
    # otherwise earn full hard recall, which is worth most of the score. `soft_only` marks it.
    if not nums and kws and any(k in blob for k in kws):
        return "soft_only"
    if any(re.search(p, str(row_value).lower()) for p in req.get("value_patterns", [])):
        return True
    tol, abs_tol, sc = req.get("tol", 0.02), req.get("abs_tol", 0.0), req.get("scales", _SCALES)
    return any(_close(v, t, tol, abs_tol, sc) for v in nums for t in _targets(req))


def _contradicts(row_value: str, hits) -> bool:
    nums = _nums(row_value)
    if len(nums) > MAX_NUMS:
        return True
    allowed = [(t, q.get("tol", 0.02), q.get("abs_tol", 0.0)) for q in hits for t in _targets(q)]
    allowed += [(float(t), 0.02, 0.0) for q in hits for t in q.get("clause_values", [])]
    for v in nums:
        if any(_close(v, t, tl, at) for t, tl, at in allowed):
            continue
        for q in hits:
            for t in _targets(q):
                if t and any(0.5 <= v * scale / t <= 2.0 for scale in q.get("scales", _SCALES)):
                    return True
    return False


def score_register(rows, rmap: dict) -> dict:
    reqs = rmap["requirements"]
    rows = rows[:MAX_ROWS]
    matched = set()
    cited = set()          # clause found and described, but its value not stated
    wrong = 0
    for r in rows:
        if _doc_match(r["document"], rmap.get("wrong_documents", [])):
            wrong += 1
            continue
        hits = [q for q in reqs if _doc_match(r["document"], q["documents"]) and _clause_match(r["clause"], q["clauses"])]
        if len({tuple(q["documents"]) for q in hits}) > 1 or len({(tuple(q["documents"]), q["clauses"][0]) for q in hits}) > 3:
            wrong += 1
            continue
        graded = [(q, _value_ok(r["value"], q, r.get("applies_to", ""))) for q in hits]
        good = [q for q, v in graded if v is True]
        substance = [q for q, v in graded if v == "soft_only"]
        # wrongness is judged on the NUMERIC requirements the row cites, so a keyword match on a valueless clause cannot
        # shield guessed numbers; a wrong row credits nothing
        num_hits = [q for q in hits if q.get("value") is not None]
        if num_hits and _contradicts(r["value"], hits):
            wrong += 1
            continue
        for q in good:
            matched.add(q["id"])
        for q in substance:
            cited.add(q["id"])
    filed = len(rows)
    precision = 1.0 - wrong / filed if filed else 0.0
    active = precision >= PRECISION_FLOOR
    hard = [q["id"] for q in reqs if q.get("hard")]
    return {
        "matched": sorted(matched) if active else [],
        "matched_raw": sorted(matched),
        "filed": filed, "wrong": wrong, "precision": precision, "active": active,
        "recall_all": len(matched | cited) / len(reqs) if reqs else 0.0,
        "recall_values": len(matched) / len(reqs) if reqs else 0.0,
        "cited_without_value": sorted(cited - matched),
        "recall_hard": len([h for h in hard if h in matched]) / len(hard) if hard else 0.0,
    }


def registered_families(rmap: dict, reg: dict, families) -> dict:
    have = set(reg["matched"])
    out = {}
    for f in families:
        gov = [q["id"] for q in rmap["requirements"] if f in q["families"] and q.get("hard")]
        out[f] = all(g in have for g in gov)
    return out
