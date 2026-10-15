"""The password policy of ALGO accounts, in ONE place.

Registration (``OrgApplicationRequest``), accepting an invitation (``AcceptInviteRequest``), changing the
password (``ChangePasswordRequest``) and the password recovery (``PasswordResetRequest``) all take their
length limits from here, so a password that one door accepts is accepted by every other door.

A leaf module: it imports nothing from the application.
"""

PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 128
