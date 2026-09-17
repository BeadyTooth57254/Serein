import json

import pytest
from fastapi.testclient import TestClient

from serein.api.arc_pages import page, PAGE_CHARS, dump, size
from serein.api.read_text import arc_materials_text
from serein.api.http import create_app
from serein.application import Application
from serein.config import Settings
from serein.core import Store
from serein.core.store import Conflict
from test_gateway_contract import arc_upload_database


@pytest.mark.parametrize('by_key', [True, False])
def test_actual_mcp_pages_preserve_complete_materials_and_fit_client(arc_upload_database, by_key):
    settings = Settings(arc_upload_database)
    services = Application(settings).services
    args = {'arc_key': 'work:synthetic', 'picks': [0, 9]} if by_key else {
        'identifier': 'narrative_uploads', 'offset': 0, 'limit': 100, 'with_evidence': True}
    expected = services.arc_picks(args['arc_key'], args['picks'], with_evidence=True) if by_key else services.materials(
        args['identifier'], offset=0, limit=100, with_evidence=True)
    encoded, cursor, seen = '', '', set()
    with TestClient(create_app(settings, token='synthetic'), headers={
        'Authorization': 'Bearer synthetic', 'Accept': 'application/json, text/event-stream'}) as client:
        for _ in range(200):
            response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'read_arc_materials', 'arguments': {**args, 'cursor': cursor}}})
            result = response.json()['result']
            assert not result['isError'], result
            assert result.get('structuredContent') is None
            assert size(dump(result)) <= PAGE_CHARS
            payload = result['content'][0]['text']
            android = dump({k: v for k, v in result.items() if k != 'content'}) + '\n\n' + payload
            assert size(android) < 5000
            assert payload.startswith('[text_page]\n') and payload.endswith('\n[/text_page]')
            header, content = payload[len('[text_page]\n'):].split('\ntext:\n', 1)
            content = content[:-len('\n[/text_page]')]
            fields = dict(line.split(': ', 1) for line in header.splitlines())
            assert int(fields['content_offset']) == len(encoded)
            assert content
            encoded += content
            if fields['content_complete'] == 'true':
                assert not fields['next_cursor']
                break
            cursor = fields['next_cursor']
            assert cursor not in seen
            seen.add(cursor)
        else:
            pytest.fail('Arc pagination did not finish')
    assert encoded == arc_materials_text(expected, with_evidence=bool(args.get('with_evidence')))
    # Pagination neither edits canonical content nor truncates direct service reads.
    assert services.arc_picks('work:synthetic', [9])['items'][0]['object']['document']['body_md'].endswith('\\')


def test_small_result_and_independent_material_list_offset(arc_upload_database):
    services = Application(Settings(arc_upload_database)).services
    first = services.materials('narrative_uploads', offset=0, limit=1)
    result = json.loads(page(first, ['narrative_uploads', 0, 1]))
    assert result['page_format'] == 'arc_materials' and not result['has_more']
    assert result['next_cursor'] is None and result['next_offset'] == 1
    assert result['items'] == first['items']


@pytest.mark.parametrize('change', ['upload', 'excluded', 'narrative', 'deleted'])
def test_continuation_rechecks_current_data_and_access(arc_upload_database, change):
    services = Application(Settings(arc_upload_database)).services
    filters = ['narrative_uploads']
    original = services.materials('narrative_uploads')
    cursor = json.loads(page(original, filters))['next_cursor']
    assert cursor
    with Store(arc_upload_database) as store:
        if change == 'upload':
            store.conn.execute("UPDATE narrative_uploads SET metadata_json=json_set(metadata_json,'$.extracted_text',?) "
                               "WHERE id='upload_selected'", ('replacement text',))
        elif change == 'excluded':
            store.conn.execute("INSERT INTO narrative_materials VALUES "
                               "('narrative_uploads',1,'removed','upload','upload_selected','excluded','{}')")
        elif change == 'narrative':
            store.revise('narrative_uploads', expected_revision=1, title='changed', body_md='changed')
        else:
            store.set_lifecycle('narrative_uploads', 'deleted')
    with pytest.raises((Conflict, ValueError)):
        page(services.materials('narrative_uploads'), filters, cursor)


def test_cursor_rejects_changed_selectors_and_malformed_values():
    original = {'items': [{'id': 'synthetic', 'body': '😀"\n\\' * 9000}]}
    cursor = json.loads(page(original, ['arc-a', [9]]))['next_cursor']
    with pytest.raises(Conflict):
        page(original, ['arc-b', [9]], cursor)
    for invalid in ('wrong', 'arc1.@@@@', 'arc1.W10', 'arc1.' + 'a' * 201):
        with pytest.raises(ValueError):
            page(original, ['arc-a', [9]], invalid)
