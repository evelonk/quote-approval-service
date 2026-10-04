from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()
# Unknown usernames still perform password verification.
DUMMY_HASH = password_hasher.hash("unused-dummy-password")
