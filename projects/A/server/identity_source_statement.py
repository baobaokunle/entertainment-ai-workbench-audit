"""Local dataset declaration only; never account or conversation authentication."""
from .identity_candidate_files import pinned_bytes, strict_json

FORMAT = 'identity-export-source-statement-1'
FIELD = 'historical_export_source_statement'


def load_export_source_statement(path, expected_sha256, *, account_key,
                                 provider_namespace, artifact_sha256):
    """Read one host-selected, pinned metadata file; failure means unknown.

    The artifact digest binds the declaration to a dataset, not a platform
    identity. No names, owner IDs, bodies or authority flags are accepted.
    """
    try:
        value = strict_json(pinned_bytes(path, expected_sha256, maximum=2048))
        expected = {'format': FORMAT, 'account_key': account_key,
                    'provider_namespace': provider_namespace,
                    'artifact_sha256': artifact_sha256, FIELD: 'user_confirmed'}
        if type(value) is dict and value == expected:
            return 'user_confirmed'
    except Exception:
        pass
    return 'unknown'
