"""batcave.secrets: los tokens reales se enmascaran y las rutas de archivos
(segmentos de palabras separados por `/`) quedan intactas.

Correr: venv/bin/python -m unittest discover -s tests -q
"""

import base64
import string
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from batcave.secrets import REDACTED, looks_like_path, mask_secrets  # noqa: E402

# Secretos falsos armados en tiempo de ejecucion (el escaner de Red Hood
# marcaria un literal con forma de token).
FAKE_GITHUB = "ghp_" + "1234567890" + string.ascii_lowercase
FAKE_BASIC = base64.b64encode(("x-access-token:" + FAKE_GITHUB).encode()).decode()
FAKE_B64_SLASHES = base64.b64encode(bytes(range(200, 245))).decode().replace("+", "/")
FAKE_B64_PLAIN = base64.b64encode(bytes(range(0, 60))).decode()


class MaskSecretsTests(unittest.TestCase):
    def test_paths_are_not_masked(self):
        for text in [
            "/home/dsantacruz/projects/portfolioServiceLauncher",
            "cd /home/dsantacruz/projects/portfolioServiceLauncher && docker compose pull nats-server",
            "/home/dsantacruz/projects/wazuh-docker/single-node/docker-compose.override.yml",
            "Feed notices-resolute-0 (/home/dsantacruz/projects/night-agent/batcave/memory/julia_cache)",
            "/usr/lib/python3/dist-packages/someModuleName/submodule/file.py",
        ]:
            with self.subTest(text=text):
                self.assertEqual(mask_secrets(text), text)

    def test_authorization_basic_is_masked(self):
        for header in ["Authorization: Basic ", "AUTHORIZATION: basic ", "Authorization: Bearer "]:
            with self.subTest(header=header):
                masked = mask_secrets(header + FAKE_BASIC)
                self.assertIn(REDACTED, masked)
                self.assertNotIn(FAKE_BASIC, masked)

    def test_bare_base64_is_masked(self):
        for token in [FAKE_BASIC, FAKE_B64_SLASHES, FAKE_B64_PLAIN]:
            with self.subTest(token=token[:12]):
                masked = mask_secrets(f"GIT_CONFIG_VALUE_0={token} y mas")
                self.assertNotIn(token, masked)
                self.assertIn(REDACTED, masked)
        self.assertIn("/", FAKE_B64_SLASHES)
        self.assertFalse(looks_like_path(FAKE_B64_SLASHES))

    def test_other_tokens_still_masked(self):
        self.assertEqual(mask_secrets("x-access-token:" + FAKE_GITHUB), REDACTED)
        self.assertNotIn(FAKE_GITHUB, mask_secrets("token " + FAKE_GITHUB))

    def test_looks_like_path(self):
        self.assertTrue(looks_like_path("/home/dsantacruz/projects/portfolioServiceLauncher"))
        self.assertFalse(looks_like_path("abcdefghijklmnopqrstuvwxyz0123456789ABCD"))
        self.assertFalse(looks_like_path("/home/dsantacruz/projects/aB3xKq9ZpLmN0vWt"))
        self.assertFalse(looks_like_path("dXNlcjpwYXNz/d29yZA=="))


if __name__ == "__main__":
    unittest.main()
