"""Authentication handlers for Exchange Web Services."""

import logging
import os
from pathlib import Path

from exchangelib import (
    Credentials,
    OAuth2AuthorizationCodeCredentials,
    OAuth2Credentials,
    NTLM,
)
from oauthlib.oauth2 import OAuth2Token

from .config import Settings
from .exceptions import AuthenticationError


class O365DeviceCodeCredentials(OAuth2AuthorizationCodeCredentials):
    """Exchange credentials acquired with an unattended device-code flow."""

    SCOPE = ["https://outlook.office365.com/EWS.AccessAsUser.All"]

    def __init__(self, client_id: str, authority: str, cache_path: str):
        import msal

        self.cache_path = Path(cache_path).expanduser()
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text(encoding="utf-8"))

        self.app = msal.PublicClientApplication(
            client_id=client_id,
            authority=authority,
            token_cache=self.cache,
        )
        access_token = self._acquire_token()
        super().__init__(access_token=access_token)

    def _acquire_token(self):
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(self.SCOPE, account=accounts[0]) if accounts else None
        if not result:
            flow = self.app.initiate_device_flow(scopes=self.SCOPE)
            if "user_code" not in flow:
                raise AuthenticationError("Device-code flow could not be initiated")
            print(flow["message"])
            result = self.app.acquire_token_by_device_flow(flow)
        result = result or {}
        if "access_token" not in result:
            error = result.get("error_description") or result.get("error") or "no access token returned"
            raise AuthenticationError(f"Device-code OAuth authentication failed: {error}")
        self._persist_cache()
        # exchangelib requires an oauthlib OAuth2Token, not a bare string --
        # MSAL's result dict already has the shape OAuth2Token expects.
        return OAuth2Token(result)

    def _persist_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        # The cache contains refresh tokens. Set owner-only permissions before
        # writing token data, and keep this path out of version control.
        if not self.cache_path.exists():
            self.cache_path.touch(mode=0o600)
        else:
            os.chmod(self.cache_path, 0o600)
        self.cache_path.write_text(self.cache.serialize(), encoding="utf-8")
        os.chmod(self.cache_path, 0o600)

    def refresh(self, session=None):
        """Refresh the access token silently using the cached account."""
        accounts = self.app.get_accounts()
        if not accounts:
            raise AuthenticationError("No cached account is available for token refresh")
        result = self.app.acquire_token_silent(self.SCOPE, account=accounts[0])
        if not result or "access_token" not in result:
            error = (result or {}).get("error_description") or "silent token refresh failed"
            raise AuthenticationError(f"Device-code OAuth refresh failed: {error}")
        self.access_token = OAuth2Token(result)
        self._persist_cache()
        return self.access_token


class AuthHandler:
    """Handle different Exchange authentication methods."""

    def __init__(self, config: Settings):
        self.config = config
        self.logger = logging.getLogger(__name__)

    def get_credentials(self) -> Credentials:
        """Get appropriate credentials based on auth type."""

        try:
            if self.config.ews_auth_type == "oauth2":
                return self._get_oauth2_credentials()
            elif self.config.ews_auth_type == "o365devicecode":
                return self._get_o365_device_code_credentials()
            elif self.config.ews_auth_type == "basic":
                return self._get_basic_credentials()
            elif self.config.ews_auth_type == "ntlm":
                return self._get_ntlm_credentials()
            else:
                raise ValueError(f"Unsupported auth type: {self.config.ews_auth_type}")
        except Exception as e:
            self.logger.error(f"Failed to get credentials: {e}")
            raise AuthenticationError(f"Authentication setup failed: {e}")

    def _get_oauth2_credentials(self) -> OAuth2Credentials:
        """Build OAuth2 credentials. exchangelib manages the token lifecycle
        internally via MSAL when given client_id/client_secret/tenant_id;
        pre-fetching a token here only added latency and a failure surface.
        """
        try:
            return OAuth2Credentials(
                client_id=self.config.ews_client_id,
                client_secret=self.config.ews_client_secret,
                tenant_id=self.config.ews_tenant_id,
                identity=self.config.ews_email
            )
        except Exception as e:
            self.logger.error(f"OAuth2 authentication failed: {e}")
            raise AuthenticationError(f"OAuth2 setup failed: {e}")

    def _get_basic_credentials(self) -> Credentials:
        """Get basic auth credentials."""
        try:
            return Credentials(
                username=self.config.ews_username,
                password=self.config.ews_password
            )
        except Exception as e:
            self.logger.error(f"Basic auth setup failed: {e}")
            raise AuthenticationError(f"Basic auth failed: {e}")

    def _get_o365_device_code_credentials(self) -> O365DeviceCodeCredentials:
        """Build credentials using a persisted MSAL device-code token cache."""
        try:
            return O365DeviceCodeCredentials(
                client_id=self.config.ews_device_code_client_id,
                authority=self.config.ews_device_code_authority,
                cache_path=self.config.ews_device_code_token_cache_path,
            )
        except Exception as e:
            self.logger.error(f"O365 device-code authentication failed: {e}")
            raise AuthenticationError(f"O365 device-code setup failed: {e}")

    def _get_ntlm_credentials(self) -> Credentials:
        """Get NTLM credentials."""
        try:
            return Credentials(
                username=self.config.ews_username,
                password=self.config.ews_password
            )
        except Exception as e:
            self.logger.error(f"NTLM auth setup failed: {e}")
            raise AuthenticationError(f"NTLM auth failed: {e}")

    def refresh_token(self) -> None:
        """Token refresh is handled inside exchangelib. Kept for API stability."""
        if self.config.ews_auth_type == "oauth2":
            self.logger.debug("OAuth2 token refresh is handled by exchangelib")
