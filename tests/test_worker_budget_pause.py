from dataclasses import replace

from app.models import Job, ProviderBudget
from app.providers import MockProvider
from app.worker import Worker
from tests.conftest import register


def test_exhausted_total_budget_leaves_queued_originals_untouched(app, client):
    register(client)
    response = client.post('/api/v1/captures/text', json={'text': 'Synthetic pending note'},
                           headers={'Idempotency-Key': 'synthetic-budget-pause'})
    job_id = response.json()['id']
    settings = replace(app.state.settings, provider='cloudru', allow_live_requests=True,
                       cloudru_model='synthetic', live_call_limit=1, live_user_call_limit=1)
    worker = Worker(app.state.sessions, settings, provider=MockProvider())
    with app.state.sessions() as db:
        budget = db.get(ProviderBudget, 'cloudru')
        budget.reserved_calls = 1
        db.commit()
    assert worker.budget_available() is False
    with app.state.sessions() as db:
        assert db.get(Job, job_id).status == 'queued'
        db.get(ProviderBudget, 'cloudru').reserved_calls = 0
        db.commit()
    assert worker.budget_available() is True


def test_mock_processing_does_not_depend_on_live_budget(app):
    worker = Worker(app.state.sessions, app.state.settings, provider=MockProvider())
    assert worker.budget_available() is True
