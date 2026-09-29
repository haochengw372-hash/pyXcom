"""pyXcom: public X collection with an existing Chrome or Edge login."""

from .client import XClient
from .models import CollectionResult, Post, Profile, classify_post
from .schema import apply_role_schema, schema_summary, write_schema_report
from .validate import finalize_collection, validate_collection

__all__ = [
    "XClient",
    "Profile",
    "Post",
    "CollectionResult",
    "classify_post",
    "schema_summary",
    "write_schema_report",
    "apply_role_schema",
    "finalize_collection",
    "validate_collection",
]
__version__ = "0.3.0"
