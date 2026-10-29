import os
import re
import sys

from urllib.parse import urljoin

import pytest
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.config import load_config  # noqa: E402

MEMBER = {"X-authentik-username": "u10114", "X-authentik-groups": "Metro SWAT"}
ADMIN = {"X-authentik-username": "u10001", "X-authentik-groups": "Metro SWAT|Metro SWAT Admins"}
OUTSIDER = {"X-authentik-username": "nobody", "X-authentik-groups": "Some Other Group"}


def make_config(tmp_path, **overrides):
    env = {"DATA_DIR": str(tmp_path / "data"), "SECRET_KEY": "test-secret", "EXPIRY_CHECK_SECONDS": "0",
           "LOG_LEVEL": "WARNING"}
    env.update({k: str(v) for k, v in overrides.items()})
    return load_config(env)


class BrowserLikeClient(FlaskClient):
    """Resolve relative Location headers the way a browser does.

    The app redirects with relative URLs; Werkzeug's test
    client copies the header into the path verbatim, so join it ourselves.
    """

    def resolve_redirect(self, response, buffered=False):
        response.location = urljoin(response.request.url, response.location)
        return super().resolve_redirect(response, buffered=buffered)


@pytest.fixture
def app(tmp_path):
    application = create_app(make_config(tmp_path), run_scheduler=False)
    application.testing = True
    application.test_client_class = BrowserLikeClient
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def token(client, headers):
    """The same stateless HMAC token the app renders into every form."""
    import hashlib
    import hmac
    secret = client.application.config["APP"].secret_key.encode()
    username = headers["X-authentik-username"].encode()
    return hmac.new(secret, username, hashlib.sha256).hexdigest()[:40]


def post(client, path, headers, **data):
    data["_csrf"] = token(client, headers)
    return client.post(path, headers=headers, data=data)
