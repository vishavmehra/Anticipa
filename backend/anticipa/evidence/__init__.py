"""Evidence engine (AI part): extraction schemas, deterministic quote verification, acceptance policy."""

from .extract import DocumentMeta, ExtractionOutcome, VerifiedFact, extract_document
from .policy import Verdict, decide
from .quotes import QuoteVerification, verify_quote
from .schemas import EvidenceCandidate, EvidenceExtraction

__all__ = [
    "DocumentMeta",
    "EvidenceCandidate",
    "EvidenceExtraction",
    "ExtractionOutcome",
    "QuoteVerification",
    "Verdict",
    "VerifiedFact",
    "decide",
    "extract_document",
    "verify_quote",
]
