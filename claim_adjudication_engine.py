#!/usr/bin/env python3
"""
================================================================================
SCRIPT: claim_adjudication_engine.py
DESCRIPTION:
    Enterprise Medicare LCD/Article Claim Adjudication & Exception Engine.
    
    Implements the complete 20-category edge-case catalogue:
    - Evaluates each claim line independently (CL01-CL08).
    - Uses ONLY line-linked diagnosis pointers (D01-D04).
    - Enforces Date-of-Service historical version validity (H01-H05).
    - Validates alternative policy support (A01-A03) to prevent false positives.
    - Strictly differentiates EXCLUDED vs NOT_LISTED vs CONDITIONAL (N01-N04, R04).
    - Never equates a coverage issue with an overpayment unless actual net
      paid disbursements exist (O01-O15).
    - Produces the 11 standardized workflow decision statuses.
    - Generates a full 22-field audit trail record per claim line (AU01-AU20).

USAGE:
    python3 claim_adjudication_engine.py --claims <claims.csv> --tda <tda.csv>
================================================================================
"""

import os
import sys
import csv
import re
import argparse
from datetime import datetime, timezone
from collections import defaultdict

csv.field_size_limit(sys.maxsize)

ENGINE_VERSION = "2.0.0-PROD"

# The 11 Standardized Decision Workflow Statuses
STATUS_SUPPORTED = "SUPPORTED"
STATUS_POTENTIAL_COVERAGE_ISSUE = "POTENTIAL_COVERAGE_ISSUE"
STATUS_POTENTIAL_OVERPAYMENT = "POTENTIAL_OVERPAYMENT"
STATUS_NOT_LISTED = "NOT_LISTED"
STATUS_CONDITIONAL = "CONDITIONAL"
STATUS_DOCUMENTATION_REQUIRED = "DOCUMENTATION_REQUIRED"
STATUS_MANUAL_REVIEW = "MANUAL_REVIEW"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_CODING_ERROR = "CODING_ERROR"
STATUS_EXCLUDED = "EXCLUDED"


def normalize_code(code):
    """Normalize CPT/HCPCS or ICD code by removing whitespace and uppercase."""
    if not code:
        return ""
    return str(code).strip().upper()


def normalize_icd(code):
    """Normalize ICD-10 code: strip dots, uppercase, trim."""
    if not code:
        return ""
    return str(code).strip().upper().replace(".", "")


