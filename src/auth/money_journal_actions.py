"""The lines of the journal of money that the services WRITE and the reconciliation (``money_reconcile_rows``) READS.  A neutral module: the product imports the names from here and never the check."""
from __future__ import annotations

PAYOUT_CREATED = "payroll_payout_created"
PAYOUT_VOIDED = "payroll_payout_voided"
ACCEPTED = "money_reconcile_accept"
REBASELINED = "money_reconcile_rebaseline"
POLICY_CHANGED = "policy_changed"                 # LESSON-RULES С0: a rule of the centre (``centre_policy``) changed - the page of the lessons shows it, not the page of the payments
TRACE_ACTIONS = (PAYOUT_CREATED, PAYOUT_VOIDED, ACCEPTED, REBASELINED, POLICY_CHANGED)        # lines of ``org_money_audit`` that are not about the settings of the centre (the settings page does not show them)
