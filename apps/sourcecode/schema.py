from datetime import datetime
from typing import Annotated, Literal

from ninja import ModelSchema, Schema
from pydantic import ConfigDict, Field, field_validator

from glitchtip.schema import CamelSchema

from .models import Repository

HexField = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}$")]


class RepositoryIn(CamelSchema):
    name: str
    url: str = ""
    provider: dict | None = None


class RepositorySchema(CamelSchema, ModelSchema):
    id: str
    created: datetime = Field(serialization_alias="dateCreated")

    class Meta:
        model = Repository
        fields = ["name", "url", "status", "provider"]

    model_config = ConfigDict(coerce_numbers_to_str=True)


class ArtifactBundleAssembleIn(Schema):
    checksum: HexField
    chunks: list[HexField]
    projects: list[str]
    version: str = Field(min_length=1, max_length=255)

    @field_validator("version")
    @classmethod
    def require_nonblank_version(cls, version: str) -> str:
        if not version.strip():
            raise ValueError("Release version must not be blank.")
        return version


AssembleState = Literal["created", "error", "not_found", "assembling", "ok"]


class AssembleResponse(Schema):
    state: AssembleState
    missingChunks: list[str] = []


class DebugSymbolBundleSchema(CamelSchema):
    id: str
    created: datetime = Field(serialization_alias="dateCreated")
    sha1: str | None = Field(validation_alias="file.checksum", default=None)
    headers: dict[str, str] | None = Field(
        validation_alias="file.headers", default=None
    )
    name: str = Field(validation_alias="file.name")
    size: int = Field(validation_alias="file.size", default=0)

    model_config = ConfigDict(coerce_numbers_to_str=True)
