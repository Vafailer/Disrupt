import pytest
from sqlalchemy import select

from app.models import User
from app.policy import POLICY_VERSION
from app.security import hash_password
from tests.conftest import register


@pytest.mark.parametrize('username', ['Марк', 'MarkМ', 'Ёжик', 'ab', 'a' * 65, 'a b', 'abc\n', 'abc ', ' abc', 'café', 'abc@'])
@pytest.mark.parametrize('action', ['register', 'login'])
def test_invalid_username_rejected_by_api(client, username, action):
    response = client.post(f'/api/v1/auth/{action}', json={
        'username': username, 'password': 'test-only-password-123',
    })
    assert response.status_code == 422
    assert 'test-only-password-123' not in response.text
    assert client.get('/api/v1/auth/me').status_code == 401


def test_username_case_does_not_create_second_account(client):
    first = register(client, 'User.Name_123-X')
    duplicate = client.post('/api/v1/auth/register', json={
        'username': 'USER.NAME_123-X', 'password': 'test-only-password-123',
        'accept_policy': True, 'policy_version': POLICY_VERSION, 'confirm_age': True,
    })
    assert duplicate.status_code == 409
    assert client.get('/api/v1/auth/me').json()['id'] == first['id']


def test_existing_cyrillic_account_not_renamed(app, client):
    with app.state.sessions() as db:
        user = User(username='марк', password_hash=hash_password('test-only-password-123'))
        db.add(user)
        db.commit()
        user_id = user.id
    assert client.post('/api/v1/auth/login', json={
        'username': 'марк', 'password': 'test-only-password-123',
    }).status_code == 422
    with app.state.sessions() as db:
        assert db.scalar(select(User).where(User.id == user_id)).username == 'марк'
