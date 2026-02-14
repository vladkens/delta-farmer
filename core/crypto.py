# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | This code is poetry (badly written)
import argparse
import base64
import getpass
import os
import re

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ENV_NAME = "DF_CONFIG_PASSWORD"
ENC_PREFIX = "enc:"
B64_PREFIX = "b64:"


def _get_env_password() -> str | None:
    pwd = (os.getenv(ENV_NAME) or "").strip()
    if pwd.startswith(B64_PREFIX):
        pwd = base64.b64decode(pwd.removeprefix(B64_PREFIX)).decode()

    pwd = pwd.strip()
    return pwd if pwd else None


def _get_encryption_password(doublecheck=False) -> str:
    cache_field = "cached_password"
    if hasattr(_get_encryption_password, cache_field):
        return getattr(_get_encryption_password, cache_field)

    pwd = _get_env_password()
    if pwd:
        setattr(_get_encryption_password, cache_field, pwd)
        return pwd

    pwd1 = getpass.getpass("Enter password for encryption/decryption: ").strip()
    if not pwd1:
        raise ValueError("Password cannot be empty")

    pwd2 = getpass.getpass("Confirm password: ").strip() if doublecheck else pwd1

    if pwd1 != pwd2:
        raise ValueError("Passwords do not match")

    setattr(_get_encryption_password, cache_field, pwd1)
    return pwd1


def _derive_key(password: str, salt: bytes, iters=480000) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters)
    key = kdf.derive(password.encode())
    return base64.urlsafe_b64encode(key)


def encrypt_value(plaintext: str, password: str | None = None) -> str:
    password = password.strip() if password else None
    password = password or _get_encryption_password(doublecheck=True)

    salt = os.urandom(16)
    key = _derive_key(password, salt)
    fernet = Fernet(key)

    encrypted = fernet.encrypt(plaintext.encode())
    combined_b64 = base64.urlsafe_b64encode(salt + encrypted).decode()

    return f"{ENC_PREFIX}{combined_b64}"


def decrypt_value(encrypted_str: str, password: str | None = None) -> str:
    if not encrypted_str.startswith(ENC_PREFIX):
        raise ValueError(f"Invalid encrypted format, must start with '{ENC_PREFIX}'")

    password = password.strip() if password else None
    password = password or _get_encryption_password()

    combined_b64 = encrypted_str[len(ENC_PREFIX) :]
    combined = base64.urlsafe_b64decode(combined_b64)
    salt, encrypted = combined[:16], combined[16:]

    try:
        key = _derive_key(password, salt)
        fernet = Fernet(key)
        decrypted = fernet.decrypt(encrypted)
        return decrypted.decode()
    except Exception:
        raise ValueError("Invalid password or corrupted data")


def is_encrypted(value: str) -> bool:
    return value.startswith(ENC_PREFIX)


# MARK: Additional helper to encrypt / decrypt fields in .toml file without reading it
# This can encrypt simple fields, without knowing the structure of the config file


def encrypt_toml_config(filepath: str, fields: list[str]):
    with open(filepath, "r") as fp:
        data = fp.read()

    tlen, elen = 4, len(ENC_PREFIX) + 4
    for field in fields:
        pattern = re.compile(rf'({field}\s*=\s*)"([^"]+)"')

        # First, check if values are already encrypted and validate the password.
        # This prevents double encryption and ensures the password is correct.
        # It also skips password verify for files with encrypted values.
        # Next, encrypt any values that are not encrypted, using the same password if provided.
        # The tool does not respect TOML structure and works with simple key-value pairs, even if they are commented.

        def check_field(match):
            value = match.group(2)
            if is_encrypted(value):
                _ = decrypt_value(value)
                print(f"Skipping already encrypted value for {field}: {value[:tlen]}...")
            return match.group(0)  # Don't change the value here, we will handle it in the next step

        def replace_field(match):
            value = match.group(2)
            if is_encrypted(value):
                return match.group(0)

            encrypted = encrypt_value(value)
            print(f"Encrypted value for {field}: {value[:tlen]}... -> {encrypted[:elen]}...")
            return f'{match.group(1)}"{encrypted}"'

        data = pattern.sub(check_field, data)
        data = pattern.sub(replace_field, data)

    with open(filepath, "w") as fp:
        fp.write(data)

    print(f"\n✓ Config encrypted: {filepath}")


def decrypt_toml_config(filepath: str, fields: list[str]):
    with open(filepath, "r") as fp:
        data = fp.read()

    tlen, elen = 4, len(ENC_PREFIX) + 4
    for field in fields:
        pattern = re.compile(rf'({field}\s*=\s*)"([^"]+)"')

        def replace_field(match):
            value = match.group(2)
            if not is_encrypted(value):
                print(f"Skipping already decrypted value for {field}: {value[:tlen]}...")
                return match.group(0)
            decrypted = decrypt_value(value)
            print(f"Decrypted value for {field}: {value[:elen]}... -> {decrypted[:tlen]}...")
            return f'{match.group(1)}"{decrypted}"'

        data = pattern.sub(replace_field, data)

    with open(filepath, "w") as fp:
        fp.write(data)

    print(f"\n✓ Config decrypted: {filepath}")


def config_cli_parser(subparsers: argparse._SubParsersAction, fields: list[str]):
    config_parser = subparsers.add_parser("config", help="Config file operations")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("encrypt", help="Encrypt privkeys in config file")
    config_sub.add_parser("decrypt", help="Decrypt privkeys in config file")

    def handle_config_command(args):
        if args.config_command is None:
            config_parser.print_help()
            return
        elif args.config_command == "encrypt":
            return encrypt_toml_config(args.config, fields)
        elif args.config_command == "decrypt":
            return decrypt_toml_config(args.config, fields)

    return handle_config_command
