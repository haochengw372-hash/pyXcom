import unittest
from unittest.mock import patch

import httpx

from pyxcom.transport import XTransport


class TransportTests(unittest.TestCase):
    def test_retries_transient_connection_errors(self):
        attempts = 0

        def handle(request):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("temporary", request=request)
            return httpx.Response(200, text="ok")

        transport = XTransport({"auth_token": "example", "ct0": "example"})
        transport._x.close()
        transport._x = httpx.Client(transport=httpx.MockTransport(handle))
        with patch("pyxcom.transport.time.sleep"):
            response = transport._get(transport._x, "https://x.com/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 3)
        transport.close()


if __name__ == "__main__":
    unittest.main()
