# Configuration loaded entirely from environment variables (or .env).
# Resolution order:
#   real environment > .env file > built-in defaults
# Required: IMMICH_URL, MEDIA_PATH, and one of IMMICH_API_KEYS / IMMICH_API_KEY.
import os
import re
from typing import Dict, Any, List


def _parse_keys(raw: str) -> List[str]:
    """Split a comma/newline-separated string into a list of keys."""
    return [k.strip() for k in re.split(r'[,\n]', raw or '') if k.strip()]


class Config:
    """Env-only configuration with .env file support."""

    def __init__(self, env_file: str = ".env"):
        self._load_dotenv(env_file)
        self._config = self._build_config()
        self._validate_config()

    def _load_dotenv(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            from dotenv import load_dotenv
        except ImportError:
            # Running in a container with env vars already set; dotenv is optional.
            return
        # override=False so real environment always wins over .env
        load_dotenv(path, override=False)

    def _build_config(self) -> Dict[str, Any]:
        return {
            'immich': {
                'url': (os.environ.get('IMMICH_URL') or '').strip(),
                'api_keys': self._resolve_keys(),
            },
            'paths': {
                'media_path': (os.environ.get('MEDIA_PATH') or '').strip(),
            },
            'sync': {
                'interval': self._int_env('SYNC_INTERVAL', 900),
            },
            'logging': {
                'level': (os.environ.get('LOG_LEVEL') or 'INFO').upper(),
                'file': os.environ.get('LOG_FILE') or '/app/logs/exif-writer.log',
            },
            'state': {
                'file': os.environ.get('STATE_FILE') or '/app/state/sync_state.json',
            },
        }

    def _resolve_keys(self) -> List[str]:
        # IMMICH_API_KEYS takes precedence; IMMICH_API_KEY is a legacy fallback.
        if raw := os.environ.get('IMMICH_API_KEYS'):
            return _parse_keys(raw)
        if raw := os.environ.get('IMMICH_API_KEY'):
            return _parse_keys(raw)
        return []

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw == '':
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from None

    def _validate_config(self) -> None:
        immich = self._config['immich']
        if not immich['url']:
            raise ValueError("IMMICH_URL is required")
        if not immich['url'].startswith(('http://', 'https://')):
            raise ValueError(
                f"IMMICH_URL must start with http:// or https://, got {immich['url']!r}")
        if not immich['api_keys']:
            raise ValueError(
                "No Immich API key configured. Set IMMICH_API_KEYS=key1,key2 "
                "(or IMMICH_API_KEY=key1).")
        for k in immich['api_keys']:
            if k.startswith('YOUR_') and k.endswith('_HERE'):
                raise ValueError("API key is still a placeholder")
        if not self._config['paths']['media_path']:
            raise ValueError("MEDIA_PATH is required")
        if self._config['sync']['interval'] <= 0:
            raise ValueError("SYNC_INTERVAL must be positive")
        if self._config['logging']['level'] not in {
            'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
        }:
            raise ValueError(
                f"LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR/CRITICAL, "
                f"got {self._config['logging']['level']!r}")

    # ----- accessors -----
    @property
    def immich_url(self) -> str:
        return self._config['immich']['url'].rstrip('/')

    @property
    def immich_api_keys(self) -> List[str]:
        return list(self._config['immich']['api_keys'])

    @property
    def media_path(self) -> str:
        return self._config['paths']['media_path']

    @property
    def sync_interval(self) -> int:
        return self._config['sync']['interval']

    @property
    def log_level(self) -> str:
        return self._config['logging']['level']

    @property
    def log_file(self) -> str:
        return self._config['logging']['file']

    @property
    def state_file(self) -> str:
        return self._config['state']['file']