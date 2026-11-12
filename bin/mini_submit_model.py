"""Model vetting for mini-submit (one shared gate: model_profile.vet_model)."""
import json
import sys
from pathlib import Path


def vet_submit_model(model):
    """Returns (model_to_submit, error_or_None); prints any warning to stderr."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import model_profile as _mp
    home = Path.home()
    try:
        rec = json.loads((home / ".darkbloom" / "local.json").read_text())
    except (OSError, ValueError):
        rec = {}
    files = _mp.served_exact_ids(home / ".config" / "darkbloom" / "provider.toml",
                                  home / ".darkbloom" / "loaded-models.json")
    res, err, warn = _mp.vet_model(model, rec, files)
    if warn:
        print(f"mini-submit: WARNING: {warn}", file=sys.stderr)
    if res and res != model:
        print(f"mini-submit: model {model} -> {res}")
    return (res, err)
