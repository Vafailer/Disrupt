from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs

import httpx
import pytest

from app.smtp_bz import ENDPOINT, SmtpBzError, SmtpBzMailer


@pytest.mark.parametrize("encoding", ["multipart", "urlencoded"])
def test_https_contract_multipart_and_escaped_html(encoding):
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == "fake-api-key"
        if encoding == "multipart":
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + request.headers["Content-Type"] + "\r\n\r\n").encode() + request.content,
            )
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
                      for part in message.iter_parts()}
        else:
            assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
            fields = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        assert fields["from"] == "sender@example.test"
        assert fields["to"] == "owner@example.test"
        assert fields["subject"] == "Проверка"
        assert fields["text"] == "<script>не HTML</script>"
        assert fields["html"] == "<pre>&lt;script&gt;не HTML&lt;/script&gt;</pre>"
        return httpx.Response(200, json={"id": "synthetic-message-1"})

    mailer = SmtpBzMailer("fake-api-key", "sender@example.test", transport=httpx.MockTransport(handler), encoding=encoding)
    assert not calls
    assert mailer.send_with_receipt("owner@example.test", "Проверка", "<script>не HTML</script>") == "synthetic-message-1"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
def test_http_errors_do_not_retry_follow_redirect_or_echo(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fake-api-key owner@example.test sensitive body",
                              headers={"Location": "https://other.invalid"})

    mailer = SmtpBzMailer("fake-api-key", "sender@example.test", transport=httpx.MockTransport(handler))
    with pytest.raises(SmtpBzError) as error:
        mailer.send("owner@example.test", "Test", "Body")
    assert error.value.http_status == status
    assert "fake-api-key" not in str(error.value) and "owner" not in str(error.value)
    assert len(calls) == 1


def test_timeout_is_ambiguous_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("fake-api-key sensitive detail", request=request)

    mailer = SmtpBzMailer("fake-api-key", "sender@example.test", transport=httpx.MockTransport(handler))
    with pytest.raises(SmtpBzError, match="mail_timeout_unknown"):
        mailer.send("owner@example.test", "Test", "Body")
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [[], {"error": "sensitive error"}, {"success": False}, {"code": 400}])
def test_invalid_or_rejected_success_response(payload):
    mailer = SmtpBzMailer("fake-api-key", "sender@example.test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload),
    ))
    with pytest.raises(SmtpBzError):
        mailer.send("owner@example.test", "Test", "Body")


def test_invalid_input_never_dispatches():
    def forbidden(_):
        raise AssertionError("Must not dispatch")

    mailer = SmtpBzMailer("fake-api-key", "sender@example.test", transport=httpx.MockTransport(forbidden))
    for to, subject, body in [("bad\naddress", "Test", "Body"), ("owner@example.test", "bad\nsubject", "Body"),
                              ("owner@example.test", "Test", ""), ("owner@example.test", "Test", "x" * 24001)]:
        with pytest.raises(SmtpBzError):
            mailer.send(to, subject, body)
