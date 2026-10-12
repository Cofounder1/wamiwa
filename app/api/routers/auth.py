"""Signup (ON-01, ON-02), sessions, password reset and two-factor login."""
from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import Anonymous, Any_, ClientIp, Services, ok
from app.core.enums import AccountType

router = APIRouter(prefix="/auth", tags=["auth"])

Password = Field(min_length=10, max_length=128)
Code = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class EmailIn(BaseModel):
    email: EmailStr


class OtpIn(BaseModel):
    email: EmailStr
    code: str = Code


class CompleteSignupIn(BaseModel):
    signup_token: str
    account_type: AccountType
    password: str = Password
    handle: str = Field(min_length=3, max_length=30)
    name: str = Field(min_length=1, max_length=120)
    date_of_birth: date | None = None
    category: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)
    totp_code: str | None = Field(default=None, max_length=6)


class RefreshIn(BaseModel):
    refresh_token: str


class ResetIn(BaseModel):
    email: EmailStr
    code: str = Code
    new_password: str = Password


class TotpIn(BaseModel):
    code: str = Code


@router.post("/signup/request-otp", dependencies=[Anonymous])
async def request_signup_otp(body: EmailIn, svc: Services, ip: ClientIp):
    return ok(await svc.auth.request_signup_otp(body.email, ip))


@router.post("/signup/verify-otp", dependencies=[Anonymous])
async def verify_signup_otp(body: OtpIn, svc: Services):
    return ok(await svc.auth.verify_signup_otp(body.email, body.code))


@router.post("/signup/complete", status_code=201, dependencies=[Anonymous])
async def complete_signup(body: CompleteSignupIn, svc: Services):
    return ok(await svc.auth.complete_signup(**body.model_dump()))


@router.post("/login", dependencies=[Anonymous])
async def login(body: LoginIn, svc: Services, ip: ClientIp):
    return ok(await svc.auth.login(body.email, body.password, body.totp_code, ip))


@router.post("/refresh", dependencies=[Anonymous])
async def refresh(body: RefreshIn, svc: Services):
    return ok(await svc.auth.refresh(body.refresh_token))


@router.post("/logout", dependencies=[Anonymous])
async def logout(body: RefreshIn, svc: Services):
    await svc.auth.logout(body.refresh_token)
    return ok()


@router.post("/password/reset-request", dependencies=[Anonymous])
async def request_password_reset(body: EmailIn, svc: Services, ip: ClientIp):
    return ok(await svc.auth.request_password_reset(body.email, ip))


@router.post("/password/reset", dependencies=[Anonymous])
async def reset_password(body: ResetIn, svc: Services):
    await svc.auth.reset_password(body.email, body.code, body.new_password)
    return ok()


@router.post("/2fa/setup")
async def setup_two_factor(p: Any_, svc: Services):
    return ok(await svc.auth.begin_totp_setup(p.account))


@router.post("/2fa/enable")
async def enable_two_factor(body: TotpIn, p: Any_, svc: Services):
    await svc.auth.enable_totp(p.account, body.code)
    return ok({"two_factor_enabled": True})


@router.post("/2fa/disable")
async def disable_two_factor(body: TotpIn, p: Any_, svc: Services):
    await svc.auth.disable_totp(p.account, body.code)
    return ok({"two_factor_enabled": False})
