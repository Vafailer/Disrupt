import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.feedback_models import Feedback
from app.models import User
from tests.conftest import register

PAYLOAD = {'kind': 'bug', 'subject': 'Не сохраняется', 'description': '<script>example</script>',
           'steps': 'Открыл заметку', 'expected': 'Сохранение', 'contact': '@synthetic'}


def send(client, key='case-1', **changes):
    return client.post('/api/v1/feedback', json=PAYLOAD | changes, headers={'Idempotency-Key': key})


def promote(app, user_id):
    with app.state.sessions() as db:
        db.get(User, user_id).role = 'admin'
        db.commit()


def test_feedback_auth_csrf_replay_and_isolation(app, client):
    assert send(client).status_code == 401
    owner = register(client)
    csrf = client.headers.pop('X-CSRF-Token')
    assert send(client).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert client.post('/api/v1/feedback', json=PAYLOAD, headers={
        'Idempotency-Key': 'origin', 'Origin': 'https://other.invalid',
    }).status_code == 403
    receipt = send(client)
    assert receipt.status_code == 201
    assert set(receipt.json()) == {'id'}
    assert send(client).json() == receipt.json()
    assert send(client, description='changed').status_code == 409
    assert client.get('/api/admin/feedback').status_code == 403
    with TestClient(app) as other:
        register(other, 'second')
        second = send(other)
        assert second.status_code == 201
        assert second.json()['id'] != receipt.json()['id']
        assert other.patch('/api/admin/feedback/' + receipt.json()['id'], json={'status':'resolved','version':1}).status_code == 403
    promote(app, owner['id'])
    rows = client.get('/api/admin/feedback').json()
    assert len(rows) == 2
    assert rows[0]['description'] == PAYLOAD['description']
    assert 'payload_hash' not in rows[0] and 'idempotency_key' not in rows[0]
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Feedback)) == 2


def test_feedback_status_versions_pagination_and_revocation(app, client):
    account = register(client)
    promote(app, account['id'])
    ids = [send(client, str(i)).json()['id'] for i in range(3)]
    first = client.get('/api/admin/feedback?limit=2')
    assert len(first.json()) == 2
    assert first.headers['X-Next-Feedback-Offset'] == '2'
    assert first.headers['Cache-Control'] == 'no-store'
    second = client.get('/api/admin/feedback?limit=2&offset=2')
    assert len(second.json()) == 1 and 'X-Next-Feedback-Offset' not in second.headers
    path = '/api/admin/feedback/' + ids[0]
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.patch(path, json={'status':'resolved','version':1}).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    updated = client.patch(path, json={'status':'resolved','version':1})
    assert updated.status_code == 200 and updated.json()['version'] == 2
    assert client.patch(path, json={'status':'in_progress','version':1}).status_code == 409
    assert len(client.get('/api/admin/feedback').json()) == 2
    assert len(client.get('/api/admin/feedback?status=resolved').json()) == 1
    assert len(client.get('/api/admin/feedback?status=all').json()) == 3
    with app.state.sessions() as db:
        db.get(User, account['id']).role = 'user'
        db.commit()
    assert client.get('/api/admin/feedback').status_code == 403
    assert client.patch(path, json={'status':'new','version':2}).status_code == 403


@pytest.mark.parametrize('changes', [{'subject':' '}, {'description':''}, {'kind':'invalid'},
                                    {'contact':'bad\x00text'}, {'steps':'bad\x00text'}, {'expected':'bad\x00text'}, {'contact':'x' * 201}, {'steps':'x' * 3001}, {'description':'x' * 5001}])
def test_feedback_limits(client, changes):
    register(client)
    assert send(client, **changes).status_code == 422


def test_feedback_rate_limit(client):
    register(client)
    for index in range(5):
        assert send(client, str(index)).status_code == 201
    assert send(client, '6').status_code == 429
    assert send(client, '0').status_code == 201


def test_feedback_postgresql_concurrent_replay(monkeypatch):
    import os
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url

    from app.config import Settings
    from app.main import create_app

    url = os.environ.get('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Local PostgreSQL is not configured')
    assert make_url(url).host in {'localhost', '127.0.0.1', '::1'}
    monkeypatch.setenv('NOTES_DATABASE_URL', url)
    monkeypatch.setenv('NOTES_PROVIDER', 'mock')
    monkeypatch.setenv('NOTES_ALLOW_LIVE_REQUESTS', 'false')
    command.upgrade(Config('alembic.ini'), 'head')
    application = create_app(Settings(database_url=url, auto_worker=False))
    try:
        with TestClient(application) as client:
            account = register(client, 'feedback_' + uuid.uuid4().hex)
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(lambda _: send(client), range(2)))
            assert [reply.status_code for reply in replies] == [201, 201]
            assert replies[0].json() == replies[1].json()
            with application.state.sessions() as db:
                assert db.scalar(select(func.count()).select_from(Feedback).where(Feedback.user_id == account['id'])) == 1
            promote(application, account['id'])
            response = client.patch('/api/admin/feedback/' + replies[0].json()['id'],
                                    json={'status': 'resolved', 'version': 1})
            assert response.status_code == 200
            assert response.json()['version'] == 2
    finally:
        application.state.engine.dispose()
