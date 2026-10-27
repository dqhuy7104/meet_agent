"""Public account and authentication contracts."""
from pydantic import BaseModel, Field, field_validator


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024, repr=False)

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value.count('@') != 1 or any(character.isspace() for character in value) or not all(value.split('@')):
            raise ValueError('A valid email identifier is required')
        return value


class LoginCredentials(Credentials):
    password: str = Field(min_length=1, max_length=1024, repr=False)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=12, max_length=1024, repr=False)


class AccountResponse(BaseModel):
    id: str
    email: str
    is_system_admin: bool
    is_disabled: bool


class SessionResponse(BaseModel):
    account: AccountResponse
    csrf_token: str
