"""register.py — score the solver's constraint register against the builder's requirement map.

Corpus navigation is a *measured* property of the submission, not an input property: a design
that satisfies a limit without having found the clause that imposes it earns no objective credit
for the items that limit binds.

Matching rules, all disclosed in instruction.md so the solver can aim at them:
  * a row cites ONE document and at most THREE clauses; more and the row matches nothing and
    counts as a wrong row (a citation that resolves to half the corpus is not a citation);
  * a row carries at most EIGHT numbers; more and it counts as a wrong row (a number sweep is
    not a reading);
  * a row whose clause does not appear anywhere in the document it cites is FABRICATED and counts
    as a wrong row. A row that cites a real clause this map does not happen to index is NEUTRAL -
    neither credited nor penalised. Documenting more genuine requirements than the builder
    indexed is thoroughness, and thoroughness is never punished;
  * every number in a row must be a value the cited clause actually states (or a stated
    equivalent, or a printed value of that clause).  One number that contradicts makes the whole
    row wrong, and a wrong row credits nothing;
  * PRECISION counts rows that should not have been filed - distractor documents, superseded
    revisions, fabricated clauses, contradicting values.  Extra rows that are genuine readings
    of the corpus are neutral: thoroughness is never punished.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

MAX_CLAUSES = 3
# EIGHT, the figure instruction.md publishes.
#

# An honest register
# can carry many rows quoting four to ten numbers, all of them honest
# readings of clauses that print that many. Charging those as fabrications would void honest
# registers for a rule the task never published.

# The sweep is stopped by the prose cap and by charging rows past the row cap, neither of which costs an honest filer
# anything.
MAX_NUMBERS = 8
# A requirement statement is a statement. A row whose prose is longer than this is a transcript
# of the document, and could use the document's digit-stripped text to satisfy every
# keyword test at once. The reference register's longest row is 104 characters.
MAX_PROSE = 300
ROW_CAP = 150


def _norm_doc(s):
    s = (s or "").lower()
    s = re.sub(r"\.(pdf|xlsx|dxf|docx|csv|xml|ply|json)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _norm_clause(s):
    s = (s or "").lower()
    s = re.sub(r"\b(par|para|paragraph|clause|section|sec|art|article|item|table|tab|note|no"
               r"|part|general|div|division|appendix|app|figure|fig|chapter|ch|subsection|rev)\b",
               " ", s)
    s = re.sub(r"[^a-z0-9.()/ -]+", " ", s)
    return " ".join(s.split())


def _numbers(*fields):
    out = []
    for f in fields:
        txt = str(f or "")
        # letter-hyphen-number designations (SL-WT, FS-2H-A, RG-1, S-201, CDU-1A) are identifiers
        txt = re.sub(r"\b[A-Za-z]{1,6}-[A-Za-z0-9]{1,4}(-[A-Za-z0-9]{1,3})?\b", " ", txt)
        # Spec section numbers, but ONLY in the forms this corpus writes them: "01 33 00" with
        # separators, or a six-digit run that starts with a zero, so a six-digit number such as
        # the owner's cost cap
        # of 310000 is still read as a number.
        txt = re.sub(r"\b\d{2}[\s.\-]\d{2}[\s.\-]\d{2}\b", " ", txt)   # spec section numbers
        txt = re.sub(r"\b0\d{5}\b", " ", txt)                          # spec section, run together
        txt = re.sub(r"\b(19|20)\d{2}\b", " ", txt)                  # years
        txt = re.sub(r"\b(29\s*cfr|ufc)\s*[\d.\-]+", " ", txt, flags=re.I)
        for m in re.finditer(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", txt, re.I):
            try:
                v = float(m.group(0))
            except ValueError:
                continue
            if math.isfinite(v):
                out.append(v)
    return out


class RegisterMap:
    def __init__(self, path=None, clause_index=None):
        base = Path(__file__).resolve().parent / "data"
        path = path or base / "register_map.json"
        with open(path, encoding="utf-8") as f:
            self.M = json.load(f)
        ci = clause_index or base / "clause_index.json"
        try:
            with open(ci, encoding="utf-8") as f:
                d = json.load(f)
            self.clause_index = {k: set(v) for k, v in d["clauses"].items()}
            self.clause_numbers = d["numbers"]
        except Exception:  # noqa: BLE001
            self.clause_index = {}
            self.clause_numbers = {}
        self.reqs = self.M["requirements"]
        self.doc_alias = {_norm_doc(k): v for k, v in self.M["document_aliases"].items()}
        self.distractors = {_norm_doc(d) for d in self.M["distractor_documents"]}
        # longest alias first, so "s 201 record slab plan rev 3" wins over "s 201"
        self._alias_order = sorted((a for a in self.doc_alias if a), key=len, reverse=True)
        self._distractor_keys = []
        for canon, keys in (self.M.get("distractor_aliases") or {}).items():
            for k in keys:
                self._distractor_keys.append((_norm_doc(k), canon))
        for d in self.M["distractor_documents"]:
            self._distractor_keys.append((_norm_doc(d), d))
        self._distractor_keys.sort(key=lambda t: len(t[0]), reverse=True)
        self.floor = self.M["precision_floor"]

    # ------------------------------------------------------------------ resolution
    def resolve_document(self, text):
        """Resolve a cited document name to its canonical identity.

        Two rules, in this order, and the order matters:

          1. DISTRACTORS FIRST, so a row reading
             "S-201 Record Slab Plan Rev 2" is not matched to the bare alias "S-201". The revision keys carry their revision, so a bare "S-201" still
             resolves to Rev 3 while "S-201 Rev 2" is charged.

          2. The alias must be CONTAINED IN the citation, never the other way round, so a short
             string that happens to be a substring of some alias does not resolve to a real document. Longest alias
             wins, so the specific title beats the bare code.
        """
        n = _norm_doc(text)
        if not n:
            return None, False
        for key, _canon in self._distractor_keys:
            if key and key in n:
                self._last_distractor = _canon
                return None, True
        for alias in self._alias_order:
            if alias in n:
                return self.doc_alias[alias], False
        return None, False

    def clause_exists(self, canon_doc, cited):
        """Does at least one of the cited clauses actually appear in that document?"""
        known = self.clause_index.get(canon_doc)
        if not known:
            return True          # no index for this document: do not penalise on ignorance
        for c in cited:
            n = _norm_clause(c)
            if not n:
                continue
            if n in known or f"note {n}" in known:
                return True
            head = n.split()[0] if n.split() else ""
            if head and (head in known or any(k.startswith(head + ".") for k in known)):
                return True
        return False

    def printed_in(self, canon_doc):
        """Every number the shipped document prints."""
        return self.clause_numbers.get(canon_doc) or []

    def clause_matches(self, req_clause, cited):
        """Does any cited clause name the requirement's clause?

        Exact equality alone is too strict in one specific direction, and it can cost HARD families:
        a citation of `Table E-1 item 61` - the phrasing the corpus itself
        uses in EOR 5.1 - against a map row reading `Table E-1` would lose F1_STRIP_FLEXURE, and
        `Table 1` against `Table 1 (vi)` would lose G13_CORING_METHOD. Penalising a
        solver for citing the project's own words, or for naming the table without its roman
        sub-index, measures citation format rather than corpus navigation.

        So two relations are accepted beyond equality:

          * the citation EXTENDS the map clause (`e-1 61` against `e-1`, `3.4 water` against
            `3.4`) - strictly more specific, and unambiguous;
          * the citation is the PARENT of a parenthesis-indexed sub-clause (`1` against
            `1 (vi)`) - bounded to parenthesised indices so it cannot collapse a dotted numeric
            series. `1` still does not claim `1.4`, `1.5`, `1.6` and `1.7` at once, which is the
            over-reach the exact rule was introduced to stop.

        A parent citation can reach two sibling rows (R021 `Table 1 (vi)` and R022
        `Table 1 (vii)`), which is safe here because credit needs the row's numbers and keywords
        too, and those siblings' keyword sets are disjoint - wet rig-mounted against dry shrouded.
        """
        a = _norm_clause(req_clause)
        ta = a.split()
        for c in cited:
            b = _norm_clause(c)
            if not b:
                continue
            if a == b or a.replace(" ", "") == b.replace(" ", ""):
                return True
            tb = b.split()
            # the citation is more specific than the map: `e-1 61` against `e-1`
            if len(tb) > len(ta) and tb[:len(ta)] == ta:
                return True
            # a parenthesis-indexed sub-clause cited by its parent: `1` against `1 (vi)`
            if len(ta) > len(tb) and ta[:len(tb)] == tb \
                    and all(t.startswith("(") and t.endswith(")") for t in ta[len(tb):]):
                return True
            # the same, written without a space: `(g)(1)` against `1910.303(g)(1)`
            if a.startswith("(") and b.endswith(a) and len(b) > len(a):
                return True
        return False

    def score(self, rows):
        """Return per-requirement match state plus recall/precision metrics.

        Duplicate rows are collapsed first, so filing the same harmless row many times cannot drive
        precision up. A register is a set of requirements, so it is scored as one."""
        seen_rows, deduped = set(), []
        for r in rows:
            # The key must be built from what the row RESOLVES to, not from how it was spelled.
            
            
            
            
            
            
            
            
            
            
            # Keying on the resolved document means any spelling that resolves
            # to one document collapses to one row, and a spelling that resolves to nothing is
            # already charged as a fabrication.
            canon, _distractor = self.resolve_document(r.get("document"))
            key = (canon or _norm_doc(r.get("document")), _norm_clause(r.get("clause")),
                   " ".join(str(r.get(k) or "").strip().lower()
                            for k in ("value", "requirement")))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            deduped.append(r)
        rows = deduped
        matched = set()
        detail = []
        # ---------------------------------------------------------------------------------
        # PRECISION IS CHARGED PER REQUIREMENT CLAIMED, not per row and not per "outcome".
        #
        
        
        
        
        
        
        
        
        
        
        
        #
        # The unit of a register is a claim about a requirement. So:
        #   * a row that credits requirements contributes those REQUIREMENT IDS (a requirement
        #     already credited by an earlier row adds nothing - it is the same claim);
        #   * a row that credits nothing contributes nothing to either side, exactly as a
        #     neutral row does. Citing a clause without reading a value off it is not a false
        #     claim, and it is not a true one either;
        #   * a WRONG row contributes one false claim, keyed on its full content INCLUDING the
        #     prose, so two materially different falsehoods are charged twice and repeating one
        #     falsehood is charged once.
        # precision = 1 - false claims / (requirements truly claimed + false claims).
        # ---------------------------------------------------------------------------------
        credited_ids = set()
        wrong_claims = set()
        neutral_keys = set()
        judged_rows = []      # rows that credit or are charged; neutral/no-claim are free

        def prose(r):
            return " ".join(str(r.get(k) or "").strip().lower()
                            for k in ("requirement", "value", "units"))

        for r in rows:
            if not any((r.get(k) or "").strip() for k in ("document", "clause", "value",
                                                          "requirement")):
                continue
            self._last_distractor = None
            canon, is_distractor = self.resolve_document(r.get("document"))
            clauses = [c for c in re.split(r"[;,/|]| and ", r.get("clause") or "") if c.strip()]
            nums = _numbers(r.get("value"), r.get("requirement"))
            nkey = tuple(sorted(round(float(v), 6) for v in nums))
            ckey = tuple(sorted(_norm_clause(c) for c in clauses))

            def bad(reason, doc, verdict):
                wrong_claims.add((reason, doc, ckey, nkey, prose(r)))
                detail.append({"row": r, "verdict": verdict})
                judged_rows.append(1)

            # neutral rows are free and do not consume the row cap: instruction.md promises
            # that registering more of the corpus than is asked for cannot hurt you, and a cap
            # that counts them against the filer breaks that promise.
            if not is_distractor and canon is not None and len(clauses) <= MAX_CLAUSES \
                    and len(nums) <= MAX_NUMBERS:
                probe = [q for q in self.reqs if q["document"] == canon
                         and self.clause_matches(q["clause"], clauses)]
                if not probe and self.clause_exists(canon, clauses):
                    neutral_keys.add((canon, ckey))
                    detail.append({"row": r, "verdict": "genuine clause, not indexed (neutral)"})
                    continue


            if is_distractor:
                bad("distractor", self._last_distractor, "distractor document")
                continue
            if canon is None:
                bad("nodoc", _norm_doc(r.get("document")), "document not in the corpus")
                continue
            if len(clauses) > MAX_CLAUSES:
                bad("clauses", canon, f"{len(clauses)} clauses cited")
                continue
            if len(nums) > MAX_NUMBERS:
                bad("numbers", canon, f"{len(nums)} numbers in one row")
                continue
            cand = [q for q in self.reqs
                    if q["document"] == canon and self.clause_matches(q["clause"], clauses)]
            if not cand:
                bad("noclause", canon, "clause does not appear in that document")
                continue
            if len({q["document"] for q in cand}) > 1 \
                    or len({q["clause"] for q in cand}) > MAX_CLAUSES:
                bad("broad", canon, "citation resolves too broadly")
                continue
            numeric = [q for q in cand if q.get("value") is not None]
            bad_number = False
            if numeric and nums:
                allowed = list(self.printed_in(canon))
                for q in numeric:
                    allowed.append(q["value"])
                    allowed += q.get("equivalents", [])
                    allowed += q.get("printed", [])
                    for v in [q["value"]] + list(q.get("equivalents", [])):
                        if isinstance(v, (int, float)) and 0 < abs(v) < 1:
                            allowed.append(v * 100.0)
                for v in nums:
                    if not any(_close(v, a) for a in allowed):
                        bad_number = True
                        break
            if bad_number:
                bad("badnum", canon, "a number contradicts the cited clause")
                continue
            if len(str(r.get("requirement") or "")) > MAX_PROSE:
                detail.append({"row": r, "verdict": "requirement prose is a transcript, "
                                                    "not a reading (no claim)"})
                continue
            credited = set()
            for q in cand:
                if q.get("value") is None:
                    if _keyword_hit(q, r):
                        credited.add(q["id"])
                else:
                    # Credit on the requirement's value, a stated equivalent, OR any number the
                    # requirement itself lists as printed by that clause - where a clause prints
                    # several numbers the map names only one, and not always the governing one.
                    ok_vals = [q["value"]] + list(q.get("equivalents", [])) \
                        + list(q.get("printed", []))
                    if any(any(_close(v, a) for v in nums) for a in ok_vals):
                        credited.add(q["id"])
            if credited:
                if len(judged_rows) >= ROW_CAP:
                    # Beyond the cap a row is CHARGED, not discarded, so a
                    # register cannot spend the cap on rows that claim nothing and then append
                    # any number of fabrications for free.
                    
                    
                    wrong_claims.add(("overcap", canon, ckey, nkey, prose(r)))
                    detail.append({"row": r, "verdict": "past the row cap"})
                    continue
                judged_rows.append(1)
                matched |= credited
                credited_ids |= credited
                detail.append({"row": r, "verdict": "matched"})
            else:
                # claims no requirement: contributes to neither side, like a neutral row, and
                # (per instruction.md) does not consume the row cap either
                detail.append({"row": r, "verdict": "no requirement credited (no claim)"})

        wrong = len(wrong_claims)
        neutral = len(neutral_keys)
        filed = len(credited_ids) + wrong
        n_hard = [q for q in self.reqs if q["tier"] == "hard"]
        recall_hard = len([q for q in n_hard if q["id"] in matched]) / max(len(n_hard), 1)
        recall_all = len(matched) / max(len(self.reqs), 1)
        # Precision counts rows that should NOT have been filed, against the rows whose fate the
        # map can judge. Neutral rows - genuine readings of clauses the map does not index - are
        # excluded from both, so filing more of them can neither help nor hurt.
        judged = filed          # neutral rows and no-claim rows never entered `filed`
        precision = 1.0 - wrong / judged if judged else 1.0
        families = {q["family"] for q in self.reqs if q["id"] in matched}
        # a family is REGISTERED only when every one of its requirements is matched
        by_fam = {}
        for q in self.reqs:
            by_fam.setdefault(q["family"], []).append(q["id"])
        registered = {f for f, ids in by_fam.items() if all(i in matched for i in ids)}
        return {"matched": sorted(matched), "wrong_rows": wrong, "rows_filed": filed,
                "neutral_rows": neutral, "rows_judged": judged,
                "recall_hard": recall_hard, "recall_all": recall_all, "precision": precision,
                "registered_families": sorted(registered), "touched_families": sorted(families),
                "precision_floor": self.floor, "detail": detail[:400]}


def _close(a, b, rel=0.02, abs_=1e-6):
    if a == b:
        return True
    return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


def _kw_in(kw, text):
    """Whole-word keyword test.

    A naked substring match let a keyword be satisfied by an accident of spelling - `rated`
    hits inside `integrated`, `no` inside `note`, `core` inside `scored`. No requirement is
    currently certified by such a hit, but the primitive should not be able to.  Hyphens and
    spaces are interchangeable so `rig-mounted` matches `rig mounted`.
    """
    pat = re.escape(kw.lower().strip()).replace(r"\-", "[- ]").replace(r"\ ", "[- ]")
    return re.search(rf"(?<![a-z0-9]){pat}(?![a-z0-9])", text) is not None


def _keyword_hit(q, row):
    # `items` names what the requirement binds, not what it says, and scanning it let a row earn
    # a valueless requirement by listing every core id. Keywords are read from the two fields
    # that carry the reading itself.
    text = " ".join(str(row.get(k) or "") for k in ("requirement", "value")).lower()
    kws = q.get("keywords") or []
    return any(_kw_in(k, text) for k in kws) if kws else True
