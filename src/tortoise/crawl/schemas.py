"""Pydantic schemas Stagehand fills with ``extract`` — one per page shape.

Plain-English instructions in the crawler ask Stagehand to fill these; the
schemas are deliberately forgiving (everything except the reference defaults)
because council pages differ in what they show.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentLink(BaseModel):
    """One document in an application's documents list."""

    name: str = ""
    url: str = ""


class WeeklyListRow(BaseModel):
    """One row of a weekly list results table."""

    reference: str
    address: str = ""
    proposal: str = ""
    validated_on: str = Field(default="", description="the validated date exactly as shown")
    url: str = Field(default="", description="link to the application's details page")


class WeeklyListPage(BaseModel):
    """One page of weekly-list results."""

    rows: list[WeeklyListRow] = Field(default_factory=list)
    has_next_page: bool = False


class ApplicationSummary(BaseModel):
    """The summary details of one application's own page."""

    comment_deadline: str = Field(
        default="", description="the date comments must be made by, exactly as shown, or empty"
    )


class DocumentsList(BaseModel):
    """The documents attached to one application."""

    documents: list[DocumentLink] = Field(default_factory=list)
