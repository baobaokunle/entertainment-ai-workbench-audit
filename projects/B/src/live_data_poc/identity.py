"""Evidence states belong to B; none constitutes A's verified identity."""
from __future__ import annotations
from .models import current_records


def evaluate_links(links, identifiers, evidence):
    users = {r["record_id"]: r for r in identifiers}
    proofs = {r["evidence_id"]: r for r in evidence}
    result = []
    for link in current_records(links):
        row = dict(link)
        left, right = users[link["left_id"]], users[link["right_id"]]
        proof = proofs[link["evidence_id"]]
        if link["state"] in {"rejected", "revoked"}:
            state, reason = link["state"], link["reason"]
        elif any(left[k] != right[k] or left[k] != link[k] for k in ("account_scope_id", "app_scope_id")):
            state, reason = "conflict", "SCOPE_MISMATCH"
        elif (left["role"], right["role"]) != ("viewer", "chat_peer"):
            state, reason = "conflict", "OWNER_OR_PEER_ROLE_MISMATCH"
        elif any(u["id_observation_status"] != "observed" for u in (left, right)):
            state, reason = "unknown", "ID_NOT_OBSERVED"
        elif link["method"] == "direct_pair" and proof["kind"] == "synthetic_direct_pair" and (proof["left_id"], proof["right_id"]) == (link["left_id"], link["right_id"]):
            state, reason = "direct_evidence", "SYNTHETIC_DIRECT_PAIR_ONLY_NOT_A_VERIFIED"
        else:
            state, reason = "candidate", "NO_DIRECT_EVIDENCE"
        row["state"], row["reason"] = state, reason
        result.append(row)
    # A direct edge is unusable when another active direct edge contradicts it.
    active = [r for r in result if r["state"] == "direct_evidence"]
    for row in active:
        if any((r["left_id"] == row["left_id"] and r["right_id"] != row["right_id"])
               or (r["right_id"] == row["right_id"] and r["left_id"] != row["left_id"]) for r in active):
            row["state"], row["reason"] = "conflict", "NON_UNIQUE_MAPPING"
    return result
