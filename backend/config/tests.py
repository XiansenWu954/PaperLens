"""Tests for config: health endpoint reads effective Django settings."""
import os
from django.test import TestCase, override_settings


class HealthEndpointTest(TestCase):
    """GPT v5 Stage A: health endpoint MUST report settings.PAPERLENS_EMBEDDING_PROVIDER,
    not a hardcoded env default. When override_settings changes the provider, health
    output MUST reflect it."""

    def test_health_reports_effective_embedding_provider(self):
        """Health endpoint reads Django settings, not os.environ directly."""
        from django.conf import settings
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # In test mode, IS_TESTING forces provider to "fake"
        self.assertEqual(data["config"]["embedding_provider"],
                         settings.PAPERLENS_EMBEDDING_PROVIDER)

    @override_settings(PAPERLENS_EMBEDDING_PROVIDER="fake")
    def test_health_reflects_overridden_provider(self):
        """When settings are overridden, health MUST report the overridden value."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["config"]["embedding_provider"], "fake")

    def test_health_reports_database_engine(self):
        """Health endpoint reports the actual database backend."""
        response = self.client.get("/")
        data = response.json()
        self.assertIn(data["config"]["database"], ("postgres", "sqlite"))

    def test_health_never_exposes_api_key(self):
        """Health endpoint MUST NOT return any key value, only a boolean.

        Uses a sentinel secret injected via mock.patch.dict to verify that the
        actual key string never appears in the serialized response — not just
        the 'sk-' prefix pattern."""
        import json
        from unittest import mock
        sentinel = "SENTINEL_STAGE_A_SECRET"
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": sentinel}, clear=False):
            response = self.client.get("/")
        data = response.json()
        self.assertTrue(data["config"]["deepseek_key_configured"],
                        "key_configured must be True when DEEPSEEK_API_KEY is set")
        raw = json.dumps(data)
        self.assertNotIn(sentinel, raw,
                         "health response must not contain the actual API key value")
        self.assertNotIn("sk-", raw)
