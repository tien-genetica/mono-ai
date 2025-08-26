from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import re
from jose import JWTError, jwt
from .config import settings
from .database import db
from .models import (
    UserResponse,
    UserRegister,
    UserLogin,
    OTPRequest,
    OTPVerify,
    Token,
)
from .utils import (
    get_password_hash,
    verify_password,
    create_access_token,
    create_refresh_token,
    generate_otp,
    send_otp_email,
    send_otp_sms,
)
from .auth import get_current_user
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.init_tables()
    try:
        yield
    finally:
        await db.disconnect()


app = FastAPI(title="Auth Service API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


 


@app.post("/register", response_model=UserResponse)
async def register(user_data: UserRegister):
    # Check if user exists
    existing_user = await db.fetchrow(
        "SELECT id FROM users WHERE email = $1 OR username = $2 OR phone = $3",
        user_data.email,
        user_data.username,
        user_data.phone,
    )

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email, username, or phone already exists",
        )

    # Hash password
    password_hash = get_password_hash(user_data.password)

    # Insert user
    user = await db.fetchrow(
        """
        INSERT INTO users (
            email, phone, username, password_hash, first_name, last_name,
            age, address, city, country, postal_code
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING *
    """,
        user_data.email,
        user_data.phone,
        user_data.username,
        password_hash,
        user_data.first_name,
        user_data.last_name,
        user_data.age,
        user_data.address,
        user_data.city,
        user_data.country,
        user_data.postal_code,
    )

    return UserResponse(**dict(user))


@app.post("/login", response_model=Token)
async def login(login_data: UserLogin):
    # Determine if identifier is email or phone
    is_email = "@" in login_data.identifier

    if is_email:
        user = await db.fetchrow(
            "SELECT * FROM users WHERE email = $1 AND is_active = TRUE",
            login_data.identifier,
        )
    else:
        user = await db.fetchrow(
            "SELECT * FROM users WHERE phone = $1 AND is_active = TRUE",
            login_data.identifier,
        )

    if not user or not verify_password(login_data.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    # Create tokens
    access_token = create_access_token(data={"sub": str(user["id"])})
    refresh_token = create_refresh_token(data={"sub": str(user["id"])})

    # Store refresh token
    expires_at = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    await db.execute(
        "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
        user["id"],
        refresh_token,
        expires_at,
    )

    return Token(access_token=access_token, refresh_token=refresh_token)


@app.post("/request-otp")
async def request_otp(otp_request: OTPRequest):
    # Find user
    if otp_request.type == "email":
        user = await db.fetchrow(
            "SELECT * FROM users WHERE email = $1 AND is_active = TRUE",
            otp_request.identifier,
        )
    else:
        user = await db.fetchrow(
            "SELECT * FROM users WHERE phone = $1 AND is_active = TRUE",
            otp_request.identifier,
        )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    # Generate OTP
    otp_code = generate_otp(settings.OTP_LENGTH)
    expires_at = datetime.utcnow() + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

    # Store OTP in database
    await db.execute(
        """
        INSERT INTO otp_codes (user_id, code, type, purpose, expires_at)
        VALUES ($1, $2, $3, $4, $5)
    """,
        user["id"],
        otp_code,
        otp_request.type,
        "login",
        expires_at,
    )

    # Send OTP
    if otp_request.type == "email":
        success = await send_otp_email(otp_request.identifier, otp_code)
    else:
        success = await send_otp_sms(otp_request.identifier, otp_code)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send OTP",
        )

    return {"message": f"OTP sent to {otp_request.type}"}


@app.post("/verify-otp", response_model=Token)
async def verify_otp(otp_data: OTPVerify):
    # Find user
    if otp_data.type == "email":
        user = await db.fetchrow(
            "SELECT * FROM users WHERE email = $1 AND is_active = TRUE",
            otp_data.identifier,
        )
    else:
        user = await db.fetchrow(
            "SELECT * FROM users WHERE phone = $1 AND is_active = TRUE",
            otp_data.identifier,
        )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    # Verify OTP
    otp_record = await db.fetchrow(
        """
        SELECT * FROM otp_codes
        WHERE user_id = $1 AND code = $2 AND type = $3
        AND used = FALSE AND expires_at > $4
        ORDER BY created_at DESC
        LIMIT 1
    """,
        user["id"],
        otp_data.code,
        otp_data.type,
        datetime.utcnow(),
    )

    if not otp_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP"
        )

    # Mark OTP as used
    await db.execute("UPDATE otp_codes SET used = TRUE WHERE id = $1", otp_record["id"])

    # Mark email/phone as verified
    if otp_data.type == "email":
        await db.execute(
            "UPDATE users SET email_verified = TRUE WHERE id = $1", user["id"]
        )
    else:
        await db.execute(
            "UPDATE users SET phone_verified = TRUE WHERE id = $1", user["id"]
        )

    # Create tokens
    access_token = create_access_token(data={"sub": str(user["id"])})
    refresh_token = create_refresh_token(data={"sub": str(user["id"])})

    # Store refresh token
    expires_at = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    await db.execute(
        "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
        user["id"],
        refresh_token,
        expires_at,
    )

    return Token(access_token=access_token, refresh_token=refresh_token)


@app.post("/refresh-token", response_model=Token)
async def refresh_token(token: str):
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id: str = payload.get("sub")

        # Verify refresh token exists in database
        token_record = await db.fetchrow(
            "SELECT * FROM refresh_tokens WHERE token = $1 AND expires_at > $2",
            token,
            datetime.utcnow(),
        )

        if not token_record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
            )

        # Delete old refresh token
        await db.execute("DELETE FROM refresh_tokens WHERE id = $1", token_record["id"])

        # Create new tokens
        access_token = create_access_token(data={"sub": user_id})
        new_refresh_token = create_refresh_token(data={"sub": user_id})

        # Store new refresh token
        expires_at = datetime.utcnow() + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        await db.execute(
            "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
            int(user_id),
            new_refresh_token,
            expires_at,
        )

        return Token(access_token=access_token, refresh_token=new_refresh_token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )


@app.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: dict = Depends(get_current_user)):
    return UserResponse(**current_user)


@app.post("/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    # Delete all refresh tokens for user
    await db.execute(
        "DELETE FROM refresh_tokens WHERE user_id = $1", current_user["id"]
    )
    return {"message": "Logged out successfully"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
