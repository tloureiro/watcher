"""Small macOS integration for Prefect."""

def flow(*args, **kwargs):
    """Prefect flow decorator with Watcher's defaults at the flow boundary only."""
    from prefect import flow as prefect_flow
    kwargs.setdefault("retries", 1)
    kwargs.setdefault("retry_delay_seconds", 60)
    kwargs.setdefault("timeout_seconds", 1800)
    kwargs.setdefault("persist_result", True)
    from .storage import WorkspaceStorage
    kwargs.setdefault("result_storage", "watcher-compact-workspace-storage/default")
    return prefect_flow(*args, **kwargs)
