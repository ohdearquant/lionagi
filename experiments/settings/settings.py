import csv
import json
import threading
from contextlib import contextmanager


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self._config = {}
        self._default_service = None

    def load_from_json(self, file_path):
        with self._lock:
            with open(file_path, "r") as file:
                self._config = json.load(file)

    def save_to_json(self, file_path):
        with self._lock:
            with open(file_path, "w") as file:
                json.dump(self._config, file, indent=4)

    def load_default_service(self, file_path):
        with self._lock:
            with open(file_path, "r") as file:
                reader = csv.DictReader(file)
                self._default_service = next(reader)

    def save_default_service(self, file_path):
        with self._lock:
            fieldnames = ["provider", "api_key", "rate_limit"]
            with open(file_path, "w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(self._default_service)

    @contextmanager
    def service_context(self, provider=None, api_key=None, rate_limit=None):
        with self._lock:
            service_config = {
                "provider": provider or self._default_service["provider"],
                "api_key": api_key or self._default_service["api_key"],
                "rate_limit": rate_limit or self._default_service["rate_limit"],
            }
            yield service_config

    def get_config(self, key):
        with self._lock:
            return self._config.get(key)

    def set_config(self, key, value):
        with self._lock:
            self._config[key] = value


# Singleton instance
lionagi_settings = Settings()
