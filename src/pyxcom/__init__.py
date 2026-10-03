"""pyXcom: public X collection with an existing Chrome, Edge or Safari login."""

from .client import XClient
from .errors import (
    APIError,
    AuthenticationError,
    IntegrityError,
    PyXcomError,
    RateLimitError,
)
from .models import CollectionResult, Post, Profile, Relationship, classify_post
from .schema import apply_role_schema, schema_summary, write_schema_report
from .tables import export_tables
from .validate_tables import validate_tables
from .validate import finalize_collection, validate_collection
from .networks import validate_network_collection
from .integrity import assess_recovery
from .recovery import apply_recovery, prepare_recovery, verify_generation

__all__ = [
    "XClient",
    "PyXcomError",
    "AuthenticationError",
    "APIError",
    "RateLimitError",
    "IntegrityError",
    "assess_recovery",
    "prepare_recovery",
    "verify_generation",
    "apply_recovery",
    "export_tables",
    "validate_tables",
    "Profile",
    "Post",
    "CollectionResult",
    "Relationship",
    "classify_post",
    "schema_summary",
    "write_schema_report",
    "apply_role_schema",
    "finalize_collection",
    "validate_collection",
    "validate_network_collection",
]
__version__ = "1.0.0"
