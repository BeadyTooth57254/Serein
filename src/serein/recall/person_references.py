"""Resolve user-side person references for relevance scoring, not source text."""

import re


_QUOTED = re.compile(r'''(“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"]*"|'[^']*'|`+[^`]*`+)''')
# Long forms first; do not split plurals or common non-pronoun words.
_PERSON_REFERENCE = re.compile(r'(?<![自忘])我们|咱们|(?<!迷)你(?!们)|您(?!们)|(?<![自忘])我(?!们)')
_PLACEHOLDERS = frozenset({'', 'AI', 'User', '用户'})


def resolve_person_references(query, identity):
    """The query is a user utterance; quoted words keep their own perspective."""
    text = str(query or '').strip()
    names = identity or {}
    assistant = str(names.get('ai_name') or '').strip()
    user = next((name for key in ('user_display_name', 'user_name')
                 if (name := str(names.get(key) or '').strip()) not in _PLACEHOLDERS), '')
    replacements = {}
    if assistant not in _PLACEHOLDERS:
        replacements.update({'你': assistant, '您': assistant})
    if user not in _PLACEHOLDERS:
        replacements['我'] = user
    if assistant not in _PLACEHOLDERS and user not in _PLACEHOLDERS:
        replacements.update(dict.fromkeys(('我们', '咱们'), user + '和' + assistant))
    parts = _QUOTED.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _PERSON_REFERENCE.sub(lambda match: replacements.get(match[0], match[0]), parts[index])
    return ''.join(parts)
