import logging

from chat import ProviderRateLimitError, answer_question
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from typing import Literal

from analytics import (
    get_sales_summary,
    get_product_revenue,
    get_region_revenue,
    get_monthly_revenue
)
from sales_upload import parse_and_validate_file, confirm_import, MAX_FILE_SIZE_BYTES

logger = logging.getLogger(__name__)

# Create FastAPI application
app = FastAPI(
    title="AI Business Intelligence Assistant",
    description="Sales analytics API powered by Python, FastAPI and MySQL",
    version="1.0.0"
)

# Allow frontend communication during development.
# Origins are restricted to the local Vite dev server.
# Methods and headers are explicitly listed; credentials are disabled
# because this application does not use cookies or session tokens.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
)

MAX_HISTORY_MESSAGES = 10
MAX_MESSAGE_CONTENT_LENGTH = 4000
MAX_HISTORY_CONTENT_LENGTH = 20000
MAX_CONVERSATION_ID_LENGTH = 128


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=MAX_MESSAGE_CONTENT_LENGTH)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("Message content cannot be empty.")
        return content


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    conversation_id: str | None = Field(
        default=None,
        max_length=MAX_CONVERSATION_ID_LENGTH,
    )
    messages: list[ConversationMessage] = Field(
        default_factory=list,
        max_length=MAX_HISTORY_MESSAGES,
    )

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("Question cannot be empty.")
        return question

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        conversation_id = value.strip()
        if not conversation_id:
            raise ValueError("Conversation ID cannot be empty.")
        return conversation_id

    @model_validator(mode="after")
    def validate_history_size(self) -> "ChatRequest":
        total_content_length = sum(len(message.content) for message in self.messages)
        if total_content_length > MAX_HISTORY_CONTENT_LENGTH:
            raise ValueError(
                "Conversation history content exceeds the maximum allowed size."
            )
        return self

# Root endpoint
@app.get("/")
def home():
    return {
        "message": "AI Business Intelligence API is running!"
    }


# 1. Overall sales summary
@app.get("/analytics/summary")
def sales_summary():
    try:
        return get_sales_summary()
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to fetch sales summary: {str(error)}"
        )


# 2. Product-wise revenue
@app.get("/analytics/products")
def product_analytics():
    try:
        return get_product_revenue()
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to fetch product analytics: {str(error)}"
        )


# 3. Region-wise revenue
@app.get("/analytics/regions")
def region_analytics():
    try:
        return get_region_revenue()
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to fetch region analytics: {str(error)}"
        )


# 4. Monthly revenue
@app.get("/analytics/monthly")
def monthly_analytics():
    try:
        return get_monthly_revenue()
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to fetch monthly analytics: {str(error)}"
        )

@app.post("/chat")
def chat(request: ChatRequest):

    try:
        question = request.question.strip()
        if not question:
            raise HTTPException(
                status_code=422,
                detail="Question cannot be empty."
            )

        answer, timings, business_data = answer_question(
            question,
            conversation_history=[
                message.model_dump() for message in request.messages
            ],
            conversation_id=request.conversation_id,
        )

        return {
            "question": question,
            "answer": answer,
            "business_data": business_data,
            "timings_ms": timings,
        }

    except HTTPException:
        raise
    except ProviderRateLimitError as error:
        detail = {
            "message": (
                "The AI service has reached its "
                f"{error.category} limit. Please try again later."
            ),
            "category": error.category,
        }
        if error.retry_after_seconds is not None:
            detail["retry_after_seconds"] = error.retry_after_seconds
        raise HTTPException(status_code=429, detail=detail) from error
    except RuntimeError as error:
        raise HTTPException(status_code=504, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unexpected chat generation failure")
        raise HTTPException(
            status_code=500,
            detail="Unable to generate an AI response right now."
        ) from error


class ImportConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(..., min_length=16, max_length=128)


@app.post("/sales/upload-preview")
async def upload_sales_preview(file: UploadFile = File(...)):
    """Upload CSV or Excel sales file, parse in-memory, validate records, and return preview.

    The file is read in 64 KB chunks so that oversized uploads are rejected
    before their full payload is loaded into memory.
    """
    filename = file.filename or "unknown"
    lower_name = filename.lower()
    if not (lower_name.endswith(".csv") or lower_name.endswith(".xlsx")):
        raise HTTPException(
            status_code=400,
            detail="Invalid file format. Only .csv and .xlsx files are supported."
        )

    # Stream the upload in chunks, aborting as soon as the size limit is exceeded.
    # This avoids buffering an arbitrarily large payload before the check.
    CHUNK_SIZE = 64 * 1024  # 64 KB
    chunks: list[bytes] = []
    total_bytes = 0
    try:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_FILE_SIZE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Uploaded file exceeds maximum limit of {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."
                )
            chunks.append(chunk)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=400, detail="Failed to read uploaded file.") from error

    content = b"".join(chunks)

    try:
        preview_data = parse_and_validate_file(content, filename)
        return preview_data
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unexpected error during sales preview processing")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while processing the sales file."
        ) from error


@app.post("/sales/import-confirm")
def confirm_sales_import(request: ImportConfirmRequest):
    """Atomically commit valid records from an unexpired preview session into MySQL."""
    try:
        result = confirm_import(request.preview_id)
        return result
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        logger.exception("Database import error")
        raise HTTPException(status_code=500, detail=str(error)) from error
    except Exception as error:
        logger.exception("Unexpected error during sales import confirmation")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while confirming the sales import."
        ) from error