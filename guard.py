"""
TaskBound guard v1

Sits between an AI agent and its tools. Every tool call is checked before it runs.
Layer 1 verifies the requester. Layer 2 verifies the action.
Low risk runs automatically. High risk goes to a human.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

import yaml

# Signs approval tokens. A real deployment would use a key from the company's key vault.
SIGNING_KEY = secrets.token_bytes(32)


def load_policy(path="policy.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class Guard:
    def __init__(self, task, inbox, customers, policy=None):
        self.policy = policy or load_policy()
        self.task = task
        self.inbox = inbox
        self.customers = customers
        self.tainted = False                      # becomes True once sensitive data is read
        self.session_id = "sess_" + secrets.token_hex(4)
        self.profile_name, self.profile = self._match_profile(task)

    # ---------- Task scoping and context minimisation ----------

    def _match_profile(self, task):
        text = task.lower()
        profiles = self.policy["task_profiles"]
        for name, profile in profiles.items():
            if name != "default" and any(word in text for word in profile.get("match", [])):
                return name, profile
        return "default", profiles["default"]

    def allowed_tools(self, all_tools):
        if not self.policy.get("context_minimisation", False):
            return all_tools
        expected = self.profile["expected_tools"]
        return [t for t in all_tools if t["name"] in expected]

    # ---------- Domain trust ----------

    @staticmethod
    def _domain(address):
        return address.split("@")[-1].strip().lower() if "@" in address else ""

    def _trust(self, domain):
        company = self.policy["company"]
        if domain in company["internal_domains"]:
            return "internal"
        if domain in company.get("approved_vendors", []):
            return "approved_vendor"
        threshold = self.policy["lookalike"]["similarity_threshold"]
        for real in company["internal_domains"]:
            name = real.split(".")[0]
            if name in domain or SequenceMatcher(None, domain, real).ratio() >= threshold:
                return "lookalike"
        return "external"

    # ---------- Layer 1: verify the requester ----------

    def _find_source(self, destination):
        """Work out which email asked for this action."""
        dest = destination.lower()
        for email in self.inbox:
            if dest in email.get("body", "").lower() or dest == email.get("from", "").lower():
                return email
        return None

    def _requester(self, source, destination):
        if source is None:
            if destination and destination.lower() in self.task.lower():
                return {"claimed": "user", "verified": True, "role": "user", "flags": []}
            return {"claimed": "unknown", "verified": False, "role": None,
                    "flags": ["unattributed_request"]}

        sender = source["from"].lower()
        trust = self._trust(self._domain(sender))
        entry = self.policy["directory"].get(sender)
        flags = []
        if trust == "lookalike":
            flags.append("lookalike_sender")
        elif trust == "internal" and entry is None:
            flags.append("sender_not_in_directory")
        verified = trust == "internal" and entry is not None
        if not verified:
            flags.append("unverified_requester")
        if re.search(r"\bapprov", source.get("body", ""), re.IGNORECASE):
            flags.append("unverified_approval_claim")
        return {"claimed": sender, "verified": verified,
                "role": entry["role"] if entry else None, "flags": flags}

    def _role_allows_sharing(self, requester, trust):
        role = requester.get("role")
        allowed = self.policy["roles"].get(role, {}).get("can_request", []) if role else []
        return (requester["verified"] and trust == "approved_vendor"
                and "share_customer_data_with_approved_vendors" in allowed)

    # ---------- Layer 2: verify the action ----------

    def _customer_hits(self, text):
        """Find customer details inside outgoing content."""
        text = text.lower()
        hits = []
        for customer in self.customers:
            fields = [key for key, value in customer.items() if str(value).lower() in text]
            if fields:
                hits.append(fields)
        return hits

    # ---------- The decision ----------

    def check(self, tool, tool_input):
        if tool in self.policy["sensitive_sources"]:
            self.tainted = True

        reasons = []
        expected = tool in self.profile["expected_tools"]
        if not expected:
            reasons.append("unexpected_tool_for_task")

        # Tools that do not move data out are never the leak: allow, but log if out of scope
        if tool not in self.policy["egress_tools"]:
            return self._finish(tool, tool_input, "allow", reasons, details=None)

        destination = tool_input.get("to", "")
        content = f'{tool_input.get("subject", "")} {tool_input.get("body", "")}'
        trust = self._trust(self._domain(destination))
        source = self._find_source(destination)
        requester = self._requester(source, destination)
        hits = self._customer_hits(content)

        reasons += requester["flags"]
        if trust == "lookalike":
            reasons.append("lookalike_destination")
        if trust != "internal":
            if hits:
                reasons.append("customer_data_external")
            elif self.tainted:
                reasons.append("sensitive_session_external_send")

        sensitive_exit = ("customer_data_external" in reasons
                          or "sensitive_session_external_send" in reasons)

        if "lookalike_destination" in reasons or "lookalike_sender" in reasons:
            decision = "verify"
        elif sensitive_exit and not self._role_allows_sharing(requester, trust):
            decision = "escalate"
        else:
            decision = "allow"

        details = {
            "destination": {"address": destination, "trust": trust},
            "requester": {k: requester[k] for k in ("claimed", "verified", "role")},
            "data": {
                "customer_records": len(hits),
                "fields": sorted({field for hit in hits for field in hit}),
                "fingerprint": "sha256:" + hashlib.sha256(content.encode()).hexdigest()[:16],
            },
        }
        return self._finish(tool, tool_input, decision, reasons, details)

    def _finish(self, tool, tool_input, decision, reasons, details):
        approver = None
        if self.policy.get("mode", "enforce") != "enforce":
            executed = True                       # monitor mode: record, never block
        elif decision == "allow":
            executed = True
        else:
            approved, token_id = self._ask_human(tool, tool_input)
            executed = approved
            approver = {"result": "approved" if approved else "declined", "token_id": token_id}

        if details is not None or decision != "allow":
            self._log(tool, decision, reasons, details, approver, executed)

        return {"decision": decision, "executed": executed,
                "message": None if executed else self._block_message(decision, reasons)}

    # ---------- Human approval with signed, short-lived tokens ----------

    def _ask_human(self, tool, tool_input):
        # Lab version uses a simulated reviewer. A real deployment would send this to the
        # company's approval channel and confirm the reviewer with MFA through its identity provider.
        if self.policy["approval"]["simulated_user"] != "approve":
            return False, None
        token = self._issue_token(tool, tool_input)
        return self._verify_token(token, tool, tool_input), token["payload"]["id"]

    @staticmethod
    def _action_hash(tool, tool_input):
        return hashlib.sha256(json.dumps([tool, tool_input], sort_keys=True).encode()).hexdigest()

    def _issue_token(self, tool, tool_input):
        payload = {"id": "apr_" + secrets.token_hex(4),
                   "action": self._action_hash(tool, tool_input),
                   "expires": time.time() + self.policy["approval"]["token_lifetime_seconds"]}
        body = json.dumps(payload, sort_keys=True).encode()
        return {"payload": payload,
                "signature": hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest()}

    def _verify_token(self, token, tool, tool_input):
        body = json.dumps(token["payload"], sort_keys=True).encode()
        expected = hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest()
        return (hmac.compare_digest(expected, token["signature"])
                and token["payload"]["expires"] > time.time()
                and token["payload"]["action"] == self._action_hash(tool, tool_input))

    @staticmethod
    def _block_message(decision, reasons):
        if decision == "verify":
            lead = ("BLOCKED by TaskBound: this involves a domain that resembles the company domain "
                    "but is not it. A human must verify the sender through a known channel first.")
        else:
            lead = "BLOCKED by TaskBound: this action was flagged as high risk and needs human approval."
        return (f"{lead} Reasons: {', '.join(reasons)}. The reviewer declined. "
                "Do not retry this by other means. Tell the user what happened.")

    # ---------- Tamper evident audit log ----------

    def _log(self, tool, decision, reasons, details, approver, executed):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": self.session_id,
            "task": self.task,
            "task_profile": self.profile_name,
            "action": tool,
            **(details or {}),
            "reasons": reasons,
            "decision": decision,
            "approver": approver,
            "executed": executed,
            "mode": self.policy.get("mode", "enforce"),
        }
        path = self.policy["logging"]["audit_file"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        previous = "0" * 64
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.read().strip().splitlines()
            if lines:
                previous = json.loads(lines[-1])["hash"]
        entry["prev_hash"] = previous
        entry["hash"] = hashlib.sha256((previous + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")