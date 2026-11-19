"""Exercise the local HTTP folder command without opening an Explorer window."""

import json
import tempfile
import unittest
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from localweb.jobs import Jobs
from server import Handler, LocalServer


class OpenFolderTest(unittest.TestCase):
    def test_only_registered_folder_can_be_opened(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            LocalServer(("127.0.0.1", 0), Handler) as server,
        ):
            root = Path(directory)
            canonical = root / "outputs" / "test"
            canonical.mkdir(parents=True)
            result_file = canonical / "result.xlsx"
            result_file.write_bytes(b"report")
            external = root / "chosen folder"
            external.mkdir()
            server.jobs = Jobs(root)
            key = "1" * 32
            import sqlite3

            with closing(sqlite3.connect(server.jobs.db)) as db, db:
                db.execute(
                    "INSERT INTO downloads VALUES (?,?,?)",
                    (key, str(result_file.relative_to(root)), str(external)),
                )
            server.cookie_name, server.cookie, server.token = "test", "cookie", "token"
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:

                def request(handle, token):
                    with closing(HTTPConnection("127.0.0.1", server.server_port)) as http:
                        http.request(
                            "POST",
                            "/api/open-folder",
                            json.dumps({"fileId": handle}),
                            headers={
                                "Content-Type": "application/json",
                                "Cookie": "test=cookie",
                                "X-Local-Token": token,
                            },
                        )
                        response = http.getresponse()
                        response.read()
                        return response.status

                with patch("server.os.startfile") as explore:
                    self.assertEqual(request(key, "wrong"), 403)
                    self.assertEqual(request(str(external), "token"), 400)
                    self.assertEqual(request("2" * 32, "token"), 404)
                    explore.assert_not_called()
                    self.assertEqual(request(key, "token"), 200)
                    explore.assert_called_once_with(external.resolve(), "explore")
            finally:
                server.shutdown()
                thread.join(3)
