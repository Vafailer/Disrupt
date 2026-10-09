import json

import httpx
import pytest

from app.providers import CloudRuProvider, ProviderError

RESULT = {"title": "Synthetic", "markdown": "Synthetic note", "conclusions": [], "items": []}


@pytest.mark.parametrize("content", [
    json.dumps(RESULT), RESULT, "```json\n" + json.dumps(RESULT) + "\n```",
    "<think>synthetic private reasoning</think>\n" + json.dumps(RESULT),
    [{"type": "text", "text": json.dumps(RESULT)}],
])
def test_recognized_json_wrappers_keep_schema_validation(content):
    assert CloudRuProvider.parse_content(content).title == "Synthetic"


@pytest.mark.parametrize("content", [None, "", "Explanation " + json.dumps(RESULT),
                                     {**RESULT, "user_id": "synthetic-forbidden-owner"},
                                     [{"type": "image", "text": json.dumps(RESULT)}]])
def test_invalid_output_is_rejected_without_logging_content(content, caplog):
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        CloudRuProvider.parse_content(content)
    assert "synthetic-forbidden-owner" not in caplog.text
    assert "Explanation" not in caplog.text


def test_deepseek_requests_final_json_without_reasoning_and_does_not_retry():
    calls = []

    def transport(request):
        calls.append(request)
        assert json.loads(request.content)["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "```json\n" + json.dumps(RESULT) + "\n```", "reasoning_content": "private-synthetic",
        }}]})

    provider = CloudRuProvider("fake-key", "deepseek-synthetic", transport=httpx.MockTransport(transport))
    assert provider.structure("Synthetic").title == "Synthetic"
    assert len(calls) == 1
