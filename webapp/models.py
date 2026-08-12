from typing import List, Optional

from pydantic import BaseModel, field_validator


class SearchRequest(BaseModel):
    city: str
    category: str
    min_reviews: Optional[int] = None
    max_reviews: Optional[int] = None
    min_rating: Optional[float] = None
    max_rating: Optional[float] = None
    has_website: Optional[bool] = None

    @field_validator("city", "category")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class Lead(BaseModel):
    business_name: str
    category: str
    city: str
    address: str
    phone: str
    website: str
    email: str = ""
    rating: Optional[float] = None
    review_count: int
    score: str
    reasoning: str
    place_id: str
    contacted: bool = False
    contacted_at: Optional[float] = None


class ContactedRequest(BaseModel):
    place_id: str
    contacted: bool
    # Business details are optional — sent when checking a box (so the
    # contacted list is self-contained) but not required when unchecking.
    business_name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    category: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    score: Optional[str] = None


class ContactedResponse(BaseModel):
    place_id: str
    contacted: bool
    contacted_at: Optional[float] = None


class ContactedStats(BaseModel):
    today: int
    this_week: int


class ContactedListEntry(BaseModel):
    place_id: str
    contacted_at: Optional[float] = None
    username: Optional[str] = None
    business_name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    category: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    score: Optional[str] = None


class SearchResponse(BaseModel):
    leads: List[Lead]
    search_id: int


class HistoryEntry(BaseModel):
    id: int
    timestamp: float
    username: str
    city: str
    category: str
    min_reviews: Optional[int] = None
    max_reviews: Optional[int] = None
    min_rating: Optional[float] = None
    max_rating: Optional[float] = None
    has_website: Optional[bool] = None
    result_count: int


class HistoryDetail(HistoryEntry):
    results: List[Lead]


class TemplateContent(BaseModel):
    content: str


class TemplateResponse(BaseModel):
    key: str
    content: str
    updated_at: Optional[float] = None
    updated_by: Optional[str] = None
