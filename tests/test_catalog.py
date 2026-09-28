import unittest
from unittest.mock import patch
from types import SimpleNamespace
from core.catalog import model_catalog
from core.config import SystemSettings, Provider

class CatalogTests(unittest.TestCase):
    def test_partner_options_are_visible_but_unconfigured(self):
        config=SystemSettings()
        with patch.object(SystemSettings,'get_api_key',return_value=None):
            providers={p['id']:p for p in model_catalog(SimpleNamespace(config=config,agents={}))['providers']}
        for alias in ('openrouter','siliconflow','together'):
            self.assertIn(alias,providers)
            self.assertTrue(providers[alias]['setup_required'])
            self.assertEqual(providers[alias]['models'],[])

    def test_configured_partner_retains_provider_specific_model_id(self):
        config=SystemSettings(providers={'openrouter':Provider(adapter='openrouter',models=['deepseek/example-model'])})
        with patch.object(SystemSettings,'get_api_key',side_effect=lambda alias:'test-key' if alias=='openrouter' else None):
            providers={p['id']:p for p in model_catalog(SimpleNamespace(config=config,agents={}))['providers']}
        self.assertFalse(providers['openrouter']['setup_required'])
        self.assertEqual(providers['openrouter']['models'][0]['id'],'openrouter/deepseek/example-model')
        self.assertEqual(providers['openrouter']['models'][0]['recommended_harness'],'direct_api')
