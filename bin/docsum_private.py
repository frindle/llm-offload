"""The one definition of 'is this queue label a private job': rows whose text must never reach a consumer that feeds
Claude or a tool-using agent (handoff lists, auto-clear digests, follow-ups, escalation findings).

  docsum-<8 hex>               doc-summary jobs (docsum_cli.prepare_job job ids are secrets.token_hex(4)). Other labels
                               that merely start with docsum- (the UI/CLI builder jobs such as docsum-ui-jobs) are
                               ordinary work and are NOT private.
  prompt-<app>-<12 hex>        prompt jobs from LAN apps (prompt_jobs.create: app is 1-32 of a-z 0-9 -, the id is
                               uuid4 hex[:12]); first user: resell-tracker's backup parser (order texts). 2026-11-16.

is_docsum_label() is the narrower check the doc-summary page uses to list its own rows."""
import re

DOCSUM_LABEL_RE = re.compile(r"docsum-[0-9a-f]{8}")
PRIVATE_LABEL_RE = re.compile(r"(?:docsum-[0-9a-f]{8}|prompt-[a-z0-9][a-z0-9-]{0,31}-[0-9a-f]{12})")


def is_private_label(label):
    return PRIVATE_LABEL_RE.fullmatch(str(label or "")) is not None


def is_docsum_label(label):
    return DOCSUM_LABEL_RE.fullmatch(str(label or "")) is not None