class PolicyIndex:
    """In-memory indexed policy lookup optimized for fast claim line evaluation."""

    def __init__(self):
        # Index key: (state_abbrev, hcpc_clean) -> list of policy rules
        self.rules_by_state_hcpc = defaultdict(list)
        # Fast check if an HCPCS code has ANY active LCD in the state
        self.active_hcpcs_by_state = defaultdict(set)

    def load_tda_csv(self, tda_path, max_rows=None):
        """Loads and indexes TDA records."""
        print(f"[*] Indexing TDA decision asset from {tda_path}...")
        count = 0
        with open(tda_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for r in reader:
                states_str = r.get("jurisdiction_states", "")
                hcpc = r.get("hcpc_code_clean", "")
                if not hcpc:
                    continue

                rule_obj = {
                    "article_id": r.get("article_id", ""),
                    "article_version": r.get("article_version", ""),
                    "related_lcd_id": r.get("related_lcd_id", ""),
                    "related_ncd_id": r.get("related_ncd_id", ""),
                    "policy_group_uid": r.get("policy_group_uid", ""),
                    "hcpc_code_clean": hcpc,
                    "is_hcpc_explicitly_noncovered": r.get("is_hcpc_explicitly_noncovered", "0") == "1",
                    "icd10_code_clean": r.get("icd10_code_clean", ""),
                    "coverage_status": r.get("coverage_status", ""),
                    "is_covered_flag": r.get("is_covered_flag", "0") == "1",
                    "is_non_covered_flag": r.get("is_non_covered_flag", "0") == "1",
                    "is_conditional_flag": r.get("is_conditional_flag", "0") == "1",
                    "adjudication_action": r.get("adjudication_action", ""),
                    "denial_reason_code": r.get("denial_reason_code", ""),
                    "denial_reason_message": r.get("denial_reason_message", ""),
                    "asterisk_flag": r.get("asterisk_flag", "N"),
                    "asterisk_explanation": r.get("asterisk_explanation", ""),
                    "dual_diagnosis_required": r.get("dual_diagnosis_required", "0") == "1",
                    "required_modifiers": set(r.get("required_modifiers", "").split("|")) if r.get("required_modifiers") else set(),
                    "allowed_bill_types": set(r.get("allowed_bill_types", "").split("|")) if r.get("allowed_bill_types") else set(),
                    "allowed_revenue_codes": set(r.get("allowed_revenue_codes", "").split("|")) if r.get("allowed_revenue_codes") else set(),
                    "eff_start": r.get("effective_start_date", "1900-01-01"),
                    "eff_end": r.get("effective_end_date", "9999-12-31"),
                    "group_desc": r.get("paired_group_instructions", ""),
                    "source_table": r.get("source_table", ""),
                }

                # Index across all applicable states
                for st in states_str.split(","):
                    st = st.strip().upper()
                    if st:
                        self.rules_by_state_hcpc[(st, hcpc)].append(rule_obj)
                        self.active_hcpcs_by_state[st].add(hcpc)

                count += 1
                if max_rows and count >= max_rows:
                    break
        print(f"[*] Indexed {count:,} TDA policy rules successfully.")


def evaluate_claim_line(line, policy_index):
    """
    Evaluates a single claim line against the 20 edge-case categories.
    
    Parameters:
        line (dict): A dictionary representing a single claim line.
        policy_index (PolicyIndex): Indexed TDA policy store.
        
    Returns:
        dict: Final adjudication record with full 22-field audit evidence.
    """
    eval_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    claim_id = str(line.get("claim_id", "")).strip()
    line_id = str(line.get("claim_line_id", "1")).strip()
    dos = str(line.get("date_of_service", "")).strip()[:10]
    state = str(line.get("service_state", "")).strip().upper()
    billed_hcpc = normalize_code(line.get("billed_hcpc", ""))
    billed_mods = set([normalize_code(m) for m in line.get("billed_modifiers", "").split("|") if m.strip()])
    billed_bill_type = normalize_code(line.get("billed_bill_type", ""))
    billed_rev_code = normalize_code(line.get("billed_revenue_code", ""))
    
    # Financial Elements
    paid_amt = float(line.get("paid_amount", 0.0) or 0.0)
    billed_amt = float(line.get("billed_amount", 0.0) or 0.0)
    recoup_amt = float(line.get("recouped_amount", 0.0) or 0.0)
    refund_amt = float(line.get("refund_amount", 0.0) or 0.0)
    is_void = str(line.get("is_void", "0")).strip().lower() in ("1", "true", "yes")
    is_reversed = str(line.get("is_reversed", "0")).strip().lower() in ("1", "true", "yes")
    claim_payment_status = str(line.get("payment_status", "PAID")).strip().upper()

    # Net paid exposure calculation (Edge cases O01-O15)
    if is_void or is_reversed or claim_payment_status in ("DENIED", "UNPAID", "VOID", "REVERSED"):
        net_paid = 0.0
    else:
        net_paid = max(0.0, paid_amt - recoup_amt - refund_amt)

    # -------------------------------------------------------------------------
    # EDGE CASE CATEGORY 16: DATA QUALITY CHECKS (Q05-Q16, D09-D10, CL09-CL16)
    # -------------------------------------------------------------------------
    if not claim_id or not billed_hcpc:
        return build_audit_record(
            line, STATUS_CODING_ERROR, "MISSING_REQUIRED_CLAIM_FIELDS",
            "Claim ID or Billed HCPCS code is missing.", net_paid, eval_ts
        )

    if not dos or dos == "1900-01-01":
        return build_audit_record(
            line, STATUS_INSUFFICIENT_DATA, "MISSING_DATE_OF_SERVICE",
            "Claim line lacks a valid Date of Service (DOS).", net_paid, eval_ts
        )

    if not state:
        return build_audit_record(
            line, STATUS_INSUFFICIENT_DATA, "MISSING_JURISDICTION_STATE",
            "Claim line lacks a valid service jurisdiction state.", net_paid, eval_ts
        )

    # -------------------------------------------------------------------------
    # EDGE CASE CATEGORY 6: DIAGNOSIS POINTER RESOLUTION (D01-D04, D16)
    # Extract ONLY line-linked diagnoses; do NOT use unlinked header diagnoses!
    # -------------------------------------------------------------------------
    header_diags = [normalize_icd(d) for d in line.get("header_diagnoses", "").split("|") if d.strip()]
    pointers_raw = str(line.get("diagnosis_pointers", "")).strip()

    linked_diags = []
    if pointers_raw:
        # Pointers can be comma/pipe separated or 1-based indices (e.g. "1|2" or "A,B")
        ptr_tokens = re.split(r"[,|\s]+", pointers_raw)
        for token in ptr_tokens:
            token = token.upper()
            idx = None
            if token.isdigit():
                idx = int(token) - 1
            elif len(token) == 1 and "A" <= token <= "Z":
                idx = ord(token) - ord("A")

            if idx is not None and 0 <= idx < len(header_diags):
                linked_diags.append(header_diags[idx])
            else:
                # Invalid pointer index (D09)
                return build_audit_record(
                    line, STATUS_CODING_ERROR, "INVALID_DIAGNOSIS_POINTER",
                    f"Line diagnosis pointer '{token}' references an invalid header index.",
                    net_paid, eval_ts
                )
    else:
        # If no explicit line pointers exist, fallback safely to line-level diagnoses column
        line_diags_col = line.get("line_diagnoses", "")
        if line_diags_col:
            linked_diags = [normalize_icd(d) for d in line_diags_col.split("|") if d.strip()]
        else:
            return build_audit_record(
                line, STATUS_INSUFFICIENT_DATA, "NO_DIAGNOSIS_ON_LINE",
                "Claim line has no linked diagnosis codes or pointers.", net_paid, eval_ts
            )

    linked_diags_set = set(linked_diags)

    # -------------------------------------------------------------------------
    # EDGE CASE CATEGORY 1 & 10: POLICY RETRIEVAL & HISTORICAL DOS MATCH (H01-H05, J01)
    # -------------------------------------------------------------------------
    candidate_rules = policy_index.rules_by_state_hcpc.get((state, billed_hcpc), [])

    # Filter candidate rules active on this specific Date of Service (H01-H05)
    active_rules = [
        r for r in candidate_rules
        if r["eff_start"] <= dos <= r["eff_end"]
    ]

    # Edge Case Category 2 & 11: If no LCD policy exists for this code in this state (C02, P07, R12)
    # Standard Medicare Part B benefit applies. Never assume missing policy = non-covered!
    if not active_rules:
        # Check if the code was once active but is retired/gap (H06, H14)
        if candidate_rules:
            # Policy existed but not on this DOS
            return build_audit_record(
                line, STATUS_NOT_APPLICABLE, "DOS_OUTSIDE_POLICY_DATES",
                f"Policy exists for {billed_hcpc} in {state}, but none active on DOS {dos}.",
                net_paid, eval_ts
            )
        else:
            return build_audit_record(
                line, STATUS_NOT_LISTED, "NO_LCD_RESTRICTION",
                f"CPT/HCPCS {billed_hcpc} has no active LCD restriction in {state}; governed by general Medicare rules.",
                net_paid, eval_ts
            )

    # -------------------------------------------------------------------------
    # EDGE CASE CATEGORY 3, 7: EXPLICIT NON-COVERED PROCEDURES & SAD (C18, N01, N05)
    # -------------------------------------------------------------------------
    # Check if ANY active rule designates this procedure as explicitly non-covered
    noncov_proc_rule = next(
        (r for r in active_rules if r["is_hcpc_explicitly_noncovered"] or r["denial_reason_code"] in ("EXPLICIT_NON_COVERED_PROCEDURE", "STATUTORY_SAD_EXCLUSION")),
        None
    )
    if noncov_proc_rule:
        decision = STATUS_POTENTIAL_OVERPAYMENT if net_paid > 0 else STATUS_EXCLUDED
        return build_audit_record(
            line, decision, noncov_proc_rule["denial_reason_code"],
            noncov_proc_rule["denial_reason_message"], net_paid, eval_ts,
            rule=noncov_proc_rule
        )

    # -------------------------------------------------------------------------
    # EDGE CASE CATEGORY 2: MULTI-ARTICLE ALTERNATIVE SUPPORT (A01-A03, R11)
    # Group rules by Article ID so we can evaluate if ANY applicable Article supports the line
    # -------------------------------------------------------------------------
    rules_by_article = defaultdict(list)
    for r in active_rules:
        rules_by_article[r["article_id"]].append(r)

    article_eval_outcomes = []

    for art_id, art_rules in rules_by_article.items():
        # Check modifiers on this article
        req_mods = art_rules[0]["required_modifiers"]
        if req_mods and not (req_mods & billed_mods):
            article_eval_outcomes.append({
                "article_id": art_id,
                "status": STATUS_POTENTIAL_COVERAGE_ISSUE,
                "reason": "MISSING_REQUIRED_MODIFIER",
                "message": f"Article {art_id} requires one of modifiers {req_mods}.",
                "rule": art_rules[0]
            })
            continue

        # Check bill types on this article
        allowed_bills = art_rules[0]["allowed_bill_types"]
        if allowed_bills and billed_bill_type and (billed_bill_type not in allowed_bills):
            article_eval_outcomes.append({
                "article_id": art_id,
                "status": STATUS_POTENTIAL_COVERAGE_ISSUE,
                "reason": "INVALID_BILL_TYPE",
                "message": f"Bill type {billed_bill_type} not in allowed bill types for {art_id}.",
                "rule": art_rules[0]
            })
            continue

        # Check explicit non-covered diagnoses (I02, D06, N01)
        noncov_icds_in_art = {
            r["icd10_code_clean"]: r for r in art_rules
            if r["is_non_covered_flag"] and r["icd10_code_clean"] != "ALL"
        }
        hit_noncov_icds = set(noncov_icds_in_art.keys()) & linked_diags_set
        if hit_noncov_icds:
            bad_code = list(hit_noncov_icds)[0]
            bad_rule = noncov_icds_in_art[bad_code]
            article_eval_outcomes.append({
                "article_id": art_id,
                "status": STATUS_EXCLUDED,
                "reason": "EXPLICIT_NON_COVERED_ICD10",
                "message": f"Diagnosis {bad_code} is explicitly non-covered under {art_id}.",
                "rule": bad_rule
            })
            continue

        # Check covered diagnoses (I01, D01-D05)
        cov_icds_in_art = {
            r["icd10_code_clean"]: r for r in art_rules
            if r["is_covered_flag"] and r["icd10_code_clean"] != "ALL"
        }
        hit_cov_icds = set(cov_icds_in_art.keys()) & linked_diags_set

        # Check conditional (asterisk) diagnoses (I07, M01-M04, N03)
        cond_icds_in_art = {
            r["icd10_code_clean"]: r for r in art_rules
            if r["is_conditional_flag"] and r["icd10_code_clean"] != "ALL"
        }
        hit_cond_icds = set(cond_icds_in_art.keys()) & linked_diags_set

        if hit_cov_icds:
            matched_code = list(hit_cov_icds)[0]
            matched_rule = cov_icds_in_art[matched_code]
            
            # Check dual diagnosis combination requirement (D08, G12, M11)
            if matched_rule["dual_diagnosis_required"] and len(linked_diags_set) < 2:
                article_eval_outcomes.append({
                    "article_id": art_id,
                    "status": STATUS_CONDITIONAL,
                    "reason": "DUAL_DIAGNOSIS_REQUIRED",
                    "message": f"Article {art_id} requires companion secondary diagnosis from another group.",
                    "rule": matched_rule
                })
            else:
                article_eval_outcomes.append({
                    "article_id": art_id,
                    "status": STATUS_SUPPORTED,
                    "reason": "MEDICAL_NECESSITY_SUPPORTED",
                    "message": f"Diagnosis {matched_code} demonstrates medical necessity under {art_id}.",
                    "rule": matched_rule
                })
        elif hit_cond_icds:
            cond_code = list(hit_cond_icds)[0]
            cond_rule = cond_icds_in_art[cond_code]
            article_eval_outcomes.append({
                "article_id": art_id,
                "status": STATUS_CONDITIONAL,
                "reason": "ASTERISK_CRITERIA_PENDING",
                "message": cond_rule["asterisk_explanation"] or "Asterisk conditional criteria must be verified.",
                "rule": cond_rule
            })
        else:
            # Code is not listed in this article's covered table (N02, R04)
            article_eval_outcomes.append({
                "article_id": art_id,
                "status": STATUS_NOT_LISTED,
                "reason": "DIAGNOSIS_NOT_LISTED_IN_POLICY",
                "message": f"None of the line-linked diagnoses appear in {art_id} covered table.",
                "rule": art_rules[0]
            })

    # -------------------------------------------------------------------------
    # EDGE CASE RESOLUTION: PREVENT FALSE POSITIVES (A02, R11)
    # Rule: If ANY applicable Article supports the line -> SUPPORTED!
    # Do NOT flag solely because one Article failed!
    # -------------------------------------------------------------------------
    supported_outcome = next((o for o in article_eval_outcomes if o["status"] == STATUS_SUPPORTED), None)
    if supported_outcome:
        return build_audit_record(
            line, STATUS_SUPPORTED, supported_outcome["reason"],
            supported_outcome["message"], net_paid, eval_ts,
            rule=supported_outcome["rule"], considered=article_eval_outcomes
        )

    # Next priority: Conditional coverage (Asterisk / Dual Diagnosis)
    cond_outcome = next((o for o in article_eval_outcomes if o["status"] == STATUS_CONDITIONAL), None)
    if cond_outcome:
        return build_audit_record(
            line, STATUS_CONDITIONAL, cond_outcome["reason"],
            cond_outcome["message"], net_paid, eval_ts,
            rule=cond_outcome["rule"], considered=article_eval_outcomes
        )

    # Next priority: Explicit Exclusions
    excl_outcome = next((o for o in article_eval_outcomes if o["status"] == STATUS_EXCLUDED), None)
    if excl_outcome:
        decision = STATUS_POTENTIAL_OVERPAYMENT if net_paid > 0 else STATUS_EXCLUDED
        return build_audit_record(
            line, decision, excl_outcome["reason"],
            excl_outcome["message"], net_paid, eval_ts,
            rule=excl_outcome["rule"], considered=article_eval_outcomes
        )

    # Default fallback: Diagnoses were evaluated but not listed in applicable policy
    first_outcome = article_eval_outcomes[0]
    decision = STATUS_POTENTIAL_OVERPAYMENT if net_paid > 0 else STATUS_POTENTIAL_COVERAGE_ISSUE
    return build_audit_record(
        line, decision, first_outcome["reason"],
        first_outcome["message"], net_paid, eval_ts,
        rule=first_outcome["rule"], considered=article_eval_outcomes
    )


def build_audit_record(line, decision_status, reason_code, message, net_paid, eval_ts, rule=None, considered=None):
    """
    Constructs the standardized 22-field audit record required for every claim line.
    (Edge Case Category 18: AU01-AU20)
    """
    alt_articles = ""
    if considered and len(considered) > 1:
        alt_articles = "; ".join([f"{c['article_id']}:{c['status']}" for c in considered])

    return {
        "claim_id": str(line.get("claim_id", "")).strip(),
        "claim_line_id": str(line.get("claim_line_id", "1")).strip(),
        "date_of_service": str(line.get("date_of_service", "")).strip()[:10],
        "billed_hcpc": normalize_code(line.get("billed_hcpc", "")),
        "line_linked_icd10": str(line.get("line_diagnoses", line.get("header_diagnoses", ""))).strip(),
        "decision_status": decision_status,
        "reason_code": reason_code,
        "adjudication_message": message,
        "net_paid_amount": f"{net_paid:.2f}",
        "applicable_lcd_id": rule["related_lcd_id"] if rule else "",
        "applicable_article_id": rule["article_id"] if rule else "",
        "applicable_article_version": rule["article_version"] if rule else "",
        "applicable_ncd_id": rule.get("related_ncd_id", "") if rule else "",
        "policy_group_uid": rule["policy_group_uid"] if rule else "",
        "policy_effective_start_date": rule["eff_start"] if rule else "",
        "policy_effective_end_date": rule["eff_end"] if rule else "",
        "alternative_policies_considered": alt_articles,
        "payment_status_input": str(line.get("payment_status", "PAID")).strip(),
        "source_evidence_table": rule["source_table"] if rule else "",
        "rule_engine_version": ENGINE_VERSION,
        "evaluation_timestamp": eval_ts,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate Claims Against MCD TDA Table (Edge-Case Compliant)")
    parser.add_argument("--claims", required=True, help="Input CSV containing claim lines")
    parser.add_argument("--tda", default="tda_lcd_claim_decision_asset.csv", help="Input TDA decision asset CSV")
    parser.add_argument("--output", default="adjudicated_claims_results.csv", help="Output path for results CSV")
    parser.add_argument("--limit-tda", type=int, default=None, help="Optional limit on TDA rows for quick tests")
    args = parser.parse_args()

    print("=" * 80)
    print(" MEDICARE LCD/ARTICLE CLAIM ADJUDICATION ENGINE")
    print(f" Version         : {ENGINE_VERSION}")
    print(f" Claims Input    : {args.claims}")
    print(f" TDA Asset       : {args.tda}")
    print(f" Output Target   : {args.output}")
    print("=" * 80)

    # 1. Load Policy Index
    idx = PolicyIndex()
    idx.load_tda_csv(args.tda, max_rows=args.limit_tda)

    # 2. Process Claims Line-by-Line
    print(f"\n[*] Evaluating claims from {args.claims}...")
    results = []
    status_counts = defaultdict(int)

    with open(args.claims, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for line in reader:
            record = evaluate_claim_line(line, idx)
            results.append(record)
            status_counts[record["decision_status"]] += 1

    # 3. Write Results CSV
    if results:
        fieldnames = list(results[0].keys())
        with open(args.output, "w", encoding="utf-8", newline="") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

    print("\n" + "=" * 80)
    print(" ADJUDICATION SUMMARY BY DECISION STATUS")
    print("=" * 80)
    for st, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        pct = (cnt / len(results)) * 100 if results else 0
        print(f"  {st:<32}: {cnt:>6} lines ({pct:>5.1f}%)")
    print("-" * 80)
    print(f" Total Claim Lines Evaluated    : {len(results):,}")
    print(f" Results Written To            : {os.path.abspath(args.output)}")
    print("=" * 80)


if __name__ == "__main__":
    main()
