import copy
from core.providers import PRESET_PROVIDERS, sync_all_live_provider_models

class ProviderCatalog:
    def __init__(self, config=None):
        self.config = config
        self.providers = copy.deepcopy(PRESET_PROVIDERS)
        if config:
            self.load_from_config(config)

    def load_from_config(self, config):
        # First merge saved config providers
        if 'providers' in config:
            for k, v in config['providers'].items():
                if k in self.providers:
                    self.providers[k].update(v)
                else:
                    self.providers[k] = v
        # Sync live models from provider APIs / CLI
        sync_all_live_provider_models(config)
        if 'providers' in config:
            for k, v in config['providers'].items():
                if k in self.providers:
                    self.providers[k].update(v)
                else:
                    self.providers[k] = v

    def list_all(self):
        if self.config:
            sync_all_live_provider_models(self.config)
            if 'providers' in self.config:
                for k, v in self.config['providers'].items():
                    if k in self.providers:
                        self.providers[k].update(v)
                    else:
                        self.providers[k] = v
        return self.providers

    def get(self, key):
        return self.providers.get(key)
