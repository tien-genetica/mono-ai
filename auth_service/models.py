from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional
from datetime import datetime
import re


class UserRegister(BaseModel):
    email: EmailStr
    phone: Optional[str] = Field(None, pattern=r"^\+?[1-9]\d{1,14}$")
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    age: Optional[int] = Field(None, ge=0, le=150)
    address: Optional[str] = None
    city: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)

    @field_validator("password")
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserLogin(BaseModel):
    identifier: str  # email or phone
    password: str


class OTPRequest(BaseModel):
    identifier: str  # email or phone
    type: str = Field(..., pattern=r"^(email|phone)$")


class OTPVerify(BaseModel):
    identifier: str
    code: str
    type: str = Field(..., pattern=r"^(email|phone)$")


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    email: str
    phone: Optional[str]
    username: str
    first_name: Optional[str]
    last_name: Optional[str]
    age: Optional[int]
    address: Optional[str]
    city: Optional[str]
    country: Optional[str]
    postal_code: Optional[str]
    is_active: bool
    is_verified: bool
    email_verified: bool
    phone_verified: bool
    created_at: datetime
