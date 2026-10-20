import os
import unittest
from unittest.mock import patch
from server.common import AppError
from server.security import DPAPIProtector, SecureEnvelope


class SecurityTests(unittest.TestCase):
    def test_aesgcm_real_roundtrip_nonce_and_aad(self):
        key = bytes(range(32))
        one = SecureEnvelope.seal(b'fictional secret', key, aad=b'route-a')
        two = SecureEnvelope.seal(b'fictional secret', key, aad=b'route-a')
        self.assertNotEqual(one, two)
        self.assertEqual(SecureEnvelope.open(one, key, aad=b'route-a'), b'fictional secret')
        self.assertNotIn(b'fictional secret', one)
        for bad_key, bad_aad in ((b'x' * 32, b'route-a'), (key, b'route-b')):
            with self.assertRaises(AppError):
                SecureEnvelope.open(one, bad_key, aad=bad_aad)
        for index in (0, len(SecureEnvelope.MAGIC), len(one)-1):
            changed = bytearray(one)
            changed[index] ^= 1
            with self.assertRaises(AppError):
                SecureEnvelope.open(bytes(changed), key, aad=b'route-a')

    def test_no_downgrade_or_short_keys(self):
        with self.assertRaises(AppError):
            SecureEnvelope.seal(b'fictional', b'k'*16)
        with self.assertRaises(AppError):
            SecureEnvelope.open(b'legacy XOR payload', b'k'*32)

    def test_dpapi_nonwindows_fails_closed(self):
        with patch('server.security.os.name', 'posix'):
            with self.assertRaises(AppError) as error:
                DPAPIProtector().protect(b'fictional bytes')
            self.assertEqual(error.exception.code, 'dpapi_unavailable')

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows DPAPI requires Windows')
    def test_real_current_windows_user_dpapi(self):
        protector = DPAPIProtector()
        data = protector.protect(b'fictional-only recovery test')
        self.assertNotIn(b'fictional-only', data)
        self.assertEqual(protector.unprotect(data), b'fictional-only recovery test')
        damaged = data[:-10] + b'0000000000'
        with self.assertRaises(AppError):
            protector.unprotect(damaged)
