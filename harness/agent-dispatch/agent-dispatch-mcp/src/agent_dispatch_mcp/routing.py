"""Routing table access, with falsification status attached to every row.

DESIGN CONSTRAINT: a consumer must not be able to read a provisional verdict as
settled. Every entry returned by this module carries an explicit ``status``, and
``recommend`` refuses to hand back a bare verdict string. The status is not a
footnote a caller can drop; it is a peer field of the verdict itself.

The three falsification facts that must survive contact with a consumer:

  1. The debugging row is VOID. That cell was context-starved and carries no
     verdict in either direction. Absence of a verdict is not a negative verdict.
  2. Every ``unsupervised`` verdict is PROVISIONAL, pending a human
     claim-vs-verify calibration read that has not been performed.
  3. One recorded zero-tool-call run was a harness parse failure, not an inert
     model, and the model in question is therefore judged more harshly than the
     evidence supports.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).parent / "data" / "routing.json"

# Statuses a caller may treat as evidence. Anything else must not drive a
# decision without a human in the loop.
ACTIONABLE = ("confirmed", "provisional")


@lru_cache(maxsize=1)
def load_table() -> dict:
    return json.loads(_DATA.read_text())


def work_classes() -> list[dict]:
    t = load_table()
    return [
        {
            "id": w["id"],
            "label": w["label"],
            "status": w["status"],
            "notes": w["notes"],
            "n_entries": len(w["entries"]),
        }
        for w in t["work_classes"]
    ]


def models() -> list[str]:
    seen: list[str] = []
    for w in load_table()["work_classes"]:
        for e in w["entries"]:
            if e["model"] not in seen:
                seen.append(e["model"])
    return seen


def query(
    work_class: str | None = None,
    model: str | None = None,
    min_status: str | None = None,
) -> dict:
    """Return matching routing entries, each with its own status.

    ``min_status`` filters to a status level -- pass ``"confirmed"`` to get only
    rows that have survived a human calibration read. That filter currently
    returns ZERO rows, and that is the correct, honest answer rather than a bug.
    """
    t = load_table()
    out = []
    for w in t["work_classes"]:
        if work_class and work_class not in (w["id"], w["label"]):
            continue
        for e in w["entries"]:
            if model and e["model"] != model:
                continue
            if min_status and e["status"] != min_status:
                continue
            out.append(
                {
                    "work_class": w["id"],
                    "work_class_label": w["label"],
                    "work_class_status": w["status"],
                    "work_class_notes": w["notes"],
                    **e,
                    "actionable": e["status"] in ACTIONABLE,
                }
            )
        if work_class and not w["entries"]:
            out.append(
                {
                    "work_class": w["id"],
                    "work_class_label": w["label"],
                    "work_class_status": w["status"],
                    "work_class_notes": w["notes"],
                    "model": None,
                    "verdict": "no verdict",
                    "status": w["status"],
                    "actionable": False,
                }
            )
    return {
        "entries": out,
        "count": len(out),
        "status_levels": t["status_levels"],
        "verdicts": t["verdicts"],
        "global_caveats": t["global_caveats"],
        "provenance": t["provenance"],
        "reminder": (
            "Every entry carries its own `status`. No row in this table is "
            "`confirmed`. Do not render a `provisional` verdict as settled, and "
            "do not read a `void` row's absent verdict as a negative one."
        ),
    }


def recommend(work_class: str, model: str | None = None) -> dict:
    """Routing recommendation for a work class, ordered best-first.

    Never returns a bare verdict. The caller gets the verdict, its status, and
    the caveats in one object because separating them is how a provisional
    finding turns into folklore.
    """
    t = load_table()
    match = next(
        (w for w in t["work_classes"] if work_class in (w["id"], w["label"])), None
    )
    if match is None:
        return {
            "error": f"unknown work class {work_class!r}",
            "known_work_classes": [w["id"] for w in t["work_classes"]],
        }

    if match["status"] == "void":
        return {
            "work_class": match["id"],
            "status": "void",
            "recommendation": None,
            "explanation": (
                "This row is VOID and carries no verdict for any model. "
                + match["notes"]
            ),
            "safe_to_act_on": False,
            "global_caveats": t["global_caveats"],
        }
    if match["status"] == "not_measured":
        return {
            "work_class": match["id"],
            "status": "not_measured",
            "recommendation": None,
            "explanation": "No data was collected for this work class. " + match["notes"],
            "safe_to_act_on": False,
            "global_caveats": t["global_caveats"],
        }

    order = {"unsupervised": 0, "harness-verified": 1, "scaffolded": 2, "don't dispatch": 3}
    entries = [e for e in match["entries"] if model is None or e["model"] == model]
    ranked = sorted(entries, key=lambda e: order.get(e["verdict"], 4))
    actionable = [e for e in ranked if e["status"] in ACTIONABLE]

    return {
        "work_class": match["id"],
        "work_class_label": match["label"],
        "status": match["status"],
        "recommendation": actionable[0] if actionable else None,
        "alternatives": actionable[1:],
        "excluded_unmeasured": [e for e in ranked if e["status"] == "not_measured"],
        "safe_to_act_on": False,
        "why_not_safe_to_act_on": (
            "No row in this table has status `confirmed`. Every verdict here is "
            "PROVISIONAL: it rests on an objective proxy (verifier passed AND "
            "self-verified in every passing run) and has not been screened by a "
            "human claim-vs-verify calibration read. The proxy is specifically "
            "blind to FABRICATED_COMPLETION -- a model claiming success on a run "
            "whose verifier failed. Use these as strong candidates to try under "
            "verification, not as settled routing policy."
        ),
        "global_caveats": t["global_caveats"],
        "provenance": t["provenance"],
    }
