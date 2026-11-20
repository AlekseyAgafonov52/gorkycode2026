import os
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

from docker.entrypoint import database_url, main


class DockerStartupTests(unittest.TestCase):
    def environment(self):
        return {"POSTGRES_HOST": "database.invalid", "POSTGRES_PORT": "5432", "POSTGRES_DB": "test",
                "POSTGRES_USER": "user", "POSTGRES_PASSWORD": "spaces $ :/@#%"}

    def test_url_encodes_special_password_characters(self):
        environment = self.environment()
        url = make_url(database_url(environment))
        self.assertEqual(url.password, environment["POSTGRES_PASSWORD"])
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.host, environment["POSTGRES_HOST"])

    def test_explicit_url_and_invalid_environment(self):
        self.assertEqual(database_url({"DATABASE_URL": "postgresql://explicit.invalid/test"}),
                         "postgresql://explicit.invalid/test")
        with self.assertRaises(ValueError):
            database_url({})
        for port in ("bad", "0", "65536"):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, "POSTGRES_PORT"):
                database_url({**self.environment(), "POSTGRES_PORT": port})

    def test_entrypoint_passes_url_only_through_environment(self):
        with patch.dict(os.environ, self.environment(), clear=True), patch("docker.entrypoint.os.execvp") as execute:
            main(["python", "-m", "alembic", "upgrade", "head"])
            self.assertEqual(make_url(os.environ["DATABASE_URL"]).password, self.environment()["POSTGRES_PASSWORD"])
            execute.assert_called_once_with("python", ["python", "-m", "alembic", "upgrade", "head"])


if __name__ == "__main__":
    unittest.main()
