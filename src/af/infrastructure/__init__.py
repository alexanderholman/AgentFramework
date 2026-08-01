"""Replaceable persistence and recording adapters."""

from af.infrastructure.task_search import task_row_to_resource
from af.infrastructure.trace_search import trace_row_to_resource

__all__ = ("task_row_to_resource", "trace_row_to_resource")
