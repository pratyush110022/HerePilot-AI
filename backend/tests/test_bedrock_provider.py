"""Bedrock provider: request shape, response parsing and error translation, all with a mocked client.

These are MOCKED tests. They prove our code builds a correct Converse request and handles the documented
response and error shapes. They do not prove that a particular AWS account can call a particular model.
"""
from __future__ import annotations

import json

import pytest

botocore_exceptions = pytest.importorskip("botocore.exceptions")
ClientError = botocore_exceptions.ClientError

from services import llm
from services.llm import (
    LLMAccessDenied, LLMAuthError, LLMBadOutput, LLMBadRequest, LLMModelUnavailable, LLMNotConfigured,
    LLMThrottled, LLMTimeout, complete_json, extract_json_object,
)
from services.llm.bedrock import BedrockProvider, translate_exception

MODEL = "test.model-v1:0"


class FakeBedrockClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def converse(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def ok(text, stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": stop, "usage": {}}


def client_error(code, message="secret detail arn:aws:iam::123456789012:user/x RequestId abc"):
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "Converse")


@pytest.mark.asyncio
async def test_converse_request_uses_configured_model_system_and_user_blocks():
    client = FakeBedrockClient(ok("hello"))
    provider = BedrockProvider(model_id=MODEL, client=client)
    text = await provider.complete(system="SYS", user="USER", max_tokens=123, temperature=0.1)
    assert text == "hello"
    request = client.requests[0]
    assert request["modelId"] == MODEL
    assert request["system"] == [{"text": "SYS"}]
    assert request["messages"] == [{"role": "user", "content": [{"text": "USER"}]}]
    assert request["inferenceConfig"] == {"maxTokens": 123, "temperature": 0.1}


@pytest.mark.asyncio
async def test_json_output_is_parsed_even_inside_a_fenced_block():
    client = FakeBedrockClient(ok('```json\n{"intent": "geocode", "place": "Pune"}\n```'))
    result = await complete_json(BedrockProvider(model_id=MODEL, client=client), system="s", user="u")
    assert result == {"intent": "geocode", "place": "Pune"}


@pytest.mark.parametrize("text", ["", "   ", "no json here", "[1, 2, 3]", '{"unterminated": '])
def test_malformed_model_output_is_a_bad_output_error(text):
    with pytest.raises(LLMBadOutput):
        extract_json_object(text)


@pytest.mark.asyncio
async def test_truncated_output_is_rejected_rather_than_half_parsed():
    client = FakeBedrockClient(ok('{"intent": "plan_trip", "places": ["A"', stop="max_tokens"))
    with pytest.raises(LLMBadOutput):
        await BedrockProvider(model_id=MODEL, client=client).complete(system="s", user="u")


@pytest.mark.asyncio
async def test_unexpected_response_shape_is_a_bad_output_error():
    with pytest.raises(LLMBadOutput):
        await BedrockProvider(model_id=MODEL, client=FakeBedrockClient({"unexpected": True})).complete(system="s", user="u")


@pytest.mark.parametrize("code, expected", [
    ("AccessDeniedException", LLMAccessDenied), ("UnrecognizedClientException", LLMAuthError), ("ExpiredTokenException", LLMAuthError),
    ("ThrottlingException", LLMThrottled), ("ServiceQuotaExceededException", LLMThrottled),
    ("ModelTimeoutException", LLMTimeout), ("ModelNotReadyException", LLMModelUnavailable),
    ("ServiceUnavailableException", LLMModelUnavailable), ("ResourceNotFoundException", LLMModelUnavailable),
    ("ValidationException", LLMBadRequest),
])
@pytest.mark.asyncio
async def test_bedrock_error_codes_map_to_safe_categories_without_copying_the_message(code, expected):
    provider = BedrockProvider(model_id=MODEL, client=FakeBedrockClient(error=client_error(code)))
    with pytest.raises(expected) as caught:
        await provider.complete(system="s", user="u")
    # Only the code survives; AWS's message (account IDs, ARNs, request IDs) is dropped.
    assert caught.value.detail == code
    assert "123456789012" not in str(caught.value) and "arn:aws" not in caught.value.detail


@pytest.mark.parametrize("name", ["NoCredentialsError", "PartialCredentialsError", "NoRegionError", "ProfileNotFound"])
def test_missing_credentials_region_or_profile_means_not_configured(name):
    cls = getattr(botocore_exceptions, name)
    kwargs = {"PartialCredentialsError": {"provider": "x", "cred_var": "y"}, "ProfileNotFound": {"profile": "p"}}.get(name, {})
    assert isinstance(translate_exception(cls(**kwargs)), LLMNotConfigured)


def test_timeouts_and_connection_failures_are_retryable_categories():
    assert isinstance(translate_exception(botocore_exceptions.ReadTimeoutError(endpoint_url="https://x")), LLMTimeout)
    assert isinstance(translate_exception(botocore_exceptions.EndpointConnectionError(endpoint_url="https://x")), LLMModelUnavailable)


def test_a_provider_without_a_model_id_is_not_available_and_never_calls_aws():
    provider = BedrockProvider(model_id="", client=None)
    assert provider.available is False
    assert provider.status()["reason"] == "BEDROCK_MODEL_ID is not set"


@pytest.mark.asyncio
async def test_calling_without_a_model_id_fails_cleanly():
    with pytest.raises(LLMNotConfigured):
        await BedrockProvider(model_id="", client=FakeBedrockClient(ok("x"))).complete(system="s", user="u")


def test_status_never_contains_credentials_or_the_profile_name(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "super-secret-profile-name")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SENTINEL-SECRET-KEY-VALUE")
    status = json.dumps(BedrockProvider(model_id=MODEL, client=FakeBedrockClient(ok("x"))).status())
    assert "super-secret-profile-name" not in status and "SENTINEL-SECRET-KEY-VALUE" not in status
    assert json.loads(status)["profile_selected"] is True


def test_provider_selection_honours_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    llm.set_provider(None)
    assert llm.get_provider().name == "none" and llm.get_provider().available is False
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    llm.set_provider(None)
    assert llm.get_provider().name == "bedrock" and llm.get_provider().model_id == MODEL
    monkeypatch.setenv("LLM_PROVIDER", "carrier-pigeon")
    llm.set_provider(None)
    assert llm.get_provider().name == "none"


def test_no_aws_credentials_in_this_process_means_the_real_client_refuses_to_build(monkeypatch):
    """With no profile, keys or region, building a client reports not-configured instead of guessing a profile."""
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION",
                "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/credentials")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    provider = BedrockProvider(model_id=MODEL)
    with pytest.raises(LLMNotConfigured):
        provider._get_client()
    assert provider.available is False


def test_a_failed_client_build_is_retried_after_a_short_pause_so_signing_in_again_recovers(monkeypatch):
    """An expired login session must not disable the assistant until the backend restarts."""
    import services.llm.bedrock as bedrock_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(bedrock_module.time, "monotonic", lambda: clock["now"])
    provider = BedrockProvider(model_id=MODEL)
    provider._build_error, provider._build_error_at = LLMNotConfigured("no AWS credentials found"), clock["now"]
    assert provider.available is False
    assert provider.status()["reason"] == "client error: not_configured"
    clock["now"] += bedrock_module._BUILD_RETRY_SECONDS + 1
    assert provider.available is True, "after the pause the next request may try to build the client again"
    assert provider.status()["reason"] is None


def test_a_missing_credential_dependency_is_reported_as_not_configured_without_leaking_text():
    error = translate_exception(botocore_exceptions.MissingDependencyException(msg='pip install "botocore[crt]" SECRET-HINT'))
    assert isinstance(error, LLMNotConfigured) and error.code == "not_configured"
    assert "SECRET-HINT" not in str(error) and error.detail == "MissingDependencyException"
