"""Daily reference rates: baseline, unchanged success, and changed-rate alert."""
import httpx
from watcher import flow
from watcher.storage import get_state, set_state
from watcher.notifications import notify
from prefect.artifacts import create_table_artifact
from prefect.runtime import flow_run


def observe(observation, send=notify):
    if observation.get("base") != "USD" or observation.get("quote") != "CAD" or float(observation["rate"]) <= 0:
        raise ValueError("Unexpected Frankfurter response")
    previous = get_state("frankfurter-baseline")
    outcome = "baseline" if previous is None else "unchanged" if previous["rate"] == observation["rate"] else "changed"
    result = {**observation, "outcome": outcome}
    if outcome == "changed":
        send(f"USD/CAD is {observation['rate']} (reference date {observation['date']}). Previously {previous['rate']}.",
             subject="USD/CAD reference rate changed", alert_id="usd-cad-rate-changed",
             key=f"usd-cad:{observation['date']}:{observation['rate']}", run_id=flow_run.id)
    # Update after delivery so failed notifications can be retried. The native
    # endpoint deduplicates the stable observation key if the response was lost.
    set_state("frankfurter-baseline", observation)
    return result


@flow(name="Frankfurter USD CAD", log_prints=True)
def rates():
    response = httpx.get("https://api.frankfurter.dev/v2/rate/usd/cad", timeout=30)
    response.raise_for_status()
    result = observe(response.json())
    create_table_artifact(key="usd-cad-reference-rate", table=[result], description="Frankfurter daily USD/CAD reference rate")
    print(result)
    return result
