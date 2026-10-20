"""Bounded quality summaries. Only fixed categories/counts cross the host boundary.

DPAPI decrypts a whole saved observation in local memory; this module never reads
body/identity scalar values and never includes source locators or field values.
"""
from .contract import DOMAINS

CATEGORIES = ('data', 'empty_response', 'no_fields', 'diagnostic', 'account')
ISSUES = frozenset('''SOURCE_PERMISSION SOURCE_HTTP SOURCE_READ_FAILED SOURCE_TIMEOUT
SOURCE_REJECTED SOURCE_SHAPE SOURCE_SIZE SOURCE_CONTENT_TYPE OWNER_UNAVAILABLE
CHAT_SDK_UNAVAILABLE CHAT_HISTORY_UNAVAILABLE CHAT_CURSOR_NOT_ADVANCING
CHAT_LIST_END_UNCONFIRMED CHAT_LIST_LOAD_STALLED CHAT_SCROLL_CONTAINER_UNAVAILABLE
CHAT_HISTORY_END_UNKNOWN CHAT_HISTORY_LOAD_STALLED CHAT_HISTORY_SOURCE_NO_MORE CHAT_COVERAGE_PARTIAL
CHAT_HISTORY_BUDGET_REACHED SESSION_BINDING_UNVERIFIED MESSAGE_CONVERSATION_BINDING_UNVERIFIED
SESSION_TIME_UNAVAILABLE FIELDS_NOT_SCALAR_OR_TOO_LARGE SHORT_TEST_BROADCAST_SKIPPED
BACKEND_STOPPED_AFTER_PERMISSION_FAILURE HTTP_401 HTTP_403
EMPTY_RESPONSE_NOT_COMPLETE NO_ALLOWLISTED_FIELDS'''.split())

def bucket():
    return {k: 0 for k in CATEGORIES}

def project(observation):
    domain = observation.get('domain')
    domain = domain if domain in DOMAINS else 'unknown'
    issues = observation.get('issues', [])
    fields = observation.get('fields', {})
    if domain == 'capability':
        # This field contains a declared data domain, never a user identifier.
        value = fields.get('domain', {}).get('value')
        domain = value if value in DOMAINS | {'backend'} else 'unknown'
        category = 'diagnostic'
    elif domain == 'account':
        category = 'account'
    elif 'EMPTY_RESPONSE_NOT_COMPLETE' in issues:
        category = 'empty_response'
    elif 'NO_ALLOWLISTED_FIELDS' in issues or not any(
        not k.startswith(('b_projection.', 'b_observer.', 'b_request.')) for k in fields
    ):
        category = 'no_fields'
    else:
        category = 'data'
    reasons = sorted(set(issues) & ISSUES)
    if category == 'diagnostic' and not reasons:
        reasons = ['OTHER_DIAGNOSTIC']
    if category == 'no_fields' and 'NO_ALLOWLISTED_FIELDS' not in reasons:
        reasons.append('NO_ALLOWLISTED_FIELDS')
    return domain, category, reasons

def add(summary, observation):
    domain, category, reasons = project(observation)
    summary['counts'][category] += 1
    row = summary['domains'].setdefault(domain, {'counts': bucket(), 'reasons': {}})
    row['counts'][category] += 1
    for reason in reasons:
        row['reasons'][reason] = row['reasons'].get(reason, 0) + 1
