"""The one definition of 'is this queue label a private doc-summary job'. doc-summary builds labels as
docsum-<8 hex chars> (docsum_cli.prepare_job job ids are secrets.token_hex(4)); other labels that merely start with
docsum- (the UI/CLI builder jobs such as docsum-ui-jobs) are ordinary work and are NOT private."""
import re

PRIVATE_LABEL_RE = re.compile(r"docsum-[0-9a-f]{8}")


def is_private_label(label):
    return PRIVATE_LABEL_RE.fullmatch(str(label or "")) is not None
