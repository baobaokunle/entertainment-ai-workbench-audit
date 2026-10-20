"""Explicit DPAPI key initialization and authenticated offline recovery packages.

Recovery files are separate from business sync. The caller must obtain passwords
through a non-echoing local prompt and must not print returned key material.
"""
import hashlib
import os
from pathlib import Path
import secrets

from .common import AppError
from .security import DPAPIProtector, SecureEnvelope


class KeyStore:
    KEY_MAGIC = b'DFA-BACKUP-KEY-1\x00'
    RECOVERY_MAGIC = b'DFA-RECOVERY-PBKDF2-SHA256-600000-1\x00'
    ITERATIONS = 600_000

    def __init__(self, path, protector=None):
        self.path = Path(path)
        self.protector = protector if protector is not None else DPAPIProtector()

    @staticmethod
    def _new_file(path, data):
        path = Path(path)
        if not path.parent.is_dir():
            raise AppError(422, 'key_directory_missing', 'Key destination directory must already exist.')
        temp = path.with_name('.key-' + secrets.token_hex(16) + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == 'nt':
                os.rename(temp, path)
            else:
                os.link(temp, path)
        except FileExistsError as exc:
            raise AppError(409, 'key_destination_exists', 'Key destination exists; overwrite is forbidden.') from exc
        except OSError as exc:
            raise AppError(503, 'key_write_failed', 'Protected key file could not be written.') from exc
        finally:
            temp.unlink(missing_ok=True)

    def initialize(self):
        if self.path.exists():
            raise AppError(409, 'key_destination_exists', 'Key store already exists; replacement is forbidden.')
        protected = self.protector.protect(self.KEY_MAGIC + secrets.token_bytes(32))
        self._new_file(self.path, protected)

    def load(self):
        try:
            if not self.path.is_file() or self.path.stat().st_size > 65536:
                raise AppError(503, 'key_missing_or_invalid', 'Backup key is missing or invalid; recovery is required.')
            clear = self.protector.unprotect(self.path.read_bytes())
            if not clear.startswith(self.KEY_MAGIC) or len(clear) != len(self.KEY_MAGIC) + 32:
                raise AppError(503, 'key_invalid', 'Protected backup key has an invalid format.')
            return clear[len(self.KEY_MAGIC):]
        except AppError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise AppError(503, 'key_unreadable', 'Protected backup key could not be read.') from exc

    @classmethod
    def _derive(cls, passphrase, salt):
        if not isinstance(passphrase, str) or not 16 <= len(passphrase) <= 1024:
            raise AppError(422, 'recovery_passphrase_invalid', 'Recovery passphrase must contain 16 to 1024 characters.')
        return hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), salt, cls.ITERATIONS, dklen=32)

    def export_recovery(self, destination, passphrase):
        salt = secrets.token_bytes(32)
        derived = self._derive(passphrase, salt)
        header = self.RECOVERY_MAGIC + salt
        encrypted = SecureEnvelope.seal(self.KEY_MAGIC + self.load(), derived, aad=header)
        self._new_file(destination, header + encrypted)
        return {'exported': True, 'format': 'dfa-recovery-v1', 'contains_encrypted_key': True}

    def import_recovery(self, source, passphrase):
        if self.path.exists():
            raise AppError(409, 'key_destination_exists', 'Key store exists; recovery cannot overwrite it.')
        source = Path(source)
        try:
            if not source.is_file() or source.stat().st_size > 65536:
                raise ValueError('size')
            blob = source.read_bytes()
            if not blob.startswith(self.RECOVERY_MAGIC) or len(blob) < len(self.RECOVERY_MAGIC) + 32 + 28:
                raise ValueError('format')
        except (OSError, ValueError) as exc:
            raise AppError(422, 'recovery_file_invalid', 'Recovery package is missing or invalid.') from exc
        end = len(self.RECOVERY_MAGIC) + 32
        header = blob[:end]
        derived = self._derive(passphrase, header[len(self.RECOVERY_MAGIC):])
        clear = SecureEnvelope.open(blob[end:], derived, aad=header)
        if not clear.startswith(self.KEY_MAGIC) or len(clear) != len(self.KEY_MAGIC) + 32:
            raise AppError(422, 'recovery_payload_invalid', 'Recovery key payload is invalid.')
        self._new_file(self.path, self.protector.protect(clear))
