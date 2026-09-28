"""Shared validation primitives; no execution behavior lives here."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class ContractModel(BaseModel):
    """Reject coercion, unknown fields, non-finite numbers, and field reassignment.

    Frozen models do not deep-freeze arbitrary dictionaries. Raw decision arguments
    remain untrusted; approval snapshots use scalar-only typed tool arguments.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Objective = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
FinalAnswer = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
]
